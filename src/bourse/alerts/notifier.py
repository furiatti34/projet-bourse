"""Notifications Windows (petit message en bas à droite de l'écran).

Windows retire une notification du centre de notifications (Windows+N) dès qu'on clique dessus.
Pour qu'une alerte y reste, le clic passe par un petit lien « projetbourse-alerte:<n° d'alerte> » :
Windows lance alors scripts/rouvrir_alerte.py, qui ouvre l'article puis remet l'alerte, sans pop-up,
dans le centre de notifications.
"""
import logging
import sys
import winreg
from datetime import datetime

from windows_toasts import Toast, WindowsToaster

from bourse.config import PROJECT_ROOT

log = logging.getLogger(__name__)

APP_NAME = "Projet Bourse - Veille"
PROTOCOL = "projetbourse-alerte"
_REG_KEY = rf"Software\Classes\{PROTOCOL}"
_HANDLER = PROJECT_ROOT / "scripts" / "rouvrir_alerte.py"


def notify(lines: list[str], url: str | None = None, alert_id: int | None = None,
           silent: bool = False, timestamp: datetime | None = None) -> bool:
    """Affiche une notification. Un clic dessus ouvre l'article (si url fournie).

    alert_id : l'alerte reste dans Windows+N même après un clic.
    silent : la place directement dans Windows+N, sans pop-up ni son.
    """
    try:
        launch = url or None
        if alert_id is not None and _register_protocol():
            launch = f"{PROTOCOL}:{alert_id}"
        toast = Toast(text_fields=lines[:3], launch_action=launch, suppress_popup=silent,
                      timestamp=timestamp)
        if alert_id is not None:
            toast.tag = f"alerte-{alert_id}"  # même étiquette = remplace l'ancienne, pas de doublon
        WindowsToaster(APP_NAME).show_toast(toast)
        return True
    except Exception as exc:
        log.warning("Notification impossible : %s", exc)
        return False


def _register_protocol() -> bool:
    """Déclare le lien projetbourse-alerte: auprès de Windows (pour l'utilisateur courant seulement)."""
    pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = sys.executable
    command = f'"{pythonw}" "{_HANDLER}" "%1"'
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Projet Bourse - alerte")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        return True
    except OSError as exc:
        log.warning("Lien projetbourse-alerte: non enregistré (%s) : le clic ouvrira l'article directement", exc)
        return False
