"""Simulation des ordres, minute par minute, avec des règles volontairement PESSIMISTES.

Décision à la clôture de la minute i  →  exécution au plus tôt pendant la minute i+1.

Deux façons d'entrer :
  - « au marché » : achat au cours d'ouverture de la minute i+1, plus le glissement (on paie l'écart
    achat/vente et un peu plus). Frais « preneur » (taker).
  - « à cours limité » : ordre d'achat posé au cours de clôture de la minute i, valable `attente`
    minutes. Considéré exécuté seulement si le prix est descendu STRICTEMENT en dessous (sinon on
    n'est pas sûr d'avoir été servi : d'autres ordres attendaient avant le nôtre). Frais « faiseur » (maker).

Sortie (la première qui arrive) :
  - objectif (ordre de vente à cours limité) : exécuté si le plus haut dépasse STRICTEMENT l'objectif,
    frais maker ;
  - stop-loss (ordre au marché) : dès que le plus bas touche le stop ; au stop moins le glissement,
    ou à l'ouverture si le prix a sauté par-dessus ; frais taker ;
  - durée maximale : vente au marché à la clôture, frais taker.
Si l'objectif et le stop sont touchés dans la même minute, on ne sait pas lequel est venu en premier :
on compte le STOP (le pire cas). Pendant la minute d'entrée, seul le stop est vérifié.

Une seule position à la fois, tout le capital engagé (le cas des 10 € : pas de levier, pas de vente à découvert).

Glissement : mesuré minute par minute dans les données (voir couts.py), au moins `Frais.glissement`.
Ordre à cours limité : posé au prix acheteur estimé (clôture moins la moitié de l'écart), pour rester un
vrai ordre « faiseur » même si la dernière transaction s'est faite au prix vendeur.
"""
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

OBJECTIF, STOP, DUREE = 1, 2, 3


@dataclass(frozen=True)
class Frais:
    nom: str
    maker: float        # en fraction : 0.001 = 0,1 %
    taker: float
    glissement: float   # glissement MINIMAL d'un ordre au marché (le mesuré s'applique s'il est plus grand)


SCENARIOS = [
    Frais("Binance standard (0,10 %)", 0.0010, 0.0010, 0.0002),
    Frais("Binance avec réduction BNB (0,075 %)", 0.00075, 0.00075, 0.0002),
    Frais("Promotion « 0 frais faiseur »", 0.0, 0.0010, 0.0002),
    Frais("Sans aucun frais (théorique)", 0.0, 0.0, 0.0002),
]


@dataclass
class Regles:
    objectif_atr: float = 1.5      # objectif = entrée + 1,5 × ATR
    stop_atr: float = 1.0          # stop = entrée − 1 × ATR
    duree: int = 30                # minutes maximum
    limite: bool = False           # entrée à cours limité (sinon au marché)
    attente: int = 3               # validité de l'ordre limité, en minutes
    objectif_min: float = 0.0      # objectif au moins à +x (fraction) — utile pour couvrir les frais


@dataclass
class Trades:
    """Résultats bruts : un élément par opération réellement prise."""
    signal: np.ndarray      # minute du signal
    entree: np.ndarray      # minute d'exécution de l'achat
    sortie: np.ndarray      # minute de la vente
    px_in: np.ndarray       # prix d'achat (avant frais)
    px_out: np.ndarray      # prix de vente (avant frais)
    motif: np.ndarray       # OBJECTIF / STOP / DUREE
    limite: bool
    ambigu: np.ndarray | None = None    # objectif ET stop touchés dans la même minute (compté en stop)
    gl_in: np.ndarray | None = None     # glissement mesuré à la minute d'achat
    gl_out: np.ndarray | None = None    # … et à la minute de vente

    def __len__(self):
        return len(self.entree)

    def _gl(self, x, fr):
        return np.maximum(fr.glissement, x if x is not None else 0.0)

    def prix_nets(self, fr: Frais) -> tuple[np.ndarray, np.ndarray]:
        """(prix payé par unité, frais compris ; prix reçu par unité, frais déduits)."""
        px_in = self.px_in * (1 if self.limite else 1 + self._gl(self.gl_in, fr))
        fee_in = fr.maker if self.limite else fr.taker
        fee_out = np.where(self.motif == OBJECTIF, fr.maker, fr.taker)
        px_out = np.where(self.motif == OBJECTIF, self.px_out, self.px_out * (1 - self._gl(self.gl_out, fr)))
        return px_in * (1 + fee_in), px_out * (1 - fee_out)

    def rendements(self, fr: Frais) -> np.ndarray:
        """Rendement net de chaque opération (0.001 = +0,1 %)."""
        if not len(self):
            return np.zeros(0)
        paye, recu = self.prix_nets(fr)
        return recu / paye - 1


