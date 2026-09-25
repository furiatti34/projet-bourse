"""Lecture du fichier de réglages config/settings.yaml."""
import os
from pathlib import Path

import yaml

# Dossier racine du projet (deux niveaux au-dessus de src/bourse/)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.yaml"


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def db_path(settings: dict) -> Path:
    """Chemin absolu de la base de données (le dossier est créé si besoin)."""
    # BOURSE_DB permet d'utiliser une autre base (tests) sans toucher à la vraie
    path = Path(os.environ.get("BOURSE_DB") or PROJECT_ROOT / settings["general"]["base_de_donnees"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
