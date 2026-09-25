"""Tableau de bord du marché, recalculé à chaque passage des robots.

Il rassemble trois sortes d'informations :
  1. chaque placement de l'« univers » (réglages, section analyse) : tendance, élan, RSI,
     volatilité, chute depuis son plus haut ;
  2. le « climat » général : peur (VIX), taux, dollar, crédit, cuivre/or, part des marchés en hausse ;
  3. TOUTES les actualités lues par la veille (pas seulement celles qui ont déclenché une alerte),
     plus les alertes récentes.
Le tout donne une note de climat de -100 (tempête) à +100 (optimisme).

Pas de triche : uniquement des données déjà publiées au moment où le robot réfléchit.
"""
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
import yfinance as yf

from bourse import clock
from bourse.alerts.engine import format_age
from bourse.alerts.scoring import _SEVERE, matches, normalize
from bourse.data import prices

log = logging.getLogger(__name__)

TRADING_DAYS = 252


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- placements

@dataclass
class AssetView:
    ticker: str
    name: str
    family: str
    leverage: int
    price: float
    ret_1d: float        # variations en %
    ret_5d: float
    ret_1m: float
    ret_3m: float
    ret_6m: float
    ma20: float
    ma50: float
    ma200: float
    rsi: float           # 0-100 : < 30 = a trop baissé trop vite, > 70 = s'est emballé
    vol: float           # volatilité annuelle en %
    drawdown: float      # % sous le plus haut d'un an (négatif)

    @property
    def above_ma50(self) -> bool:
        return self.price > self.ma50

    @property
    def above_ma200(self) -> bool:
        return self.price > self.ma200

    @property
    def momentum(self) -> float:
        """Élan de fond (semaines, mois)."""
        return 0.2 * self.ret_5d + 0.3 * self.ret_1m + 0.3 * self.ret_3m + 0.2 * self.ret_6m

    @property
    def sprint(self) -> float:
        """Élan très court terme (jours) : ce qui monte le plus vite en ce moment."""
        return 0.2 * self.ret_1d + 0.5 * self.ret_5d + 0.3 * self.ret_1m

    @property
    def label(self) -> str:
        return f"{self.name} ({self.ticker})"


def _rsi(closes: pd.Series, n: int = 14) -> float:
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _ret(closes: pd.Series, days: int) -> float:
    days = min(days, len(closes) - 1)
    return float((closes.iloc[-1] / closes.iloc[-1 - days] - 1) * 100)


def asset_view(ticker: str, info: dict, closes: pd.Series) -> AssetView | None:
    closes = closes.dropna()
    if len(closes) < 30:
        return None
    returns = closes.pct_change().dropna()
    return AssetView(
        ticker=ticker, name=info.get("nom", ticker), family=info.get("famille", "actions"),
        leverage=int(info.get("levier", 1)), price=float(closes.iloc[-1]),
        ret_1d=_ret(closes, 1), ret_5d=_ret(closes, 5), ret_1m=_ret(closes, 21),
        ret_3m=_ret(closes, 63), ret_6m=_ret(closes, 126),
        ma20=float(closes.iloc[-20:].mean()), ma50=float(closes.iloc[-50:].mean()),
        ma200=float(closes.iloc[-200:].mean()), rsi=_rsi(closes),
        vol=float(returns.iloc[-20:].std() * math.sqrt(TRADING_DAYS) * 100),
        drawdown=float((closes.iloc[-1] / closes.iloc[-TRADING_DAYS:].max() - 1) * 100),
    )


# ---------------------------------------------------------------- climat

@dataclass
class Factor:
    name: str
    value: float         # -1 (très mauvais signe) … +1 (très bon signe)
    weight: float        # importance dans la note de climat
    detail: str

    @property
    def icon(self) -> str:
        return "🟢" if self.value >= 0.25 else "🔴" if self.value <= -0.25 else "⚪"


@dataclass
class NewsClimate:
    n_articles: int = 0             # articles lus ces dernières 24 h
    severe_share: float = 0.0       # part de ces articles au vocabulaire de crise
    baseline_share: float | None = None   # la même part les jours précédents
    alerts: list[dict] = field(default_factory=list)   # alertes des 48 dernières heures


