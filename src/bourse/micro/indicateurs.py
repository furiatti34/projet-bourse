"""Indicateurs techniques, calculés minute par minute.

Règle anti-triche : la valeur à la minute i n'utilise QUE les minutes 0…i (i incluse, puisque la
décision est prise à la clôture de la minute i et exécutée au plus tôt à la minute i+1).
Le test tests/test_micro.py le vérifie en coupant les données : les valeurs passées ne doivent pas changer.
"""
import numpy as np
import pandas as pd


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


def rstd(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).std().to_numpy()


def rmax(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


def rmin(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if k < len(x):
        out[k:] = x[:-k] if k else x
    return out


def rsi(c: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.maximum(d, 0)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.maximum(-d, 0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()


def atr(h, l, c, n=14) -> np.ndarray:
    pc = shift(c)
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def adx(h, l, c, n=14) -> np.ndarray:
    up, dn = np.diff(h, prepend=h[0]), -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(h, l, c, n)
    w = lambda x: pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, ndi = 100 * w(pdm) / a, 100 * w(ndm) / a
        dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    return w(np.nan_to_num(dx))


def calculer(d: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Tous les indicateurs utilisés par les stratégies."""
    o, h, l, c, v, vb = d["o"], d["h"], d["l"], d["c"], d["v"], d["vb"]
    f: dict[str, np.ndarray] = {}
    f["atr"] = atr(h, l, c, 14)
    f["atr_pct"] = f["atr"] / c
    for n in (9, 21, 50, 200, 600):
        f[f"ema{n}"] = ema(c, n)
    for n in (2, 7, 14):
        f[f"rsi{n}"] = rsi(c, n)
    m20, s20 = sma(c, 20), rstd(c, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        f["bb_z"] = (c - m20) / s20
        f["bb_largeur"] = 4 * s20 / m20
    f["bb_largeur_rang"] = pd.Series(f["bb_largeur"]).rolling(1440, min_periods=200).rank(pct=True).to_numpy()
    # Stochastique (14, 3)
    hh, ll = rmax(h, 14), rmin(l, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        k = 100 * (c - ll) / (hh - ll)
    f["stoch_k"] = sma(np.nan_to_num(k, nan=50), 3)
    f["stoch_d"] = sma(f["stoch_k"], 3)
    # MACD (12, 26, 9)
    macd = ema(c, 12) - ema(c, 26)
    f["macd_hist"] = (macd - ema(macd, 9)) / c
    # CCI (20)
    tp = (h + l + c) / 3
    md = 0.8 * rstd(tp, 20)     # écart absolu moyen ≈ 0,8 × écart-type (beaucoup plus rapide à calculer)
    with np.errstate(divide="ignore", invalid="ignore"):
        f["cci"] = (tp - sma(tp, 20)) / (0.015 * md)
    f["adx"] = adx(h, l, c, 14)
    # VWAP : glissant sur 60 minutes, et de la journée (remis à zéro à minuit UTC)
    pv = tp * v
    with np.errstate(divide="ignore", invalid="ignore"):
        vw60 = pd.Series(pv).rolling(60, min_periods=10).sum() / pd.Series(v).rolling(60, min_periods=10).sum()
        f["vwap60_ecart"] = (c - vw60.to_numpy()) / f["atr"]
        jour = (d["t"] // 86400).astype(np.int64)
        cpv = pd.Series(pv).groupby(jour).cumsum().to_numpy()
        cv = pd.Series(v).groupby(jour).cumsum().to_numpy()
        f["vwap_jour_ecart"] = (c - cpv / cv) / f["atr"]
    # Canaux de Donchian : plus haut / plus bas des N minutes PRÉCÉDENTES (minute en cours exclue)
    for n in (20, 60, 240):
        f[f"haut{n}"] = shift(rmax(h, n))
        f[f"bas{n}"] = shift(rmin(l, n))
    # Volume et flux des acheteurs pressés (« taker ») : +1 = que des achats au marché, -1 = que des ventes
    with np.errstate(divide="ignore", invalid="ignore"):
        f["vol_z"] = (v - sma(v, 60)) / rstd(v, 60)
        f["flux"] = np.nan_to_num(2 * vb / v - 1)
        f["flux5"] = np.nan_to_num(2 * pd.Series(vb).rolling(5).sum().to_numpy()
                                   / pd.Series(v).rolling(5).sum().to_numpy() - 1)
    for k in (1, 3, 5, 15, 60):
        f[f"ret{k}"] = c / shift(c, k) - 1
    # Nombre de minutes rouges (ou vertes) d'affilée
    rouge = (c < o).astype(int)
    grp = np.cumsum(rouge == 0)
    f["rouges_suite"] = pd.Series(rouge).groupby(grp).cumsum().to_numpy()
    verte = (c > o).astype(int)
    f["vertes_suite"] = pd.Series(verte).groupby(np.cumsum(verte == 0)).cumsum().to_numpy()
    f["minute"] = ((d["t"] // 60) % 60).astype(int)
    f["heure"] = ((d["t"] // 3600) % 24).astype(int)
    return f
