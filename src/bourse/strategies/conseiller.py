"""« Mon conseiller » : un conseiller long terme pour le propriétaire, d'abord en argent fictif (23/10/2026).

Il ne cherche pas à deviner la Bourse : il applique un plan écrit d'après le profil (réglages « parametres ») :
  - versement mensuel fictif (comme un virement automatique) ;
  - répartition selon le temps qui reste avant d'avoir besoin de l'argent (« horizon ») : plus la date approche,
    plus la part d'actions baisse, jusqu'à 0 quelques mois avant ;
  - l'argent versé sert d'abord à racheter la ligne la plus en retard sur sa part (une seule opération par mois,
    peu de frais) ; on ne vend que si une ligne s'écarte trop de sa part, ou pour suivre la baisse prévue des
    actions ;
  - il lit tout ce que la veille analyse (climat, alertes), le dit, mais ne change PAS le plan pour ça :
    pour un horizon court, une baisse de la Bourse se traverse avec peu d'actions, pas en jouant les devins.
Performance : gain sur l'argent versé ; l'indice de comparaison reçoit les mêmes versements, aux mêmes dates.
"""
import calendar
import json
from datetime import date

from bourse import clock
from bourse.data.prices import quote
from bourse.execution.broker import BUY, SELL
from bourse.execution.paper_broker import PENDING

from .base import CASH_BUFFER, Strategy


