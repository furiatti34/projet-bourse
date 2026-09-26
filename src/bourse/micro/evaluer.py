"""Banc d'essai du microtrading : toutes les stratégies, toutes les paires, tous les scénarios de frais.

    python -m bourse.micro.evaluer

Trois périodes qui ne se chevauchent pas (comme pour le labo) :
    entraînement  2021-01 → 2023-12   on regarde tout, on élimine ce qui ne marche pas
    validation    2024-01 → 2025-06   seules les survivantes de l'entraînement sont regardées
    examen        2025-07 → aujourd'hui   joué UNE fois, seulement pour les survivantes de la validation

Une stratégie « survit » à une période si, pour un scénario de frais donné :
    - elle gagne en moyenne par opération, toutes paires confondues,
    - elle gagne sur plus de la moitié des paires,
    - elle a fait au moins 100 opérations (sinon c'est peut-être de la chance).

Le « modèle IA » apprend sur l'entraînement seul, avec tous les indicateurs et toutes les figures à la fois.
Résultats : data/micro/resultats.json et data/micro/rapport.md.
"""
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timezone

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import MICRO_DIR, PAIRES_BANC, donnees
from .moteur import SCENARIOS, Regles, bilan, simuler
from .strategies import Strategie, catalogue, contexte

PERIODES = {
    "entrainement": (date(2021, 1, 1), date(2024, 1, 1)),
    "validation": (date(2024, 1, 1), date(2025, 7, 1)),
    "examen": (date(2025, 7, 1), None),
}
MIN_OPERATIONS = 100


def ts(d: date | None) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() if d else np.inf


# ------------------------------------------------------------------ modèle IA (régression logistique)

