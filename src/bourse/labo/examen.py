"""L'EXAMEN final du labo (2023 → aujourd'hui) : joué UNE seule fois, pour toutes les versions ensemble.

    python -m bourse.labo.examen

Versions comparées sur exactement les mêmes données (copie figée des archives) :
  - « Présent »   : les robots de la course du présent, sans aucune correction ;
  - « Méthode A » : les robots du labo avec les corrections de la méthode A (data/labo/versions/methode_A/) ;
  - « Méthode B » : les robots du labo tels qu'au moment de l'examen (méthode B, révision comprise).

Garde-fous :
  - un fichier data/labo/EXAMEN_JOUE est créé au lancement : l'examen ne peut plus jamais être relancé
    (le relancer après avoir vu les notes, ce serait regarder les réponses) ;
  - refus de démarrer tant que les archives d'actualités 2023-2026 ne sont pas téléchargées (sinon
    l'examen ne serait pas passé dans les mêmes conditions que l'entraînement).
"""
import json
import os
import sqlite3
import subprocess
import time
from datetime import date, datetime

import yaml

from bourse.backtest import simulation as sim
from bourse.config import PROJECT_ROOT, load_settings

from . import LAB, LAB_DIR
from .evaluer import EXAM, PYTHONW, ROBOT_NAMES, freeze_archive, metrics

VERROU = LAB_DIR / "EXAMEN_JOUE"
VERSIONS_A = LAB_DIR / "versions" / "methode_A" / "parametres.yaml"


def reglages_labo(fichier) -> dict:
    """Réglages du présent, robots remplacés par leur version labo avec les réglages de `fichier`."""
    s = json.loads(json.dumps(load_settings()))
    over = yaml.safe_load(open(fichier, encoding="utf-8")) or {}
    for p in s["paper_trading"]["portefeuilles"]:
        nom = p.get("strategie")
        if nom and f"labo_{nom}" in LAB:
            p["strategie"] = f"labo_{nom}"
            p["parametres"] = {**(p.get("parametres") or {}), **(over.get(nom) or {})}
    return s


def archives_pretes(seuil: float = 0.95) -> bool:
    conn = sqlite3.connect(f"file:{PROJECT_ROOT / 'data' / 'historique' / 'actualites.db'}?mode=ro", uri=True)
    try:
        jours = (date.today() - EXAM[0]).days
        rows = conn.execute("SELECT origin, COUNT(*) FROM hist_coverage WHERE day >= ? GROUP BY origin",
                            (EXAM[0].isoformat(),)).fetchall()
    finally:
        conn.close()
    return bool(rows) and min(n for _, n in rows) >= seuil * jours


def jouer() -> dict:
    if VERROU.exists():
        raise SystemExit(f"L'examen a déjà été joué ({VERROU.read_text(encoding='utf-8').strip()}). "
                         "Il ne se rejoue pas : ce serait regarder les réponses.")
    if not archives_pretes():
        raise SystemExit("Archives d'actualités 2023-2026 incomplètes : examen reporté.")
    VERROU.write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")
    dossier = LAB_DIR / "essais" / f"{datetime.now():%Y%m%d-%H%M%S}_EXAMEN"
    dossier.mkdir(parents=True)
    archive = freeze_archive(dossier)
    fin = date.today()
    versions = {"present": load_settings(), "methode_A": reglages_labo(VERSIONS_A),
                "methode_B": reglages_labo(LAB_DIR.parent.parent / "src" / "bourse" / "labo" / "parametres.yaml")}
    noms = list(ROBOT_NAMES.values())
    jobs = {}
    for v, reglages in versions.items():
        path = sim.create(EXAM[0], fin, noms, settings=reglages, folder=dossier, name=f"{v}.db", download_news=False)
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONIOENCODING": "utf-8",
               "BOURSE_ARCHIVE": str(archive)}
        jobs[v] = (subprocess.Popen([str(PYTHONW), "-m", "bourse.backtest.simulation", str(path)], cwd=PROJECT_ROOT,
                                    env=env, stdout=open(path.with_suffix(".log"), "w", encoding="utf-8"),
                                    stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)), path)
    t0 = time.monotonic()
    for proc, _ in jobs.values():
        proc.wait()
    doc = {"date": datetime.now().isoformat(timespec="minutes"), "periode": [EXAM[0].isoformat(), fin.isoformat()],
           "duree_min": round((time.monotonic() - t0) / 60, 1),
           "resultats": {v: metrics(path) for v, (_, path) in jobs.items()}}
    (LAB_DIR / "examen.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


if __name__ == "__main__":
    print(json.dumps(jouer(), ensure_ascii=False, indent=2))
