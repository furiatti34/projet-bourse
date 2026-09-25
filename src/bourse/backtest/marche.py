"""Cours historiques servis « à l'heure » : à l'instant simulé, seuls les cours déjà publiés existent.

Yahoo Finance ne garde, pour les années passées, qu'une barre par jour (ouverture, clôture…).
Chaque barre devient donc deux événements datés :
  - le cours d'ouverture, connu à l'heure d'ouverture de sa Bourse (+ 20 min de retard de publication) ;
  - le cours de clôture, connu à l'heure de clôture (+ 20 min).
Entre les deux, le dernier cours connu est celui de l'ouverture.

Pas de triche :
  - un ordre est exécuté au premier cours de marché APRÈS sa création, et seulement une fois ce cours
    publié (même règle qu'au présent, mais avec des cours d'ouverture/clôture au lieu de barres de 5 min) ;
  - une ouverture douteuse (identique à la clôture, ou jour sans échange) est ignorée : elle pourrait
    être la clôture recopiée, que personne ne connaissait encore le matin ;
  - un titre qui n'existait pas encore est introuvable, comme un code inconnu au présent.
"""
import logging
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from bourse import clock
from bourse.config import PROJECT_ROOT
from bourse.data.prices import Quote

log = logging.getLogger(__name__)

CACHE = PROJECT_ROOT / "data" / "historique" / "cours.pkl"
CACHE_MAX_AGE = timedelta(days=1)
PUBLICATION_DELAY = timedelta(minutes=20)   # retard de publication des cours (comme Yahoo au présent)

EUROPE = ("Europe/Berlin", "09:00", "17:30")
US = ("America/New_York", "09:30", "16:00")


@dataclass(frozen=True)
class Session:
    tz: str
    open: str | None      # None = cours d'ouverture ignoré (horaires de la barre incertains)
    close: str

    def times(self, day: pd.Timestamp) -> tuple[pd.Timestamp | None, pd.Timestamp]:
        zone = ZoneInfo(self.tz)

        def at(hhmm: str) -> pd.Timestamp:
            h, m = map(int, hhmm.split(":"))
            return pd.Timestamp(datetime(day.year, day.month, day.day, h, m, tzinfo=zone)).tz_convert("UTC")
        return (at(self.open) if self.open else None), at(self.close)


def session_of(ticker: str, day: pd.Timestamp | None = None) -> Session:
    """Horaires de cotation (heure locale) de la barre quotidienne de ce titre."""
    if ticker.endswith((".DE", ".PA", ".MI", ".AS", ".BR", ".MC")) or ticker == "^STOXX50E":
        return Session(EUROPE[0], EUROPE[1], "17:35" if ticker.startswith("^") else EUROPE[2])
    if ticker == "^N225":   # Tokyo ferme à 15h30 depuis le 5 novembre 2024
        late = day is not None and day >= pd.Timestamp("2024-11-05")
        return Session("Asia/Tokyo", "09:00", "15:30" if late else "15:00")
    if ticker == "^HSI":
        return Session("Asia/Hong_Kong", "09:30", "16:10")
    if ticker.endswith("=F") or ticker == "DX-Y.NYB":   # contrats à terme : séance de la veille au soir
        return Session("America/New_York", None, "17:00")
    if ticker.endswith("=X"):                            # devises : barre de minuit à minuit (Londres)
        return Session("Europe/London", None, "23:59")
    if ticker in ("^VIX", "^VIX3M"):
        return Session(US[0], US[1], "16:15")
    if ticker == "^TNX":
        return Session(US[0], US[1], "17:00")
    return Session(*US)


# ---------------------------------------------------------------- téléchargement et nettoyage

