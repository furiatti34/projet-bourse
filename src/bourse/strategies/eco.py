"""« Robot Éco » (risque élevé) : le robot boosté à l'IA.

Les autres robots suivent des règles fixes. Lui réfléchit comme un gérant : toutes les quelques heures
(et tout de suite après une alerte forte), il donne à l'IA d'Éco 1 un dossier complet :
  - le climat du marché, ses signaux et les chiffres de tous les placements suivis ;
  - les alertes récentes de la veille ;
  - ce qu'en disent les économistes de la bibliothèque (écoles différentes) ;
  - son portefeuille actuel et sa performance face à l'indice mondial.
L'IA choisit alors la répartition, levier et paris à la baisse compris, en expliquant pourquoi.

Garde-fous (l'IA ne peut pas les contourner) : placements de l'univers uniquement, 60 % au plus sur un
seul titre, 50 % au plus en levier ou à la baisse, stop-loss par position entre deux réflexions.
L'IA tourne sur ce PC (Ollama) : gratuit. Si elle est indisponible, le robot garde ses positions.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from bourse.config import db_path, load_settings
from bourse.database import connect
from bourse.execution.broker import SELL
from bourse.execution.paper_broker import PENDING

from .base import CASH_BUFFER, Strategy

log = logging.getLogger(__name__)

OLLAMA = "http://localhost:11434"
THEMES = ["perspectives marchés actions récession", "inflation taux banques centrales",
          "or dollar refuge", "dette publique obligations"]

SYSTEM = """Tu es le gérant du « Robot Éco », un portefeuille d'argent FICTIF (paper trading, cours réels) \
dont le but est de battre l'indice mondial. Tu reçois un dossier : climat du marché, placements disponibles, \
alertes, avis d'économistes d'écoles différentes, ton portefeuille. Décide la répartition cible.