def eur_txt(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def months_between(a: date, b: date) -> float:
    return (b.year - a.year) * 12 + (b.month - a.month) + (b.day - a.day) / 30.4


class ConseillerStrategy(Strategy):
    name = "conseiller"
    description = ("Ton conseiller long terme : versement chaque mois, répartition selon ton horizon (moins "
                   "d'actions à l'approche de la date où tu as besoin de l'argent), aucun pari sur l'actualité.")

    # ----- le plan -----

    def months_left(self) -> float:
        need = date.fromisoformat(str(self.params["besoin_le"]))
        return months_between(clock.now().date(), need)

    def plan(self) -> dict[str, float]:
        """Parts voulues : actions dégressives jusqu'à 0 `sortie_mois` avant la date, le reste au monétaire."""
        p = self.params
        left, full, out = self.months_left(), p["mois_actions_max"], p["sortie_mois"]
        share = p["actions_max"] * min(1.0, max(0.0, (left - out) / (full - out)))
        return {p["titre_actions"]: round(share, 3), p["titre_monetaire"]: round(1 - CASH_BUFFER - share, 3)}

    # ----- versements -----

    def deposit(self, broker, state: dict, amount: float, why: str) -> None:
        """Ajoute `amount` € d'argent versé ; l'indice de comparaison « reçoit » le même versement aujourd'hui
        (on recale son cours de départ pour que sa valeur augmente exactement de `amount`)."""
        row = broker.conn.execute("SELECT initial_cash, benchmark, benchmark_start FROM portfolios WHERE id = ?",
                                  (broker.portfolio_id,)).fetchone()
        capital, bench_now = row["initial_cash"], quote(row["benchmark"]).price_eur
        bench_value = capital * bench_now / row["benchmark_start"]
        new_capital = capital + amount
        new_start = new_capital * bench_now / (bench_value + amount)
        state.setdefault("versements", []).append({"time": clock.now().isoformat(), "eur": amount})
        # capital, indice ET mémoire enregistrés ensemble : si le passage plante ensuite, le versement n'est
        # ni perdu ni refait (sinon la mémoire n'est sauvée qu'à la fin du passage → versement compté 2 fois)
        broker.conn.execute("UPDATE portfolios SET initial_cash = ?, benchmark_start = ?, state = ? WHERE id = ?",
                            (round(new_capital, 2), new_start, json.dumps(state, ensure_ascii=False),
                             broker.portfolio_id))
        broker.conn.commit()
        self.note(f"{why} : + {eur_txt(amount)} (total versé : {eur_txt(new_capital)}).")

    def monthly_deposit(self, broker, state: dict) -> None:
        """Un versement par mois, le même jour que le départ (le 31 → le dernier jour des mois plus courts) ;
        rattrapé au passage suivant si le PC dormait."""
        today = clock.now().date()
        last = date.fromisoformat(state["dernier_versement"])
        anchor = state.setdefault("jour_versement", last.day)
        year, month = last.year + (last.month // 12), last.month % 12 + 1
        due = date(year, month, min(anchor, calendar.monthrange(year, month)[1]))
        if today >= due:
            state["dernier_versement"] = due.isoformat()     # avancé AVANT l'enregistrement du versement
            self.deposit(broker, state, float(self.params["versement_mensuel"]), "Versement mensuel")

    # ----- événements -----

    def on_start(self, broker, state):
        state["dernier_versement"] = clock.now().date().isoformat()
        state["jour_versement"] = clock.now().day
        state["versements"] = [{"time": clock.now().isoformat(), "eur": broker.initial_cash()}]
        self.note(f"Démarrage : {eur_txt(broker.initial_cash())} de départ, puis "
                  f"{eur_txt(self.params['versement_mensuel'])} chaque mois. Besoin de l'argent vers le "
                  f"{date.fromisoformat(str(self.params['besoin_le'])):%d/%m/%Y}.")

    def on_alert(self, alert, broker, state):
        if alert.level == "FORTE":
            state["derniere_alerte"] = f"{alert.title} ({alert.score}/100)"

    def on_cycle(self, broker, state):
        p = self.params
        self.monthly_deposit(broker, state)
        left = self.months_left()
        targets = self.plan()
        names = {p["titre_actions"]: "actions du monde entier", p["titre_monetaire"]: "monétaire (sans risque)"}
        self.think(f"Il reste environ {max(left, 0):.0f} mois avant ton besoin d'argent : plan = "
                   + ", ".join(f"{w:.0%} {names[t]}" for t, w in targets.items()) + ".")
        if self.view is not None:
            self.think(f"Ce que dit la veille : {self.view.headline()}. Je ne change pas le plan pour ça : avec "
                       "peu d'actions, une baisse se traverse sans vendre.")
        if state.get("derniere_alerte"):
            self.think(f"Dernière alerte forte : « {state['derniere_alerte']} ». Même consigne : on tient le plan.")
        if broker.orders(PENDING):
            self.think("Un ordre attend l'ouverture du marché : je ne fais rien d'autre en attendant.")
            return
        total = broker.total_value()
        held = {x["ticker"]: x["valeur_eur"] for x in broker.positions()}
        # 1) une ligne trop loin de sa part → on vend l'excédent ; pendant la baisse prévue des actions (moins de
        #    `mois_actions_max` mois), tout excédent d'actions est vendu, au plus une fois par mois, pour arriver
        #    vraiment à 0 % d'actions à la date prévue
        gliding = left < p["mois_actions_max"]
        month = clock.now().strftime("%Y-%m")
        for t, w in targets.items():
            excess = held.get(t, 0) - w * total
            glide = (gliding and t == p["titre_actions"] and state.get("vente_plan") != month
                     and excess >= max(p["ordre_min_eur"], quote(t).price_eur))
            if (excess > p["ecart_max"] * total or glide) and excess >= p["ordre_min_eur"]:
                if glide:
                    state["vente_plan"] = month
                broker.place_order(t, SELL, amount_eur=excess, reason="Plan : ramener la ligne à sa part")
                self.note(f"Je vends pour {eur_txt(excess)} de {names[t]} : la ligne dépasse sa part prévue.")
                return
        # 2) l'argent disponible va aux lignes en retard sur leur part, au prorata de leur retard
        cash = broker.cash() - CASH_BUFFER * total
        if cash < p["ordre_min_eur"]:
            return
        gaps = {t: max(0.0, w * total - held.get(t, 0)) for t, w in targets.items()}
        if not sum(gaps.values()):
            gaps = dict(targets)
        buys = {t: cash * g / sum(gaps.values()) for t, g in gaps.items()}
        # une part entière au moins, et pas d'ordre minuscule : sinon, cet argent va à l'autre ligne
        # prix d'une part tout compris (glissement, frais, minimum de 1 €) : sinon le courtier achète 0 titre et
        # rejette l'ordre, repassé à chaque passage
        unit = {t: quote(t).price_eur * 1.003 + 1.0 for t in buys}
        small = [t for t, eur in buys.items() if eur < max(p["ordre_min_eur"], unit[t])]
        if small and len(small) < len(buys):
            rest = sum(buys.pop(t) for t in small)
            for t in buys:
                buys[t] += rest / len(buys)
        elif small:
            self.think(f"{eur_txt(cash)} disponibles, pas assez pour une part : j'attends le prochain versement.")
            return
        for t, eur in buys.items():
            broker.place_order(t, BUY, amount_eur=eur, reason="Plan : placer l'argent disponible")
        self.note("Je place l'argent disponible : " + ", ".join(f"{eur_txt(e)} en {names[t]}" for t, e in buys.items())
                  + ".")
