"""« Robot Opportuniste » (risque moyen) : il aime les soldes.

Un cœur dans l'indice mondial (50 à 75 % selon le climat du marché), et le reste pour des occasions :
  - « soldes » repérés par l'analyse : un placement dont la tendance de fond monte (au-dessus de sa
    moyenne 200 jours) mais qui vient de trop baisser trop vite (RSI bas) → il parie sur le rebond ;
  - panique après une alerte : il achète le marché qui vient de chuter.
Chaque occasion est revendue à l'objectif de gain, au stop-loss ou au bout de la durée maximale.
Par temps de tempête, il n'achète pas de soldes : « on ne rattrape pas un couteau qui tombe ».
"""
from datetime import datetime

from bourse import clock
from bourse.data.prices import price_at, quote
from bourse.execution.paper_broker import PENDING

from .base import Strategy

SALE_FAMILIES = ("actions", "or", "matieres")


class OpportunisteStrategy(Strategy):
    name = "opportuniste"
    description = ("Cœur dans l'indice mondial + achat de « soldes » (bons placements qui ont trop baissé) "
                   "et des marchés qui paniquent après une alerte.")

    def core_share(self) -> float:
        p = self.params
        return p["coeur_min"] + (p["coeur_max"] - p["coeur_min"]) * (self.view.risk_score + 100) / 200

    def trade_tickers(self, state: dict) -> set[str]:
        return {t["ticker"] for t in state.get("trades", [])}

    def on_start(self, broker, state):
        self.note("Démarrage : un cœur dans l'indice mondial, le reste en réserve pour les occasions.")

    def on_alert(self, alert, broker, state):
        now = clock.now()
        what = self.describe(alert, now)
        if not alert.severe_terms:
            self.note(f"J'ignore l'{what} : pas de vocabulaire de crise, donc pas de panique à acheter.")
            return
        freshness = self.freshness(alert, now)
        if freshness == 0:
            self.note(f"J'ignore l'{what} : trop ancienne, l'occasion est passée.")
            return

        # Quel marché a chuté ? Sinon : le marché d'actions le plus survendu d'après l'analyse
        markets = self.params["marches"]
        falling = sorted((m for m in alert.moves if m["ticker"] in markets and m["change_pct"] < 0),
                         key=lambda m: m["zscore"])
        if falling:
            target = markets[falling[0]["ticker"]]
            market_txt = f"{falling[0]['name']} ({falling[0]['change_pct']:+.1f} %)"
        elif self.view:
            candidates = self.view.pick(families=("actions",), leverage=(1,),
                                        exclude={self.params["titre_coeur"]} | self.trade_tickers(state))
            if not candidates:
                return
            best = min(candidates, key=lambda a: a.rsi)
            target, market_txt = best.ticker, f"{best.name} (RSI {best.rsi:.0f})"
        else:
            self.note(f"{what} : pas d'analyse du marché pour choisir quoi acheter, je passe mon tour.")
            return
        if target in self.trade_tickers(state):
            self.note(f"{what} : j'ai déjà un pari en cours sur {target}.")
            return

        # Le marché a-t-il déjà rebondi depuis l'annonce ? Alors c'est trop tard.
        then = price_at(target, alert.published)
        now_price = quote(target).price
        if then and now_price > then * 1.01:
            self.note(f"{what} : {target} a déjà rebondi de {now_price / then - 1:+.1%} depuis l'annonce, "
                      "trop tard pour acheter.")
            return
        amount = min(broker.total_value() * self.params["mise_par_occasion"] * freshness, broker.cash() - 50)
        if amount < 500:
            self.note(f"{what} : plus assez de liquidités, je ne peux pas en profiter.")
            return
        self.open_trade(broker, state, target, amount,
                        why=f"{what} → achat de {target} (pari sur le rebond de {market_txt})")
        self.note(f"Suite à l'{what}, j'achète {target} pour {amount:,.0f} € en pariant sur le rebond de "
                  f"{market_txt}. Revente à +{self.params['objectif_gain']:.0%}, "
                  f"-{self.params['stop_perte']:.0%} ou dans {self.params['duree_jours']} jours.")

    def hunt_sales(self, broker, state) -> None:
        p, view = self.params, self.view
        taken = self.trade_tickers(state)
        sales = [a for a in view.pick(families=SALE_FAMILIES, leverage=(1,), exclude={p["titre_coeur"]} | taken)
                 if a.above_ma200 and a.rsi < p["rsi_soldes"]]
        if not sales:
            self.think(f"Aucun solde : aucun bon placement n'a un RSI sous {p['rsi_soldes']}.")
            return
        best = min(sales, key=lambda a: a.rsi)
        self.think(f"Solde repéré : {best.label}, RSI {best.rsi:.0f}, {best.ret_5d:+.1f} % sur 5 jours "
                   f"mais tendance de fond haussière.")
        if view.risk_score <= -30:
            self.think("Mais le climat est à la tempête : je n'achète pas (un couteau qui tombe).")
            return
        if len(taken) >= p["max_occasions"]:
            self.think(f"Mais j'ai déjà {len(taken)} occasions en cours, c'est mon maximum.")
            return
        if not self.once_per_day(state, "last_sale_day"):
            return
        amount = min(broker.total_value() * p["mise_par_occasion"], broker.cash() - 50)
        if amount < 500:
            self.think("Mais je n'ai plus assez de liquidités.")
            return
        why = (f"Solde : {best.label} a trop baissé (RSI {best.rsi:.0f}, {best.ret_5d:+.1f} % en 5 jours) "
               "alors que sa tendance de fond monte → pari sur le rebond")
        self.open_trade(broker, state, best.ticker, amount, why=why)
        self.note(f"{why}. J'achète pour {amount:,.0f} €.")

    def on_cycle(self, broker, state):
        self.manage_trades(broker, state)
        self.think_climate()
        if self.view is None:
            return
        core = round(self.core_share(), 3)
        self.think(f"Cœur dans l'indice mondial visé : {core:.0%} (selon le climat), "
                   f"{len(state.get('trades', []))} occasion(s) en cours.")
        if not broker.orders(PENDING):
            self.hunt_sales(broker, state)
        if self.rebalance(broker, {self.params["titre_coeur"]: core}, exclude=self.trade_tickers(state),
                          why=f"{self.view.headline()} → cœur à {core:.0%}", tolerance=self.params["tolerance"]):
            self.note(f"{self.view.headline()} : je règle mon cœur dans l'indice mondial à {core:.0%}.")
