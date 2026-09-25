"""Heure du logiciel.

En temps normal : l'heure réelle (UTC). Pendant une simulation dans le passé (onglet « Passé »),
l'heure est celle de la simulation : les robots, le courtier et la veille lisent tous `now()`,
ils vivent donc à la date simulée sans le savoir, avec exactement le même code qu'au présent.
"""
from datetime import datetime, timezone

_simulated: datetime | None = None


def now() -> datetime:
    return _simulated or datetime.now(timezone.utc)


def set_simulated(when: datetime | None) -> None:
    """Fixe l'heure simulée (None = retour à l'heure réelle)."""
    global _simulated
    _simulated = when


def is_simulated() -> bool:
    return _simulated is not None
