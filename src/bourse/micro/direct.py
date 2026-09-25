"""Paper trading EN DIRECT du microtrading : vrais prix Binance minute par minute, argent FICTIF.

    python -m bourse.micro.direct            tourne en continu (une vérification par minute)
    python -m bourse.micro.direct --une-fois une seule vérification (essai)

Les robots et leurs réglages sont dans config/micro.yaml. Aucun compte, aucune clé, aucun argent :
seules les données publiques de Binance sont lues.

Mêmes règles que le banc d'essai (bourse.micro.moteur), minute par minute :
  - un signal est lu à la clôture d'une minute ; l'achat au marché se fait au cours d'ouverture de la
    minute suivante (+ glissement) ; un achat à cours limité n'est servi que si le prix passe dessous ;
  - l'objectif et le stop sont des ordres qui, chez un vrai courtier, attendent SUR la plateforme :
    ils continuent donc de fonctionner PC éteint (on les rejoue au retour) ;
  - en revanche, aucun NOUVEL achat n'est décidé pendant que le PC était éteint.
"""
import json
import sqlite3
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import yaml

from bourse.config import PROJECT_ROOT

from . import MICRO_DIR, evaluer
from .moteur import DUREE, OBJECTIF, SCENARIOS, STOP, Regles
from .strategies import catalogue, contexte

CONFIG = PROJECT_ROOT / "config" / "micro.yaml"
BASE = MICRO_DIR / "direct.db"
VERROU = MICRO_DIR / "direct.pid"
API = "https://data-api.binance.vision/api/v3/klines"
HISTORIQUE = 3000        # minutes relues à chaque passage (le temps que les indicateurs se stabilisent)
FRAIS = 90               # une minute n'est « en direct » que si elle a fermé il y a moins de 90 s
MOTIFS = {OBJECTIF: "objectif", STOP: "stop-loss", DUREE: "durée max"}


