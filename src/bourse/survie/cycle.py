"""Un passage du Robot Éco Survie, et le rapport qui le compare au Robot Éco.

Sa propre base (data/survie/survie.db) ; la base principale n'est ouverte qu'en lecture seule.
"""
import json
import logging
import sqlite3

from bourse import clock
from bourse.alerts.events import last_alert_id, new_alerts
from bourse.analysis import build_view
from bourse.config import db_path
from bourse.data.prices import quote
from bourse.database import connect
from bourse.execution.cycle import benchmark_value, register_names
from bourse.execution.paper_broker import FILLED, PaperBroker

from .robot import BASE, DOSSIER, NOM, SURVIE, EcoSurvieStrategy

log = logging.getLogger(__name__)


def lecture_seule(settings: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path(settings)}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def parametres(settings: dict) -> dict:
    """Exactement les réglages du Robot Éco (relus à chaque passage), plus ceux du mode survie."""
    eco = next(p for p in settings["paper_trading"]["portefeuilles"] if p.get("strategie") == "eco")
    return {**eco.get("parametres", {}), **SURVIE}


def portefeuille(conn: sqlite3.Connection, settings: dict) -> sqlite3.Row:
    if not conn.execute("SELECT 1 FROM portfolios WHERE name = ?", (NOM,)).fetchone():
        bench = settings["general"]["indice_reference"]
        conn.execute("INSERT INTO portfolios (name, strategy, initial_cash, created, benchmark, benchmark_start)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (NOM, EcoSurvieStrategy.name, settings["general"]["capital_fictif"], clock.now().isoformat(),
                      bench, quote(bench).price_eur))
        conn.commit()
    return conn.execute("SELECT * FROM portfolios WHERE name = ?", (NOM,)).fetchone()


def run(settings: dict) -> list[str]:
    DOSSIER.mkdir(parents=True, exist_ok=True)
    register_names(settings)
    principale = lecture_seule(settings)
    conn = connect(BASE)
    try:
        try:
            view = build_view(settings, principale)
        except Exception:
            log.exception("Analyse du marché impossible")
            view = None
        pf = portefeuille(conn, settings)
        broker = PaperBroker(conn, pf["id"], settings["paper_trading"]["frais"])
        strategy = EcoSurvieStrategy(parametres(settings), view)
        state = json.loads(pf["state"] or "{}")
        state.setdefault("last_alert_id", last_alert_id(principale))   # pas d'alertes d'avant sa naissance
        if "started" not in state:
            strategy.on_start(broker, state)
            state["started"] = clock.now().isoformat()
        try:
            for alert in new_alerts(principale, state["last_alert_id"]):
                if not state.get("mort"):
                    strategy.on_alert(alert, broker, state)
                state["last_alert_id"] = alert.id
            strategy.on_cycle(broker, state)
        except Exception as exc:
            log.exception("Robot Éco Survie en erreur")
            strategy.note(f"Erreur du robot : {exc}")
        state["reflexion"] = {"time": clock.now().isoformat(), "lines": strategy.thoughts}
        conn.execute("UPDATE portfolios SET state = ? WHERE id = ?", (json.dumps(state, ensure_ascii=False), pf["id"]))
        conn.commit()
        notes = strategy.notes + broker.process_pending()
        now = clock.now().isoformat()
        conn.executemany("INSERT INTO journal (portfolio_id, time, message) VALUES (?, ?, ?)",
                         [(pf["id"], now, m) for m in notes])
        try:   # la « valeur » d'Éco Survie est son solde : portefeuille moins ce qu'il a payé pour réfléchir
            conn.execute("INSERT INTO snapshots (portfolio_id, time, value_eur, cash_eur, benchmark_eur)"
                         " VALUES (?, ?, ?, ?, ?)",
                         (pf["id"], now, broker.total_value() - state.get("couts_reflexion", 0.0), broker.cash(),
                          benchmark_value(pf)))
        except Exception as exc:
            log.warning("Photo du portefeuille impossible : %s", exc)
        conn.commit()
        return notes
    finally:
        conn.close()
        principale.close()


# ---------- comparaison avec le Robot Éco ----------

def _bilan(conn: sqlite3.Connection, pid: int, depuis: str) -> dict | None:
    snaps = conn.execute("SELECT time, value_eur, benchmark_eur FROM snapshots WHERE portfolio_id = ? AND time >= ?"
                         " ORDER BY time", (pid, depuis)).fetchall()
    if len(snaps) < 1:
        return None
    first, last = snaps[0], snaps[-1]
    ordres = conn.execute("SELECT COUNT(*), COALESCE(SUM(fees_eur), 0) FROM orders WHERE portfolio_id = ? AND status = ?"
                          " AND filled_at >= ?", (pid, FILLED, depuis)).fetchone()
    return {"debut": first["value_eur"], "fin": last["value_eur"], "perf": last["value_eur"] / first["value_eur"] - 1,
            "indice": last["benchmark_eur"] / first["benchmark_eur"] - 1, "ordres": ordres[0], "frais": ordres[1],
            "le": last["time"]}


def rapport(settings: dict) -> str:
    if not BASE.exists():
        return "Le Robot Éco Survie n'a pas encore fait son premier passage."
    conn = connect(BASE)
    principale = lecture_seule(settings)
    try:
        pf = conn.execute("SELECT * FROM portfolios WHERE name = ?", (NOM,)).fetchone()
        state = json.loads(pf["state"] or "{}")
        depuis = pf["created"]
        survie = _bilan(conn, pf["id"], depuis)
        eco_row = principale.execute("SELECT id FROM portfolios WHERE strategy = 'eco'").fetchone()
        eco = _bilan(principale, eco_row["id"], depuis) if eco_row else None
        pauses = state.get("pauses", [])
        lignes = [f"# Robot Éco Survie — bilan depuis le {depuis[:16].replace('T', ' ')} UTC", ""]
        if state.get("mort"):
            lignes.append(f"💀 **Mort le {state['mort'][:16].replace('T', ' ')} UTC.**")
        lignes += [f"- Réflexions payées : {state.get('nb_reflexions', 0)} pour {state.get('couts_reflexion', 0):.0f} € "
                   f"(pause moyenne choisie : {sum(pauses) / len(pauses):.0f} h)" if pauses else
                   f"- Réflexions payées : {state.get('nb_reflexions', 0)}",
                   f"- Seuil de mort : {SURVIE['seuil_mort']:,} €".replace(",", " "), "",
                   "| | Éco Survie | Robot Éco | Indice mondial |", "|---|---|---|---|"]
        def cell(b, cle, fmt):
            return fmt(b[cle]) if b else "—"
        pct = lambda x: f"{x * 100:+.2f} %"
        lignes += [f"| Performance sur la période | {cell(survie, 'perf', pct)} | {cell(eco, 'perf', pct)} | "
                   f"{cell(survie, 'indice', pct)} |",
                   f"| Ordres exécutés | {cell(survie, 'ordres', str)} | {cell(eco, 'ordres', str)} | |",
                   f"| Frais de courtage | {cell(survie, 'frais', lambda x: f'{x:.0f} €')} | "
                   f"{cell(eco, 'frais', lambda x: f'{x:.0f} €')} | |"]
        lignes += ["", "Éco Survie démarre à 100 000 € tout neufs, le Robot Éco garde son portefeuille existant : on "
                   "compare l'évolution en % sur la même période, pas les montants."]
        return "\n".join(lignes)
    finally:
        conn.close()
        principale.close()
