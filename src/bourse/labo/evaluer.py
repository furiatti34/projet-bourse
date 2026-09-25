"""Banc d'essai du labo : compare la version actuelle des robots du labo à une correction candidate,
sur l'entraînement et la validation, avec exactement les mêmes données.

    python -m bourse.labo.evaluer <nom de l'essai> '<réglages candidats en JSON>' [robots]

Exemple : python -m bourse.labo.evaluer frais-kamikaze '{"kamikaze": {"garde_min_heures": 72}}' kamikaze

Toutes les simulations d'un essai lisent une COPIE FIGÉE des archives d'actualités (le téléchargement
continue à côté) : la version actuelle et la candidate voient donc exactement les mêmes informations.
Elles n'utilisent que les actualités déjà téléchargées (aucune requête vers Google pendant l'essai).
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from bourse.backtest import simulation as sim
from bourse.config import PROJECT_ROOT, load_settings

from . import LAB_DIR, lab_settings

TRAIN = (date(2016, 9, 26), date(2019, 12, 31))
VALID = (date(2020, 1, 1), date(2022, 12, 31))
EXAM = (date(2023, 1, 1), None)      # jamais utilisé ici
ROBOT_NAMES = {"prudent": "Robot Prudent", "opportuniste": "Robot Opportuniste", "audacieux": "Robot Audacieux",
               "kamikaze": "Robot Kamikaze"}
PYTHONW = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"


def news_until(archive: Path) -> date:
    """Dernier jour jusqu'où les actualités Google sont téléchargées sans trou (depuis le début)."""
    conn = sqlite3.connect(archive)
    days = {r[0] for r in conn.execute("SELECT day FROM hist_coverage WHERE origin LIKE 'google:%'"
                                       " GROUP BY day HAVING COUNT(*) >= 3")}
    d = date(2016, 9, 1)
    while (d + timedelta(days=1)).isoformat() in days:
        d += timedelta(days=1)
    return d


def freeze_archive(folder: Path) -> Path:
    frozen = folder / "archive_figee.db"
    src = sqlite3.connect(PROJECT_ROOT / "data" / "historique" / "actualites.db", timeout=60)
    dst = sqlite3.connect(frozen)
    src.backup(dst)
    dst.close()
    src.close()
    return frozen


def metrics(path: Path) -> dict:
    conn = sqlite3.connect(path)
    info = sim.get_info(conn)
    out = {"statut": info.get("statut")}
    for pid, name, cash in conn.execute("SELECT id, name, initial_cash FROM portfolios"):
        s = pd.read_sql("SELECT time, value_eur, benchmark_eur FROM snapshots WHERE portfolio_id = ? ORDER BY time",
                        conn, params=(pid,))
        if s.empty:
            continue
        v, b = s["value_eur"], s["benchmark_eur"]
        years = max((pd.Timestamp(s["time"].iloc[-1]) - pd.Timestamp(s["time"].iloc[0])).days / 365.25, 0.01)
        fees, trades = conn.execute("SELECT COALESCE(SUM(fees_eur), 0), COUNT(*) FROM orders WHERE portfolio_id = ?"
                                    " AND status = 'EXECUTE'", (pid,)).fetchone()
        perf = v.iloc[-1] / cash - 1
        out[name] = {
            "perf_pct": round(perf * 100, 2),
            "par_an_pct": round(((1 + perf) ** (1 / years) - 1) * 100, 2),
            "indice_pct": round((b.iloc[-1] / b.iloc[0] - 1) * 100, 2),
            "chute_max_pct": round(((v / v.cummax()) - 1).min() * 100, 2),
            "frais_eur": round(fees), "ordres": trades,
        }
    return out


def run(name: str, candidate: dict, robots: list[str]) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = LAB_DIR / "essais" / f"{stamp}_{name}"
    folder.mkdir(parents=True, exist_ok=True)
    archive = freeze_archive(folder)
    train_end = min(TRAIN[1], news_until(archive) - timedelta(days=1))
    periods = {"entrainement": (TRAIN[0], train_end), "validation": VALID}
    base = lab_settings(load_settings())
    cand = json.loads(json.dumps(base))
    for p in cand["paper_trading"]["portefeuilles"]:
        key = str(p.get("strategie", "")).removeprefix("labo_")
        p["parametres"] = {**(p.get("parametres") or {}), **(candidate.get(key) or {})}
    names = [ROBOT_NAMES[r] for r in robots]
    jobs = {}
    for version, settings in (("actuelle", base), ("candidate", cand)):
        if version == "candidate" and not candidate:
            continue
        for period, (start, end) in periods.items():
            path = sim.create(start, end, names, settings=settings, folder=folder,
                              name=f"{version}_{period}.db", download_news=False)
            env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONIOENCODING": "utf-8",
                   "BOURSE_ARCHIVE": str(archive)}
            proc = subprocess.Popen([str(PYTHONW), "-m", "bourse.backtest.simulation", str(path)], cwd=PROJECT_ROOT,
                                    env=env, stdout=open(path.with_suffix(".log"), "w", encoding="utf-8"),
                                    stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            jobs[(version, period)] = (proc, path)
    t0 = time.monotonic()
    for proc, _ in jobs.values():
        proc.wait()
    result = {"essai": name, "candidat": candidate, "robots": names, "date": stamp,
              "periodes": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in periods.items()},
              "duree_min": round((time.monotonic() - t0) / 60, 1),
              "resultats": {f"{v}/{p}": metrics(path) for (v, p), (_, path) in jobs.items()}}
    (folder / "resultats.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    essai = sys.argv[1]
    cand = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    rob = sys.argv[3].split(",") if len(sys.argv) > 3 else list(ROBOT_NAMES)
    print(json.dumps(run(essai, cand, rob), ensure_ascii=False, indent=2))