def lire_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def connexion(path: Path = BASE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS robots (nom TEXT PRIMARY KEY, capital_depart REAL, cash REAL,
            etat TEXT, dernier_t REAL, cree TEXT);
        CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY, robot TEXT, paire TEXT,
            signal_t REAL, entree_t REAL, sortie_t REAL, px_in REAL, px_out REAL, motif TEXT,
            rendement REAL, capital_apres REAL);
        CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, t TEXT, robot TEXT, message TEXT);
    """)
    return conn


def bougies(paire: str, n: int = HISTORIQUE) -> dict[str, np.ndarray]:
    """Les n dernières minutes CLOSES."""
    rows, fin = [], None
    while len(rows) < n:
        params = {"symbol": paire, "interval": "1m", "limit": 1000}
        if fin:
            params["endTime"] = fin
        r = requests.get(API, params=params, timeout=20)
        r.raise_for_status()
        lot = r.json()
        if not lot:
            break
        rows = lot + rows
        fin = int(lot[0][0]) - 1
    now_ms = time.time() * 1000
    rows = [x for x in rows if x[6] < now_ms]            # la minute en cours n'est pas finie
    a = np.array([[x[0] / 1000, x[1], x[2], x[3], x[4], x[5], x[8], x[9]] for x in rows], dtype=float)
    _, idx = np.unique(a[:, 0], return_index=True)
    a = a[idx]
    return {k: a[:, i] for i, k in enumerate(("t", "o", "h", "l", "c", "v", "n", "vb"))}


def _note(conn, robot, msg):
    conn.execute("INSERT INTO journal (t, robot, message) VALUES (?, ?, ?)",
                 (datetime.now().isoformat(timespec="seconds"), robot, msg))


class Robot:
    """Un robot : une stratégie du catalogue, une paire, une façon d'entrer, un scénario de frais."""

    def __init__(self, cfg: dict, strategies: dict, capital: float):
        self.nom, self.paire = cfg["nom"], cfg["paire"]
        self.strategie = strategies[cfg["strategie"]]
        self.regles: Regles = replace(self.strategie.regles, limite=cfg.get("entree") == "limite")
        self.frais = next(f for f in SCENARIOS if f.nom == cfg["frais"])
        self.capital = capital

    def charger(self, conn):
        row = conn.execute("SELECT * FROM robots WHERE nom = ?", (self.nom,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO robots VALUES (?, ?, ?, ?, ?, ?)",
                         (self.nom, self.capital, self.capital, "{}", None, datetime.now().isoformat()))
            _note(conn, self.nom, f"Démarrage avec {self.capital:.2f} € fictifs.")
            return {"cash": self.capital, "etat": {}, "dernier_t": None}
        return {"cash": row["cash"], "etat": json.loads(row["etat"]), "dernier_t": row["dernier_t"]}

    def sauver(self, conn, s):
        conn.execute("UPDATE robots SET cash = ?, etat = ?, dernier_t = ? WHERE nom = ?",
                     (s["cash"], json.dumps(s["etat"]), s["dernier_t"], self.nom))

    def _sortie(self, conn, s, e, j, d, px, motif):
        fr, lim = self.frais, e["limite"]
        px_in = e["px_in"] * (1 if lim else 1 + fr.glissement)
        fee_in = fr.maker if lim else fr.taker
        fee_out = fr.maker if motif == OBJECTIF else fr.taker
        px_net = px if motif == OBJECTIF else px * (1 - fr.glissement)
        r = px_net * (1 - fee_out) / (px_in * (1 + fee_in)) - 1
        s["cash"] *= 1 + r
        conn.execute("INSERT INTO operations (robot, paire, signal_t, entree_t, sortie_t, px_in, px_out, motif,"
                     " rendement, capital_apres) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (self.nom, self.paire, e["signal_t"], e["entree_t"], d["t"][j], e["px_in"], px,
                      MOTIFS[motif], r, s["cash"]))
        _note(conn, self.nom, f"Vente ({MOTIFS[motif]}) à {px:.2f} : {r * 100:+.3f} % → {s['cash']:.4f} €")
        s["etat"] = {}

    def minute(self, conn, s, d, j, signal: bool, en_direct: bool):
        """Traite la minute j (qui vient de fermer)."""
        e, R = s["etat"], self.regles
        o, h, l, c, t = d["o"][j], d["h"][j], d["l"][j], d["c"][j], d["t"][j]
        if e.get("etape") == "achat_marche":                    # achat au marché décidé à la minute d'avant
            if not en_direct or t - e["signal_t"] != 60:
                s["etat"] = {}                                   # PC éteint entre-temps : l'achat n'a pas eu lieu
                return
            e.update(etape="position", px_in=o, entree_t=t, age=0,
                     tp=o + max(R.objectif_atr * e["atr"], R.objectif_min * o), sl=o - R.stop_atr * e["atr"])
            _note(conn, self.nom, f"Achat au marché à {o:.2f} (objectif {e['tp']:.2f}, stop {e['sl']:.2f})")
        elif e.get("etape") == "achat_limite":
            if l < e["limite_px"]:
                p = e["limite_px"]
                e.update(etape="position", px_in=p, entree_t=t, age=0,
                         tp=p + max(R.objectif_atr * e["atr"], R.objectif_min * p), sl=p - R.stop_atr * e["atr"])
                _note(conn, self.nom, f"Achat à cours limité servi à {p:.2f}")
            elif (t - e["signal_t"]) / 60 >= R.attente:
                _note(conn, self.nom, "Ordre à cours limité non servi : annulé")
                s["etat"] = {}
                return
            else:
                return
        if e.get("etape") == "position":
            premiere = e["age"] == 0
            if l <= e["sl"]:
                px = o if (not premiere and o < e["sl"]) else e["sl"]
                return self._sortie(conn, s, e, j, d, px, STOP)
            if not premiere and h > e["tp"]:
                return self._sortie(conn, s, e, j, d, e["tp"], OBJECTIF)
            e["age"] += 1
            if e["age"] >= R.duree:
                return self._sortie(conn, s, e, j, d, c, DUREE)
            return
        # à plat : nouveau signal ?
        if signal and en_direct:
            atr = float(self._atr[j])
            if not np.isfinite(atr) or atr <= 0:
                return
            if R.limite:
                s["etat"] = {"etape": "achat_limite", "signal_t": t, "limite_px": c, "atr": atr, "limite": True}
                _note(conn, self.nom, f"Signal « {self.strategie.nom} » : ordre d'achat à {c:.2f}")
            else:
                s["etat"] = {"etape": "achat_marche", "signal_t": t, "atr": atr, "limite": False}
                _note(conn, self.nom, f"Signal « {self.strategie.nom} » : achat au marché")

    def passage(self, conn, d, ctx, maintenant: float):
        s = self.charger(conn)
        sig = self.strategie.signal(ctx)
        self._atr = ctx["f"]["atr"]
        t = d["t"]
        debut = np.searchsorted(t, s["dernier_t"], side="right") if s["dernier_t"] else len(t) - 1
        for j in range(debut, len(t)):
            self.minute(conn, s, d, j, bool(sig[j]), en_direct=maintenant - (t[j] + 60) < FRAIS)
            s["dernier_t"] = float(t[j])
        self.sauver(conn, s)
        conn.commit()


def robots_config() -> list[Robot]:
    cfg = lire_config()
    strat = {s.nom: s for s in catalogue()}
    if any(r["strategie"] == evaluer.NOM_MODELE for r in cfg.get("robots", [])):
        strat[evaluer.NOM_MODELE] = evaluer.strategie_modele(evaluer.charger_modele())
    return [Robot(r, strat, cfg.get("capital_fictif", 10)) for r in cfg.get("robots", [])]


def passage(robots: list[Robot], conn) -> None:
    paires = sorted({r.paire for r in robots})
    maintenant = time.time()
    for p in paires:
        d = bougies(p)
        ctx = contexte(d)
        for r in robots:
            if r.paire == p:
                r.passage(conn, d, ctx, maintenant)


def boucle() -> None:
    VERROU.write_text(str(__import__("os").getpid()))
    conn = connexion()
    robots = robots_config()
    _note(conn, "", f"Paper trading micro démarré ({len(robots)} robots).")
    conn.commit()
    while True:
        # 3 secondes après chaque changement de minute (le temps que Binance publie la minute close)
        time.sleep(60 - time.time() % 60 + 3)
        try:
            passage(robots, conn)
        except Exception:
            _note(conn, "", "Erreur : " + traceback.format_exc(limit=2)[-500:])
            conn.commit()
            time.sleep(20)


if __name__ == "__main__":
    if "--une-fois" in sys.argv:
        passage(robots_config(), connexion())
        print("Passage effectué.")
    else:
        boucle()