def _variables(ctx: dict, idx: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Toutes les informations disponibles aux minutes `idx`, mises à la même échelle."""
    f = {k: v[idx] for k, v in ctx["f"].items()}
    c = ctx["d"]["c"][idx]
    cols, noms = [], []
    a = f["atr"]
    for k in ("ema9", "ema21", "ema50", "ema200", "ema600", "haut20", "bas20", "haut60", "bas60",
              "haut240", "bas240"):
        cols.append((c - f[k]) / a); noms.append(f"écart {k}")
    for k in ("rsi2", "rsi7", "rsi14", "bb_z", "bb_largeur_rang", "stoch_k", "stoch_d", "cci", "adx",
              "vwap60_ecart", "vwap_jour_ecart", "vol_z", "flux", "flux5", "rouges_suite", "vertes_suite"):
        cols.append(f[k]); noms.append(k)
    for k in ("ret1", "ret3", "ret5", "ret15", "ret60"):
        cols.append(f[k] / f["atr_pct"]); noms.append(k)
    cols.append(f["macd_hist"] / f["atr_pct"]); noms.append("macd")
    cols.append(np.log(f["atr_pct"])); noms.append("volatilité")
    for h in range(0, 24, 4):
        cols.append(((f["heure"] >= h) & (f["heure"] < h + 4)).astype(float)); noms.append(f"heure {h}-{h+4}")
    cols.append(np.isin(f["minute"], (14, 29, 44, 59)).astype(float)); noms.append("fin de quart d'heure")
    for grp in ("p1", "p5", "p15", "g1", "g5", "g15"):
        for k, v in ctx[grp].items():
            cols.append(v[idx].astype(float)); noms.append(f"{k} ({grp})")
    X = np.column_stack(cols).astype(np.float32)
    X[~np.isfinite(X)] = 0
    return np.clip(X, -20, 20), noms


def _logistique(X, y, l2=1e-3, iters=300):
    """Régression logistique (descente de gradient Adam), sans bibliothèque externe."""
    w, b = np.zeros(X.shape[1], dtype=np.float64), 0.0
    m, v, mb, vb = np.zeros_like(w), np.zeros_like(w), 0.0, 0.0
    lr, b1, b2 = 0.05, 0.9, 0.999
    for t in range(1, iters + 1):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = X.T @ (p - y) / len(y) + l2 * w
        gb = float((p - y).mean())
        m = b1 * m + (1 - b1) * g; v = b2 * v + (1 - b2) * g * g
        mb = b1 * mb + (1 - b1) * gb; vb = b2 * vb + (1 - b2) * gb * gb
        w -= lr * (m / (1 - b1 ** t)) / (np.sqrt(v / (1 - b2 ** t)) + 1e-8)
        b -= lr * (mb / (1 - b1 ** t)) / (np.sqrt(vb / (1 - b2 ** t)) + 1e-8)
    return w, b


REGLES_MODELE = Regles(objectif_atr=3.0, stop_atr=2.0, duree=30, objectif_min=0.0030)
SEUIL_MOUVEMENT = 0.0030     # le modèle cherche les minutes suivies d'une hausse de +0,30 % (de quoi payer les frais)


def echantillon_modele(ctx: dict) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Exemples d'apprentissage pris dans l'entraînement (une minute sur 7)."""
    lo, hi = ts(PERIODES["entrainement"][0]), ts(PERIODES["entrainement"][1]) - 3600   # 1 h de marge avant la validation
    d = ctx["d"]
    c, o = d["c"], d["o"]
    n = len(c)
    # À apprendre : le plus haut atteint dans les 30 minutes après l'achat (ouverture de i+1)
    fwd = np.full(n, np.nan)
    hh = sliding_window_view(d["h"], 30).max(axis=1)          # hh[j] = max(h[j … j+29])
    fwd[:n - 31] = hh[1:n - 30] / o[1:n - 30] - 1
    sel = np.flatnonzero((d["t"] >= lo) & (d["t"] < hi) & np.isfinite(fwd))[::7]
    X, noms = _variables(ctx, sel)
    return X, (fwd[sel] > SEUIL_MOUVEMENT).astype(float), noms


def entrainer_modele(echantillons: list) -> dict:
    X = np.concatenate([e[0] for e in echantillons])
    y = np.concatenate([e[1] for e in echantillons])
    noms = echantillons[0][2]
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    w, b = _logistique((X - mu) / sd, y)
    p = 1 / (1 + np.exp(-(((X - mu) / sd) @ w + b)))
    seuil = float(np.quantile(p, 0.995))                         # les 0,5 % de minutes les plus prometteuses
    ordre = np.argsort(-np.abs(w))
    return {"w": w, "b": b, "mu": mu, "sd": sd, "seuil": seuil, "noms": noms, "taux_base": float(y.mean()),
            "principales": [(noms[i], float(w[i])) for i in ordre[:12]]}


NOM_MODELE = "Modèle IA (tous les indicateurs et patterns)"


def charger_modele(fichier: str = "modele.json") -> dict:
    """Un modèle appris par un banc d'essai (par défaut le dernier : data/micro/modele.json)."""
    m = json.loads((MICRO_DIR / fichier).read_text(encoding="utf-8"))
    for k in ("w", "mu", "sd"):
        m[k] = np.array(m[k])
    return m


def strategie_modele(modele: dict) -> Strategie:
    def signal(ctx):
        out = np.zeros(ctx["n"], dtype=bool)
        for i in range(0, ctx["n"], 400_000):                     # par morceaux (mémoire)
            idx = np.arange(i, min(i + 400_000, ctx["n"]))
            X, _ = _variables(ctx, idx)
            p = 1 / (1 + np.exp(-(((X - modele["mu"]) / modele["sd"]) @ modele["w"] + modele["b"])))
            out[idx] = p > modele["seuil"]
        return out
    return Strategie(NOM_MODELE, "Combinaison",
                     "Régression logistique qui pèse ensemble les 41 indicateurs et les 81 figures, "
                     "apprise uniquement sur 2021-2023.", signal, REGLES_MODELE)


# ------------------------------------------------------------------ banc d'essai

def _passe(bilans_paires: list[dict], rends: list[np.ndarray]) -> bool:
    tous = np.concatenate(rends) if rends else np.zeros(0)
    actives = [b for b in bilans_paires if b["operations"]]
    positives = sum(1 for b in actives if b["moyenne_pb"] > 0)
    return bool(len(tous) >= MIN_OPERATIONS and tous.mean() > 0 and positives * 2 > len(actives))


def evaluer(paires=PAIRES_BANC, verbose=True) -> dict:
    t0 = time.time()
    # 1er passage : exemples d'apprentissage du modèle IA (une paire à la fois, pour la mémoire)
    ech = []
    for p in paires:
        ctx = contexte(donnees.charger(p, PERIODES["entrainement"][0]))
        ech.append(echantillon_modele(ctx))
        del ctx
    modele = entrainer_modele(ech)
    del ech
    strategies = catalogue() + [strategie_modele(modele)]
    if verbose:
        print(f"Modèle IA appris ({time.time() - t0:.0f} s). {len(strategies)} stratégies × 2 façons d'entrer "
              f"× {len(SCENARIOS)} scénarios de frais.", flush=True)

    # 2e passage : toutes les opérations de toutes les stratégies, paire par paire
    trades, jours, ref, fin_donnees = {}, {}, {}, {}
    for p in paires:
        ctx = contexte(donnees.charger(p, PERIODES["entrainement"][0]))
        d = ctx["d"]
        fin_donnees[p] = datetime.fromtimestamp(d["t"][-1], timezone.utc).isoformat()
        for per, (a, b) in PERIODES.items():
            jours[per, p] = (min(ts(b), d["t"][-1]) - max(ts(a), d["t"][0])) / 86400
            m = (d["t"] >= ts(a)) & (d["t"] < ts(b))
            if m.any():
                ref.setdefault(per, {})[p] = float(d["c"][m][-1] / d["o"][m][0] - 1)
        for si, s in enumerate(strategies):
            sig = s.signal(ctx)
            for limite in (False, True):
                r = replace(s.regles, limite=limite)
                for per, (a, b) in PERIODES.items():
                    trades[si, limite, per, p] = simuler(d, ctx["f"]["atr"], sig, r, ts(a), ts(b))
        del ctx
        if verbose:
            print(f"  {p} simulée ({time.time() - t0:.0f} s)", flush=True)

    lignes = []
    for si, s in enumerate(strategies):
        for limite in (False, True):
            for fr in SCENARIOS:
                ligne = {"strategie": s.nom, "famille": s.famille, "description": s.description,
                         "entree": "à cours limité" if limite else "au marché", "frais": fr.nom,
                         "regles": asdict(replace(s.regles, limite=limite)), "periodes": {}}
                vivant = True
                for per in PERIODES:
                    if per == "examen" and not vivant:
                        break                                  # l'examen n'est joué que par les survivantes
                    res, rends = {}, []
                    for p in paires:
                        tr = trades[si, limite, per, p]
                        res[p] = bilan(tr, fr, None, jours[per, p])
                        rends.append(tr.rendements(fr))
                    ok = _passe(list(res.values()), rends)
                    tous = np.concatenate(rends)
                    ligne["periodes"][per] = {"paires": res, "reussi": ok, "operations": int(len(tous)),
                                              "moyenne_pb": float(tous.mean() * 1e4) if len(tous) else 0.0,
                                              "gagnantes": float((tous > 0).mean()) if len(tous) else 0.0}
                    vivant = vivant and ok
                lignes.append(ligne)

    out = {"date": datetime.now().isoformat(timespec="minutes"), "paires": list(paires),
           "periodes": {k: [str(a), str(b) if b else "aujourd'hui"] for k, (a, b) in PERIODES.items()},
           "fin_donnees": fin_donnees, "acheter_garder": ref,
           "modele": {"seuil": modele["seuil"], "taux_base": modele["taux_base"],
                      "principales": modele["principales"]},
           "nombre_tests": len(lignes), "lignes": lignes}
    MICRO_DIR.mkdir(parents=True, exist_ok=True)
    (MICRO_DIR / "resultats.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(MICRO_DIR / "modele.json", "w", encoding="utf-8") as fh:
        json.dump({k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in modele.items()}, fh)
    if verbose:
        print(f"Terminé en {time.time() - t0:.0f} s.", flush=True)
    return out


if __name__ == "__main__":
    evaluer(sys.argv[1:] or PAIRES_BANC)
