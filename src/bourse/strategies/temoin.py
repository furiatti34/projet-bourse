"""« Portefeuille Gilbert (inchangé) » : le témoin du Robot Retraité.

Il reçoit exactement le même portefeuille réel au même moment (mêmes titres, mêmes quantités, sans frais), puis
on n'y touche plus jamais. Comparer le Robot Retraité à ce témoin répond à la seule question qui compte :
l'IA fait-elle mieux que le portefeuille laissé tel quel ?
"""
from bourse.data.prices import KNOWN_NAMES

from .base import Strategy
from .retraite import load_start, transfer


class TemoinStrategy(Strategy):
    name = "temoin"
    description = "Le portefeuille réel de départ, laissé tel quel (aucun achat, aucune vente) : la référence à battre."

    def __init__(self, params: dict, view=None):
        super().__init__(params, view)
        self.start = load_start(params)
        KNOWN_NAMES.update(self.start.get("noms", {}))

    def on_start(self, broker, state):
        lines = transfer(broker, self.start)
        self.note(f"Démarrage : je reçois le portefeuille réel du relevé du {self.start['releve_du']} "
                  f"({len(lines)} lignes, mêmes quantités, sans frais). Ensuite, on n'y touche plus.")

    def on_cycle(self, broker, state):
        self.think("Je ne fais rien, c'est voulu : je montre ce que devient le portefeuille réel si on le laisse "
                   "tel quel. Le Robot Retraité doit faire mieux que moi.")
