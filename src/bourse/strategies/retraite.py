"""« Robot Retraité » (risque faible) : un retraité qui veut que son épargne garde son pouvoir d'achat.

Il démarre avec un vrai portefeuille de particulier, recopié en argent fictif dans les mêmes proportions
(fichier PRIVÉ data/prive/portefeuille_retraite.yaml, jamais publié en ligne). Ensuite, la même IA que le Robot Éco
(gratuite, sur ce PC) le gère avec un caractère de retraité :
  - objectif : battre l'inflation française (Eurostat, mise à jour chaque mois) + un petit supplément ;
  - prudent et patient : une réflexion par semaine, positions gardées au moins un mois, pas de levier ni de pari
    à la baisse, pas de crypto, 20 % au plus sur un seul placement ;
  - il peut garder les placements de départ, en alléger, ou se diversifier dans les placements suivis par le site.

Présent uniquement (pas dans l'onglet « Passé ») : les placements de départ ne sont pas dans les archives du passé.
"""
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

from bourse import clock
from bourse.analysis.market_view import asset_view, download_closes
from bourse.config import PROJECT_ROOT
from bourse.data.prices import KNOWN_NAMES, fx_to_eur, quote
from bourse.execution.broker import BUY
from bourse.execution.paper_broker import FILLED

from .eco import EcoStrategy

log = logging.getLogger(__name__)

EUROSTAT = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
            "?geo=FR&coicop18=TOTAL&unit=RCH_A&lastTimePeriod=1")
INFLATION_CACHE = PROJECT_ROOT / "data" / "inflation_france.json"
_closes_cache: dict = {}

SYSTEM = """Tu gères le « Robot Retraité » : l'épargne d'un retraité (argent FICTIF, cours réels). Son but n'est pas \
de devenir riche mais que son argent garde son pouvoir d'achat : battre l'inflation, plus un petit supplément si \
possible. Une grosse perte lui ferait beaucoup plus de mal qu'un gros gain ne lui ferait de bien.

Tu reçois un dossier : inflation et objectif, climat du marché, ses placements (avec leurs chiffres), les autres \
placements possibles, les alertes, des avis d'économistes. Décide la répartition cible.

Règles : uniquement les tickers de la liste ; parts entre 0 et 0.2 ; somme des parts ≤ 0.99 (le reste en liquide). \
Pas de levier, pas de pari à la baisse, pas de crypto. Préfère des placements solides et diversifiés (pays, \
secteurs) ; garde une réserve sûre (monétaire, obligations) pour ne jamais devoir vendre au pire moment. \
Chaque achat ou vente coûte : repars de sa répartition actuelle, garde ce qui va bien, ne change que ce qui a une \
raison nette. Raisonne sur les faits du dossier, et explique tes choix en français, simplement, en phrases courtes."""


def inflation_france() -> tuple[float, str] | None:
    """Inflation française sur un an (indice des prix harmonisé, Eurostat) et son mois ; relue au plus une fois
    par jour, la dernière valeur connue sert si Eurostat ne répond pas."""
    cached = None
    if INFLATION_CACHE.exists():
        cached = json.loads(INFLATION_CACHE.read_text(encoding="utf-8"))
        if time.time() - cached["lu_le"] < 86400:
            return cached["taux"], cached["mois"]
    try:
        j = requests.get(EUROSTAT, timeout=20).json()
        month, idx = next(iter(j["dimension"]["time"]["category"]["index"].items()))
        rate = float(j["value"][str(idx)])
        INFLATION_CACHE.write_text(json.dumps({"taux": rate, "mois": month, "lu_le": time.time()}), encoding="utf-8")
        return rate, month
    except Exception as exc:     # réseau, format changé…
        log.warning("Inflation Eurostat illisible : %s", exc)
        return (cached["taux"], cached["mois"]) if cached else None


def load_start(params: dict) -> dict:
    return yaml.safe_load(Path(PROJECT_ROOT / params["portefeuille_depart"]).read_text(encoding="utf-8"))


def transfer(broker, start: dict) -> dict[str, int]:
    """Le portefeuille réel entre tel quel : chaque ligne est inscrite comme reçue (mêmes quantités, sans frais,
    au dernier cours connu à cet instant), les espèces restent des espèces. Le capital de départ du portefeuille
    devient exactement la valeur de l'ensemble à ce moment-là."""
    now = clock.now().isoformat()
    quantities = dict(start["quantites"])
    sub = start.get("monetaire_remplace")
    cash_extra = 0.0
    if sub:   # fonds sans cours en continu → même valeur en placement monétaire équivalent
        try:
            nav = float(yf_last_close(sub["fonds"]))
        except Exception:
            nav = float(sub["valeur_releve"])
        value = sub["parts"] * nav
        unit = quote(sub["par"]).price_eur
        quantities[sub["par"]] = int(value // unit)
        cash_extra = value - quantities[sub["par"]] * unit
    total = float(start["especes_eur"]) + cash_extra
    for ticker, qty in quantities.items():
        q = quote(ticker)
        fx = fx_to_eur(q.currency)
        total += qty * q.price * fx
        broker.conn.execute(
            "INSERT INTO orders (portfolio_id, created, ticker, side, quantity, status, reason, filled_at, filled_qty,"
            " fill_price, currency, fx_to_eur, fees_eur, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (broker.portfolio_id, now, ticker, BUY, qty, FILLED, "Transfert du portefeuille réel", now, qty,
             q.price, q.currency, fx, f"Relevé du {start['releve_du']} : titres reçus, sans frais"))
    broker.conn.execute("UPDATE portfolios SET initial_cash = ? WHERE id = ?", (round(total, 2), broker.portfolio_id))
    broker.conn.commit()
    return quantities


