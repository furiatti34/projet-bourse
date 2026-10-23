"""Courbe de valeur d'un portefeuille, recalculée à partir des VRAIS cours du marché.

À chaque instant : liquidités + (titres détenus à cet instant × cours réel à cet instant).
Rien n'est inventé : seuls les ordres réellement exécutés et les cours publiés sont utilisés.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from bourse.data.prices import fx_to_eur, history
from bourse.execution.broker import BUY
from bourse.execution.paper_broker import FILLED


def _resolution(span: timedelta) -> tuple[str, str]:
    """Plus la période est longue, moins les points sont serrés (limites de Yahoo Finance)."""
    if span <= timedelta(days=4):
        return "5d", "5m"
    if span <= timedelta(days=55):
        return "60d", "30m"
    if span <= timedelta(days=700):
        return "2y", "1h"
    return "max", "1d"


def fills(conn: sqlite3.Connection, portfolio_id: int) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM orders WHERE portfolio_id = ? AND status = ? ORDER BY filled_at, id",
                     conn, params=(portfolio_id, FILLED))
    df["filled_at"] = pd.to_datetime(df["filled_at"], utc=True, format="ISO8601")
    return df


def deposits_of(portfolio: sqlite3.Row) -> pd.Series | None:
    """Versements datés (€) d'un portefeuille qui en reçoit (le conseiller les note dans sa mémoire), sinon None."""
    try:
        v = json.loads(portfolio["state"] or "{}").get("versements")
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    if not v:
        return None
    s = pd.Series([float(x["eur"]) for x in v],
                  index=pd.to_datetime([x["time"] for x in v], utc=True, format="ISO8601"))
    return s.groupby(level=0).sum().sort_index()


def equity_curve(conn: sqlite3.Connection, portfolio: sqlite3.Row) -> pd.DataFrame:
    """Colonnes : value (portefeuille, €) et benchmark (même capital placé dans l'indice, €).
    Avec des versements : à chaque instant, seul l'argent déjà versé compte, et l'indice reçoit chaque versement
    à sa date (jamais le capital d'aujourd'hui appliqué au passé)."""
    start = pd.Timestamp(portfolio["created"])
    period, interval = _resolution(datetime.now(timezone.utc) - start.to_pydatetime())
    trades = fills(conn, portfolio["id"])
    tickers = sorted(set(trades["ticker"]) | {portfolio["benchmark"]})

    closes = {}
    for ticker in tickers:
        bars = history(ticker, period, interval)
        if not bars.empty:
            closes[ticker] = bars["Close"].tz_convert("UTC")
    closes = pd.DataFrame(closes).sort_index().ffill()
    before = closes[closes.index <= start]
    if not before.empty:  # point de départ : derniers cours connus à la création
        closes.loc[start] = before.iloc[-1]
    closes = closes[closes.index >= start].sort_index()
    if closes.empty:
        return pd.DataFrame(columns=["value", "benchmark"])

    index = closes.index
    deposits = deposits_of(portfolio)
    bench = closes[portfolio["benchmark"]]

    def as_of(series: pd.Series) -> pd.Series:     # cumul connu à chaque instant de la courbe
        return series.cumsum().reindex(index.union(series.index)).ffill().fillna(0).reindex(index)

    if deposits is None:
        cash = pd.Series(float(portfolio["initial_cash"]), index=index)
        bench_value = portfolio["initial_cash"] * bench / portfolio["benchmark_start"]
    else:
        cash = as_of(deposits)
        price_then = bench.reindex(bench.index.union(deposits.index)).ffill().bfill().reindex(deposits.index)
        bench_value = as_of(deposits / price_then) * bench
    value = cash.copy()
    for ticker, group in trades.groupby("ticker"):
        signed = group["filled_qty"].where(group["side"] == BUY, -group["filled_qty"])
        gross = group["filled_qty"] * group["fill_price"] * group["fx_to_eur"]
        cash_flow = (-gross).where(group["side"] == BUY, gross) - group["fees_eur"]
        # cumul des achats/ventes connus à chaque instant de la courbe
        position = signed.groupby(group["filled_at"]).sum().cumsum()
        flows = cash_flow.groupby(group["filled_at"]).sum().cumsum()
        held = position.reindex(index.union(position.index)).ffill().fillna(0).reindex(index)
        spent = flows.reindex(index.union(flows.index)).ffill().fillna(0).reindex(index)
        price = closes[ticker] if ticker in closes else pd.Series(group["fill_price"].iloc[-1], index=index)
        # (taux de change actuel : approximation pour les titres hors euro)
        value += spent + held * price.fillna(group["fill_price"].iloc[0]) * fx_to_eur(group["currency"].iloc[-1])

    return pd.DataFrame({"value": value, "benchmark": bench_value}).dropna()