@lru_cache(maxsize=50_000)
def _is_severe(title: str) -> bool:
    """Le titre emploie-t-il un vocabulaire de crise ? (mémorisé : les mêmes titres reviennent à chaque passage)"""
    return bool(matches(normalize(title), _SEVERE))


def news_climate(conn: sqlite3.Connection, now: datetime) -> NewsClimate:
    day_ago, week_ago = (now - timedelta(hours=24)).isoformat(), (now - timedelta(days=8)).isoformat()
    rows = conn.execute("SELECT title, first_seen FROM articles WHERE first_seen >= ?", (week_ago,)).fetchall()
    recent = [_is_severe(r["title"]) for r in rows if r["first_seen"] >= day_ago]
    older = [_is_severe(r["title"]) for r in rows if r["first_seen"] < day_ago]
    alerts = [dict(r) for r in conn.execute(
        "SELECT level, score, title_fr, created FROM alerts WHERE is_test = 0 AND created >= ?"
        " ORDER BY created DESC", ((now - timedelta(hours=48)).isoformat(),))]
    return NewsClimate(
        n_articles=len(recent),
        severe_share=sum(recent) / len(recent) if recent else 0.0,
        baseline_share=sum(older) / len(older) if len(older) >= 200 else None,
        alerts=alerts,
    )


def climate_factors(closes: pd.DataFrame, roles: dict[str, str], assets: dict[str, AssetView],
                    news: NewsClimate, now: datetime) -> list[Factor]:
    def series(role: str) -> pd.Series | None:
        ticker = roles.get(role)
        if ticker in closes and closes[ticker].dropna().size >= 30:
            return closes[ticker].dropna()
        return None

    factors = []

    world = assets.get(roles.get("monde", ""))
    if world:
        v = (0.5 if world.above_ma200 else -0.5) + (0.5 if world.above_ma50 else -0.5)
        factors.append(Factor("Tendance mondiale", v, 20,
                              f"indice mondial {'au-dessus' if world.above_ma200 else 'en dessous'} de sa "
                              f"moyenne 200 jours, {'au-dessus' if world.above_ma50 else 'en dessous'} "
                              f"de sa moyenne 50 jours ({world.ret_1m:+.1f} % sur 1 mois)"))

    stocks = [a for a in assets.values() if a.family == "actions" and a.leverage == 1]
    if stocks:
        share = sum(a.above_ma50 for a in stocks) / len(stocks)
        factors.append(Factor("Largeur du marché", _clamp((share - 0.5) * 2), 10,
                              f"{share:.0%} des marchés d'actions suivis sont en tendance haussière"))

    vix = series("peur")
    if vix is not None:
        level = float(vix.iloc[-1])
        factors.append(Factor("Peur (VIX)", _clamp((20 - level) / 10), 15,
                              f"VIX à {level:.1f} (calme sous 15, inquiétude au-dessus de 25)"))
        vix3m = series("peur_3mois")
        if vix3m is not None:
            ratio = level / float(vix3m.iloc[-1])
            factors.append(Factor("Panique à court terme", -1.0 if ratio > 1 else 0.3, 10,
                                  f"peur à 1 mois / peur à 3 mois = {ratio:.2f} "
                                  f"({'panique immédiate' if ratio > 1 else 'normal'})"))

    hy, safe = series("credit_risque"), series("credit_sur")
    if hy is not None and safe is not None:
        ratio = (hy / safe).dropna()
        chg = _ret(ratio, 21)
        factors.append(Factor("Crédit", _clamp(chg / 2), 10,
                              f"obligations d'entreprises fragiles vs État : {chg:+.1f} % sur 1 mois "
                              f"({'confiance' if chg >= 0 else 'méfiance'} des prêteurs)"))

    copper, gold = series("cuivre"), series("or")
    if copper is not None and gold is not None:
        chg = _ret((copper / gold).dropna(), 21)
        factors.append(Factor("Cuivre / or", _clamp(chg / 5), 5,
                              f"{chg:+.1f} % sur 1 mois ({'économie qui accélère' if chg >= 0 else 'on se réfugie dans l’or'})"))

    rates = series("taux")
    if rates is not None:
        chg = float(rates.iloc[-1] - rates.iloc[-min(22, len(rates))])
        factors.append(Factor("Taux américains", _clamp(-chg / 0.5, -1, 0.5), 5,
                              f"taux à 10 ans {rates.iloc[-1]:.2f} % ({chg:+.2f} point sur 1 mois)"))

    dollar = series("dollar")
    if dollar is not None:
        chg = _ret(dollar, 21)
        factors.append(Factor("Dollar", _clamp(-chg / 3, -1, 0.5), 5,
                              f"{chg:+.1f} % sur 1 mois (un dollar qui flambe = argent qui fuit le risque)"))

    if news.n_articles:
        if news.baseline_share is not None:
            extra = news.severe_share - news.baseline_share
            v = _clamp(-extra / max(news.baseline_share, 0.05) / 2, -1, 0.3)
            txt = (f"{news.severe_share:.0%} des {news.n_articles} articles des dernières 24 h parlent de "
                   f"crise, contre {news.baseline_share:.0%} les jours précédents")
        else:
            v = _clamp((0.15 - news.severe_share) / 0.15, -1, 0.3)
            txt = (f"{news.severe_share:.0%} des {news.n_articles} articles des dernières 24 h parlent de "
                   "crise (pas encore assez d'historique pour comparer)")
        factors.append(Factor("Ton des actualités", v, 10, txt))

    worst, worst_txt = 0.3, "aucune alerte ces dernières 48 h"
    for a in news.alerts:
        age = now - datetime.fromisoformat(a["created"])
        v = -(1.0 if a["level"] == "FORTE" else 0.5) * max(0.0, 1 - age.total_seconds() / 3600 / 48)
        if v < worst:
            worst, worst_txt = v, f"alerte {a['level']} il y a {format_age(age)} : « {a['title_fr']} »"
    factors.append(Factor("Alertes", worst, 10, worst_txt))
    return factors


