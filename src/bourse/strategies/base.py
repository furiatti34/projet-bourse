"""Modèle commun des stratégies.

Une stratégie reçoit des événements et passe des ordres via le courtier.
`state` est sa mémoire (un dictionnaire sauvegardé entre deux passages).
`self.view` est l'analyse du marché du passage en cours (None si les données sont indisponibles).
Chaque décision est expliquée avec `self.note(...)` : elle apparaît dans le journal du robot.
Son raisonnement du moment (`self.think(...)`) s'affiche dans l'interface.
"""
from abc import ABC
from datetime import datetime, timedelta

from bourse import clock
from bourse.alerts.engine import format_age
from bourse.alerts.events import AlertEvent
from bourse.analysis import MarketView
from bourse.data.prices import quote
from bourse.execution.broker import BUY, SELL
from bourse.execution.paper_broker import CANCELLED, FILLED, PENDING, REJECTED, PaperBroker

CASH_BUFFER = 0.01   # 1 % toujours gardé en liquide pour payer les frais


class Strategy(ABC):
    name: str = ""
    description: str = ""

    def __init__(self, params: dict, view: MarketView | None = None):
        self.params = params
        self.view = view
        self.notes: list[str] = []
        self.thoughts: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)

    def think(self, message: str) -> None:
        self.thoughts.append(message)

    # ----- événements -----

    def on_start(self, broker: PaperBroker, state: dict) -> None:
        """Appelé une seule fois, au tout premier passage."""

    def on_alert(self, alert: AlertEvent, broker: PaperBroker, state: dict) -> None:
        """Appelé pour chaque nouvelle alerte, même détectée en retard (PC éteint)."""

    def on_cycle(self, broker: PaperBroker, state: dict) -> None:
        """Appelé à chaque passage (toutes les 15 minutes)."""

    # ----- outils communs -----

    def freshness(self, alert: AlertEvent, now: datetime) -> float:
        """1 = information toute fraîche ; décroît jusqu'à 0 au-delà de `delai_max_heures`.
        Plus une nouvelle est ancienne, plus le marché l'a déjà intégrée dans les prix."""
        max_hours = self.params.get("delai_max_heures", 24)
        hours = alert.age(now).total_seconds() / 3600
        if hours <= 1:
            return 1.0
        return max(0.0, 1 - (hours - 1) / (max_hours - 1))

    def describe(self, alert: AlertEvent, now: datetime) -> str:
        return (f"alerte {alert.level} {alert.score}/100 « {alert.title} » "
                f"(publiée il y a {format_age(alert.age(now))})")

    def think_climate(self) -> None:
        """Résume dans sa réflexion ce que dit l'analyse du marché."""
        if self.view is None:
            self.think("⚠️ Analyse du marché indisponible (pas de connexion ?) : je ne change rien.")
            return
        self.think(self.view.headline())
        ranked = sorted(self.view.factors, key=lambda f: -f.value * f.weight)
        shown = ranked[:3] + [f for f in ranked[-2:] if f.value < 0 and f not in ranked[:3]]
        for f in shown:   # les 3 meilleurs signaux et les 2 plus inquiétants
            self.think(f"{f.icon} {f.name} : {f.detail}")

    @staticmethod
    def once_per_day(state: dict, key: str) -> bool:
        """Vrai une seule fois par jour pour cette clé (évite de s'agiter à chaque passage)."""
        today = clock.now().date().isoformat()
        if state.get(key) == today:
            return False
        state[key] = today
        return True

    def rebalance(self, broker: PaperBroker, targets: dict[str, float], why: str,
                  tolerance: float, exclude=()) -> bool:
        """Ramène le portefeuille vers les parts voulues (ex. {"IUSQ.DE": 0.6}).
        Les titres absents de `targets` sont vendus, sauf ceux de `exclude` (gérés à part).
        Ne fait rien si tout est à moins de `tolerance` de sa cible, ou si des ordres attendent encore."""
        if broker.orders(PENDING):
            self.think("J'ai des ordres en attente d'exécution (marché fermé ?) : "
                       "je ne réajuste rien avant qu'ils soient passés.")
            return False
        total = broker.total_value()
        held = {p["ticker"]: p["valeur_eur"] for p in broker.positions() if p["ticker"] not in exclude}
        diffs = {t: targets.get(t, 0) * total - held.get(t, 0) for t in set(targets) | set(held)}
        if not diffs or max(abs(d) for d in diffs.values()) < tolerance * total:
            return False
        min_trade = max(300.0, 0.01 * total)
        for ticker, diff in sorted(diffs.items(), key=lambda x: x[1]):     # ventes d'abord
            if diff <= -min_trade:
                broker.place_order(ticker, SELL, amount_eur=None if targets.get(ticker, 0) == 0 else -diff,
                                   reason=why)
        for ticker, diff in sorted(diffs.items(), key=lambda x: -x[1]):
            if diff >= min_trade:
                broker.place_order(ticker, BUY, amount_eur=diff, reason=why)
        return True

    @staticmethod
    def describe_targets(targets: dict[str, float], view: MarketView | None) -> str:
        def name(t):
            a = view.asset(t) if view else None
            return a.name if a else t
        return ", ".join(f"{w:.0%} {name(t)}" for t, w in sorted(targets.items(), key=lambda x: -x[1]) if w > 0)

    def open_trade(self, broker: PaperBroker, state: dict, ticker: str, amount_eur: float, why: str) -> None:
        """Achat suivi : revendu automatiquement à l'objectif, au stop-loss ou après la durée max."""
        order_id = broker.place_order(ticker, BUY, amount_eur=amount_eur, reason=why)
        until = clock.now() + timedelta(days=self.params["duree_jours"])
        state.setdefault("trades", []).append(
            {"order_id": order_id, "ticker": ticker, "until": until.isoformat(), "why": why})

    def manage_trades(self, broker: PaperBroker, state: dict) -> None:
        now = clock.now()
        target, stop = self.params["objectif_gain"], self.params["stop_perte"]
        for trade in list(state.get("trades", [])):
            order = broker.order(trade["order_id"])
            if order["status"] in (REJECTED, CANCELLED):
                state["trades"].remove(trade)
                continue
            if order["status"] != FILLED:
                continue  # achat pas encore exécuté
            change = quote(trade["ticker"]).price / order["fill_price"] - 1
            if change >= target:
                why = f"objectif atteint ({change:+.1%})"
            elif change <= -stop:
                why = f"stop-loss déclenché ({change:+.1%})"
            elif now >= datetime.fromisoformat(trade["until"]):
                why = f"durée maximale écoulée ({change:+.1%})"
            else:
                continue
            broker.place_order(trade["ticker"], SELL, quantity=order["filled_qty"],
                               reason=f"Revente {trade['ticker']} : {why}")
            self.note(f"Revente de {trade['ticker']} : {why}.")
            state["trades"].remove(trade)
