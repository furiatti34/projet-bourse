"""Stratégies. Pour en ajouter une : créer un fichier ici et l'inscrire dans STRATEGIES."""
from .audacieux import AudacieuxStrategy
from .base import Strategy
from .conseiller import ConseillerStrategy
from .eco import EcoStrategy
from .kamikaze import KamikazeStrategy
from .methode_a import METHODES
from .opportuniste import OpportunisteStrategy
from .prudent import PrudentStrategy
from .retraite import RetraiteStrategy
from .temoin import TemoinStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    s.name: s for s in (PrudentStrategy, OpportunisteStrategy, AudacieuxStrategy, KamikazeStrategy, EcoStrategy,
                             RetraiteStrategy, TemoinStrategy, ConseillerStrategy, *METHODES)
}
