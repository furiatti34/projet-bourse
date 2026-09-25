"""Courtier FICTIF pour le paper trading : cours réels, argent fictif.

Règles d'exécution (voir bourse.data.prices.fill_price) : un ordre est exécuté au premier
cours publié APRÈS sa création. Marché fermé → il attend l'ouverture, comme chez un vrai courtier.
"""
import math
import sqlite3
from datetime import datetime, timedelta

from bourse import clock
from bourse.data.prices import currency_of, fill_price, fx_to_eur, quote

from .broker import BUY, SELL, Broker, Holding

PENDING, FILLED, CANCELLED, REJECTED = "EN_ATTENTE", "EXECUTE", "ANNULE", "REJETE"
ORDER_EXPIRY = timedelta(days=7)


class PaperBroker(Broker):
    def __init__(self, conn: sqlite3.Connection, portfolio_id: int, fees: dict):
        self.conn = conn
        self.portfolio_id = portfolio_id
        self.fee_pct = fees["pourcentage"] / 100
        self.fee_min = fees["minimum_eur"]
        self.fx_fee_pct = fees["change_pourcentage"] / 100
        self.slippage = fees["glissement_pourcentage"] / 100

    # ---------- ordres ----------

    def place_order(self, ticker, side, quantity=None, amount_eur=None, reason=""):
        ticker = ticker.strip().upper()
        if side not in (BUY, SELL):
            raise ValueError(f"Sens d'ordre inconnu : {side}")
        try:
            if not currency_of(ticker):
                raise ValueError
        except Exception:
            raise ValueError(f"Titre introuvable : {ticker}") from None
        cur = self.conn.execute(
            "INSERT INTO orders (portfolio_id, created, ticker, side, quantity, amount_eur, status, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.portfolio_id, clock.now().isoformat(), ticker, side,
             quantity, amount_eur, PENDING, reason),
        )
        self.conn.commit()
        return cur.lastrowid

    def cancel_order(self, order_id: int) -> None:
        self.conn.execute("UPDATE orders SET status = ?, note = 'Annulé' WHERE id = ? AND status = ?"
                          " AND portfolio_id = ?", (CANCELLED, order_id, PENDING, self.portfolio_id))
        self.conn.commit()

    def orders(self, status: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM orders WHERE portfolio_id = ?", [self.portfolio_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        return self.conn.execute(sql + " ORDER BY id DESC", params).fetchall()

    def fees(self, notional_eur: float, currency: str) -> float:
        fee = max(self.fee_min, self.fee_pct * notional_eur)
        if currency != "EUR":
            fee += self.fx_fee_pct * notional_eur
        return fee

    def order(self, order_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def process_pending(self) -> list[str]:
        """Essaie d'exécuter les ordres en attente. Renvoie un message par ordre traité.
        Un achat attend que les ventes passées avant lui soient exécutées (il a besoin de leur argent)."""
        messages = []
        sale_pending = False
        for order in reversed(self.orders(PENDING)):  # du plus ancien au plus récent
            if order["side"] == BUY and sale_pending:
                continue
            created = datetime.fromisoformat(order["created"])
            try:
                fill = fill_price(order["ticker"], created)
            except Exception as exc:
                messages.append(f"Ordre {order['id']} : cours indisponible ({exc})")
                sale_pending |= order["side"] == SELL
                continue
            if fill is None:
                if clock.now() - created > ORDER_EXPIRY:
                    self._close(order["id"], REJECTED, note="Expiré : marché resté fermé 7 jours")
                else:
                    sale_pending |= order["side"] == SELL
                continue
            messages.append(self._execute(order, *fill))
        return messages

    def _execute(self, order, price: float, when: datetime) -> str:
        currency = currency_of(order["ticker"])
        fx = fx_to_eur(currency)
        rate = self.fee_pct + (self.fx_fee_pct if currency != "EUR" else 0)
        note = ""

        if order["side"] == BUY:
            price *= 1 + self.slippage
            unit_eur = price * fx
            cash = self.cash()
            affordable = max(0, math.floor((cash - self.fee_min) / (unit_eur * (1 + rate))))
            if order["quantity"]:
                wanted = int(order["quantity"])
            elif order["amount_eur"]:
                wanted = math.floor(order["amount_eur"] / unit_eur)
            else:
                wanted = affordable
            qty = min(wanted, affordable)
            while qty > 0 and qty * unit_eur + self.fees(qty * unit_eur, currency) > cash:
                qty -= 1
            if 0 < qty < wanted:
                note = f"Réduit de {wanted} à {qty} titres (cash insuffisant)"
            if qty <= 0:
                return self._close(order["id"], REJECTED, note="Cash insuffisant")
        else:
            price *= 1 - self.slippage
            unit_eur = price * fx
            held = self.holdings().get(order["ticker"])
            held_qty = held.quantity if held else 0
            if order["quantity"]:
                wanted = int(order["quantity"])
            elif order["amount_eur"]:
                wanted = round(order["amount_eur"] / unit_eur)
            else:
                wanted = held_qty
            qty = min(wanted, held_qty)
            if 0 < qty < wanted:
                note = f"Réduit de {wanted} à {qty} titres (position insuffisante)"
            if qty <= 0:
                return self._close(order["id"], REJECTED, note="Aucun titre à vendre")

        fees = self.fees(qty * unit_eur, currency)
        self.conn.execute(
            "UPDATE orders SET status = ?, filled_at = ?, filled_qty = ?, fill_price = ?, currency = ?,"
            " fx_to_eur = ?, fees_eur = ?, note = ? WHERE id = ?",
            (FILLED, when.isoformat(), qty, price, currency, fx, fees, note, order["id"]),
        )
        self.conn.commit()
        return (f"Ordre {order['id']} exécuté : {order['side']} {qty} × {order['ticker']} "
                f"à {price:.2f} {currency} (frais {fees:.2f} €)")

    def _close(self, order_id: int, status: str, note: str) -> str:
        self.conn.execute("UPDATE orders SET status = ?, note = ? WHERE id = ?", (status, note, order_id))
        self.conn.commit()
        return f"Ordre {order_id} {status.lower()} : {note}"

    # ---------- portefeuille ----------

    def initial_cash(self) -> float:
        return self.conn.execute("SELECT initial_cash FROM portfolios WHERE id = ?",
                                 (self.portfolio_id,)).fetchone()[0]

    def _fills(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE portfolio_id = ? AND status = ? ORDER BY filled_at, id",
            (self.portfolio_id, FILLED)).fetchall()

    def cash(self) -> float:
        cash = self.initial_cash()
        for f in self._fills():
            gross = f["filled_qty"] * f["fill_price"] * f["fx_to_eur"]
            cash += -gross - f["fees_eur"] if f["side"] == BUY else gross - f["fees_eur"]
        return cash

    def holdings(self) -> dict[str, Holding]:
        """Positions détenues, avec prix de revient moyen (frais d'achat inclus)."""
        result: dict[str, Holding] = {}
        for f in self._fills():
            h = result.setdefault(f["ticker"], Holding(f["ticker"], 0, 0.0))
            if f["side"] == BUY:
                h.quantity += f["filled_qty"]
                h.cost_eur += f["filled_qty"] * f["fill_price"] * f["fx_to_eur"] + f["fees_eur"]
            elif h.quantity:
                h.cost_eur -= h.cost_eur / h.quantity * f["filled_qty"]
                h.quantity -= f["filled_qty"]
        return {t: h for t, h in result.items() if h.quantity > 0}

    def positions(self) -> list[dict]:
        """Positions avec leur valeur actuelle en euros."""
        rows = []
        for h in self.holdings().values():
            q = quote(h.ticker)
            value = h.quantity * q.price_eur
            rows.append({
                "ticker": h.ticker, "quantite": h.quantity, "cours": q.price, "devise": q.currency,
                "valeur_eur": value, "prix_revient_eur": h.cost_eur,
                "gain_eur": value - h.cost_eur,
                "gain_pct": (value / h.cost_eur - 1) * 100 if h.cost_eur else 0.0,
            })
        return rows

    def total_value(self) -> float:
        return self.cash() + sum(p["valeur_eur"] for p in self.positions())
