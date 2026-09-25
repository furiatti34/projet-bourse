"""Cours des titres et conversion en euros (source : Yahoo Finance via yfinance)."""
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from bourse import clock

# Certaines places cotent en centimes : Londres (GBp), Johannesburg (ZAc), Tel-Aviv (ILA)
SUBUNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}

# Un cours plus vieux que ça = marché fermé (certaines Bourses ont 15-20 min de retard)
FRESH_DELAY = timedelta(minutes=45)

_cache: dict[str, tuple[float, object]] = {}

# Simulation dans le passé : les cours viennent de l'historique, arrêté à l'heure simulée
# (voir bourse.backtest.marche). None = cours réels de Yahoo Finance.
_provider = None


def set_provider(provider) -> None:
    global _provider
    _provider = provider


def _cached(key: str, ttl: float, compute):
    now = time.monotonic()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    value = compute()
    _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    """Oublie les cours gardés en mémoire (bouton « Rafraîchir ») : les prochains appels repartent de Yahoo."""
    _cache.clear()


def currency_of(ticker: str) -> str:
    if _provider:
        return _provider.currency_of(ticker)
    return _cached(f"cur:{ticker}", 86400, lambda: yf.Ticker(ticker).fast_info.currency)


def fx_to_eur(currency: str) -> float:
    """Multiplier un prix dans cette devise par ce taux donne le prix en euros."""
    factor = 1.0
    if currency in SUBUNITS:
        currency, divisor = SUBUNITS[currency]
        factor = 1 / divisor
    if currency == "EUR":
        return factor
    if _provider:
        return factor * _provider.fx_to_eur(currency)
    # EURUSD=X = nombre de dollars pour 1 euro
    rate = _cached(f"fx:{currency}", 600,
                   lambda: yf.Ticker(f"EUR{currency}=X").fast_info.last_price)
    return factor / rate


def intraday_bars(ticker: str) -> pd.DataFrame:
    """Barres de 5 minutes sur les 5 derniers jours (index en UTC).
    Cours BRUTS (auto_adjust=False) : exactement ceux cotés en Bourse, non retouchés des dividendes."""
    def load():
        bars = yf.Ticker(ticker).history(period="5d", interval="5m", auto_adjust=False)
        bars.index = bars.index.tz_convert("UTC")
        return bars
    return _cached(f"bars:{ticker}", 60, load)


def history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Historique en cours BRUTS, identiques à ceux affichés par les sites boursiers."""
    return _cached(f"hist:{ticker}:{period}:{interval}", 300,
                   lambda: yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False))


# Noms en clair des titres utilisés par les robots (les autres : nom fourni par Yahoo)
KNOWN_NAMES = {
    "IUSQ.DE": "Indice mondial MSCI ACWI (iShares)",
    "4GLD.DE": "Or physique (Xetra-Gold)",
    "LQQ.PA": "Nasdaq-100 avec levier ×2 (Amundi)",
    "DBPK.DE": "S&P 500 ×2 inverse : gagne quand ça baisse (Xtrackers)",
    "XMK9.DE": "Japon MSCI (Xtrackers)",
    "SXR8.DE": "États-Unis S&P 500 (iShares)",
    "EXW1.DE": "Zone euro Euro Stoxx 50 (iShares)",
    "ICGA.DE": "Chine MSCI (iShares)",
    "CRUD.MI": "Pétrole WTI (WisdomTree)",
}


def name_of(ticker: str) -> str:
    if ticker in KNOWN_NAMES:
        return KNOWN_NAMES[ticker]

    def load():
        try:
            info = yf.Ticker(ticker).info
            return info.get("shortName") or info.get("longName") or ticker
        except Exception:
            return ticker
    return _cached(f"name:{ticker}", 86400, load)


def day_change_pct(ticker: str) -> float | None:
    """Variation depuis la clôture de la veille, en %."""
    def load():
        info = yf.Ticker(ticker).fast_info
        return (info.last_price / info.previous_close - 1) * 100 if info.previous_close else None
    try:
        return _cached(f"day:{ticker}", 120, load)
    except Exception:
        return None


@dataclass
class Quote:
    price: float       # dans la devise de cotation
    currency: str
    time: datetime     # heure de la dernière barre (UTC)
    market_open: bool

    @property
    def price_eur(self) -> float:
        return self.price * fx_to_eur(self.currency)


def quote(ticker: str) -> Quote:
    if _provider:
        return _provider.quote(ticker)
    bars = intraday_bars(ticker)
    if bars.empty:
        raise ValueError(f"Aucun cours trouvé pour {ticker}")
    last_time = bars.index[-1].to_pydatetime()
    return Quote(
        price=float(bars["Close"].iloc[-1]),
        currency=currency_of(ticker),
        time=last_time,
        market_open=clock.now() - last_time < FRESH_DELAY,
    )


def price_at(ticker: str, when: datetime) -> float | None:
    """Dernier cours connu à un instant passé (None si trop ancien pour les données en 5 min)."""
    if _provider:
        return _provider.price_at(ticker, when)
    bars = intraday_bars(ticker)
    before = bars[bars.index <= pd.Timestamp(when)]
    return float(before["Close"].iloc[-1]) if not before.empty else None


def fill_price(ticker: str, created: datetime) -> tuple[float, datetime] | None:
    """Prix d'exécution honnête d'un ordre passé à l'instant `created` : le cours d'ouverture
    de la première barre de 5 minutes qui commence APRÈS l'ordre.
    Tant qu'elle n'existe pas (marché fermé, ou cours publiés avec 15-20 min de retard),
    renvoie None et l'ordre attend. Aucun ordre ne peut donc profiter d'un prix antérieur
    à la décision : impossible d'« acheter au prix d'avant » une nouvelle déjà connue.
    """
    if _provider:
        return _provider.fill_price(ticker, created)
    bars = intraday_bars(ticker)
    after = bars[bars.index >= pd.Timestamp(created)]
    if after.empty:
        return None
    return float(after["Open"].iloc[0]), after.index[0].to_pydatetime()
