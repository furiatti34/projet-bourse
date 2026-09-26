"""Bougies d'une minute, gratuites et sans compte (archives publiques de Binance).

    python -m bourse.micro.donnees            télécharge ce qui manque pour toutes les paires

Chaque mois est gardé dans data/micro/<PAIRE>/<AAAA-MM>.npz. Le mois en cours est complété
jour par jour (archives quotidiennes), puis par l'API publique pour les dernières heures.

Colonnes (une ligne par minute, heure UTC) :
    t        début de la minute (secondes depuis 1970)
    o h l c  ouverture, plus haut, plus bas, clôture
    v        volume (en crypto)
    n        nombre de transactions
    vb       volume des acheteurs « pressés » (ordres au marché côté achat)
    q        volume en euros
    qb       volume en euros des acheteurs pressés
             → prix moyen des achats au marché (qb/vb) et des ventes au marché ((q-qb)/(v-vb)) :
               leur écart mesure l'écart achat/vente RÉELLEMENT payé cette minute-là (voir couts.py)
"""
import io
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

from . import MICRO_DIR, PAIRES

ARCHIVE = "https://data.binance.vision/data/spot"
API = "https://data-api.binance.vision/api/v3/klines"
COLS = ("t", "o", "h", "l", "c", "v", "n", "vb", "q", "qb")
DEBUT = date(2021, 1, 1)


def _parse(lines) -> dict[str, np.ndarray]:
    rows = [r for r in lines if r and r[0][:1].isdigit()]    # certaines archives ont une ligne d'en-tête
    a = np.array([[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                   float(r[8]), float(r[9]), float(r[7]), float(r[10])] for r in rows],
                 dtype=float).reshape(-1, len(COLS))
    t = a[:, 0]
    # Depuis 2025, Binance note l'heure en microsecondes (avant : millisecondes)
    t = np.where(t > 1e14, t / 1e6, t / 1e3)
    a[:, 0] = np.round(t)
    return {k: a[:, i] for i, k in enumerate(COLS)}


def _zip(url: str) -> dict[str, np.ndarray] | None:
    for essai in range(4):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                text = z.read(z.namelist()[0]).decode()
            return _parse(line.split(",") for line in text.splitlines())
        except (requests.RequestException, zipfile.BadZipFile):
            time.sleep(2 + 3 * essai)
    raise RuntimeError(f"Téléchargement impossible : {url}")


def _concat(parts: list[dict]) -> dict[str, np.ndarray]:
    parts = [p for p in parts if p and len(p["t"])]
    if not parts:
        return {k: np.zeros(0) for k in COLS}
    d = {k: np.concatenate([p[k] for p in parts]) for k in COLS}
    _, idx = np.unique(d["t"], return_index=True)             # doublons éventuels
    return {k: v[idx] for k, v in d.items()}


def api_recent(symbol: str, since: float) -> dict[str, np.ndarray]:
    """Minutes CLOSES depuis `since` (secondes), par l'API publique."""
    parts, start = [], int(since * 1000)
    now_ms = int(time.time() * 1000)
    while start < now_ms:
        r = requests.get(API, params={"symbol": symbol, "interval": "1m", "startTime": start, "limit": 1000},
                         timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        rows = [x for x in rows if x[6] < now_ms]              # la minute en cours n'est pas finie
        if not rows:
            break
        parts.append(_parse([[str(v) for v in x] for x in rows]))
        start = int(rows[-1][0]) + 60_000
        if len(rows) < 999:
            break
    return _concat(parts)


def _mois(debut: date, fin: date):
    d = date(debut.year, debut.month, 1)
    while d <= fin:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def telecharger(symbol: str, verbose=True) -> None:
    dossier = MICRO_DIR / symbol
    dossier.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    for m in _mois(DEBUT, today):
        f = dossier / f"{m:%Y-%m}.npz"
        mois_fini = (m.year, m.month) != (today.year, today.month)
        if f.exists() and mois_fini:
            with np.load(f) as z:
                if set(COLS) <= set(z.files):
                    continue                                    # déjà là, avec toutes les colonnes
        if mois_fini:
            d = _zip(f"{ARCHIVE}/monthly/klines/{symbol}/1m/{symbol}-1m-{m:%Y-%m}.zip")
        else:
            jours = [m + timedelta(days=i) for i in range((today - m).days)]
            d = _concat([_zip(f"{ARCHIVE}/daily/klines/{symbol}/1m/{symbol}-1m-{j:%Y-%m-%d}.zip")
                         for j in jours])
        if d is None or not len(d["t"]):
            continue                                            # paire pas encore cotée ce mois-là
        np.savez_compressed(f, **d)
        if verbose:
            print(f"{symbol} {m:%Y-%m} : {len(d['t'])} minutes", flush=True)


def charger(symbol: str, debut: date | None = None, fin: date | None = None,
            completer_api: bool = False) -> dict[str, np.ndarray]:
    """Toutes les minutes enregistrées entre `debut` (inclus) et `fin` (exclu)."""
    files = sorted((MICRO_DIR / symbol).glob("*.npz"))
    parts = []
    for f in files:
        y, mo = map(int, f.stem.split("-"))
        if fin and date(y, mo, 1) >= fin:
            continue
        if debut and date(y + (mo == 12), mo % 12 + 1, 1) <= debut:
            continue
        with np.load(f) as z:
            parts.append({k: z[k] for k in COLS})
    d = _concat(parts)
    if completer_api:
        since = d["t"][-1] + 60 if len(d["t"]) else time.time() - 86400
        d = _concat([d, api_recent(symbol, since)])
    lo = datetime(debut.year, debut.month, debut.day, tzinfo=timezone.utc).timestamp() if debut else -np.inf
    hi = datetime(fin.year, fin.month, fin.day, tzinfo=timezone.utc).timestamp() if fin else np.inf
    keep = (d["t"] >= lo) & (d["t"] < hi)
    return {k: v[keep] for k, v in d.items()}


if __name__ == "__main__":
    for s in sys.argv[1:] or PAIRES:
        telecharger(s)
    print("Terminé.")
