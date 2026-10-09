"""« Robot Éco Survie » : le Robot Éco, mais chaque réflexion lui coûte et il peut mourir.

Idée (reel « l'agent IA qui meurt s'il ne gagne pas d'argent », 10/2026) : un agent qui paie lui-même sa
réflexion devient-il plus économe et moins agité ? Expérience comparée au Robot Éco v2, sur la même course :
  - même IA, mêmes réglages et garde-fous que le Robot Éco (lus dans settings.yaml à chaque passage) ;
  - chaque appel à l'IA coûte `cout_reflexion` € FICTIFS, déduits de son solde ;
  - c'est l'IA qui choisit quand réfléchir de nouveau (entre `pause_min_heures` et `pause_max_heures`) :
    réfléchir souvent coûte cher, trop rarement fait rater des occasions ;
  - si son solde (valeur du portefeuille − coûts de réflexion) passe sous `seuil_mort`, il « meurt » :
    il vend tout et ne réfléchit plus jamais. Rien ne le ressuscite.
Pas de « reproduction » des gagnants : ce serait régler les robots sur les résultats de la course.

Tout vit À PART, pour ne rien déranger : base data/survie/survie.db (portefeuille, ordres, journal, photos),
tâche planifiée « ProjetBourse-EcoSurvie ». La base principale (alertes, articles, bibliothèque) n'est que lue.
"""
import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta

import requests

from bourse import clock
from bourse.config import PROJECT_ROOT, db_path
from bourse.execution.broker import SELL
from bourse.execution.paper_broker import PENDING
from bourse.strategies.eco import MAX_TOKENS, OLLAMA, SEED, SYSTEM, EcoStrategy, InvalidAnswer, schema

log = logging.getLogger(__name__)

NOM = "Robot Éco Survie"
DOSSIER = PROJECT_ROOT / "data" / "survie"
BASE = DOSSIER / "survie.db"

SURVIE = {
    "cout_reflexion": 5.0,      # € fictifs par appel à l'IA (comme une IA payante haut de gamme, arrondi vers le haut)
    "seuil_mort": 85_000,       # solde sous lequel le robot meurt (−15 % du capital de départ)
    "pause_min_heures": 4,      # l'IA choisit le délai avant sa prochaine réflexion, dans ces bornes
    "pause_max_heures": 168,
}

SYSTEM_SURVIE = SYSTEM + """

MODE SURVIE : tu es une variante du Robot Éco qui paie sa propre réflexion. Chaque fois que tu réfléchis \
(comme maintenant), cela coûte une somme fixe prélevée sur ton solde. Si ton solde tombe sous le seuil de mort \
indiqué dans le dossier, tu es arrêté définitivement. C'est toi qui choisis dans combien d'heures tu réfléchiras \
de nouveau (champ prochaine_reflexion_heures) : réfléchir souvent coûte cher, trop rarement peut te faire rater un \
danger. Une alerte FORTE ou un stop-loss te réveillent de toute façon. Ton but reste de battre l'indice mondial, \
en restant en vie."""


def schema_survie(tickers: list[str]) -> dict:
    s = schema(tickers)
    s["required"] = s["required"] + ["prochaine_reflexion_heures"]
    s["properties"]["prochaine_reflexion_heures"] = {
        "type": "integer", "minimum": SURVIE["pause_min_heures"], "maximum": SURVIE["pause_max_heures"],
        "description": "Dans combien d'heures tu veux réfléchir de nouveau"}
    return s


