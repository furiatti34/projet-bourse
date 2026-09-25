"""Micro : robot de microtrading (scalping) sur les cryptos, bougies d'une minute.

Entièrement séparé des robots de la course du présent : rien ici n'est importé par veille.py ni par
les robots existants. Argent FICTIF uniquement.

    donnees.py     téléchargement gratuit des bougies 1 minute (Binance, sans compte)
    indicateurs.py indicateurs techniques (RSI, Bollinger, VWAP, flux acheteurs…)
    patterns.py    figures de chandeliers et figures chartistes
    strategies.py  le catalogue des stratégies testées
    moteur.py      simulation des ordres (frais, glissement, ordres à cours limité)
    evaluer.py     banc d'essai : entraînement → validation → examen
    direct.py      paper trading en direct sur les vrais prix
"""
from bourse.config import PROJECT_ROOT

MICRO_DIR = PROJECT_ROOT / "data" / "micro"
PAIRES = ["BTCEUR", "ETHEUR", "SOLEUR"]
# Banc d'essai : on ajoute des cryptos qui se sont effondrées (Terra/LUNA a disparu en mai 2022, Polkadot et
# Avalanche ont perdu 80-90 %) pour éviter le « biais du survivant » : ne tester que sur des gagnantes connues.
PAIRES_BANC = PAIRES + ["XRPEUR", "ADAEUR", "DOGEEUR", "DOTEUR", "AVAXEUR", "LUNAEUR"]
