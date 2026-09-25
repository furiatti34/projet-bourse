"""Labo : version d'essai des robots, utilisée SEULEMENT par les simulations dans le passé.

La course du présent garde ses robots (bourse.strategies) : une correction du labo n'y passe
qu'avec l'accord du propriétaire. Chaque correction est notée dans data/labo/carnet.md.

Règles du labo (pas de triche) :
  - corriger des défauts de comportement, jamais « éviter ce qui a perdu » (aucune liste noire
    de placements ni de dates) ;
  - une correction n'est gardée que si elle améliore l'entraînement (2016-2019) ET la validation
    (2020-2022) ; l'examen (2023-2026) n'est joué qu'une fois, à la toute fin.
"""
import json
from pathlib import Path

import yaml

from bourse.config import PROJECT_ROOT
from bourse.strategies import STRATEGIES

from .strategies import LAB

HERE = Path(__file__).parent
PARAMS = HERE / "parametres.yaml"       # réglages du labo, par-dessus ceux du présent
LAB_DIR = PROJECT_ROOT / "data" / "labo"
JOURNAL = LAB_DIR / "carnet.md"


def register() -> None:
    """Rend les robots du labo utilisables (dans le programme de simulation uniquement)."""
    STRATEGIES.update(LAB)


def lab_settings(settings: dict) -> dict:
    """Réglages du présent, avec les robots remplacés par leur version labo."""
    s = json.loads(json.dumps(settings))
    overrides = (yaml.safe_load(PARAMS.read_text(encoding="utf-8")) or {}) if PARAMS.exists() else {}
    for p in s["paper_trading"]["portefeuilles"]:
        name = p.get("strategie")
        if name and f"labo_{name}" in LAB:
            p["strategie"] = f"labo_{name}"
            p["parametres"] = {**(p.get("parametres") or {}), **(overrides.get(name) or {})}
    return s