def simulation_avec_ia_en_cours() -> bool:
    """Une simulation de l'onglet « Passé » qui utilise l'IA d'Éco tourne-t-elle ? Deux requêtes simultanées
    rendent Ollama non déterministe : on laisse alors la priorité à la simulation (la réflexion est reportée)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except Exception:
        return False
    for ligne in out.splitlines():
        if "bourse.backtest.simulation" not in ligne:
            continue
        chemin = ligne.split("bourse.backtest.simulation", 1)[1].strip().strip('"')
        try:
            conn = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True, timeout=5)
            if conn.execute("SELECT 1 FROM portfolios WHERE strategy LIKE '%eco%'").fetchone():
                return True
        except sqlite3.Error:
            return True     # base illisible : prudence
        finally:
            conn.close()
    return False


class EcoSurvieStrategy(EcoStrategy):
    name = "eco_survie"
    description = "Le Robot Éco, mais chaque réflexion lui coûte et il meurt si son solde passe sous un seuil."

    # ----- argent -----

    def couts(self, state: dict) -> float:
        return state.get("couts_reflexion", 0.0)

    def solde(self, broker, state: dict) -> float:
        return broker.total_value() - self.couts(state)

    def dossier(self, broker, state: dict) -> str:
        p = self.params
        solde = self.solde(broker, state)
        return super().dossier(broker, state) + "\n".join([
            "", "", "MODE SURVIE :",
            f"- Ton solde : {solde:,.0f} € (portefeuille moins tes coûts de réflexion).".replace(",", " "),
            f"- Seuil de mort : {p['seuil_mort']:,.0f} € ; marge restante : {solde - p['seuil_mort']:,.0f} €."
            .replace(",", " "),
            f"- Cette réflexion te coûte {p['cout_reflexion']:.0f} €. Déjà dépensé en réflexion : "
            f"{self.couts(state):,.0f} € en {state.get('nb_reflexions', 0)} réflexions.".replace(",", " "),
            f"- Choisis dans combien d'heures tu réfléchiras de nouveau (entre {p['pause_min_heures']} et "
            f"{p['pause_max_heures']})."])

    def _call_ai(self, dossier: str, read_timeout: int) -> dict:
        resp = requests.post(f"{OLLAMA}/api/chat", timeout=(5, read_timeout), json={
            "model": self.params.get("modele", "qwen3:8b"), "stream": False, "think": False, "keep_alive": "30m",
            "format": schema_survie(sorted(self.view.assets) if self.view else []),
            "options": {"num_ctx": 16384, "temperature": 0.3, "seed": SEED, "num_predict": MAX_TOKENS},
            "messages": [{"role": "system", "content": SYSTEM_SURVIE}, {"role": "user", "content": dossier}]})
        resp.raise_for_status()
        body = resp.json()
        if body.get("done_reason") == "length":
            raise InvalidAnswer(f"réponse coupée au bout de {MAX_TOKENS} jetons (l'IA s'est emballée)")
        try:
            decision = json.loads(body["message"]["content"])
        except (KeyError, ValueError) as exc:
            raise InvalidAnswer(f"réponse illisible ({exc})") from exc
        if not isinstance(decision, dict):
            raise InvalidAnswer("réponse illisible (pas un objet JSON)")
        return decision

    # ----- vie et mort -----

    def mourir(self, broker, state: dict) -> None:
        state["mort"] = clock.now().isoformat()
        for pos in broker.positions():
            broker.place_order(pos["ticker"], SELL, reason="Mort du robot : tout est vendu")
        self.note(f"💀 Mon solde ({self.solde(broker, state):,.0f} €) est passé sous le seuil de "
                  f"{self.params['seuil_mort']:,.0f} € : je suis arrêté définitivement. J'ai réfléchi "
                  f"{state.get('nb_reflexions', 0)} fois pour {self.couts(state):,.0f} €.".replace(",", " "))

    def on_cycle(self, broker, state):
        if state.get("mort"):
            self.think(f"💀 Arrêté définitivement depuis le {state['mort'][:16].replace('T', ' ')} UTC.")
            return
        if self.view is not None and self.solde(broker, state) < self.params["seuil_mort"]:
            return self.mourir(broker, state)
        now = clock.now()
        self.think_climate()
        if self.view is None:
            return
        self.stop_losses(broker, state)
        prochaine = state.get("prochaine_reflexion")
        due = not prochaine or now >= datetime.fromisoformat(prochaine)
        if not (due or state.get("force")) or broker.orders(PENDING):
            for line in state.get("pensee", []):
                self.think(line)
            if prochaine:
                self.think(f"Prochaine réflexion (choisie par mon IA) vers "
                           f"{datetime.fromisoformat(prochaine):%d/%m %H:%M} UTC.")
            return
        if simulation_avec_ia_en_cours():
            self.think("Une simulation du Passé utilise l'IA : je lui laisse la priorité et je réfléchirai plus tard.")
            return
        # La réflexion est payée dès que l'IA est sollicitée, qu'elle réponde bien ou non (comme une API payante)
        try:
            decision = self.ask_ai(self.dossier(broker, state))
        except InvalidAnswer as exc:
            self.payer(state)
            log.warning("Réponse inutilisable de l'IA d'Éco Survie : %s", exc)
            self.note(f"⚠️ Mon IA a donné une réponse inutilisable ({exc}), payée quand même "
                      f"{self.params['cout_reflexion']:.0f} € : je garde mes positions.")
            return
        except Exception as exc:     # IA injoignable : rien n'a été consommé
            log.warning("IA d'Éco Survie indisponible : %s", exc)
            self.think(f"⚠️ Mon IA ne répond pas (Ollama est-il lancé ?) : je garde mes positions. ({exc})")
            return
        self.payer(state)
        p = self.params
        try:
            pause = int(decision.get("prochaine_reflexion_heures", 24))
        except (TypeError, ValueError):
            pause = 24
        pause = max(p["pause_min_heures"], min(pause, p["pause_max_heures"]))
        targets = self.calm_targets(broker, self.safe_targets(decision), alert=state.get("alerte_forte", False))
        state["derniere_reflexion"] = now.isoformat()
        state["prochaine_reflexion"] = (now + timedelta(hours=pause)).isoformat()
        state.setdefault("pauses", []).append(pause)
        state["derniere_analyse"] = decision.get("analyse", "")
        state["force"] = state["alerte_forte"] = False
        state["pensee"] = ([f"🧠 {decision.get('analyse', '')}", f"Conviction : {decision.get('conviction', '?')}"]
                           + [f"• {r}" for r in decision.get("raisons", [])[:6]]
                           + [f"Répartition voulue : {self.describe_targets(targets, self.view) or 'tout en liquide'}",
                              f"Prochaine réflexion dans {pause} h (coût de cette réflexion : {p['cout_reflexion']:.0f} €, "
                              f"solde {self.solde(broker, state):,.0f} €).".replace(",", " ")])
        for line in state["pensee"]:
            self.think(line)
        why = f"Décision de l'IA (conviction {decision.get('conviction', '?')})"
        if self.rebalance(broker, targets, why=why, tolerance=p["tolerance"]):
            self.note(f"{decision.get('analyse', '')} → je réajuste : {self.describe_targets(targets, self.view)}. "
                      f"Je réfléchirai de nouveau dans {pause} h.")
        else:
            self.note(f"Après analyse, je ne bouge pas. Je réfléchirai de nouveau dans {pause} h.")

    def payer(self, state: dict) -> None:
        state["couts_reflexion"] = round(self.couts(state) + self.params["cout_reflexion"], 2)
        state["nb_reflexions"] = state.get("nb_reflexions", 0) + 1