Règles : uniquement les tickers de la liste ; parts entre 0 et 0.6 ; somme des parts ≤ 0.99 (le reste en liquide) ; \
levier et paris à la baisse autorisés (au plus 0.5 au total) mais seulement avec une vraie conviction. \
Les frais coûtent : ne change pas tout sans raison nette. Raisonne sur les faits du dossier, confronte les avis \
des économistes aux chiffres, et explique tes choix en français, en phrases courtes."""

SCHEMA = {"type": "object", "required": ["analyse", "allocation", "raisons", "conviction"], "properties": {
    "analyse": {"type": "string", "description": "Lecture de la situation en 3 à 5 phrases"},
    "allocation": {"type": "array", "items": {"type": "object", "required": ["ticker", "part"], "properties": {
        "ticker": {"type": "string"}, "part": {"type": "number"}}}},
    "raisons": {"type": "array", "items": {"type": "string"}},
    "conviction": {"type": "string", "enum": ["faible", "moyenne", "forte"]}}}


class EcoStrategy(Strategy):
    name = "eco"
    description = ("Boosté à l'IA : lit le climat du marché, les alertes et les économistes, puis choisit "
                   "lui-même sa répartition (levier compris), avec des garde-fous stricts.")

    # ----- le dossier donné à l'IA -----

    def dossier(self, broker, state: dict) -> str:
        v = self.view
        lines = [f"Date : {datetime.now():%d/%m/%Y %H:%M}. {v.headline()}", "", "Signaux du marché :"]
        lines += [f"- {f.name} : {f.value * 100:+.0f}/100 — {f.detail}" for f in v.factors]
        lines += ["", "Placements disponibles (ticker | nom | famille | levier | 5j % | 1m % | 3m % | 6m % | "
                      "au-dessus moy. 200j | RSI | vs plus haut 1 an %) :"]
        for a in sorted(v.assets.values(), key=lambda a: (a.family, a.leverage)):
            lines.append(f"{a.ticker} | {a.name} | {a.family} | {a.leverage} | {a.ret_5d:+.1f} | {a.ret_1m:+.1f} | "
                         f"{a.ret_3m:+.1f} | {a.ret_6m:+.1f} | {'oui' if a.above_ma200 else 'non'} | {a.rsi:.0f} | "
                         f"{a.drawdown:+.1f}")
        alerts = state.get("alertes", [])[-8:]
        lines += ["", "Alertes récentes :"] + ([f"- {a}" for a in alerts] or ["- aucune"])
        lines += ["", "Ce qu'en disent les économistes (extraits de la bibliothèque) :"] + self.economists(alerts)
        total = broker.total_value()
        held = broker.positions()
        lines += ["", f"Ton portefeuille : {total:,.0f} € dont {broker.cash() / total:.0%} de liquidités.".replace(",", " ")]
        lines += [f"- {p['ticker']} : {p['valeur_eur'] / total:.0%} (gain {p['gain_pct']:+.1f} %)" for p in held]
        if state.get("derniere_analyse"):
            lines += ["", f"Ta dernière analyse ({state.get('derniere_reflexion', '')[:16]}) : {state['derniere_analyse']}"]
        return "\n".join(lines)

    def economists(self, alerts: list[str]) -> list[str]:
        """Passages de la bibliothèque sur les grands thèmes du moment (et sur les alertes)."""
        from bourse.eco import bibliotheque
        settings = load_settings()
        conn = connect(db_path(settings))
        try:
            bibliotheque.init(conn)
            seen, out = set(), []
            for query in THEMES + [a.split(" : ", 1)[-1] for a in alerts[-2:]]:
                for h in bibliotheque.search(conn, query, limit=2):
                    if h["doc_id"] in seen:
                        continue
                    seen.add(h["doc_id"])
                    out.append(f"- {h['author'] or h['source']} ({h['source']}, {(h['published'] or '')[:10]}), "
                               f"« {h['title']} » : {h['body'][:500]}")
            return out[:8] or ["- bibliothèque vide"]
        finally:
            conn.close()

    # ----- l'appel à l'IA -----

    def ask_ai(self, dossier: str) -> dict:
        model = self.params.get("modele", "qwen3:8b")
        resp = requests.post(f"{OLLAMA}/api/chat", timeout=(5, 420), json={
            "model": model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"num_ctx": 16384, "temperature": 0.3},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": dossier}]})
        resp.raise_for_status()
        return json.loads(resp.json()["message"]["content"])

    def safe_targets(self, decision: dict) -> dict[str, float]:
        """Applique les garde-fous à la répartition proposée par l'IA."""
        p, universe = self.params, self.view.assets
        targets: dict[str, float] = {}
        for item in decision.get("allocation", []):
            ticker = str(item.get("ticker", "")).strip().upper()
            if ticker in universe:
                targets[ticker] = targets.get(ticker, 0) + max(0.0, min(float(item.get("part", 0)), p["part_max"]))
        levered = sum(w for t, w in targets.items() if universe[t].leverage != 1)
        if levered > p["levier_max"]:
            self.think(f"Garde-fou : l'IA voulait {levered:.0%} en levier/baisse, ramené à {p['levier_max']:.0%}.")
            for t in targets:
                if universe[t].leverage != 1:
                    targets[t] *= p["levier_max"] / levered
        total = sum(targets.values())
        if total > 1 - CASH_BUFFER:
            targets = {t: w * (1 - CASH_BUFFER) / total for t, w in targets.items()}
        return {t: round(w, 3) for t, w in targets.items() if w >= 0.02}

    # ----- événements -----

    def on_start(self, broker, state):
        state["force"] = True
        self.note("Démarrage : je prépare mon premier dossier pour l'IA.")

    def on_alert(self, alert, broker, state):
        now = datetime.now(timezone.utc)
        state.setdefault("alertes", []).append(f"{alert.level} {alert.score}/100 ({now:%d/%m %H:%M}) : {alert.title}")
        state["alertes"] = state["alertes"][-15:]
        if alert.level == "FORTE" and self.freshness(alert, now) > 0:
            state["force"] = True
            self.note(f"{self.describe(alert, now)} : je refais mon analyse tout de suite.")

    def stop_losses(self, broker, state) -> None:
        """Entre deux réflexions : une position qui chute trop est coupée, sans attendre l'IA."""
        stop = self.params["stop_perte"]
        for p in broker.positions():
            if p["gain_pct"] <= -stop * 100 and not any(o["ticker"] == p["ticker"] for o in broker.orders(PENDING)):
                broker.place_order(p["ticker"], SELL, reason=f"Stop-loss ({p['gain_pct']:+.1f} %)")
                self.note(f"Stop-loss sur {p['ticker']} ({p['gain_pct']:+.1f} %) : je coupe et je réfléchirai "
                          "de nouveau au prochain passage.")
                state["force"] = True

    def on_cycle(self, broker, state):
        now = datetime.now(timezone.utc)
        self.think_climate()
        if self.view is None:
            return
        self.stop_losses(broker, state)
        last = state.get("derniere_reflexion")
        due = not last or now - datetime.fromisoformat(last) >= timedelta(hours=self.params["reflexion_heures"])
        if not (due or state.get("force")) or broker.orders(PENDING):
            for line in state.get("pensee", []):
                self.think(line)
            if last:
                self.think(f"Prochaine réflexion approfondie vers "
                           f"{datetime.fromisoformat(last) + timedelta(hours=self.params['reflexion_heures']):%d/%m %H:%M} UTC.")
            return
        try:
            decision = self.ask_ai(self.dossier(broker, state))
        except Exception as exc:
            log.warning("IA du Robot Éco indisponible : %s", exc)
            self.think(f"⚠️ Mon IA ne répond pas (Ollama est-il lancé ?) : je garde mes positions. ({exc})")
            return
        targets = self.safe_targets(decision)
        state["derniere_reflexion"] = now.isoformat()
        state["derniere_analyse"] = decision.get("analyse", "")
        state["force"] = False
        state["pensee"] = ([f"🧠 {decision.get('analyse', '')}", f"Conviction : {decision.get('conviction', '?')}"]
                           + [f"• {r}" for r in decision.get("raisons", [])[:6]]
                           + [f"Répartition voulue : {self.describe_targets(targets, self.view) or 'tout en liquide'}"])
        for line in state["pensee"]:
            self.think(line)
        why = f"Décision de l'IA (conviction {decision.get('conviction', '?')})"
        if self.rebalance(broker, targets, why=why, tolerance=self.params["tolerance"]):
            self.note(f"{decision.get('analyse', '')} → je réajuste : {self.describe_targets(targets, self.view)}.")
        else:
            self.note("Après analyse, mon portefeuille est déjà proche de ce que je veux : je ne bouge pas.")
