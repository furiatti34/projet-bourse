"""Robots du labo : les robots du présent, plus les corrections gardées (voir data/labo/carnet.md).

Au départ, chaque robot du labo est identique à celui du présent. Une correction s'ajoute ici,
en surchargeant une méthode, avec un commentaire qui renvoie au numéro du carnet.
"""
from datetime import datetime, timedelta

from bourse import clock
from bourse.strategies.audacieux import ATTACK, BEAR, CRISIS, NASDAQ, NEUTRAL, AudacieuxStrategy
from bourse.execution.broker import SELL
from bourse.execution.paper_broker import FILLED
from bourse.strategies.base import CASH_BUFFER
from bourse.strategies.kamikaze import FAMILIES, KamikazeStrategy
from bourse.strategies.opportuniste import OpportunisteStrategy
from bourse.strategies.prudent import PrudentStrategy


class _ScoreOverride:
    """L'analyse du marché telle quelle, avec une autre note de climat (voir carnet n°4)."""

    def __init__(self, view, score: int):
        self._view, self.risk_score = view, score

    def __getattr__(self, name):
        return getattr(self._view, name)


class _Filtered:
    """L'analyse du marché, en ne proposant que les placements qui passent un filtre (voir carnet n°17)."""

    def __init__(self, view, keep):
        self._view, self._keep = view, keep

    def pick(self, *args, **kwargs):
        return [a for a in self._view.pick(*args, **kwargs) if self._keep(a)]

    def __getattr__(self, name):
        return getattr(self._view, name)


class LabPrudent(PrudentStrategy):
    name = "labo_prudent"

    def on_cycle(self, broker, state):
        """Carnet n°4 : la part d'actions suit la moyenne de la note de climat des `lissage_jours` derniers jours
        (au présent : la note du moment, qui varie beaucoup d'un jour à l'autre et provoque des réajustements)."""
        days = int(self.params.get("lissage_jours", 0))
        if days > 1 and self.view is not None:
            history = state.setdefault("climats", {})
            history[clock.now().date().isoformat()] = self.view.risk_score
            for old in sorted(history)[:-days]:
                del history[old]
            smooth = round(sum(history.values()) / len(history))
            self.think(f"Note de climat moyenne sur {len(history)} jour(s) : {smooth:+d} (aujourd'hui {self.view.risk_score:+d}).")
            self.view = _ScoreOverride(self.view, smooth)
        super().on_cycle(broker, state)

    def targets(self, state: dict) -> dict[str, float]:
        """Carnet n°8 : obligations seulement si leur cours est au-dessus de sa moyenne 200 jours
        (`obligations_tendance`) ; sinon cette part va au placement sans risque.
        Carnet n°12 : quand l'indice mondial est au-dessus de ses moyennes 50 et 200 jours, la part d'actions
        ne descend pas sous `actions_min_tendance`."""
        floor = self.params.get("actions_min_tendance")
        world = self.view.asset(self.params["titre_actions"])
        if floor and world is not None and world.above_ma200 and world.above_ma50:
            self.params = {**self.params, "actions_min": max(self.params["actions_min"], floor)}
            self.think(f"Le marché mondial est en tendance haussière : au moins {floor:.0%} d'actions.")
        targets = super().targets(state)
        p = self.params
        gold = self.view.asset(p["titre_refuge"])
        if p.get("or_tendance") and gold is not None and not gold.above_ma200 and p["titre_refuge"] in targets:
            share = targets.pop(p["titre_refuge"])   # carnet n°16 : or seulement s'il est en tendance haussière
            targets[p["titre_monetaire"]] = round(targets.get(p["titre_monetaire"], 0) + share, 3)
            self.think("L'or est sous sa moyenne 200 jours : je le remplace par le placement sans risque.")
        bonds = self.view.asset(p["titre_obligations"])
        if p.get("obligations_tendance") and bonds is not None and not bonds.above_ma200:
            share = targets.pop(p["titre_obligations"], 0)
            targets[p["titre_monetaire"]] = round(targets.get(p["titre_monetaire"], 0) + share, 3)
            self.think("Les obligations sont sous leur moyenne 200 jours : je les remplace par le placement sans risque.")
        return targets


