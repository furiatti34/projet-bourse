"""Mouvements de marché : détecte si un indicateur bouge de façon inhabituelle aujourd'hui."""
import logging
from dataclasses import dataclass

import yfinance as yf

from bourse.data import prices

log = logging.getLogger(__name__)


@dataclass
class Move:
    ticker: str
    name: str
    keywords: list[str]
    change_pct: float  # variation du jour, en %
    zscore: float      # combien de fois la variation « normale » (écart-type des 60 derniers jours)

    @property
    def unusual(self) -> bool:
        return abs(self.zscore) >= 2


def market_moves(indicators: list[dict]) -> dict[str, Move]:
    """Télécharge 6 mois de cours et calcule le mouvement du jour de chaque indicateur."""
    tickers = [ind["ticker"] for ind in indicators]
    if prices._provider:   # simulation dans le passé : historique arrêté à l'heure simulée
        closes = prices._provider.daily_closes(tickers, days=183)
    else:
        data = yf.download(tickers, period="6mo", interval="1d", progress=False, auto_adjust=True)
        closes = data["Close"]

    moves = {}
    for ind in indicators:
        if ind["ticker"] not in closes:
            continue
        returns = closes[ind["ticker"]].dropna().pct_change().dropna()
        if len(returns) < 30:
            continue
        today = returns.iloc[-1]
        normal = returns.iloc[-61:-1].std()
        moves[ind["ticker"]] = Move(
            ticker=ind["ticker"],
            name=ind["nom"],
            keywords=ind.get("mots", []),
            change_pct=float(today * 100),
            zscore=float(today / normal) if normal > 0 else 0.0,
        )
    return moves
