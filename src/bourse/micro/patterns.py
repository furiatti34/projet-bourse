"""Figures de chandeliers (« patterns ») et figures chartistes.

Chaque fonction reçoit des bougies (o, h, l, c) d'une durée quelconque et renvoie un tableau vrai/faux :
vrai à la bougie où la figure est COMPLÈTE (on ne la connaît qu'à sa clôture).
`multi_echelle` fabrique des bougies de 5 ou 15 minutes à partir de celles d'une minute, et replace
le signal sur la minute qui ferme la grande bougie (jamais avant).

Sources des définitions : les figures classiques de Steve Nison (« Japanese Candlestick Charting
Techniques »), Thomas Bulkowski (« Encyclopedia of Candlestick Charts »), et les études sur les cryptos
citées dans data/micro/sources.md.
"""
import numpy as np

from .indicateurs import rmax, rmin, shift, sma


def _parts(o, h, l, c):
    corps = np.abs(c - o)
    etendue = np.maximum(h - l, 1e-12)
    haute = h - np.maximum(o, c)
    basse = np.minimum(o, c) - l
    return corps, etendue, haute, basse


def _p(x, k=1):
    return shift(x, k)


def _pb(x, k=1):
    """Décale un tableau vrai/faux de k bougies (faux au début)."""
    return np.nan_to_num(shift(x.astype(float), k)).astype(bool)


def figures(o, h, l, c) -> dict[str, np.ndarray]:
    corps, et, haute, basse = _parts(o, h, l, c)
    vert, rouge = c > o, c < o
    corps_moy = sma(corps, 20)
    grand = corps > 1.3 * corps_moy
    petit = corps < 0.5 * corps_moy
    baisse_avant = _p(c, 1) < _p(c, 6)          # la tendance des 5 bougies précédentes était baissière
    hausse_avant = _p(c, 1) > _p(c, 6)
    po, ph, pl, pc = _p(o), _p(h), _p(l), _p(c)
    pvert, prouge = pc > po, pc < po
    pcorps = np.abs(pc - po)
    f: dict[str, np.ndarray] = {}

    # --- une bougie ---
    f["marteau"] = baisse_avant & (basse >= 2 * corps) & (haute <= 0.3 * np.maximum(corps, 1e-12) + 0.1 * et) & (corps > 0)
    f["marteau_inverse"] = baisse_avant & (haute >= 2 * corps) & (basse <= 0.1 * et) & (corps > 0)
    f["etoile_filante"] = hausse_avant & (haute >= 2 * corps) & (basse <= 0.1 * et) & (corps > 0)
    f["pendu"] = hausse_avant & (basse >= 2 * corps) & (haute <= 0.1 * et) & (corps > 0)
    f["doji"] = corps <= 0.1 * et
    f["doji_libellule"] = f["doji"] & (basse >= 0.7 * et) & baisse_avant
    f["marubozu_vert"] = vert & grand & (haute + basse <= 0.1 * et)
    f["marubozu_rouge"] = rouge & grand & (haute + basse <= 0.1 * et)
    f["pin_bar_haussiere"] = (basse >= 0.66 * et) & (np.minimum(o, c) > l + 0.6 * et) & (l <= rmin(l, 10))

    # --- deux bougies ---
    f["avalement_haussier"] = baisse_avant & prouge & vert & (o <= pc) & (c >= po) & (corps > pcorps)
    f["avalement_baissier"] = hausse_avant & pvert & rouge & (o >= pc) & (c <= po) & (corps > pcorps)
    f["harami_haussier"] = baisse_avant & prouge & (pcorps > 1.3 * corps_moy) & vert & (o > pc) & (c < po)
    f["harami_baissier"] = hausse_avant & pvert & (pcorps > 1.3 * corps_moy) & rouge & (o < pc) & (c > po)
    f["penetrante"] = baisse_avant & prouge & (pcorps > corps_moy) & vert & (o < pl) & (c > (po + pc) / 2) & (c < po)
    f["couverture_nuage_noir"] = hausse_avant & pvert & (pcorps > corps_moy) & rouge & (o > ph) & (c < (po + pc) / 2) & (c > po)
    tol = 0.05 * et
    f["pinces_bas"] = baisse_avant & prouge & vert & (np.abs(l - pl) <= tol)
    f["pinces_haut"] = hausse_avant & pvert & rouge & (np.abs(h - ph) <= tol)
    interieure = (h <= ph) & (l >= pl)
    f["bougie_interieure"] = interieure
    f["bougie_englobante"] = (h > ph) & (l < pl)
    # Cassure de bougie intérieure : la bougie d'avant était « intérieure », celle-ci ferme au-dessus
    f["cassure_interieure"] = _pb(interieure) & (c > ph)

    # --- trois bougies ---
    o2, c2, l2, h2 = _p(o, 2), _p(c, 2), _p(l, 2), _p(h, 2)
    corps2 = np.abs(c2 - o2)
    f["etoile_du_matin"] = (_p(c, 2) < _p(c, 7)) & (c2 < o2) & (corps2 > corps_moy) & (pcorps < 0.5 * corps_moy) \
        & (np.maximum(po, pc) < c2 + 0.1 * corps2) & vert & (c > (o2 + c2) / 2)
    f["etoile_du_soir"] = (_p(c, 2) > _p(c, 7)) & (c2 > o2) & (corps2 > corps_moy) & (pcorps < 0.5 * corps_moy) \
        & (np.minimum(po, pc) > c2 - 0.1 * corps2) & rouge & (c < (o2 + c2) / 2)
    f["trois_soldats"] = vert & pvert & (c2 > o2) & (c > pc) & (pc > c2) & (o > po) & (po > o2) \
        & (corps > 0.6 * corps_moy) & (pcorps > 0.6 * corps_moy) & (corps2 > 0.6 * corps_moy) & (haute < 0.3 * et)
    f["trois_corbeaux"] = rouge & prouge & (c2 < o2) & (c < pc) & (pc < c2) & (o < po) & (po < o2) \
        & (corps > 0.6 * corps_moy) & (pcorps > 0.6 * corps_moy) & (corps2 > 0.6 * corps_moy) & (basse < 0.3 * et)
    # Hikkake haussier (Dan Chesler) : bougie intérieure, puis fausse cassure vers le bas,
    # puis clôture au-dessus du haut de la bougie intérieure dans les 3 bougies suivantes.
    hik = np.zeros(len(c), dtype=bool)
    inside = (h <= _p(h)) & (l >= _p(l))
    fausse = _pb(inside) & (h < _p(h)) & (l < _p(l))   # bougie i : fausse cassure
    for k in (1, 2, 3):
        # fausse cassure à i-k, bougie intérieure à i-k-1 : on casse son haut maintenant
        hk = shift(h, k + 1)
        deja = np.zeros(len(c), dtype=bool)
        for j in range(1, k):
            deja |= shift(c, j) > shift(h, k + 1)
        hik |= _pb(fausse, k) & (c > hk) & ~deja
    f["hikkake_haussier"] = hik
    return {k: np.nan_to_num(v.astype(float)).astype(bool) for k, v in f.items()}


