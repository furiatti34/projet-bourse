"""Une simulation dans le passé : les robots revivent une période, quart d'heure par quart d'heure.

Chaque simulation a sa propre base (data/simulations/…) : la course du présent n'est jamais touchée.
Tout le reste est le code du présent, inchangé : mêmes robots, même analyse du marché, même courtier,
même calcul des alertes. Seuls changent l'heure (bourse.clock), la source des cours
(bourse.backtest.marche) et la source des actualités (bourse.backtest.actualites).

Déroulement, toutes les 15 minutes simulées (comme la veille automatique du présent) :
  1. les articles devenus visibles entrent dans la base de la simulation ;
  2. la veille regroupe et note les articles des dernières 24 h → alertes éventuelles ;
  3. chaque robot fait son passage (analyse du marché, alertes, décisions) ;
  4. les ordres en attente sont exécutés au premier cours publié après leur création.

Lancement : python -m bourse.backtest.simulation <fichier .db de la simulation>
"""
import json
import logging
import sqlite3
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bourse import clock
from bourse.alerts.engine import _recent_alert_tokens
from bourse.alerts.market import market_moves
from bourse.alerts.scoring import cluster, same_story, score_story, tokens
from bourse.analysis.market_view import MarketView, asset_view, climate_factors, download_closes, news_climate
from bourse.config import PROJECT_ROOT, load_settings
from bourse.data import prices
from bourse.database import connect
from bourse.execution.cycle import (benchmark_value, ensure_portfolios, portfolio_configs, register_names,
                                    run_robot, write_journal)
from bourse.execution.paper_broker import PaperBroker

from . import actualites
from .marche import build_market

log = logging.getLogger(__name__)

SIM_DIR = PROJECT_ROOT / "data" / "simulations"
STEP = timedelta(minutes=15)
ROBOTS_ALLOWED = ("prudent", "opportuniste", "audacieux", "kamikaze")   # Éco : plus tard

INFO_SCHEMA = "CREATE TABLE IF NOT EXISTS simulation (cle TEXT PRIMARY KEY, valeur TEXT)"


# ---------------------------------------------------------------- informations de la simulation

def set_info(conn: sqlite3.Connection, **values) -> None:
    conn.executemany("INSERT OR REPLACE INTO simulation (cle, valeur) VALUES (?, ?)",
                     [(k, json.dumps(v, ensure_ascii=False, default=str)) for k, v in values.items()])
    conn.commit()


def get_info(conn: sqlite3.Connection) -> dict:
    conn.execute(INFO_SCHEMA)
    return {r[0]: json.loads(r[1]) for r in conn.execute("SELECT cle, valeur FROM simulation")}


