"""Catalogue des stratégies de microtrading testées.

Chaque stratégie dit QUAND acheter (un signal vrai/faux par minute) et avec quelles règles de sortie.
Les réglages sont ceux des manuels et des études (pas optimisés sur nos données) : on teste des idées
connues, on ne cherche pas le réglage qui aurait marché par hasard.
"""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .indicateurs import atr, calculer, shift
from .moteur import Regles
from .patterns import figures, figures_chartistes, multi_echelle, replacer


@dataclass
class Strategie:
    nom: str
    famille: str
    description: str
    signal: Callable[[dict], np.ndarray]
    regles: Regles = field(default_factory=Regles)


def contexte(d: dict[str, np.ndarray]) -> dict:
    """Tout ce que les stratégies peuvent regarder (calculé une fois par paire)."""
    ctx = {"d": d, "f": calculer(d), "n": len(d["c"])}
    n = ctx["n"]
    ctx["p1"] = figures(d["o"], d["h"], d["l"], d["c"])
    ctx["g1"] = figures_chartistes(d["o"], d["h"], d["l"], d["c"], ctx["f"]["atr"])
    for k in (5, 15):
        b, ferme = multi_echelle(d, k)
        a = atr(b["h"], b["l"], b["c"], 14)
        ctx[f"p{k}"] = {nom: replacer(s, ferme, n) for nom, s in figures(b["o"], b["h"], b["l"], b["c"]).items()}
        ctx[f"g{k}"] = {nom: replacer(s, ferme, n)
                        for nom, s in figures_chartistes(b["o"], b["h"], b["l"], b["c"], a).items()}
    return ctx


def _croise_dessus(a, b):
    return (a > b) & (shift(a) <= shift(b))


# Règles de sortie par horizon
R1 = Regles(objectif_atr=1.5, stop_atr=1.0, duree=15)        # scalping 1 minute
R5 = Regles(objectif_atr=3.0, stop_atr=2.0, duree=60)        # bougies de 5 minutes
R15 = Regles(objectif_atr=5.0, stop_atr=3.5, duree=180)      # bougies de 15 minutes
RMR = Regles(objectif_atr=2.0, stop_atr=2.0, duree=30)       # retour à la moyenne

HAUSSIERES = ["marteau", "marteau_inverse", "doji_libellule", "marubozu_vert", "pin_bar_haussiere",
              "avalement_haussier", "harami_haussier", "penetrante", "pinces_bas", "cassure_interieure",
              "etoile_du_matin", "trois_soldats", "hikkake_haussier"]
BAISSIERES = ["etoile_filante", "pendu", "marubozu_rouge", "avalement_baissier", "harami_baissier",
              "couverture_nuage_noir", "pinces_haut", "etoile_du_soir", "trois_corbeaux"]
CHARTISTES = ["drapeau_haussier", "double_creux", "rebond_support", "cassure_range", "triangle_ascendant"]
NOMS = {n: n.replace("_", " ") for n in HAUSSIERES + BAISSIERES + CHARTISTES}