class LabOpportuniste(OpportunisteStrategy):
    name = "labo_opportuniste"

    def on_cycle(self, broker, state):
        total = broker.total_value()
        trades = self.trade_tickers(state)
        held = sum(p["valeur_eur"] for p in broker.positions() if p["ticker"] in trades)
        self._occupied = held / total if total else 0.0
        super().on_cycle(broker, state)

    def core_share(self) -> float:
        """Carnet n°5 : l'argent qui attend des occasions ne dort plus. Hors tempête, la réserve en liquide se
        limite à UNE mise (`reserve_une_mise`) ; le reste rejoint le cœur (indice mondial)."""
        base = super().core_share()
        if not self.params.get("reserve_une_mise") or self.view.risk_score <= -30:
            return base
        free = 1 - CASH_BUFFER - self.params["mise_par_occasion"] - getattr(self, "_occupied", 0.0)
        return round(max(base, free), 3)

    def hunt_sales(self, broker, state) -> None:
        """Carnet n°13 : pas de « soldes » quand le marché mondial lui-même est sous sa moyenne 200 jours
        (`soldes_si_monde_haussier`) : dans un marché qui baisse, ce qui baisse n'est pas en solde."""
        world = self.view.asset(self.params["titre_coeur"])
        if self.params.get("soldes_si_monde_haussier") and world is not None and not world.above_ma200:
            self.think("Le marché mondial est sous sa moyenne 200 jours : je ne cherche pas de soldes.")
            return
        floor = self.params.get("soldes_chute_max")
        if floor is not None:   # carnet n°17 : pas de « solde » sur un placement effondré (couteau qui tombe)
            original = self.view
            self.view = _Filtered(original, lambda a: a.drawdown >= floor)
            try:
                super().hunt_sales(broker, state)
            finally:
                self.view = original
            return
        super().hunt_sales(broker, state)

    def _trailing(self, broker, state) -> None:
        """Carnet n°20 : `stop_suiveur` → une occasion montée à +4 % est revendue si elle revient à zéro
        (on ne laisse pas un gain se transformer en perte)."""
        for trade in list(state.get("trades", [])):
            order = broker.order(trade["order_id"])
            if order["status"] != FILLED:
                continue
            asset = self.view.asset(trade["ticker"]) if self.view else None
            if asset is None:
                continue
            change = asset.price / order["fill_price"] - 1
            trade["pic"] = max(trade.get("pic", 0.0), change)
            if trade["pic"] >= 0.04 and change <= 0:
                broker.place_order(trade["ticker"], SELL, quantity=order["filled_qty"],
                                   reason=f"Revente {trade['ticker']} : gain de {trade['pic']:+.1%} revenu à zéro")
                self.note(f"Revente de {trade['ticker']} : le gain de {trade['pic']:+.1%} est revenu à zéro.")
                state["trades"].remove(trade)

    def manage_trades(self, broker, state) -> None:
        if self.params.get("stop_suiveur"):
            self._trailing(broker, state)
        self._manage_trades_rsi(broker, state)

    def _manage_trades_rsi(self, broker, state) -> None:
        """Carnet n°9 : une occasion est revendue dès que le rebond est fait (RSI revenu au-dessus de
        `sortie_rsi`), au lieu d'attendre l'objectif, le stop-loss ou la durée maximale."""
        level = self.params.get("sortie_rsi")
        if level and self.view is not None:
            for trade in list(state.get("trades", [])):
                order = broker.order(trade["order_id"])
                asset = self.view.asset(trade["ticker"])
                if order["status"] != FILLED or asset is None or asset.rsi < level:
                    continue
                broker.place_order(trade["ticker"], SELL, quantity=order["filled_qty"],
                                   reason=f"Revente {trade['ticker']} : rebond fait (RSI {asset.rsi:.0f})")
                self.note(f"Revente de {trade['ticker']} : rebond fait (RSI {asset.rsi:.0f}).")
                state["trades"].remove(trade)
        super().manage_trades(broker, state)