def open_sim(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")   # base jetable : la vitesse compte plus que la sécurité
    conn.execute(INFO_SCHEMA)
    return conn


def sim_settings(settings: dict, robots: list[str]) -> dict:
    """Les réglages du présent, avec seulement les robots choisis (Éco et le portefeuille manuel exclus)."""
    s = json.loads(json.dumps(settings))
    s["paper_trading"]["portefeuilles"] = [p for p in s["paper_trading"]["portefeuilles"]
                                           if str(p.get("strategie")).removeprefix("labo_") in ROBOTS_ALLOWED
                                           and p["nom"] in robots]
    return s


def create(start: date, end: date, robots: list[str], settings: dict | None = None, folder: Path | None = None,
           name: str | None = None, download_news: bool = True) -> Path:
    """Prépare une nouvelle simulation (elle démarre avec `launch`).
    `settings` déjà préparés (ex. version labo des robots) : utilisés tels quels.
    download_news=False : n'utilise que les actualités déjà téléchargées (tests du labo)."""
    folder = folder or SIM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / (name or f"sim_{start:%Y%m%d}_{end:%Y%m%d}_{stamp}.db")
    path.unlink(missing_ok=True)
    conn = open_sim(path)
    set_info(conn, debut=start.isoformat(), fin=end.isoformat(), robots=robots, statut="en_attente",
             progression=0.0, message="En attente du démarrage", cree_le=datetime.now(timezone.utc).isoformat(),
             reglages=sim_settings(settings or load_settings(), robots), ecarts=[],
             telecharger_actualites=download_news)
    conn.close()
    return path


def launch(path: Path) -> None:
    """Lance la simulation dans un programme séparé (l'interface reste libre)."""
    import os
    import subprocess
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    log_file = open(path.with_suffix(".log"), "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(python if python.exists() else sys.executable), "-m", "bourse.backtest.simulation",
                      str(path)], cwd=PROJECT_ROOT, stdout=log_file, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                     env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONIOENCODING": "utf-8"})


def list_simulations() -> list[tuple[Path, dict]]:
    result = []
    for path in sorted(SIM_DIR.glob("sim_*.db"), reverse=True):
        try:
            conn = sqlite3.connect(path, timeout=5)
            info = get_info(conn)
            conn.close()
            result.append((path, info))
        except sqlite3.Error:
            continue
    return result


# ---------------------------------------------------------------- courtier plus rapide

class SimBroker(PaperBroker):
    """Le même courtier fictif, qui garde en mémoire ses positions entre deux ordres exécutés
    (en quelques années, un robot passe des milliers d'ordres : tout relire à chaque calcul serait trop lent)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cash = self._holdings = None

    def _forget(self) -> None:
        self._cash = self._holdings = None

    def _execute(self, order, price, when):
        self._forget()
        try:
            return super()._execute(order, price, when)
        finally:
            self._forget()   # l'exécution a lu l'ancien solde : on l'oublie une fois l'ordre enregistré

    def cash(self) -> float:
        if self._cash is None:
            self._cash = super().cash()
        return self._cash

    def holdings(self):
        if self._holdings is None:
            self._holdings = super().holdings()
        return {t: type(h)(h.ticker, h.quantity, h.cost_eur) for t, h in self._holdings.items()}


# ---------------------------------------------------------------- la veille, à l'heure simulée

class SimWatch:
    """Même chaîne que bourse.alerts.engine.run_watch, sans téléchargement ni notification :
    articles des dernières 24 h → sujets → score → alertes (au plus 3 par passage, pas de doublon sur 48 h)."""

    def __init__(self, settings: dict):
        self.cfg = settings["veille"]
        self.visible: list = []           # articles déjà publiés (Article)
        self.native = self.cfg["langue_cible"]

    def add(self, articles) -> None:
        self.visible += articles

    def run(self, conn: sqlite3.Connection) -> list[str]:
        now = clock.now()
        window = now - timedelta(hours=self.cfg["fenetre_heures"])
        self.visible = [a for a in self.visible if a.published >= window - timedelta(hours=1)]
        recent = list({a.id: a for a in self.visible if a.published >= window}.values())
        if not recent:
            return []
        try:
            moves = market_moves(self.cfg["indicateurs_marche"])
        except Exception:
            moves = {}
        stories = sorted((score_story(s, moves, self.cfg["seuil_alerte_forte"], self.cfg["seuil_alerte"])
                          for s in cluster(recent)), key=lambda s: -s.score)
        already = _recent_alert_tokens(conn, hours=48)
        raised = []
        for story in stories:
            if len(raised) >= self.cfg["max_alertes_par_passage"] or not story.level:
                break
            if any(same_story(tokens(a.title), t) for a in story.articles for t in already):
                continue
            # Titre en français s'il existe un article français sur le sujet (pas de traduction automatique ici)
            native = [a for a in story.articles if a.lang == self.native]
            title_fr = native[0].title if native else story.lead.title
            moves_json = [{"ticker": m.ticker, "name": m.name, "change_pct": m.change_pct, "zscore": m.zscore}
                          for m in story.moves]
            conn.execute(
                "INSERT INTO alerts (created, published, score, level, title, title_fr, url, sources, reasons,"
                " severe_terms, moves, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (now.isoformat(), story.published.isoformat(), story.score, story.level, story.lead.title,
                 title_fr, story.lead.url, ", ".join(story.sources), " | ".join(story.reasons),
                 json.dumps(story.severe_terms), json.dumps(moves_json, ensure_ascii=False)))
            already.append(tokens(story.lead.title))
            raised.append(f"ALERTE {story.level} {story.score}/100 : {title_fr}")
        conn.commit()
        return raised


# ---------------------------------------------------------------- la simulation

def _grid(start: datetime, end: datetime) -> list[datetime]:
    steps, t = [], start
    while t <= end:
        steps.append(t)
        t += STEP
    return steps


def _ceil(t: datetime) -> datetime:
    base = t.replace(minute=0, second=0, microsecond=0)
    while base < t:
        base += STEP
    return base


def run(path: Path) -> None:
    conn = open_sim(path)
    info = get_info(conn)
    settings = info["reglages"]
    start_day, end_day = date.fromisoformat(info["debut"]), date.fromisoformat(info["fin"])
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=timezone.utc)
    end = min(datetime(end_day.year, end_day.month, end_day.day, 23, 45, tzinfo=timezone.utc),
              datetime.now(timezone.utc) - timedelta(hours=1))
    t_begin = time.monotonic()
    ecarts: list[str] = []

    def status(**values):
        set_info(conn, **values)

    if any(str(p.get("strategie", "")).startswith("labo_") for p in settings["paper_trading"]["portefeuilles"]):
        from bourse import labo
        labo.register()
    # 1. Les cours
    status(statut="preparation", message="Chargement des cours historiques…")
    market = build_market(settings)
    ecarts += [f"Cours corrigé : {n}" for n in market.notes]
    missing = [u["nom"] for u in settings["analyse"]["univers"] if not market.listed_at(u["ticker"], start)]
    if missing:
        ecarts.append(f"{len(missing)} placements n'existaient pas encore au départ (ils apparaîtront à leur "
                      f"création) : {', '.join(missing)}")
    prices.set_provider(market)
    register_names(settings)

    # 2. Les actualités (Google et communiqués officiels : téléchargés s'il en manque)
    news_from = start_day - timedelta(days=9)   # l'analyse compare au ton des 8 jours précédents
    archive = actualites.connect_archive()
    status(message="Téléchargement des actualités d'époque manquantes…")
    last_say = [0.0]

    def say(text):
        if time.monotonic() - last_say[0] > 2:
            status(message=f"Actualités d'époque : {text}")
            last_say[0] = time.monotonic()
    if info.get("telecharger_actualites", True):
        actualites.prefetch(settings, news_from, end_day, kinds=("officiel", "google"), progress=say)
    coverage = actualites.coverage_report(archive, settings, start_day, end_day)
    status(couverture=coverage)

    # 3. Départ
    clock.set_simulated(start)
    watch = SimWatch(settings)
    before = actualites.articles_between(archive, datetime(1990, 1, 1, tzinfo=timezone.utc), start)
    before = [(a, seen) for a, seen in before if seen >= datetime.combine(news_from, datetime.min.time(), timezone.utc)]
    _save_articles(conn, before)
    watch.add([a for a, _ in before])
    portfolios = {p["name"]: p for p in ensure_portfolios(conn, settings)}
    configs = portfolio_configs(settings)
    fees = settings["paper_trading"]["frais"]
    brokers = {name: SimBroker(conn, p["id"], fees) for name, p in portfolios.items()}

    # quand la veille doit tourner : nouveaux articles, ou nouveaux cours des indicateurs surveillés
    indicators = [i["ticker"] for i in settings["veille"]["indicateurs_marche"]]
    watch_times = {_ceil(t) for t in market.event_times(indicators, start, end)}
    snapshot_times = {_ceil(t) for t in market.event_times([settings["general"]["indice_reference"]], start, end)}

    steps = _grid(start, end)
    status(statut="en_cours", message="Les robots sont au travail", ecarts=ecarts, heure_simulee=start.isoformat())
    last_seen = start
    view_cache: dict = {}
    n_alerts = 0
    for i, t in enumerate(steps):
        clock.set_simulated(t)
        # 1. nouveaux articles visibles
        new = actualites.articles_between(archive, last_seen, t)
        last_seen = t
        if new:
            _save_articles(conn, new)
            watch.add([a for a, _ in new])
        # 2. la veille
        alerts = watch.run(conn) if (new or t in watch_times) else []
        n_alerts += len(alerts)
        # 3. les robots (tous les quarts d'heure, comme au présent)
        view = _view(settings, conn, view_cache)
        for name, p in portfolios.items():
            row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (p["id"],)).fetchone()
            broker = brokers[name]
            try:
                notes = run_robot(conn, row, broker, configs[name], view)
            except Exception as exc:
                log.exception("Robot %s en erreur", name)
                notes = [f"Erreur du robot : {exc}"]
            notes += broker.process_pending()
            if notes:
                write_journal(conn, p["id"], notes)
            if t in snapshot_times or i == 0 or i == len(steps) - 1:
                _snapshot(conn, row, broker)
        # 4. avancement (et arrêt demandé depuis l'interface ?)
        if i % 96 == 0 or i == len(steps) - 1:
            stop = conn.execute("SELECT valeur FROM simulation WHERE cle = 'statut'").fetchone()
            if stop and json.loads(stop[0]) == "arret_demande":
                status(statut="arretee", message=f"Arrêtée à la demande le {t:%d/%m/%Y}", heure_simulee=t.isoformat(),
                       duree_s=time.monotonic() - t_begin)
                return
            status(progression=(i + 1) / len(steps), heure_simulee=t.isoformat(), alertes=n_alerts,
                   duree_s=time.monotonic() - t_begin)
    status(statut="terminee", progression=1.0, message="Simulation terminée", heure_simulee=end.isoformat(),
           alertes=n_alerts, duree_s=time.monotonic() - t_begin)


def _save_articles(conn, articles) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO articles (id, title, url, source, published, summary, lang, first_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(a.id, a.title, a.url, a.source, a.published.isoformat(), a.summary, a.lang, seen.isoformat())
         for a, seen in articles])
    conn.commit()


def _view(settings, conn, cache: dict):
    """L'analyse du marché, exactement comme bourse.analysis.build_view, refaite à CHAQUE passage.
    Seule optimisation : l'étude de chaque placement (moyennes, RSI…) n'est refaite que si un cours a changé."""
    cfg = settings["analyse"]
    roles = cfg["climat"]
    now = clock.now()
    try:
        tickers = cache.get("tickers") or list(dict.fromkeys([u["ticker"] for u in cfg["univers"]]
                                                             + list(roles.values())))
        cache["tickers"] = tickers
        closes = download_closes(tickers)
        key = (closes.index[-1], tuple(closes.iloc[-1].fillna(-1.0)), tuple(closes.iloc[-2].fillna(-1.0)), len(closes))
        if cache.get("key") != key:
            assets = {}
            for info in cfg["univers"]:
                if info["ticker"] in closes:
                    view = asset_view(info["ticker"], info, closes[info["ticker"]])
                    if view:
                        assets[info["ticker"]] = view
            cache["key"], cache["assets"] = key, assets
        assets = cache["assets"]
        news = news_climate(conn, now)
        return MarketView(now, assets, climate_factors(closes, roles, assets, news, now), news)
    except Exception:
        log.exception("Analyse du marché impossible")
        return None


def _snapshot(conn, portfolio, broker) -> None:
    conn.execute(
        "INSERT INTO snapshots (portfolio_id, time, value_eur, cash_eur, benchmark_eur) VALUES (?, ?, ?, ?, ?)",
        (portfolio["id"], clock.now().isoformat(), broker.total_value(), broker.cash(), benchmark_value(portfolio)))
    conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sim_path = Path(sys.argv[1])
    try:
        run(sim_path)
    except Exception as exc:
        traceback.print_exc()
        c = open_sim(sim_path)
        set_info(c, statut="erreur", message=f"Erreur : {exc}")
