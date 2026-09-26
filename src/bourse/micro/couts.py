"""Coûts réels d'exécution, mesurés dans les données (et non supposés).

Écart achat/vente (« spread »)
------------------------------
Binance ne publie pas l'historique des meilleurs prix acheteur/vendeur pour le marché au comptant.
Mais chaque bougie d'une minute donne, séparément, le volume et le montant des ACHATS au marché
(ils paient le prix vendeur, « ask ») et des VENTES au marché (ils reçoivent le prix acheteur, « bid ») :

    prix moyen des achats au marché  = qb / vb
    prix moyen des ventes au marché  = (q - qb) / (v - vb)

Leur écart, rapporté au prix, mesure l'écart réellement payé cette minute-là. Il est bruité (le prix
bouge aussi pendant la minute, ce qui le gonfle en moyenne : l'estimation est plutôt PESSIMISTE),
d'où une médiane glissante sur 30 minutes où les deux côtés ont échangé.
Vérification contre les vrais prix acheteur/vendeur relevés en direct : voir data/micro/calibration/.

Un ordre au marché paie la moitié de l'écart (on achète à l'ask au lieu du milieu), avec un plancher.
"""
import numpy as np
import pandas as pd

PLANCHER = 0.0002          # glissement minimal d'un ordre au marché : 0,02 %
FENETRE = 30


def ecart(d: dict[str, np.ndarray]) -> np.ndarray:
    """Écart achat/vente estimé, en fraction du prix, minute par minute."""
    v, vb = d["v"], d["vb"]
    if "q" not in d:                                   # anciennes données sans les montants
        return np.full(len(v), np.nan)
    q, qb = d["q"], d["qb"]
    vs = v - vb
    with np.errstate(divide="ignore", invalid="ignore"):
        achat = np.where(vb > 0, qb / vb, np.nan)
        vente = np.where(vs > 0, (q - qb) / vs, np.nan)
        brut = (achat - vente) / ((achat + vente) / 2)
    med = pd.Series(brut).rolling(FENETRE * 4, min_periods=5).median()   # 30 minutes « valides » environ
    return np.clip(med.ffill().to_numpy(), 0, 0.05)


def glissement(d: dict[str, np.ndarray]) -> np.ndarray:
    """Glissement d'un ordre au marché, minute par minute : la moitié de l'écart, au moins le plancher."""
    e = ecart(d)
    return np.maximum(PLANCHER, np.nan_to_num(e / 2, nan=PLANCHER))


# Règles de quantité de Binance (relevées le 26/09/2026 sur l'API publique « exchangeInfo »).
# Avec 10 €, on ne peut acheter qu'un multiple du « pas » : le reste attend en liquide.
PAS = {"BTCEUR": 0.00001, "ETHEUR": 0.0001, "SOLEUR": 0.001, "XRPEUR": 0.1, "ADAEUR": 0.1,
       "DOGEEUR": 1.0, "DOTEUR": 0.01, "AVAXEUR": 0.01, "LUNAEUR": 0.01}
MINIMUM = {"DOGEEUR": 1.0}             # montant minimal d'un ordre (5 € pour les autres paires)
MINIMUM_DEFAUT = 5.0
