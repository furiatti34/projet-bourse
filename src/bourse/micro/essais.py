"""Essais d'amélioration du modèle IA, un changement à la fois (voir data/micro/carnet.md).

    python -m bourse.micro.essais <numéro>        1, 2, 3 ou 4 (4 = le meilleur des 1-3 en ordre limité)
    python -m bourse.micro.essais examen <nom>    LA seule fois où l'examen est joué (version finale)

Chaque essai est comparé à la référence (le modèle IA du banc d'essai corrigé) sur les MÊMES données,
entraînement et validation seulement. Résultats : data/micro/essais/<nom>.json.
"""
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import MICRO_DIR, PAIRES_BANC, couts, donnees
from .evaluer import (PERIODES, REGLES_MODELE, _logistique, _passe, _variables, charger_modele,
                      strategie_modele, ts)
from .moteur import SCENARIOS, Regles, bilan, simuler
from .strategies import contexte

DOSSIER = MICRO_DIR / "essais"
STANDARD = SCENARIOS[0]
# Référence des essais 1 à 4 : le modèle du banc corrigé, ré-appris après la correction de la volatilité (essai 0)
REFERENCE = "essais/essai0_correction_volatilite_modele.json"
REGLES_GROS = Regles(objectif_atr=3.0, stop_atr=2.0, duree=120, objectif_min=0.0060)


def _selection(d, horizon: int) -> np.ndarray:
    """Minutes d'apprentissage : entraînement seulement, une sur 7, avec une marge avant la validation
    plus longue que l'horizon (sinon la réponse à apprendre déborderait sur la validation)."""
    lo = ts(PERIODES["entrainement"][0])
    hi = ts(PERIODES["entrainement"][1]) - (horizon + 60) * 60
    n = len(d["t"])
    ok = (d["t"] >= lo) & (d["t"] < hi)
    ok[n - horizon - 5:] = False
    return np.flatnonzero(ok)[::7]


def exemples_hausse(ctx, seuil: float, horizon: int):
    """Étiquette d'origine : le prix monte-t-il de `seuil` dans les `horizon` minutes après l'achat ?"""
    d = ctx["d"]
    sel = _selection(d, horizon)
    hh = sliding_window_view(d["h"], horizon).max(axis=1)
    y = hh[sel + 1] / d["o"][sel + 1] - 1 > seuil
    X, noms = _variables(ctx, sel)
    return X, y.astype(float), noms


def exemples_nets(ctx, regles: Regles, gl):
    """Essai 1 : si j'achetais à cette minute (règles et frais réels), gagnerais-je APRÈS frais ?"""
    d = ctx["d"]
    sel = _selection(d, regles.duree)
    mask = np.zeros(len(d["t"]), dtype=bool)
    mask[sel] = True
    tr = simuler(d, ctx["f"]["atr"], mask, regles, gliss=gl, chacun=True)
    y = tr.rendements(STANDARD) > 0
    X, noms = _variables(ctx, tr.signal)
    return X, y.astype(float), noms


def apprendre(exemples: list) -> dict:
    X = np.concatenate([e[0] for e in exemples])
    y = np.concatenate([e[1] for e in exemples])
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    w, b = _logistique((X - mu) / sd, y)
    p = 1 / (1 + np.exp(-(((X - mu) / sd) @ w + b)))
    noms = exemples[0][2]
    ordre = np.argsort(-np.abs(w))
    return {"w": w, "b": b, "mu": mu, "sd": sd, "seuil": float(np.quantile(p, 0.995)), "noms": noms,
            "taux_base": float(y.mean()), "principales": [(noms[i], float(w[i])) for i in ordre[:12]]}


def _charger(p):
    ctx = contexte(donnees.charger(p, PERIODES["entrainement"][0]))
    return ctx, couts.glissement(ctx["d"], p)


def variantes(essai: int):
    """(nom, fabrique) : fabrique(contextes) → liste de (nom, stratégie, filtre de liquidité ou None)."""
    if essai == 0:
        # Pas une amélioration : la correction d'un défaut. Même étiquette, mêmes règles ; seule change la
        # lecture de la volatilité. Comparée au modèle du banc (v2) pour information.
        def fab():
            ex = []
            for p in PAIRES_BANC:
                ctx, _ = _charger(p)
                ex.append(exemples_hausse(ctx, 0.003, 30))
                del ctx
            m = apprendre(ex)
            v2 = strategie_modele(charger_modele("modele_reference_v2.json"))
            return m, [("Référence (modèle v2 du banc)", v2, False),
                       ("Essai 0 : volatilité corrigée", strategie_modele(m), False)]
        return "essai0_correction_volatilite", fab
    ref = charger_modele(REFERENCE)
    reference = ("Référence (modèle v2 corrigé)", strategie_modele(ref), False)
    if essai == 1:
        def fab():
            ex = []
            for p in PAIRES_BANC:
                ctx, gl = _charger(p)
                ex.append(exemples_nets(ctx, REGLES_MODELE, gl))
                del ctx
            m = apprendre(ex)
            return m, [reference, ("Essai 1 : étiquette nette", strategie_modele(m), False)]
        return "essai1_etiquette_nette", fab
    if essai == 2:
        return "essai2_filtre_liquidite", lambda: (None, [reference, ("Essai 2 : filtre de liquidité",
                                                                      strategie_modele(ref), True)])
    if essai == 3:
        def fab():
            ex = []
            for p in PAIRES_BANC:
                ctx, _ = _charger(p)
                ex.append(exemples_hausse(ctx, 0.006, 120))
                del ctx
            m = apprendre(ex)
            return m, [reference, ("Essai 3 : viser plus gros", strategie_modele(m, REGLES_GROS), False)]
        return "essai3_viser_gros", fab
    raise ValueError(essai)


