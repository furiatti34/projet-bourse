"""Un passage de paper trading : stratégies → exécution des ordres → journal et photo du portefeuille."""
import json
import logging
import sqlite3
from datetime import datetime

from bourse import clock
from bourse.alerts.events import last_alert_id, new_alerts
from bourse.analysis import MarketView, build_view
from bourse.config import db_path
from bourse.data.prices import KNOWN_NAMES, quote
from bourse.database import connect
from bourse.strategies import STRATEGIES

from .paper_broker import PaperBroker

log = logging.getLogger(__name__)


def register_names(settings: dict) -> None:
    """Noms en clair des placements de l'analyse (affichés dans l'interface)."""
    KNOWN_NAMES.update({u["ticker"]: u["nom"] for u in settings.get("analyse", {}).get("univers", [])})


def portfolio_configs(settings: dict) -> dict[str, dict]:
    return {p["nom"]: p for p in settings["paper_trading"]["portefeuilles"]}


def ensure_portfolios(conn: sqlite3.Connection, settings: dict) -> list[sqlite3.Row]:
    """Crée les portefeuilles du fichier de réglages s'ils n'existent pas encore."""
    benchmark = settings["general"]["indice_reference"]
    for p in settings["paper_trading"]["portefeuilles"]:
        exists = conn.execute("SELECT 1 FROM portfolios WHERE name = ?", (p["nom"],)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO portfolios (name, strategy, initial_cash, created, benchmark, benchmark_start)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (p["nom"], p.get("strategie"), settings["general"]["capital_fictif"],
                 clock.now().isoformat(), benchmark, quote(benchmark).price_eur),
            )
        else:  # le fichier de réglages fait foi : on peut changer le caractère d'un robot existant
            conn.execute("UPDATE portfolios SET strategy = ? WHERE name = ?", (p.get("strategie"), p["nom"]))
    conn.commit()
    names = list(portfolio_configs(settings))
    rows = conn.execute("SELECT * FROM portfolios").fetchall()
    return sorted((r for r in rows if r["name"] in names), key=lambda r: names.index(r["name"]))


def make_broker(conn: sqlite3.Connection, portfolio: sqlite3.Row, settings: dict) -> PaperBroker:
    return PaperBroker(conn, portfolio["id"], settings["paper_trading"]["frais"])


def benchmark_value(portfolio: sqlite3.Row) -> float:
    """Ce que vaudrait le capital de départ s'il avait simplement été placé dans l'indice."""
    return portfolio["initial_cash"] * quote(portfolio["benchmark"]).price_eur / portfolio["benchmark_start"]


def write_journal(conn: sqlite3.Connection, portfolio_id: int, messages: list[str]) -> None:
    now = clock.now().isoformat()
    conn.executemany("INSERT INTO journal (portfolio_id, time, message) VALUES (?, ?, ?)",
                     [(portfolio_id, now, m) for m in messages])
    conn.commit()


def take_snapshot(conn: sqlite3.Connection, portfolio: sqlite3.Row, broker: PaperBroker) -> None:
    conn.execute(
        "INSERT INTO snapshots (portfolio_id, time, value_eur, cash_eur, benchmark_eur) VALUES (?, ?, ?, ?, ?)",
        (portfolio["id"], clock.now().isoformat(), broker.total_value(),
         broker.cash(), benchmark_value(portfolio)),
    )
    conn.commit()


def run_robot(conn: sqlite3.Connection, portfolio: sqlite3.Row, broker: PaperBroker, config: dict,
              view: MarketView | None = None) -> list[str]:
    strategy = STRATEGIES[portfolio["strategy"]](config.get("parametres", {}), view)
    state = json.loads(portfolio["state"] or "{}")
    # Un robot tout neuf ne réagit pas aux alertes d'avant sa naissance
    state.setdefault("last_alert_id", last_alert_id(conn))
    if "started" not in state:
        strategy.on_start(broker, state)
        state["started"] = clock.now().isoformat()
    for alert in new_alerts(conn, state["last_alert_id"]):
        try:
            strategy.on_alert(alert, broker, state)
        except Exception as exc:   # une alerte illisible ne doit pas bloquer le robot à chaque passage
            log.exception("Robot %s : erreur sur l'alerte %s", portfolio["name"], alert.id)
            strategy.note(f"Erreur en traitant l'alerte « {alert.title} » : {exc}")
        state["last_alert_id"] = alert.id
    strategy.on_cycle(broker, state)
    state["reflexion"] = {"time": clock.now().isoformat(), "lines": strategy.thoughts}
    conn.execute("UPDATE portfolios SET state = ? WHERE id = ?",
                 (json.dumps(state, ensure_ascii=False), portfolio["id"]))
    conn.commit()
    return strategy.notes


def run_trading_cycle(settings: dict) -> list[str]:
    messages = []
    configs = portfolio_configs(settings)
    register_names(settings)
    with connect(db_path(settings)) as conn:
        try:
            view = build_view(settings, conn)   # une seule analyse, partagée par tous les robots
            messages.append(f"Analyse : {len(view.assets)} placements étudiés, {view.headline()}")
        except Exception:
            log.exception("Analyse du marché impossible")
            view = None
        for portfolio in ensure_portfolios(conn, settings):
            broker = make_broker(conn, portfolio, settings)
            notes = []
            if portfolio["strategy"]:
                try:
                    notes = run_robot(conn, portfolio, broker, configs[portfolio["name"]], view)
                except Exception as exc:
                    log.exception("Robot %s en erreur", portfolio["name"])
                    notes = [f"Erreur du robot : {exc}"]
            notes += broker.process_pending()
            write_journal(conn, portfolio["id"], notes)
            messages += [f"[{portfolio['name']}] {m}" for m in notes]
            try:
                take_snapshot(conn, portfolio, broker)
            except Exception as exc:
                log.warning("Photo du portefeuille %s impossible : %s", portfolio["name"], exc)
    return messages