def download(tickers: list[str], force: bool = False) -> dict[str, pd.DataFrame]:
    """Historique quotidien complet (cours BRUTS), gardé sur le disque une journée."""
    cached: dict[str, pd.DataFrame] = {}
    if CACHE.exists():
        with open(CACHE, "rb") as f:
            saved = pickle.load(f)
        fresh = datetime.now(timezone.utc) - saved["time"] < CACHE_MAX_AGE
        cached = saved["data"]
        if fresh and not force and all(t in cached for t in tickers):
            return {t: cached[t] for t in tickers}
    data = yf.download(tickers, period="max", interval="1d", auto_adjust=False, group_by="ticker",
                       progress=False, threads=True)
    result = {}
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"])
        except KeyError:
            df = pd.DataFrame()
        if df.empty and t in cached:   # Yahoo en panne pour ce titre : on garde l'ancienne copie
            df = cached[t]
        result[t] = df
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump({"time": datetime.now(timezone.utc), "data": {**cached, **result}}, f)
    return result


NICE_FACTORS = [2, 3, 4, 5, 8, 10, 15, 20, 25, 50, 100]


def clean(ticker: str, df: pd.DataFrame, leverage: int = 1) -> tuple[pd.DataFrame, list[str]]:
    """Corrige les erreurs connues de Yahoo. Renvoie (données propres, liste des corrections)."""
    notes = []
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    if not ticker.endswith("=F"):   # le pétrole a vraiment coté sous zéro en avril 2020
        df = df[df["Close"] > 0]
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")]
    tradable = not ticker.startswith("^") and "=" not in ticker and ticker != "DX-Y.NYB"
    # 1. Division de titres non corrigée (ex. 821 → 39) : le cours ne peut pas bouger autant en un jour
    if tradable:
        close = df["Close"]
        for day in close.index[1:]:
            i = close.index.get_loc(day)
            ratio = close.iloc[i] / close.iloc[i - 1]
            if 1 / 2.5 < ratio < 2.5:
                continue
            nxt = close.iloc[i + 1] / close.iloc[i - 1] if i + 1 < len(close) else ratio
            if 1 / 2 < nxt < 2:
                continue   # pic isolé : traité plus bas
            factor = min(NICE_FACTORS, key=lambda f: abs(np.log(max(ratio, 1 / ratio) / f)))
            if abs(np.log(max(ratio, 1 / ratio) / factor)) > np.log(1.2):
                continue
            k = 1 / factor if ratio < 1 else factor
            before = df.index < day
            df.loc[before, ["Open", "High", "Low", "Close"]] *= k
            df.loc[before, "Volume"] /= k
            close = df["Close"]
            notes.append(f"{ticker} : division du titre le {day:%d/%m/%Y} (×{k:g}) non corrigée par Yahoo, "
                         "corrigée ici")
    # 2. Pics d'un jour qui reviennent aussitôt (souvent un jour sans échange) : cours erroné
    threshold = 0.08 * max(1, abs(leverage))
    no_volume = df["Volume"].median() == 0   # indices (VIX…) : jamais de volume, on ne peut pas s'y fier
    if no_volume:
        threshold = 0.4
    close = df["Close"]
    r = close.pct_change()
    bad = []
    for i in range(1, len(close) - 1):
        if abs(r.iloc[i]) <= threshold:
            continue
        back = close.iloc[i + 1] / close.iloc[i - 1] - 1
        if abs(back) < threshold / 4 and (no_volume or df["Volume"].iloc[i] == 0 or abs(r.iloc[i]) > 2 * threshold):
            bad.append(close.index[i])
    if bad:
        df = df.drop(bad)
        notes.append(f"{ticker} : {len(bad)} cours aberrant(s) retiré(s) (pic d'un jour sans échange, "
                     f"ex. {bad[0]:%d/%m/%Y})")
    return df, notes


# ---------------------------------------------------------------- marché simulé