def catalogue() -> list[Strategie]:
    S: list[Strategie] = []
    c = lambda x: x["d"]["c"]
    F = lambda x: x["f"]

    # ---------------- Retour à la moyenne : « ça a trop baissé trop vite, ça va rebondir » ----------------
    S += [
        Strategie("RSI(2) sous 10, tendance haussière", "Retour à la moyenne",
                  "Méthode de Larry Connors : le RSI très court est au plus bas alors que la tendance de fond monte.",
                  lambda x: (F(x)["rsi2"] < 10) & (c(x) > F(x)["ema600"]), RMR),
        Strategie("RSI(2) sous 5", "Retour à la moyenne", "RSI très court extrêmement survendu, sans filtre.",
                  lambda x: F(x)["rsi2"] < 5, RMR),
        Strategie("RSI(14) sous 25", "Retour à la moyenne", "RSI classique très survendu.",
                  lambda x: (F(x)["rsi14"] < 25) & (shift(F(x)["rsi14"]) >= 25), RMR),
        Strategie("Sous la bande de Bollinger (-2,5σ)", "Retour à la moyenne",
                  "Le prix sort très en dessous de sa moyenne des 20 dernières minutes.",
                  lambda x: (F(x)["bb_z"] < -2.5) & (shift(F(x)["bb_z"]) >= -2.5), RMR),
        Strategie("Loin sous le VWAP 60 min", "Retour à la moyenne",
                  "Le prix est à plus de 3 ATR sous le prix moyen pondéré par les volumes de la dernière heure.",
                  lambda x: (F(x)["vwap60_ecart"] < -3) & (shift(F(x)["vwap60_ecart"]) >= -3), RMR),
        Strategie("Loin sous le VWAP du jour", "Retour à la moyenne",
                  "Même idée avec le VWAP de la journée (très suivi par les traders professionnels).",
                  lambda x: (F(x)["vwap_jour_ecart"] < -5) & (shift(F(x)["vwap_jour_ecart"]) >= -5), RMR),
        Strategie("Stochastique qui remonte sous 20", "Retour à la moyenne",
                  "La stochastique croise sa moyenne vers le haut en zone survendue.",
                  lambda x: _croise_dessus(F(x)["stoch_k"], F(x)["stoch_d"]) & (F(x)["stoch_d"] < 20), RMR),
        Strategie("CCI sous -200", "Retour à la moyenne", "Indice CCI extrême.",
                  lambda x: (F(x)["cci"] < -200) & (shift(F(x)["cci"]) >= -200), RMR),
        Strategie("4 minutes rouges d'affilée", "Retour à la moyenne", "Quatre baisses consécutives.",
                  lambda x: F(x)["rouges_suite"] == 4, RMR),
        Strategie("6 minutes rouges d'affilée", "Retour à la moyenne", "Six baisses consécutives.",
                  lambda x: F(x)["rouges_suite"] == 6, RMR),
        Strategie("Contre la bougie poussée par les vendeurs", "Retour à la moyenne",
                  "Étude Kitron & Wengrowicz (2026) : après une forte minute de baisse causée par des ventes "
                  "au marché, le prix a tendance à revenir.",
                  lambda x: (F(x)["ret1"] < -1.5 * F(x)["atr_pct"]) & (F(x)["flux"] < -0.5),
                  Regles(objectif_atr=1.0, stop_atr=1.5, duree=15)),
        Strategie("Chute de 3 ATR en 5 minutes", "Retour à la moyenne", "Petit krach éclair.",
                  lambda x: (F(x)["ret5"] < -3 * F(x)["atr_pct"]) & (shift(F(x)["ret5"]) >= -3 * F(x)["atr_pct"]),
                  RMR),
    ]
    # ---------------- Élan / cassure : « ça part, on monte dans le train » ----------------
    S += [
        Strategie("Cassure du plus haut 60 min + volume", "Élan / cassure",
                  "Le prix dépasse le plus haut de la dernière heure avec un volume inhabituel.",
                  lambda x: (c(x) > F(x)["haut60"]) & (F(x)["vol_z"] > 2), R5),
        Strategie("Cassure du plus haut 4 h", "Élan / cassure", "Canal de Donchian de 240 minutes.",
                  lambda x: (c(x) > F(x)["haut240"]) & (shift(c(x)) <= shift(F(x)["haut240"])), R15),
        Strategie("Croisement moyennes 9/21 dans la tendance", "Élan / cassure",
                  "La moyenne courte passe au-dessus de la moyenne moyenne, tendance de fond haussière.",
                  lambda x: _croise_dessus(F(x)["ema9"], F(x)["ema21"]) & (c(x) > F(x)["ema200"]), R5),
        Strategie("MACD repasse positif", "Élan / cassure", "Histogramme du MACD qui redevient positif.",
                  lambda x: (F(x)["macd_hist"] > 0) & (shift(F(x)["macd_hist"]) <= 0) & (c(x) > F(x)["ema600"]), R5),
        Strategie("Compression puis cassure (squeeze)", "Élan / cassure",
                  "Les bandes de Bollinger sont très serrées (calme avant la tempête), puis le prix casse vers le haut.",
                  lambda x: (shift(F(x)["bb_largeur_rang"]) < 0.1) & (c(x) > F(x)["haut20"]), R5),
        Strategie("Rafale d'achats au marché", "Élan / cassure",
                  "Les acheteurs pressés dominent depuis 5 minutes (flux d'ordres) : la pression continue souvent un peu.",
                  lambda x: (F(x)["flux5"] > 0.6) & (F(x)["ret5"] > 0) & (F(x)["vol_z"] > 1), R1),
        Strategie("Explosion de volume à la hausse", "Élan / cassure",
                  "Volume 4 fois au-dessus de la normale sur une grande bougie verte.",
                  lambda x: (F(x)["vol_z"] > 4) & (F(x)["ret1"] > F(x)["atr_pct"]), R1),
        Strategie("Repli sur la moyenne en forte tendance", "Élan / cassure",
                  "Tendance forte (ADX > 30, moyennes alignées) et le prix revient toucher la moyenne 21.",
                  lambda x: (F(x)["adx"] > 30) & (F(x)["ema9"] > F(x)["ema21"]) & (F(x)["ema21"] > F(x)["ema50"])
                  & (x["d"]["l"] <= F(x)["ema21"]) & (c(x) > F(x)["ema21"]), R5),
    ]
    # ---------------- Effets d'horloge ----------------
    S += [
        Strategie("Tour de bougie (minute 0 de chaque heure)", "Horloge",
                  "Étude « turn-of-the-candle » (2023) : les hausses se concentrent sur la première minute "
                  "des bougies de 15 minutes. Achat à xxh00, revente une minute plus tard.",
                  lambda x: F(x)["minute"] == 59, Regles(objectif_atr=50, stop_atr=50, duree=1)),
        Strategie("Tour de bougie (minutes 0, 15, 30, 45)", "Horloge", "Même effet sur chaque quart d'heure.",
                  lambda x: np.isin(F(x)["minute"], (14, 29, 44, 59)), Regles(objectif_atr=50, stop_atr=50, duree=1)),
    ]
    # ---------------- Figures de chandeliers ----------------
    for k, R in ((1, R1), (5, R5), (15, R15)):
        for nom in HAUSSIERES:
            S.append(Strategie(f"{NOMS[nom].capitalize()} ({k} min)", f"Chandeliers {k} min",
                               f"Figure haussière « {NOMS[nom]} » sur des bougies de {k} minute(s).",
                               lambda x, nom=nom, k=k: x[f"p{k}"][nom], R))
        for nom in BAISSIERES:
            S.append(Strategie(f"{NOMS[nom].capitalize()} inversée ({k} min)", f"Chandeliers {k} min",
                               f"Figure baissière « {NOMS[nom]} » jouée à l'envers (achat), certaines études "
                               f"trouvant que ces figures annoncent en fait un rebond sur les cryptos.",
                               lambda x, nom=nom, k=k: x[f"p{k}"][nom], R))
    # ---------------- Figures chartistes ----------------
    for k, R in ((1, R1), (5, R5), (15, R15)):
        for nom in CHARTISTES:
            S.append(Strategie(f"{NOMS[nom].capitalize()} ({k} min)", "Figures chartistes",
                               f"Figure « {NOMS[nom]} » sur des bougies de {k} minute(s).",
                               lambda x, nom=nom, k=k: x[f"g{k}"][nom], R))
    # ---------------- Combinaison ----------------
    retour = [s for s in S if s.famille == "Retour à la moyenne"]
    S.append(Strategie("Vote : au moins 2 signaux de rebond", "Combinaison",
                       "Achat seulement quand au moins deux signaux de « retour à la moyenne » sont d'accord.",
                       lambda x: np.sum([s.signal(x) for s in retour], axis=0) >= 2, RMR))
    return S
