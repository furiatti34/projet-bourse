"""Version en ligne du site, en mode SPECTATEUR (lecture seule).

Le site en ligne (Streamlit Cloud) ne touche jamais aux données de ce PC : il télécharge une COPIE
(bourse.db + simulations du passé), que le PC publie toutes les 15 min (scripts/publier_en_ligne.py).
Même si un visiteur contournait l'interface, il ne modifierait que cette copie, remplacée au
téléchargement suivant : les robots et le portefeuille du propriétaire restent hors d'atteinte.

Réglages (secrets de l'application sur Streamlit Cloud) :
    BOURSE_SPECTATEUR = "1"
    GITHUB_DEPOT = "compte/projet-bourse"   # dépôt privé qui reçoit la copie
    GITHUB_JETON = "github_pat_…"           # jeton en lecture seule sur ce dépôt
Sur ce PC, aucun de ces réglages n'existe : le site local fonctionne comme avant.
"""
import io
import os
import shutil
import threading
import time
import zipfile

import requests

from bourse.config import PROJECT_ROOT

DATA = PROJECT_ROOT / "data"
BRANCHE = "donnees"           # branche du dépôt qui porte la copie (réécrite à chaque publication)
ARCHIVE = "donnees.zip"
FRAICHEUR_S = 5 * 60          # on retélécharge au plus toutes les 5 min
_verrou = threading.Lock()


def _reglage(nom: str) -> str | None:
    if os.environ.get(nom):
        return os.environ[nom]
    try:
        import streamlit as st
        return st.secrets.get(nom)
    except Exception:         # pas de secrets (site local)
        return None


def spectateur() -> bool:
    """Vrai sur le site en ligne : aucune action possible, consultation seulement."""
    return str(_reglage("BOURSE_SPECTATEUR") or "") == "1"


def preparer() -> bool:
    """Au début de chaque affichage : en mode spectateur, rafraîchit la copie des données si besoin."""
    if not spectateur():
        return False
    marque = DATA / ".telechargement"
    with _verrou:
        if marque.exists() and time.time() - marque.stat().st_mtime < FRAICHEUR_S:
            return True
        try:
            _telecharger()
        except Exception:
            if not (DATA / "bourse.db").exists():
                raise         # rien à montrer sans une première copie
            # sinon on garde la copie précédente et on réessaiera plus tard
        marque.touch()
    return True


def _telecharger() -> None:
    depot, jeton = _reglage("GITHUB_DEPOT"), _reglage("GITHUB_JETON")
    rep = requests.get(f"https://api.github.com/repos/{depot}/contents/{ARCHIVE}", params={"ref": BRANCHE},
                       headers={"Accept": "application/vnd.github.raw",
                                **({"Authorization": f"Bearer {jeton}"} if jeton else {})},
                       timeout=60)
    rep.raise_for_status()
    tmp = DATA / ".nouvelle_copie"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(rep.content)) as z:
        z.extractall(tmp)
    (DATA / "simulations").mkdir(parents=True, exist_ok=True)
    recues = {p.name for p in (tmp / "simulations").glob("*.db")}
    for ancienne in (DATA / "simulations").glob("*.db"):
        if ancienne.name not in recues:
            ancienne.unlink(missing_ok=True)
    for fichier in tmp.rglob("*"):
        if fichier.is_file():
            cible = DATA / fichier.relative_to(tmp)
            cible.parent.mkdir(parents=True, exist_ok=True)
            os.replace(fichier, cible)     # remplacement d'un bloc : jamais de fichier à moitié écrit
    shutil.rmtree(tmp, ignore_errors=True)