class HistoricalMarket:
    """Remplace Yahoo Finance pendant une simulation (voir bourse.data.prices.set_provider)."""

    def __init__(self, raw: dict[str, pd.DataFrame], leverage: dict[str, int] | None = None,
                 currencies: dict[str, str] | None = None):
        leverage = leverage or {}
        self.currencies = currencies or {}
        self.notes: list[str] = []
        self.events: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        closes, opens, close_t, open_t = {}, {}, {}, {}
        for ticker, df in raw.items():
            if df is None or df.empty:
                continue
            df, notes = clean(ticker, df, leverage.get(ticker, 1))
            self.notes += notes
            if df.empty:
                continue
            ct, ot = [], []
            for day in df.index:
                o, c = session_of(ticker, day).times(day)
                ct.append(c)
                ot.append(o)
            ct = pd.DatetimeIndex(ct).as_unit("ns")
            vol_ok = (df["Volume"] > 0) if not (ticker.startswith("^") or "=" in ticker) else True
            open_ok = (df["Open"] > 0) & (df["Open"] != df["Close"]) & vol_ok & pd.Series(
                [o is not None for o in ot], index=df.index)
            ot = pd.DatetimeIndex([o if o is not None else c for o, c in zip(ot, ct)]).as_unit("ns")
            closes[ticker] = df["Close"]
            opens[ticker] = df["Open"].where(open_ok)
            close_t[ticker] = pd.Series(ct.asi8, index=df.index)
            open_t[ticker] = pd.Series(ot.asi8, index=df.index).where(open_ok)
            # événements (heure de marché, heure de publication, prix, 1 = ouverture)
            market = np.concatenate([ot.asi8[open_ok.values], ct.asi8])
            price = np.concatenate([df["Open"].values[open_ok.values], df["Close"].values])
            kind = np.concatenate([np.ones(int(open_ok.sum()), dtype=np.int8), np.zeros(len(ct), dtype=np.int8)])
            order = np.argsort(market, kind="stable")
            market, price, kind = market[order], price[order], kind[order]
            known = market + PUBLICATION_DELAY // pd.Timedelta(1, "ns")
            self.events[ticker] = (market, known, price.astype(float), kind)
        self.close = pd.DataFrame(closes).sort_index()
        self.open = pd.DataFrame(opens).reindex(self.close.index)
        delay = PUBLICATION_DELAY // pd.Timedelta(1, "ns")
        self.close_known = (pd.DataFrame(close_t).reindex(self.close.index) + delay).to_numpy(dtype=float)
        self.open_known = (pd.DataFrame(open_t).reindex(self.close.index) + delay).to_numpy(dtype=float)
        self.columns = {t: i for i, t in enumerate(self.close.columns)}
        self.dates = self.close.index

    # ----- ce que voit le robot -----

    def _now_ns(self) -> int:
        return pd.Timestamp(clock.now()).value

    def first_date(self, ticker: str) -> pd.Timestamp | None:
        ev = self.events.get(ticker)
        return pd.Timestamp(ev[0][0], tz="UTC") if ev is not None else None

    def _last(self, ticker: str, when_ns: int) -> int:
        """Indice du dernier événement publié à cet instant (-1 = aucun)."""
        ev = self.events.get(ticker)
        if ev is None:
            return -1
        return int(np.searchsorted(ev[1], when_ns, side="right")) - 1

    def listed(self, ticker: str) -> bool:
        return self._last(ticker, self._now_ns()) >= 0

    def listed_at(self, ticker: str, when: datetime) -> bool:
        return self._last(ticker, pd.Timestamp(when).value) >= 0

    def currency_of(self, ticker: str) -> str:
        if not self.listed(ticker):   # titre pas encore créé à cette date
            raise ValueError(f"{ticker} n'existe pas encore à cette date")
        if ticker in self.currencies:
            return self.currencies[ticker]
        if ticker.endswith((".DE", ".PA", ".MI", ".AS", ".BR", ".MC")):
            return "EUR"
        return "USD" if not ticker.startswith("^") else ""

    def fx_to_eur(self, currency: str) -> float:
        rate = self.last_price(f"EUR{currency}=X")
        if rate is None:
            raise ValueError(f"Taux de change EUR/{currency} inconnu à cette date")
        return 1 / rate

    def last_price(self, ticker: str, when: datetime | None = None) -> float | None:
        now = self._now_ns()
        when_ns = min(now, pd.Timestamp(when).value) if when is not None else now
        i = self._last(ticker, when_ns)
        return float(self.events[ticker][2][i]) if i >= 0 else None

    def quote(self, ticker: str) -> Quote:
        i = self._last(ticker, self._now_ns())
        if i < 0:
            raise ValueError(f"Aucun cours trouvé pour {ticker} à cette date")
        market, _, price, kind = self.events[ticker]
        return Quote(price=float(price[i]), currency=self.currency_of(ticker),
                     time=pd.Timestamp(market[i], tz="UTC").to_pydatetime(), market_open=bool(kind[i]))

    def price_at(self, ticker: str, when: datetime) -> float | None:
        return self.last_price(ticker, when)

    def fill_price(self, ticker: str, created: datetime) -> tuple[float, datetime] | None:
        """Premier cours de marché APRÈS l'ordre, à condition qu'il soit déjà publié."""
        ev = self.events.get(ticker)
        if ev is None:
            return None
        market, known, price, _ = ev
        i = int(np.searchsorted(market, pd.Timestamp(created).value, side="right"))
        if i >= len(market) or known[i] > self._now_ns():
            return None
        return float(price[i]), pd.Timestamp(market[i], tz="UTC").to_pydatetime()

    def daily_closes(self, tickers: list[str], days: int) -> pd.DataFrame:
        """Comme yf.download(period=…, interval="1d") à l'instant simulé : la barre du jour contient le
        dernier cours publié (ouverture si la séance n'est pas finie), rien au-delà."""
        now = clock.now()
        now_ns = float(pd.Timestamp(now).value)
        start = pd.Timestamp(now.date()) - pd.Timedelta(days=days)
        lo = int(self.dates.searchsorted(start))
        hi = int(self.dates.searchsorted(pd.Timestamp(now.date()) + pd.Timedelta(days=2)))
        cols = [self.columns[t] for t in tickers if t in self.columns]
        names = [t for t in tickers if t in self.columns]
        close = self.close.to_numpy()[lo:hi][:, cols] if cols else np.empty((hi - lo, 0))
        opn = self.open.to_numpy()[lo:hi][:, cols] if cols else close
        ck, ok = self.close_known[lo:hi][:, cols], self.open_known[lo:hi][:, cols]
        values = np.where(ck <= now_ns, close, np.where(ok <= now_ns, opn, np.nan))
        df = pd.DataFrame(values, index=self.dates[lo:hi], columns=names)
        return df.dropna(how="all")

    def event_times(self, tickers: list[str], start: datetime, end: datetime) -> list[datetime]:
        """Heures de publication de tous les cours entre deux dates (pour cadencer la simulation)."""
        lo, hi = pd.Timestamp(start).value, pd.Timestamp(end).value
        times = set()
        for t in tickers:
            ev = self.events.get(t)
            if ev is None:
                continue
            known = ev[1]
            times.update(known[(known > lo) & (known <= hi)].tolist())
        return [pd.Timestamp(t, tz="UTC").to_pydatetime() for t in sorted(times)]


def build_market(settings: dict, extra: list[str] = ()) -> HistoricalMarket:
    """Télécharge (ou relit) tout ce que les robots peuvent étudier ou acheter."""
    cfg = settings["analyse"]
    leverage = {u["ticker"]: int(u.get("levier", 1)) for u in cfg["univers"]}
    tickers = [u["ticker"] for u in cfg["univers"]] + list(cfg["climat"].values())
    tickers += [i["ticker"] for i in settings["veille"]["indicateurs_marche"]]
    for p in settings["paper_trading"]["portefeuilles"]:   # titres fixés dans les réglages des robots
        for key, value in (p.get("parametres") or {}).items():
            if key.startswith("titre_") and isinstance(value, str):
                tickers.append(value)
            if key == "marches" and isinstance(value, dict):
                tickers += list(value.values())
    tickers += [settings["general"]["indice_reference"], *extra]
    tickers = list(dict.fromkeys(tickers))
    t0 = time.monotonic()
    raw = download(tickers)
    log.info("Historique de %d titres chargé en %.0f s", len(tickers), time.monotonic() - t0)
    return HistoricalMarket(raw, leverage)