def _premier(mask: np.ndarray) -> np.ndarray:
    """Pour chaque ligne, position du premier vrai (ou la largeur si aucun)."""
    any_ = mask.any(axis=1)
    return np.where(any_, mask.argmax(axis=1), mask.shape[1])


def simuler(d: dict[str, np.ndarray], atr: np.ndarray, signaux: np.ndarray, r: Regles,
            debut: float = -np.inf, fin: float = np.inf, gliss: np.ndarray | None = None,
            chacun: bool = False) -> Trades:
    """Opérations déclenchées par `signaux` (vrai/faux par minute) entre les heures `debut` et `fin`.
    `gliss` : glissement mesuré par minute (couts.glissement) ; absent = le minimum du scénario de frais.
    `chacun` : chaque signal donne son opération, même en chevauchant les autres (pour étiqueter les
    exemples d'apprentissage du modèle IA : « si j'achetais ici, que se passerait-il ? »)."""
    o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
    if gliss is None:
        gliss = np.zeros(len(c))
    n, H, W = len(c), r.duree, r.attente
    cand = np.flatnonzero(signaux & (t >= debut) & (t < fin) & np.isfinite(atr) & (atr > 0))
    cand = cand[cand < n - H - W - 2]
    # Une minute manquante (bourse arrêtée, panne) juste après le signal : on ne trade pas
    cand = cand[t[cand + 1] - t[cand] == 60]
    if not len(cand):
        e = np.zeros(0, dtype=int)
        z = np.zeros(0)
        return Trades(e, e, e, z, z, e, r.limite, np.zeros(0, dtype=bool), z, z)

    # --- entrée ---
    if r.limite:
        lim = c[cand] * (1 - gliss[cand])            # au prix acheteur estimé (clôture − ½ écart)
        fen = sliding_window_view(l, W)[cand + 1]                     # minutes i+1 … i+W
        k = _premier(fen < lim[:, None])
        ok = k < W
        # Un ordre non servi occupe quand même le robot jusqu'à son annulation (un seul ordre à la fois)
        rates = cand[~ok]
        cand, lim, k = cand[ok], lim[ok], k[ok]
        ent = cand + 1 + k
        px_in = lim
    else:
        rates = np.zeros(0, dtype=int)
        ent = cand + 1
        px_in = o[ent]
    base = px_in
    a = atr[cand]
    tp = base + np.maximum(r.objectif_atr * a, r.objectif_min * base)
    sl = base - r.stop_atr * a

    # --- sortie : on regarde les H minutes à partir de la minute d'entrée ---
    hw = sliding_window_view(h, H)[ent]
    lw = sliding_window_view(l, H)[ent]
    ow = sliding_window_view(o, H)[ent]
    touche_tp = hw > tp[:, None]
    touche_tp[:, 0] = False                         # minute d'entrée : on ignore l'objectif (pessimiste)
    touche_sl = lw <= sl[:, None]
    k_tp, k_sl = _premier(touche_tp), _premier(touche_sl)
    stop_first = k_sl <= k_tp                        # égalité → le stop (pire cas)
    ambigu = (k_sl == k_tp) & (k_sl < H)
    motif = np.where((k_sl == H) & (k_tp == H), DUREE, np.where(stop_first, STOP, OBJECTIF))
    k_out = np.where(motif == DUREE, H - 1, np.where(stop_first, k_sl, k_tp))
    sortie = ent + k_out
    rows = np.arange(len(ent))
    gap = ow[rows, k_out]
    px_out = np.where(motif == OBJECTIF, tp,
                      np.where(motif == STOP, np.where((k_out > 0) & (gap < sl), gap, sl), c[sortie]))

    # --- une seule position à la fois : on saute les signaux pendant qu'on est déjà en position ---
    # (ordres limités non servis compris : ils bloquent jusqu'à leur annulation, minute i+W)
    tous = np.r_[cand, rates]
    fins = np.r_[sortie, rates + W]
    servi = np.r_[np.ones(len(cand), dtype=bool), np.zeros(len(rates), dtype=bool)]
    ordre = np.argsort(tous, kind="stable")
    garde = np.zeros(len(ent), dtype=bool)
    libre = -1
    for i in ordre:
        if tous[i] > libre:
            libre = fins[i]
            if servi[i]:
                garde[i] = True
    g = np.ones(len(ent), dtype=bool) if chacun else garde
    return Trades(cand[g], ent[g], sortie[g], px_in[g], px_out[g], motif[g], r.limite, ambigu[g],
                  gliss[ent[g]], gliss[sortie[g]])


