"""Stratégies. Pour en ajouter une : créer un fichier ici et l'inscrire dans STRATEGIES."""
from .audacieux import AudacieuxStrategy
from .base import Strategy
from .eco import EcoStrategy
from .kamikaze import KamikazeStrategy
from .opportuniste import OpportunisteStrategy
from .prudent import PrudentStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    s.name: s for s in (PrudentStrategy, OpportunisteStrategy, AudacieuxStrategy, KamikazeStrategy, EcoStrategy)
}