def yf_last_close(ticker: str) -> float:
    import yfinance as yf
    closes = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)["Close"].dropna()
    return float(closes.iloc[-1])


class RetraiteStrategy(EcoStrategy):
    name = "retraite"
    description = ("Un retraité qui veut garder son pouvoir d'achat : part d'un vrai portefeuille de particulier, "
                   "vise l'inflation + un petit supplément, prudent et patient (IA, une réflexion par semaine).")
    system_prompt = SYSTEM

    def __init__(self, params: dict, view=None):
        if clock.is_simulated():
            raise RuntimeError("Le Robot Retraité ne vit qu'au présent")
        super().__init__(params, view)
        self.start = load_start(params)
        KNOWN_NAMES.update(self.start.get("noms", {}))
        if view is not None:
            self.view = replace(view, assets=self.allowed_assets(view))

    def allowed_assets(self, view) -> dict:
        """Les placements de l'analyse sans levier ni crypto, plus ceux de son portefeuille de départ."""
        assets = {t: a for t, a in view.assets.items()
                  if a.leverage == 1 and a.family not in ("crypto", "matieres")}   # l'or reste permis
        own = [t for t in self.start["lignes"] if t not in assets]
        key = tuple(own)
        if key not in _closes_cache or time.time() - _closes_cache[key][0] > 3600:
            _closes_cache[key] = (time.time(), download_closes(own))
        closes = _closes_cache[key][1]
        for t in own:
            if t in closes:
                family = "monetaire" if t == "XEON.DE" else "actions"
                a = asset_view(t, {"nom": self.start["noms"].get(t, t), "famille": family, "levier": 1}, closes[t])
                if a:
                    assets[t] = a
        return assets

    # ----- dossier : l'objectif « battre l'inflation » en tête -----

    def dossier(self, broker, state: dict) -> str:
        lines = []
        infl = inflation_france()
        extra = self.params.get("objectif_au_dessus_inflation", 0.02) * 100
        started = datetime.fromisoformat(state.get("started", clock.now().isoformat()))
        years = max((clock.now() - started).days / 365.25, 1 / 365.25)
        perf = (broker.total_value() / broker.initial_cash() - 1) * 100
        if infl:
            rate, month = infl
            goal = ((1 + (rate + extra) / 100) ** years - 1) * 100
            lines += [f"Inflation en France sur un an (Eurostat, {month}) : {rate:.1f} %. Ton objectif : inflation "
                      f"+ {extra:.0f} points, soit environ {rate + extra:.1f} % par an.",
                      f"Depuis le départ ({started:%d/%m/%Y}, {years * 12:.1f} mois) : {perf:+.2f} % ; l'objectif sur "
                      f"cette durée est {goal:+.2f} %, l'inflation seule {((1 + rate / 100) ** years - 1) * 100:+.2f} %.",
                      ""]
        return "\n".join(lines) + super().dossier(broker, state)

    # ----- départ : le portefeuille réel transféré -----

    def on_start(self, broker, state):
        lines = transfer(broker, self.start)
        state["herites"] = list(self.start["lignes"])
        # première vraie réflexion après une semaine : on regarde d'abord vivre le portefeuille de départ
        state["derniere_reflexion"] = clock.now().isoformat()
        self.note(f"Démarrage : je reçois le portefeuille réel du relevé du {self.start['releve_du']} "
                  f"({len(lines)} lignes, mêmes quantités, sans frais : "
                  + f"{broker.total_value():,.0f}".replace(",", " ") + " €). Première réflexion dans une semaine.")

    def calm_targets(self, broker, targets: dict[str, float], alert: bool) -> dict[str, float]:
        """En plus des freins du Robot Éco : un retraité change par petites touches. Au plus `rotation_max` du
        portefeuille vendu par réflexion (sauf alerte FORTE) ; au-delà, tous les changements sont réduits d'autant."""
        out = super().calm_targets(broker, targets, alert)
        limit = self.params.get("rotation_max", 0.20)
        total = broker.total_value()
        current = {p["ticker"]: p["valeur_eur"] / total for p in broker.positions()}
        sold = sum(max(0.0, share - out.get(t, 0)) for t, share in current.items())
        if alert or sold <= limit:
            return out
        k = limit / sold
        self.think(f"Garde-fou : l'IA voulait vendre {sold:.0%} du portefeuille d'un coup ; un retraité avance "
                   f"par petites touches, je n'en fais que {limit:.0%} cette semaine.")
        tickers = set(out) | set(current)
        scaled = {t: round(current.get(t, 0) + (out.get(t, 0) - current.get(t, 0)) * k, 3) for t in tickers}
        return {t: w for t, w in scaled.items() if w >= 0.005}

    def last_buys(self, broker) -> dict[str, datetime]:
        """Les achats du départ ne comptent pas comme « récents » : ce sont des placements hérités."""
        buys = super().last_buys(broker)
        orders = broker.orders()
        first = min((datetime.fromisoformat(o["created"]) for o in orders), default=None)
        if first is None:
            return buys
        return {t: d for t, d in buys.items() if d > first + timedelta(days=3)}