def bilan(tr: Trades, fr: Frais, jours: float, capital: float = 10.0, pas: float = 0.0,
          minimum: float = 0.0) -> dict:
    """Chiffres clés d'une série d'opérations pour un scénario de frais.

    Deux calculs :
      - par opération (en %), sans se soucier de la taille du compte ;
      - l'argent réel d'un compte de `capital` €, qui ne peut acheter qu'un multiple de `pas` (règle de
        Binance) et pas moins de `minimum` € : c'est ce que les 10 € seraient vraiment devenus.
    """
    vide = {"operations": 0, "par_jour": 0.0, "gagnantes": 0.0, "moyenne_pb": 0.0, "brut_pb": 0.0,
            "gain_moyen_pb": 0.0, "perte_moyenne_pb": 0.0, "facteur_profit": 0.0, "facteur_profit_brut": 0.0,
            "duree_moy_min": 0.0, "exposition": 0.0, "ambigus": 0.0, "total": 0.0, "total_brut": 0.0,
            "par_an": 0.0, "pire_baisse": 0.0, "capital_final": capital, "couts_eur": 0.0,
            "operations_impossibles": 0, "motifs": {}}
    n = len(tr)
    if n == 0:
        return vide
    r = tr.rendements(fr)
    brut = tr.px_out / tr.px_in - 1                      # avant frais ET avant glissement
    duree = tr.sortie - tr.entree + 1
    gains, pertes = r[r > 0], r[r < 0]
    gb, pb_ = brut[brut > 0].sum(), -brut[brut < 0].sum()

    # --- le compte de `capital` € ---
    paye, recu = tr.prix_nets(fr)
    cash, courbe, couts, impossibles = capital, [], 0.0, 0
    for i in range(n):
        q = cash / paye[i]
        if pas:
            q = np.floor(q / pas + 1e-9) * pas
        if q <= 0 or q * tr.px_in[i] < minimum:
            impossibles += 1                             # compte trop petit pour passer l'ordre
            courbe.append(cash)
            continue
        cash += q * (recu[i] - paye[i])
        couts += q * ((tr.px_out[i] - recu[i]) + (paye[i] - tr.px_in[i]))   # frais + glissement
        courbe.append(cash)
    courbe = np.array(courbe)
    pic = np.maximum.accumulate(np.r_[capital, courbe])[1:]
    total = courbe[-1] / capital - 1
    return {
        "operations": n,
        "par_jour": n / max(jours, 1),
        "gagnantes": float((r > 0).mean()),
        "moyenne_pb": float(r.mean() * 1e4),            # espérance par opération, après frais (1 pb = 0,01 %)
        "brut_pb": float(brut.mean() * 1e4),            # … avant frais et glissement
        "gain_moyen_pb": float(gains.mean() * 1e4) if len(gains) else 0.0,
        "perte_moyenne_pb": float(pertes.mean() * 1e4) if len(pertes) else 0.0,
        "facteur_profit": float(gains.sum() / -pertes.sum()) if len(pertes) else float("inf"),
        "facteur_profit_brut": float(gb / pb_) if pb_ else float("inf"),
        "duree_moy_min": float(duree.mean()),
        "exposition": float(duree.sum() / max(jours * 1440, 1)),    # part du temps passée en position
        "ambigus": float(tr.ambigu.mean()) if tr.ambigu is not None else 0.0,
        "motifs": {nom: float((tr.motif == k).mean()) for k, nom in ((OBJECTIF, "objectif"), (STOP, "stop"),
                                                                     (DUREE, "duree"))},
        "total": float(total),
        "total_brut": float(np.prod(1 + brut) - 1),
        "par_an": float((1 + total) ** (365 / max(jours, 1)) - 1) if total > -1 else -1.0,
        "pire_baisse": float((courbe / pic - 1).min()),
        "capital_final": float(courbe[-1]),
        "couts_eur": float(couts),
        "operations_impossibles": impossibles,
    }