def figures_chartistes(o, h, l, c, atr) -> dict[str, np.ndarray]:
    """Figures sur plusieurs dizaines de bougies."""
    f: dict[str, np.ndarray] = {}
    haut20, bas20 = shift(rmax(h, 20)), shift(rmin(l, 20))
    # Drapeau haussier : forte hausse (≥ 3 ATR en 10 bougies), puis 5 bougies serrées (≤ 1,2 ATR), puis cassure
    impulsion = (shift(c, 5) - shift(c, 15)) >= 3 * shift(atr, 5)
    serre = (shift(rmax(h, 5)) - shift(rmin(l, 5))) <= 1.2 * atr
    f["drapeau_haussier"] = impulsion & serre & (c > shift(rmax(h, 5)))
    # Double creux : deux plus bas proches (≤ 0,5 ATR d'écart), séparés par un rebond ≥ 1,5 ATR,
    # puis clôture au-dessus du sommet du rebond (la « ligne de cou »).
    n = len(c)
    dc = np.zeros(n, dtype=bool)
    bas_rec = rmin(l, 10)
    for gap in (10, 20, 30):
        b1 = shift(bas_rec, gap)                       # creux n°1 (il y a `gap` bougies)
        b2 = bas_rec                                    # creux n°2 (les 10 dernières)
        cou = shift(rmax(h, gap))                       # sommet entre les deux
        dc |= (np.abs(b2 - b1) <= 0.5 * atr) & (cou - np.maximum(b1, b2) >= 1.5 * atr) \
            & (c > cou) & (shift(c) <= cou)
    f["double_creux"] = dc
    # Rebond sur support : touche le plus bas des 240 dernières bougies puis referme au-dessus
    bas240 = shift(rmin(l, 240))
    f["rebond_support"] = (l <= bas240 * 1.0002) & (c > bas240 + 0.5 * atr) & (c > o)
    # Cassure de range : sortie par le haut d'un canal de 20 bougies après compression
    largeur = haut20 - bas20
    f["cassure_range"] = (c > haut20) & (largeur < 4 * atr)
    # Triangle ascendant (simplifié) : sommets plats, creux montants, puis cassure du plafond
    plafond = shift(rmax(h, 30))
    creux_montants = rmin(l, 10) > shift(rmin(l, 10), 10)
    creux_montants &= shift(rmin(l, 10), 10) > shift(rmin(l, 10), 20)
    f["triangle_ascendant"] = creux_montants & (np.abs(shift(rmax(h, 15)) - shift(rmax(h, 15), 15)) <= 0.5 * atr) \
        & (c > plafond)
    return {k: np.nan_to_num(v.astype(float)).astype(bool) for k, v in f.items()}


def multi_echelle(d: dict[str, np.ndarray], k: int):
    """Bougies de k minutes. Renvoie (bougies, index de la minute qui ferme chaque bougie)."""
    minute = (d["t"] // 60).astype(np.int64)
    seau = minute // k
    fin = (minute + 1) % k == 0                   # dernière minute du seau
    # Une bougie n'est retenue que si sa dernière minute existe (sinon on ne saurait pas qu'elle est finie)
    debut_seau = np.r_[True, seau[1:] != seau[:-1]]
    ids = np.cumsum(debut_seau) - 1
    ng = ids[-1] + 1 if len(ids) else 0
    o = np.full(ng, np.nan); h = np.full(ng, -np.inf); l = np.full(ng, np.inf); c = np.full(ng, np.nan)
    o[ids[debut_seau]] = d["o"][debut_seau]
    np.maximum.at(h, ids, d["h"]); np.minimum.at(l, ids, d["l"])
    c[ids] = d["c"]                               # la dernière écriture gagne = clôture du seau
    ferme = np.full(ng, -1)
    ferme[ids[fin]] = np.flatnonzero(fin)
    ok = ferme >= 0
    return {"o": o[ok], "h": h[ok], "l": l[ok], "c": c[ok]}, ferme[ok]


def replacer(sig: np.ndarray, ferme: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, dtype=bool)
    out[ferme[sig]] = True
    return out
