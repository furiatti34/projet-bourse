"""« Robot Audacieux » (risque élevé) : effet de levier, dans un sens ou dans l'autre.

Il lit l'analyse du marché et choisit une posture :
  - ATTAQUE (climat bon et Nasdaq en tendance haussière) : 100 % Nasdaq-100 ×2 ;
  - NEUTRE : 50 % Nasdaq-100 ×2 + 50 % sur le marché qui a le plus d'élan en ce moment ;
  - BAISSE (climat mauvais ET tendance mondiale cassée) : 60 % pari à la baisse ×2, 40 % sans risque.
Alerte de crise TOUTE FRAÎCHE : pari à la baisse ×2 à 100 % pendant quelques jours, avec stop-loss.
Il ne change de posture que si le signal se confirme deux passages de suite (pas de coup de tête).
"""
from datetime import datetime, timedelta

from bourse import clock
from bourse.data.prices import quote
from bourse.execution.paper_broker import PENDING

from .base import CASH_BUFFER, Strategy

ATTACK, NEUTRAL, BEAR, CRISIS = "attaque", "neutre", "baisse", "pari de crise"
NASDAQ = "SXRV.DE"


class AudacieuxStrategy(Strategy):
    name = "audacieux"
    description = ("Levier ×2 sur le Nasdaq par beau temps, mélange avec le marché le plus dynamique sinon, "
                   "pari à la baisse ×2 quand le climat tourne ou sur alerte de crise fraîche.")

    def wanted_mode(self) -> str:
        p, view = self.params, self.view
        risk = view.risk_score
        nasdaq, world = view.asset(NASDAQ), view.factor("Tendance mondiale")
        if risk <= p["seuil_defense"] and world and world.value < 0:
            return BEAR
        if risk >= p["seuil_attaque"] and (nasdaq is None or nasdaq.above_ma50):
            return ATTACK
        return NEUTRAL

    def targets(self, mode: str) -> dict[str, float]:
        p, full = self.params, 1 - CASH_BUFFER
        if mode == CRISIS:
            return {p["titre_baisse"]: full}
        if mode == BEAR:
            return {p["titre_baisse"]: 0.60, p["titre_monetaire"]: round(full - 0.60, 3)}
        if mode == ATTACK:
            return {p["titre_attaque"]: full}
        movers = self.view.pick(families=("actions",), leverage=(1,))
        best = max(movers, key=lambda a: a.momentum)
        self.think(f"Marché le plus dynamique en ce moment : {best.label} (élan {best.momentum:+.1f}).")
        return {p["titre_attaque"]: 0.50, best.ticker: round(full - 0.50, 3)}

    def on_start(self, broker, state):
        state["mode"] = None
        self.note("Démarrage : je choisis ma posture d'après le climat du marché.")

    def on_alert(self, alert, broker, state):
        now = clock.now()
        what = self.describe(alert, now)
        if not alert.severe_terms:
            self.note(f"J'ignore l'{what} : pas de vocabulaire de crise.")
            return
        if self.freshness(alert, now) == 0:
            self.note(f"J'ignore l'{what} : plus de {self.params['delai_max_heures']} h, "
                      "trop tard pour un pari rapide.")
            return
        until = now + timedelta(days=self.params["duree_pari_jours"])
        state["crisis_until"] = until.isoformat()
        self.note(f"Suite à l'{what}, je parie à 100 % sur la BAISSE du marché américain (×2) "
                  f"jusqu'au {until:%d/%m %H:%M}, stop-loss à -{self.params['stop_perte']:.0%}.")

    def crisis_active(self, broker, state) -> bool:
        until = state.get("crisis_until")
        if not until:
            return False
        if clock.now() >= datetime.fromisoformat(until):
            self.note("Fin de mon pari de crise : je reprends ma posture normale.")
            state["crisis_until"] = None
            return False
        held = broker.holdings().get(self.params["titre_baisse"])
        if held and state.get("mode") == CRISIS:
            change = quote(held.ticker).price_eur * held.quantity / held.cost_eur - 1
            if change <= -self.params["stop_perte"]:
                self.note(f"Stop-loss : mon pari de crise perd {change:.1%}, je l'abandonne.")
                state["crisis_until"] = None
                return False
            self.think(f"Pari de crise en cours ({change:+.1%}).")
        self.think(f"Pari de crise jusqu'au {datetime.fromisoformat(until):%d/%m %H:%M}.")
        return True

    def choose_mode(self, state) -> str:
        current, wanted = state.get("mode"), self.wanted_mode()
        if current in (None, CRISIS) or wanted == current:
            state["candidate"] = None
            return wanted
        # Changement de posture : il faut voir le même signal deux passages de suite
        if state.get("candidate") == wanted:
            state["candidate"] = None
            return wanted
        state["candidate"] = wanted
        self.think(f"Le climat pousse vers la posture « {wanted} », j'attends confirmation au prochain passage.")
        return current

    def on_cycle(self, broker, state):
        self.think_climate()
        crisis = self.crisis_active(broker, state)
        if self.view is None and not crisis:
            return
        mode = CRISIS if crisis else self.choose_mode(state)
        self.think(f"Posture : {mode}.")
        targets = self.targets(mode)
        changed = mode != state.get("mode")
        headline = self.view.headline() if self.view else "Alerte de crise"
        if self.rebalance(broker, targets, why=f"{headline} → posture « {mode} »",
                          tolerance=0.02 if changed else self.params["tolerance"]):
            self.note(f"{headline}. Posture « {mode} » : {self.describe_targets(targets, self.view)}.")
            state["mode"] = mode
        elif changed and not broker.orders(PENDING):
            state["mode"] = mode
