"""« Robot Prudent » (risque faible) : il ne cherche pas à gagner gros, il cherche à ne pas perdre gros.

Chaque jour, il lit l'analyse du marché et répartit l'argent entre 4 placements :
actions du monde, obligations, placement sans risque (monétaire) et or.
  - Beau temps (note de climat élevée) → plus d'actions (jusqu'à 75 %).
  - Tempête → moins d'actions (jusqu'à 35 %), plus d'or et de placement sans risque.
  - Taux qui montent vite → il évite les obligations (elles baissent quand les taux montent).
  - Alerte de crise → il met en plus 10 à 20 % à l'abri dans l'or pendant quelques jours.
"""
from datetime import datetime, timedelta

from bourse import clock
from bourse.execution.paper_broker import PENDING

from .base import CASH_BUFFER, Strategy


class PrudentStrategy(Strategy):
    name = "prudent"
    description = ("Répartit entre actions du monde, obligations, monétaire et or selon le climat du marché ; "
                   "se met à l'abri dans l'or lors des alertes de crise.")

    def targets(self, state: dict) -> dict[str, float]:
        p, view = self.params, self.view
        risk = view.risk_score
        now = clock.now()
        episodes = [e for e in state.get("episodes", []) if datetime.fromisoformat(e["until"]) > now]
        shelter = sum(e["share"] for e in episodes)

        stocks = p["actions_min"] + (p["actions_max"] - p["actions_min"]) * (risk + 100) / 200
        gold = min(p["or_base"] + max(0, -risk) / 100 * 0.10 + shelter, p["part_refuge_max"])
        stocks = max(stocks - shelter, 0.20)
        rest = max(0.0, 1 - CASH_BUFFER - stocks - gold)
        rates = view.factor("Taux américains")
        bonds_share = 0.3 if rates and rates.value <= -0.5 else 0.6

        self.think(f"Actions {stocks:.0%} (entre {p['actions_min']:.0%} et {p['actions_max']:.0%} selon "
                   f"le climat {risk:+d}), or {gold:.0%}"
                   + (f" dont {shelter:.0%} de mise à l'abri suite aux alertes" if shelter else "") + ".")
        if bonds_share < 0.6:
            self.think("Les taux montent vite : je préfère le placement sans risque aux obligations.")
        return {p["titre_actions"]: round(stocks, 3), p["titre_refuge"]: round(gold, 3),
                p["titre_obligations"]: round(rest * bonds_share, 3),
                p["titre_monetaire"]: round(rest * (1 - bonds_share), 3)}

    def on_start(self, broker, state):
        state.setdefault("episodes", [])
        self.note("Démarrage : je répartis le capital selon le climat du marché.")

    def on_alert(self, alert, broker, state):
        now = clock.now()
        what = self.describe(alert, now)
        if not alert.severe_terms:
            self.note(f"J'ignore l'{what} : pas de vocabulaire de crise.")
            return
        freshness = self.freshness(alert, now)
        if freshness == 0:
            self.note(f"J'ignore l'{what} : trop ancienne, le marché l'a déjà intégrée.")
            return
        episodes = state.setdefault("episodes", [])
        sheltered = sum(e["share"] for e in episodes)
        base = self.params["part_alerte_forte" if alert.level == "FORTE" else "part_alerte_importante"]
        share = round(min(base * freshness, self.params["part_refuge_max"] - sheltered), 3)
        if share < 0.02:
            self.note(f"{what} : déjà {sheltered:.0%} à l'abri (maximum atteint), je ne bouge pas.")
            return
        until = now + timedelta(days=self.params["duree_jours"])
        episodes.append({"share": share, "until": until.isoformat(), "alert": alert.title})
        state["force"] = True
        self.note(f"Suite à l'{what}, je mets {share:.0%} de plus à l'abri dans l'or jusqu'au {until:%d/%m}"
                  + ("" if freshness == 1 else f" (réaction réduite car l'information date un peu)") + ".")

    def on_cycle(self, broker, state):
        now = clock.now()
        for e in [e for e in state.get("episodes", []) if datetime.fromisoformat(e["until"]) <= now]:
            state["episodes"].remove(e)
            state["force"] = True
            self.note(f"Fin de la mise à l'abri liée à « {e['alert']} ».")
        self.think_climate()
        if self.view is None:
            return
        targets = self.targets(state)
        # Réajustement une fois par jour (ou tout de suite après une alerte)
        if not (state.get("force") or state.get("last_check") != now.date().isoformat()):
            return
        if self.rebalance(broker, targets, why=f"{self.view.headline()} → réajustement prudent",
                          tolerance=self.params["tolerance"]):
            self.note(f"{self.view.headline()}. Je réajuste : {self.describe_targets(targets, self.view)}.")
        if not broker.orders(PENDING):
            state["last_check"] = now.date().isoformat()
            state["force"] = False