def comparer(strategies, limite=False, periodes=("entrainement", "validation")) -> dict:
    """Mêmes données, mêmes paires : chaque stratégie, période par période, au scénario standard (et les autres)."""
    trades = {}
    jours = {}
    for p in PAIRES_BANC:
        ctx, gl = _charger(p)
        d = ctx["d"]
        for per in periodes:
            a, b = PERIODES[per]
            jours[per, p] = max(0.0, (min(ts(b), d["t"][-1]) - max(ts(a), d["t"][0])) / 86400)
        for nom, s, filtre in strategies:
            sig = s.signal(ctx)
            if filtre:
                sig = sig & (gl <= couts.PLANCHER + 1e-12)
            r = replace(s.regles, limite=limite and not nom.startswith("Référence"))
            for per in periodes:
                a, b = PERIODES[per]
                trades[nom, per, p] = simuler(d, ctx["f"]["atr"], sig, r, ts(a), ts(b), gl)
        del ctx
    out = {}
    for nom, s, _ in strategies:
        out[nom] = {"regles": asdict(replace(s.regles, limite=limite and not nom.startswith("Référence"))),
                    "frais": {}}
        for fr in SCENARIOS:
            res = {}
            for per in periodes:
                bil = {p: bilan(trades[nom, per, p], fr, jours[per, p], 10.0, couts.PAS.get(p, 0.0),
                                couts.MINIMUM.get(p, couts.MINIMUM_DEFAUT)) for p in PAIRES_BANC}
                r_all = np.concatenate([trades[nom, per, p].rendements(fr) for p in PAIRES_BANC])
                brut = np.concatenate([trades[nom, per, p].px_out / trades[nom, per, p].px_in - 1
                                       for p in PAIRES_BANC])
                res[per] = {"operations": int(len(r_all)),
                            "moyenne_pb": float(r_all.mean() * 1e4) if len(r_all) else 0.0,
                            "brut_pb": float(brut.mean() * 1e4) if len(brut) else 0.0,
                            "gagnantes": float((r_all > 0).mean()) if len(r_all) else 0.0,
                            "reussi": _passe(list(bil.values()), [trades[nom, per, p].rendements(fr)
                                                                  for p in PAIRES_BANC]),
                            "paires": bil}
            out[nom]["frais"][fr.nom] = res
    return out


def lancer(essai: int) -> dict:
    t0 = time.time()
    DOSSIER.mkdir(parents=True, exist_ok=True)
    if essai == 4:
        # le meilleur des essais 1 à 3 (selon la règle du carnet), en entrée à cours limité
        meilleur = choisir_meilleur()
        if meilleur is None:
            print("Aucun essai 1-3 n'a été gardé : l'essai 4 porte sur la référence.")
        nom_f, strategies = _strategies_gardees(meilleur)
        res = comparer(strategies, limite=True)
        nom = "essai4_cours_limite"
    else:
        nom, fab = variantes(essai)
        modele, strategies = fab()
        if modele is not None:
            with open(DOSSIER / f"{nom}_modele.json", "w", encoding="utf-8") as fh:
                json.dump({k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in modele.items()}, fh)
        res = comparer(strategies)
    doc = {"essai": nom, "date": datetime.now().isoformat(timespec="minutes"), "resultats": res,
           "duree_s": round(time.time() - t0)}
    (DOSSIER / f"{nom}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def verdict(doc: dict) -> tuple[bool, str]:
    """Règle du carnet : meilleur que la référence en entraînement ET en validation (frais standard, ≥ 100 op.)."""
    res = doc["resultats"]
    ref, = [v for k, v in res.items() if k.startswith("Référence")]
    cand, = [v for k, v in res.items() if not k.startswith("Référence")]
    R, C = ref["frais"][STANDARD.nom], cand["frais"][STANDARD.nom]
    lignes, ok = [], True
    for per in ("entrainement", "validation"):
        mieux = C[per]["moyenne_pb"] > R[per]["moyenne_pb"] and C[per]["operations"] >= 100
        ok &= mieux
        lignes.append(f"{per} : référence {R[per]['moyenne_pb'] / 100:+.3f} % ({R[per]['operations']} op.) → "
                      f"essai {C[per]['moyenne_pb'] / 100:+.3f} % ({C[per]['operations']} op.) "
                      f"{'✅' if mieux else '❌'}")
    return ok, " ; ".join(lignes)


def choisir_meilleur() -> str | None:
    gardes = []
    for f in sorted(DOSSIER.glob("essai[123]_*.json")):
        if f.stem.endswith("_modele"):
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        ok, _ = verdict(doc)
        if ok:
            cand, = [v for k, v in doc["resultats"].items() if not k.startswith("Référence")]
            gardes.append((cand["frais"][STANDARD.nom]["validation"]["moyenne_pb"], f.stem))
    return max(gardes)[1] if gardes else None


def _strategies_gardees(nom: str | None):
    """La version gardée, au marché (« Référence » de l'essai 4) contre la même en ordre à cours limité."""
    if nom is None:
        s, filtre = strategie_modele(charger_modele(REFERENCE)), False
    elif nom.startswith("essai2"):
        s, filtre = strategie_modele(charger_modele(REFERENCE)), True
    else:
        regles = REGLES_GROS if nom.startswith("essai3") else REGLES_MODELE
        s, filtre = strategie_modele(charger_modele(f"essais/{nom}_modele.json"), regles), False
    return nom, [(f"Référence ({nom or 'modèle v2'}, au marché)", s, filtre),
                 (f"Essai 4 : {nom or 'modèle v2'} en ordre limité", s, filtre)]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    doc = lancer(int(sys.argv[1]))
    ok, texte = verdict(doc)
    print(("GARDÉ" if ok else "REJETÉ") + " — " + texte)
