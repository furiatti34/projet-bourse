"""Révision des corrections de la méthode A avec la règle de la méthode B (data/labo/methodes.md).

    python -m bourse.labo.revision            lance tout de suite
    python -m bourse.labo.revision --attendre attend que les archives 2020-2022 soient téléchargées

Pour chaque correction gardée par la méthode A, on compare le robot du labo AVEC elle (« actuelle ») au même
robot SANS elle (« candidate », réglage du présent), toutes les autres corrections restant en place.
La correction est confirmée si la version AVEC fait mieux à l'entraînement ET en 2020, 2021 et 2022 ;
sinon elle est à retirer. Les quatre robots sont indépendants : une passe teste une correction par robot.
Résultats : data/labo/revision.json et un tableau ajouté au carnet.
"""
import json
import sqlite3
import sys
import time
from datetime import date, datetime

from bourse.config import PROJECT_ROOT

from . import JOURNAL, LAB_DIR
from .evaluer import ROBOT_NAMES, run

# Retirer une correction = remettre le réglage du présent (config/settings.yaml) ou la désactiver.
CORRECTIONS = {
    "prudent": [("n°4 climat lissé sur 5 jours", {"lissage_jours": 0}),
                ("n°24 jusqu'à 85 % d'actions", {"actions_max": 0.75}),
                ("n°28 au moins 45 % d'actions", {"actions_min": 0.35})],
    "opportuniste": [("n°5 une seule mise en réserve", {"reserve_une_mise": False}),
                     ("n°13 pas de soldes en marché baissier", {"soldes_si_monde_haussier": False}),
                     ("n°25 30 jours pour rebondir", {"duree_jours": 15})],
    "audacieux": [("n°2 garde son marché d'accompagnement (top 5)", {"garder_rang": 1}),
                  ("n°6 marge anti-ping-pong de 10 points", {"marge_posture": 0})],
    "kamikaze": [("n°1 garde un cheval tant qu'il est dans le top 5", {"garder_rang": 2}),
                 ("n°22 paris à la baisse seulement par tempête", {"baisse_si_tempete": False})],
}
PERIODES = ["entrainement", "validation_2020", "validation_2021", "validation_2022"]
ARCHIVE = PROJECT_ROOT / "data" / "historique" / "actualites.db"


def archives_pretes(seuil: float = 0.95) -> bool:
    """Vrai quand chaque source d'archives couvre au moins `seuil` des jours de 2020-2022."""
    conn = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    try:
        jours = (date(2022, 12, 31) - date(2020, 1, 1)).days + 1
        rows = conn.execute("SELECT origin, COUNT(*) FROM hist_coverage WHERE origin LIKE 'wayback:%' "
                            "AND day BETWEEN '2020-01-01' AND '2022-12-31' GROUP BY origin").fetchall()
    finally:
        conn.close()
    return bool(rows) and min(n for _, n in rows) >= seuil * jours


def reviser() -> dict:
    passes = max(len(v) for v in CORRECTIONS.values())
    resultats = []
    for i in range(passes):
        candidat = {r: corr[i][1] for r, corr in CORRECTIONS.items() if i < len(corr)}
        robots = list(candidat)
        res = run(f"revision{i + 1}", candidat, robots, "B")
        R = res["resultats"]
        for r in robots:
            nom, _ = CORRECTIONS[r][i]
            n = ROBOT_NAMES[r]
            avec = {p: R[f"actuelle/{p}"][n]["perf_pct"] for p in PERIODES}
            sans = {p: R[f"candidate/{p}"][n]["perf_pct"] for p in PERIODES}
            erreurs = sum(R[f"{v}/{p}"][n].get("erreurs", 0) for v in ("actuelle", "candidate") for p in PERIODES)
            resultats.append({"robot": n, "correction": nom, "avec": avec, "sans": sans, "erreurs": erreurs,
                              "confirmee": erreurs == 0 and all(avec[p] > sans[p] for p in PERIODES),
                              "dossier": res["date"]})
    doc = {"date": datetime.now().isoformat(timespec="minutes"), "resultats": resultats}
    (LAB_DIR / "revision.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    _carnet(doc)
    return doc


def _carnet(doc: dict) -> None:
    pct = lambda x: f"{x:+.1f} %".replace(".", ",")
    lignes = ["", f"## Révision des corrections de la méthode A avec la règle B ({doc['date'][:16].replace('T', ' ')})",
              "Chaque correction est retirée seule (les autres restent) : « avec » doit battre « sans » à l'entraînement "
              "ET en 2020, 2021 et 2022.", "",
              "| Robot | Correction | Entraînement avec / sans | 2020 | 2021 | 2022 | Verdict |", "|---|---|---|---|---|---|---|"]
    for r in doc["resultats"]:
        cell = lambda p: f"{pct(r['avec'][p])} / {pct(r['sans'][p])}"
        verdict = ("⚠️ INVALIDE (erreurs du robot)" if r["erreurs"] else
                   "✅ confirmée" if r["confirmee"] else "❌ à retirer (méthode B)")
        lignes.append(f"| {r['robot'].replace('Robot ', '')} | {r['correction']} | {cell('entrainement')} | "
                      f"{cell('validation_2020')} | {cell('validation_2021')} | {cell('validation_2022')} | {verdict} |")
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


if __name__ == "__main__":
    if "--attendre" in sys.argv:
        while not archives_pretes():
            time.sleep(1800)                      # on revérifie toutes les 30 minutes
    print(json.dumps(reviser(), ensure_ascii=False, indent=2))