# ---------------------------------------------------------------- vue d'ensemble

@dataclass
class MarketView:
    time: datetime
    assets: dict[str, AssetView]
    factors: list[Factor]
    news: NewsClimate

    @property
    def risk_score(self) -> int:
        """Note de climat : -100 (tempête) … +100 (optimisme)."""
        total = sum(f.weight for f in self.factors)
        return round(sum(f.value * f.weight for f in self.factors) / total * 100) if total else 0

    @property
    def mood(self) -> str:
        s = self.risk_score
        return ("🟢 optimiste" if s >= 30 else "🟡 plutôt calme" if s >= 0
                else "🟠 méfiant" if s > -30 else "🔴 tempête")

    def factor(self, name: str) -> Factor | None:
        return next((f for f in self.factors if f.name == name), None)

    def asset(self, ticker: str) -> AssetView | None:
        return self.assets.get(ticker)

    def pick(self, families=None, leverage=None, exclude=()) -> list[AssetView]:
        return [a for a in self.assets.values()
                if (families is None or a.family in families)
                and (leverage is None or a.leverage in leverage)
                and a.ticker not in exclude]

    def headline(self) -> str:
        return f"Climat du marché {self.risk_score:+d}/100 ({self.mood})"


def analyse(closes: pd.DataFrame, universe: list[dict], roles: dict[str, str],
            news: NewsClimate, now: datetime) -> MarketView:
    assets = {}
    for info in universe:
        if info["ticker"] in closes:
            view = asset_view(info["ticker"], info, closes[info["ticker"]])
            if view:
                assets[info["ticker"]] = view
    return MarketView(now, assets, climate_factors(closes, roles, assets, news, now), news)


def download_closes(tickers: list[str]) -> pd.DataFrame:
    """Un an de cours quotidiens BRUTS (la barre du jour = dernier cours publié)."""
    if prices._provider:   # simulation dans le passé : historique arrêté à l'heure simulée
        return prices._provider.daily_closes(tickers, days=365)
    data = yf.download(tickers, period="1y", interval="1d", progress=False, auto_adjust=False,
                       group_by="ticker", threads=True)
    closes = {}
    for t in tickers:
        try:
            closes[t] = data[t]["Close"]
        except KeyError:
            log.warning("Analyse : pas de cours pour %s", t)
    return pd.DataFrame(closes)


def build_view(settings: dict, conn: sqlite3.Connection) -> MarketView:
    cfg = settings["analyse"]
    roles = cfg["climat"]
    tickers = list(dict.fromkeys([u["ticker"] for u in cfg["univers"]] + list(roles.values())))
    now = clock.now()
    return analyse(download_closes(tickers), cfg["univers"], roles, news_climate(conn, now), now)
