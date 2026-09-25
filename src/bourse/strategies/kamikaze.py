"""« Robot Kamikaze » (risque extrême) : gagner le plus possible, le plus vite possible.

À chaque passage, il classe TOUS les placements de l'analyse (levier ×2 et ×3, crypto, pétrole,
paris à la baisse…) selon leur élan très court terme (ce qui monte le plus vite depuis quelques jours),
et met tout sur les 2 premiers : 70 % sur le n°1, 29 % sur le n°2.
  - Il change de cheval dès qu'un autre va nettement plus vite (mais garde une position au moins
    quelques heures, sinon les frais le ruineraient).
  - Stop-loss serré : une position qui perd 7 % est coupée, et il boude ce placement 48 h.
  - Dès +12 % de gain, il sécurise : il revend si ça recule de 5 % depuis le plus haut.
  - Alerte de crise TRÈS fraîche : tout sur le Nasdaq ×3 À LA BAISSE pendant 2 jours.
Quand le marché s'effondre, les paris à la baisse deviennent ce qui « monte le plus vite » :
il les trouve tout seul, sans qu'on lui dise.
Pas de triche : mêmes règles d'exécution que les autres (prix publié APRÈS sa décision, frais réels).
"""
from datetime import datetime, timedelta

from bourse import clock
from bourse.execution.broker import SELL
from bourse.execution.paper_broker import PENDING

from .base import Strategy

FAMILIES = ("actions", "or", "matieres", "crypto")


class KamikazeStrategy(Strategy):
    name = "kamikaze"
    description = ("Tout sur les 2 placements qui montent le plus vite (levier ×3, crypto, paris à la baisse "
                   "compris), change dès qu'un autre va plus vite ; stop-loss serré.")

    def on_start(self, broker, state):
        self.note("Démarrage : je cherche ce qui monte le plus vite, et j'y mets tout. 🚀")

    def on_alert(self, alert, broker, state):
        now = clock.now()
        what = self.describe(alert, now)
        if not alert.severe_terms or self.freshness(alert, now) == 0:
            self.note(f"J'ignore l'{what} : pas assez grave ou pas assez fraîche pour moi.")
            return
        until = now + timedelta(hours=self.params["pari_crise_heures"])
        state["crisis_until"] = until.isoformat()
        self.note(f"Suite à l'{what}, je mise TOUT sur la chute du Nasdaq (×3 à la baisse) "
                  f"jusqu'au {until:%d/%m %H:%M}.")

    # ----- surveillance des positions -----

    def guard_positions(self, broker, state, now: datetime) -> bool:
        """Stop-loss et sécurisation des gains. Renvoie True si quelque chose a été vendu."""
        p = self.params
        entries = state.setdefault("entries", {})
        banned = state.setdefault("banned", {})
        crisis_ticker = p["titre_crise"] if state.get("crisis_until") else None
        held = {pos["ticker"]: pos for pos in broker.positions()}
        for ticker in list(entries):
            if ticker not in held:
                del entries[ticker]
        sold = False
        for ticker, pos in held.items():
            gain = pos["gain_pct"] / 100
            entry = entries.setdefault(ticker, {"since": now.isoformat(), "peak": gain})
            entry["peak"] = max(entry["peak"], gain)
            stop = p["stop_crise"] if ticker == crisis_ticker else p["stop_perte"]
            from_peak = (1 + gain) / (1 + entry["peak"]) - 1
            why = None
            if gain <= -stop:
                why = f"stop-loss à {gain:+.1%}"
                banned[ticker] = (now + timedelta(hours=p["banni_heures"])).isoformat()
                if ticker == crisis_ticker:
                    state["crisis_until"] = None
            elif entry["peak"] >= p["securisation_des"] and from_peak <= -p["securisation_recul"]:
                why = (f"je sécurise mon gain de {gain:+.1%} (il était monté à {entry['peak']:+.1%})")
                banned[ticker] = (now + timedelta(hours=p["banni_heures"] / 4)).isoformat()
            if why:
                broker.place_order(ticker, SELL, reason=f"Kamikaze : {why}")
                self.note(f"Je vends {ticker} : {why}.")
                sold = True
            else:
                self.think(f"Je tiens {ticker} : {gain:+.1%} (plus haut {entry['peak']:+.1%}).")
        for ticker, until in list(banned.items()):
            if datetime.fromisoformat(until) <= now:
                del banned[ticker]
        return sold

    # ----- choix des chevaux -----

    def pick_horses(self, state, now: datetime) -> list:
        p, view = self.params, self.view
        ranked = sorted((a for a in view.pick(families=FAMILIES, exclude=set(state.get("banned", {})))
                         if a.sprint > 0 and a.ret_5d > 0), key=lambda a: -a.sprint)
        self.think("Ce qui monte le plus vite : " + ", ".join(
            f"{a.name} ({a.sprint:+.1f})" for a in ranked[:4]) if ranked else "Rien ne monte en ce moment.")
        if not ranked:
            return []
        best = ranked[0].sprint
        horses = []
        for ticker, entry in state.get("entries", {}).items():   # garder ses chevaux ?
            a = view.asset(ticker)
            if a is None:
                continue
            young = now - datetime.fromisoformat(entry["since"]) < timedelta(hours=p["duree_min_heures"])
            if a in ranked[:2] or young or best - a.sprint < p["ecart_rotation"]:
                horses.append(a)
        for a in ranked:
            if len(horses) >= 2:
                break
            if a not in horses:
                horses.append(a)
        return sorted(horses[:2], key=lambda a: -a.sprint)

    def on_cycle(self, broker, state):
        p, now = self.params, clock.now()
        self.think_climate()
        if broker.orders(PENDING):
            self.think("J'attends que mes ordres soient exécutés.")
            return
        if self.guard_positions(broker, state, now):
            return
        crisis = state.get("crisis_until")
        if crisis and datetime.fromisoformat(crisis) <= now:
            state["crisis_until"] = crisis = None
            self.note("Fin de mon pari de crise.")
        if crisis:
            targets, why = {p["titre_crise"]: 0.99}, "pari de crise : tout sur la chute du Nasdaq ×3"
        elif self.view is None:
            return
        else:
            horses = self.pick_horses(state, now)
            if len(horses) == 2:
                targets = {horses[0].ticker: p["part_numero_1"], horses[1].ticker: p["part_numero_2"]}
            else:
                targets = {h.ticker: 0.99 for h in horses}
            why = ("course à l'élan : " + " + ".join(f"{h.name} ({h.sprint:+.1f})" for h in horses)
                   if horses else "rien ne monte, je reste en liquide")
        same_horses = set(targets) == set(state.get("entries", {}))
        if self.rebalance(broker, targets, why=f"Kamikaze → {why}", tolerance=0.45 if same_horses else 0.02):
            self.note(f"Je fonce : {why}.")