class LabAudacieux(AudacieuxStrategy):
    name = "labo_audacieux"

    def on_cycle(self, broker, state):
        self._state = state
        # Carnet n°32 (méthode B) : la posture suit la moyenne de la note de climat des `lissage_jours` derniers
        # jours, comme Prudent (n°4), au lieu de la note du moment : moins d'allers-retours entre postures.
        days = int(self.params.get("lissage_jours", 0))
        if days > 1 and self.view is not None:
            history = state.setdefault("climats", {})
            history[clock.now().date().isoformat()] = self.view.risk_score
            for old in sorted(history)[:-days]:
                del history[old]
            smooth = round(sum(history.values()) / len(history))
            self.think(f"Note de climat moyenne sur {len(history)} jour(s) : {smooth:+d} (aujourd'hui {self.view.risk_score:+d}).")
            self.view = _ScoreOverride(self.view, smooth)
        super().on_cycle(broker, state)

    def wanted_mode(self) -> str:
        """Carnet n°3 : l'attaque (100 % Nasdaq ×2) exige en plus que le Nasdaq soit au-dessus de sa moyenne
        200 jours (`attaque_tendance_longue`). Au présent, la moyenne 50 jours suffit."""
        mode = super().wanted_mode()
        if mode == ATTACK and self.params.get("attaque_tendance_longue"):
            nasdaq = self.view.asset(NASDAQ)
            if nasdaq is not None and not nasdaq.above_ma200:
                self.think("Climat favorable, mais le Nasdaq est sous sa moyenne 200 jours : pas de levier à 100 %.")
                return NEUTRAL
        # Carnet n°6 : une posture n'est quittée que si la note s'éloigne du seuil de `marge_posture` points
        margin = self.params.get("marge_posture")
        current = getattr(self, "_state", {}).get("mode")
        if margin and mode == NEUTRAL and current in (ATTACK, BEAR):
            p, risk = self.params, self.view.risk_score
            nasdaq, world = self.view.asset(NASDAQ), self.view.factor("Tendance mondiale")
            if current == ATTACK and risk >= p["seuil_attaque"] - margin and (nasdaq is None or nasdaq.above_ma50):
                self.think(f"Note {risk:+d} un peu sous le seuil d'attaque, mais dans la marge : je garde l'attaque.")
                return ATTACK
            if current == BEAR and risk <= p["seuil_defense"] + margin and world and world.value < 0:
                self.think(f"Note {risk:+d} un peu au-dessus du seuil de défense, mais dans la marge : je reste en baisse.")
                return BEAR
        # Carnet n°10 : pas d'attaque au levier quand la peur est forte (VIX au-dessus de `attaque_vix_max`)
        vix_max = self.params.get("attaque_vix_max")
        fear = self.view.factor("Peur (VIX)")
        if mode == ATTACK and vix_max and fear is not None and fear.value < (20 - vix_max) / 10:
            self.think(f"Peur trop forte ({fear.detail}) : pas d'attaque au levier.")
            return NEUTRAL
        return mode

    def targets(self, mode: str) -> dict[str, float]:
        if mode == BEAR and self.params.get("baisse_en_liquide"):
            # carnet n°18 : en posture « baisse », tout au placement sans risque au lieu du pari à la baisse ×2
            self.think("Posture baisse : je me mets à l'abri dans le placement sans risque, sans pari à la baisse.")
            return {self.params["titre_monetaire"]: round(1 - CASH_BUFFER, 3)}
        return self._voile(mode, self._targets(mode))

    def _voile(self, mode: str, targets: dict[str, float]) -> dict[str, float]:
        """Carnet n°34 (méthode B) : « par mer agitée, on réduit la voilure ». Avec `voile_selon_volatilite`,
        chaque placement à levier (×2, ou ×2 à la baisse) est réduit quand sa volatilité des 20 derniers jours
        dépasse sa volatilité HABITUELLE (médiane de tout ce que le robot a observé jusqu'ici, jamais la suite) :
        part × habituelle ÷ actuelle. Le reste va au placement sans risque. Pari de crise : inchangé."""
        if not self.params.get("voile_selon_volatilite") or mode == CRISIS or self.view is None:
            return targets
        state = getattr(self, "_state", {})
        seen = state.setdefault("volatilites", {})
        today = clock.now().date().isoformat()
        out, money = dict(targets), self.params["titre_monetaire"]
        for ticker, share in targets.items():
            asset = self.view.asset(ticker)
            if asset is None or abs(asset.leverage) < 2 or not asset.vol:
                continue
            hist = seen.setdefault(ticker, {})
            hist[today] = asset.vol
            if len(hist) < 60:                         # pas encore assez vu pour savoir ce qui est « habituel »
                continue
            vols = sorted(hist.values())
            usual = vols[len(vols) // 2]
            scale = min(1.0, usual / asset.vol)
            if scale < 0.999:
                cut = round(share * (1 - scale), 3)
                out[ticker] = round(share - cut, 3)
                out[money] = round(out.get(money, 0) + cut, 3)
                self.think(f"{asset.name} est agité (volatilité {asset.vol:.0f} % contre {usual:.0f} % d'habitude) : "
                           f"je n'en garde que {scale:.0%}.")
        return out

    def _targets(self, mode: str) -> dict[str, float]:
        """Carnet n°2 : en posture neutre, le marché d'accompagnement (50 %) est gardé tant qu'il reste dans
        le top `garder_rang` de l'élan (1 au présent : il changeait dès qu'un autre passait devant)."""
        keep_rank = int(self.params.get("garder_rang", 1))
        if mode != NEUTRAL or keep_rank <= 1:
            return super().targets(mode)
        p, full = self.params, 1 - CASH_BUFFER
        ranked = sorted(self.view.pick(families=("actions",), leverage=(1,)), key=lambda a: -a.momentum)
        state = getattr(self, "_state", {})
        current = next((a for a in ranked[:keep_rank] if a.ticker == state.get("compagnon")), None)
        best = current or ranked[0]
        state["compagnon"] = best.ticker
        self.think(f"Marché d'accompagnement : {best.label} (élan {best.momentum:+.1f}"
                   + (", gardé car encore dans le haut du classement)" if current and best is not ranked[0] else ")"))
        attack = p["titre_attaque"]
        share = float(p.get("neutre_part_attaque", 0.50))   # carnet n°21 : moins de levier en posture neutre
        nasdaq = self.view.asset(NASDAQ)
        if p.get("neutre_sans_levier_si_baisse") and nasdaq is not None and not nasdaq.above_ma50:
            attack = NASDAQ   # carnet n°14 : Nasdaq ×1 au lieu de ×2 quand il est sous sa moyenne 50 jours
            self.think("Le Nasdaq est sous sa moyenne 50 jours : ma moitié Nasdaq passe sans levier.")
        return {attack: share, best.ticker: round(full - share, 3)}


class LabKamikaze(KamikazeStrategy):
    name = "labo_kamikaze"

    def guard_positions(self, broker, state, now: datetime) -> bool:
        """Carnet n°15 : avec `stop_selon_levier`, le stop-loss vaut `stop_perte` × le levier du placement
        (7 % du marché sous-jacent : −21 % sur un ×3), au lieu de −7 % quel que soit le levier."""
        if not self.params.get("stop_selon_levier") or self.view is None:
            return super().guard_positions(broker, state, now)
        base = self.params["stop_perte"]
        sold = False
        for pos in broker.positions():
            asset = self.view.asset(pos["ticker"])
            lev = abs(asset.leverage) if asset else 1
            self.params = {**self.params, "stop_perte": base * max(1, lev)}
            sold |= self._guard_one(broker, state, now, pos["ticker"])
        self.params = {**self.params, "stop_perte": base}
        held = {p["ticker"] for p in broker.positions()}
        for ticker in [t for t in state.get("entries", {}) if t not in held]:
            del state["entries"][ticker]
        return sold

    def _guard_one(self, broker, state, now, ticker) -> bool:
        """guard_positions du présent, limité à un seul placement (pour lui appliquer son propre stop)."""
        original = broker.positions
        others = {k: v for k, v in state.get("entries", {}).items() if k != ticker}
        broker.positions = lambda: [p for p in original() if p["ticker"] == ticker]
        try:
            return super().guard_positions(broker, state, now)
        finally:
            broker.positions = original
            state.setdefault("entries", {}).update(others)   # le présent efface les autres placements

    def pick_horses(self, state, now: datetime) -> list:
        """Carnet n°1 : un cheval déjà détenu est gardé tant qu'il reste dans le top `garder_rang`
        (2 au présent). Achat : toujours les 2 plus rapides. Évite de tout revendre dès que le classement
        bouge d'une place (chaque aller-retour coûte ~0,3 % de frais et d'écart de prix)."""
        p, view = self.params, self.view
        keep_rank = int(p.get("garder_rang", 2))
        # Carnet n°7 : `exiger_tendance` → seulement des placements au-dessus de leur moyenne 50 jours
        trend = bool(p.get("exiger_tendance"))
        # Carnet n°22 : `baisse_si_tempete` → paris à la baisse seulement quand le climat est à la tempête
        no_short = bool(p.get("baisse_si_tempete")) and view.risk_score > -30
        # Carnet n°35 (méthode B) : `elan_de_fond` → classer selon l'élan de fond (semaines, mois) au lieu du
        # « sprint » de quelques jours, que les études trouvent surtout fait de bruit qui se retourne.
        speed = (lambda a: a.momentum) if p.get("elan_de_fond") else (lambda a: a.sprint)
        ranked = sorted((a for a in view.pick(families=FAMILIES, exclude=set(state.get("banned", {})))
                         if speed(a) > 0 and a.ret_5d > 0 and (not trend or a.above_ma50)
                         and not (no_short and a.leverage < 0)), key=lambda a: -speed(a))
        self.think("Ce qui monte le plus vite : " + ", ".join(
            f"{a.name} ({speed(a):+.1f})" for a in ranked[:4]) if ranked else "Rien ne monte en ce moment.")
        if not ranked:
            return []
        best = speed(ranked[0])
        horses = []
        for ticker, entry in state.get("entries", {}).items():   # garder ses chevaux ?
            a = view.asset(ticker)
            if a is None:
                continue
            young = now - datetime.fromisoformat(entry["since"]) < timedelta(hours=p["duree_min_heures"])
            if a in ranked[:keep_rank] or young or best - speed(a) < p["ecart_rotation"]:
                horses.append(a)
        for a in ranked:
            if len(horses) >= 2:
                break
            if a not in horses:
                horses.append(a)
        horses = horses[:2]
        # Carnet n°33 (méthode B) : `confirmer_rotation` → un cheval détenu n'est lâché que si l'envie de le
        # lâcher dure depuis la veille au moins (pas sur un classement d'un seul jour). Le stop-loss et la
        # sécurisation des gains restent immédiats (guard_positions, avant ce choix).
        if p.get("confirmer_rotation"):
            held = [a for a in (view.asset(t) for t in state.get("entries", {})) if a is not None]
            wanted = {a.ticker for a in horses}
            pending = state.setdefault("a_lacher", {})
            today = now.date().isoformat()
            kept = [a for a in held if a.ticker not in wanted and pending.setdefault(a.ticker, today) == today]
            for t in [t for t in pending if t in wanted or t not in {a.ticker for a in held}]:
                del pending[t]
            if kept:
                self.think("Je voudrais changer de cheval, mais j'attends demain pour confirmer : "
                           + ", ".join(a.name for a in kept) + ".")
                horses = [a for a in horses if a in held] + kept
                horses += [a for a in ranked if a not in horses][: max(0, 2 - len(horses))]
                horses = horses[:2]
        return sorted(horses, key=lambda a: -speed(a))


LAB = {cls.name: cls for cls in (LabPrudent, LabOpportuniste, LabAudacieux, LabKamikaze)}
