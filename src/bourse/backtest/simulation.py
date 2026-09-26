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

Le Robot Éco (IA) : son IA a appris sur des textes allant jusqu'à fin 2024 environ, et elle est sortie le
29 avril 2025 : elle ne peut rien savoir d'après. Il n'est donc simulé qu'à partir du 1er mai 2025 (ECO_DEBUT),
sinon il connaîtrait déjà la suite de l'histoire. Sa bibliothèque d'économistes se remplit au fil du temps
simulé, texte par texte, à l'heure de publication. Une simulation avec Éco dure des jours (l'IA réfléchit
~2 min toutes les 4 h simulées) : elle reprend d'elle-même après une coupure (voir `resume_orphans`).

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
ROBOTS_ALLOWED = ("prudent", "opportuniste", "audacieux", "kamikaze", "eco")
# Le Robot Éco ne remonte pas avant la sortie de son IA (qwen3, 29/04/2025) : elle connaîtrait la suite.
ECO_DEBUT = date(2025, 5, 1)
ECO_MINUTES_PAR_JOUR = 6   # mesuré sur ce PC (qwen3:8b, ~2 min par réflexion) : sert à estimer la durée
LIBRARY_DB = PROJECT_ROOT / "data" / "bourse.db"   # bibliothèque d'Éco 1 du présent (lue, jamais modifiée)

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
    """Les réglages du présent, avec seulement les robots choisis (le portefeuille manuel exclu)."""
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
    settings = settings or load_settings()
    if start < ECO_DEBUT and any(p["nom"] in robots and uses_eco(p) for p in settings["paper_trading"]["portefeuilles"]):
        raise ValueError(f"Le Robot Éco ne peut pas partir avant le {ECO_DEBUT:%d/%m/%Y} : son IA connaît "
                         "déjà ce qui s'est passé avant.")
    folder = folder or SIM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / (name or f"sim_{start:%Y%m%d}_{end:%Y%m%d}_{stamp}.db")
    path.unlink(missing_ok=True)
    conn = open_sim(path)
    set_info(conn, debut=start.isoformat(), fin=end.isoformat(), robots=robots, statut="en_attente",
             progression=0.0, message="En attente du démarrage", cree_le=datetime.now(timezone.utc).isoformat(),
             reglages=sim_settings(settings, robots), ecarts=[],
             telecharger_actualites=download_news)
    conn.close()
    return path


def uses_eco(portfolio_cfg: dict) -> bool:
    return str(portfolio_cfg.get("strategie", "")).removeprefix("labo_") == "eco"


def estimated_minutes(start: date, end: date, with_eco: bool) -> float:
    """Durée de calcul estimée. Sans Éco : ~1,5 min par année. Avec Éco : ~6 min par jour simulé (son IA
    réfléchit toutes les 4 h, sauf quand des ordres attendent l'ouverture de la Bourse, comme au présent)."""
    days = max((end - start).days, 1)
    minutes = days / 365 * 1.5
    if with_eco:
        minutes += days * ECO_MINUTES_PAR_JOUR
    return minutes


def launch(path: Path) -> int:
    """Lance la simulation dans un programme séparé (l'interface reste libre). Son numéro de programme est noté
    tout de suite dans la simulation : une relance automatique ne la démarre donc jamais en double."""
    import os
    import subprocess
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    log_file = open(path.with_suffix(".log"), "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([str(python if python.exists() else sys.executable), "-m", "bourse.backtest.simulation",
                      str(path)], cwd=PROJECT_ROOT, stdout=log_file, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                     # PYTHONHASHSEED fixé : même ordre de parcours des ensembles d'un lancement à l'autre, donc
                     # une simulation relancée à l'identique passe ses ordres dans le même ordre
                     env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYTHONIOENCODING": "utf-8",
                          "PYTHONHASHSEED": "0"})
    conn = open_sim(path)
    set_info(conn, pid=proc.pid)
    conn.close()
    # surveille le retour devant le PC pour y afficher l'avancement (une seule copie tourne à la fois)
    watcher = PROJECT_ROOT / "scripts" / "surveille_retour.pyw"
    pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if sys.platform == "win32" and watcher.exists() and pythonw.exists():
        subprocess.Popen([str(pythonw), str(watcher)], cwd=PROJECT_ROOT, creationflags=flags, close_fds=True)
    return proc.pid


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


# ---------------------------------------------------------------- la bibliothèque d'Éco, au fil du temps

def library_available(published: str) -> datetime:
    """Heure à partir de laquelle un texte de la bibliothèque est lisible : son heure de publication ; si seule
    la date est connue (00:00:00), le lendemain à 00:00 UTC, pour ne jamais le montrer trop tôt."""
    t = datetime.fromisoformat(published)
    return t + timedelta(days=1) if (t.hour, t.minute, t.second) == (0, 0, 0) else t


class SimLibrary:
    """Copie la bibliothèque du présent dans la base de la simulation, texte par texte, quand il devient lisible.
    La recherche (et son classement) ne porte donc que sur ce qui existait à l'heure simulée."""

    def __init__(self, conn: sqlite3.Connection, source: Path = LIBRARY_DB):
        from bourse.eco import bibliotheque
        self.conn, self.bib = conn, bibliotheque
        bibliotheque.init(conn)
        self.docs: list[tuple[datetime, dict]] = []
        if source.exists():
            live = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
            live.row_factory = sqlite3.Row
            try:
                rows = live.execute("SELECT url, title, author, source, lang, published, text FROM eco_docs"
                                    " WHERE published IS NOT NULL").fetchall()
            except sqlite3.Error:
                rows = []
            live.close()
            self.docs = sorted(((library_available(r["published"]), dict(r)) for r in rows), key=lambda x: x[0])

    def add_until(self, t: datetime) -> int:
        """Ajoute tous les textes devenus lisibles jusqu'à `t` (ceux déjà présents sont ignorés)."""
        n = 0
        while self.docs and self.docs[0][0] <= t:
            _, d = self.docs.pop(0)
            self.bib._store(self.conn, {"nom": d["source"], "langue": d["lang"]}, d["url"], d["title"], d["author"],
                            d["published"], d["text"])
            n += 1
        if n:
            self.conn.commit()
        return n


# ---------------------------------------------------------------- archives pas encore téléchargées

ARCHIVE_WAIT_MAX = 3600   # secondes sans aucun progrès du téléchargement avant de continuer sans les archives


def archives_ready(archive: sqlite3.Connection, settings: dict, day: date) -> bool:
    """Les archives des flux (BBC, CNBC, Le Monde…) de ce jour sont-elles téléchargées ? Oui si chaque flux a
    été traité ce jour-là, ou si le téléchargement est déjà passé au jour suivant (un flux manquant est alors un
    trou définitif de l'archive, pas un retard)."""
    origins = [actualites.wayback_origin(f) for f in settings["veille"]["sources"] if not f.get("agregateur")]
    if not origins:
        return True
    marks = ",".join("?" * len(origins))
    done = archive.execute(f"SELECT COUNT(DISTINCT origin) FROM hist_coverage WHERE day = ? AND origin IN ({marks})",
                           (day.isoformat(), *origins)).fetchone()[0]
    if done == len(origins):
        return True
    return archive.execute(f"SELECT 1 FROM hist_coverage WHERE day > ? AND origin IN ({marks}) LIMIT 1",
                           (day.isoformat(), *origins)).fetchone() is not None


def wait_for_archives(archive, settings: dict, day: date, say) -> bool:
    """La simulation ne dépasse jamais le téléchargement des archives : elle l'attend tant qu'il progresse.
    Renvoie False si le téléchargement ne progresse plus (arrêté) : la simulation continue sans ces archives."""
    last_count, last_progress = None, time.monotonic()
    while not archives_ready(archive, settings, day):
        count = archive.execute("SELECT COUNT(*) FROM hist_coverage").fetchone()[0]
        if count != last_count:
            last_count, last_progress = count, time.monotonic()
        elif time.monotonic() - last_progress > ARCHIVE_WAIT_MAX:
            return False
        say(f"En attente des archives d'actualités du {day:%d/%m/%Y} (BBC, CNBC, Le Monde… : téléchargement en "
            "cours)")
        time.sleep(30)
    return True


# ---------------------------------------------------------------- reprise après une coupure

def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform != "win32":
        import os
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259   # STILL_ACTIVE


def resume_orphans() -> list[str]:
    """Relance les simulations interrompues (PC redémarré, programme fermé…). Appelé par la veille automatique."""
    relaunched = []
    for path, info in list_simulations():
        status = info.get("statut")
        if status not in ("preparation", "en_cours", "arret_demande") or _pid_alive(info.get("pid")):
            continue
        conn = open_sim(path)
        if status == "arret_demande":
            set_info(conn, statut="arretee", message="Arrêtée à la demande")
        else:
            set_info(conn, message="Reprise après une interruption…")
            launch(path)
            relaunched.append(path.name)
        conn.close()
    return relaunched


MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]


def _human(minutes: float) -> str:
    if minutes < 90:
        return f"{max(1, round(minutes))} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h"
    return f"{minutes / 60 / 24:.1f} jours".replace(".", ",")


def status_lines() -> list[str]:
    """Où en sont les simulations : lignes pour la notification Windows (démarrage, réveil, retour au PC).
    Une simulation terminée est annoncée une seule fois, avec le robot en tête."""
    lines = []
    for path, info in list_simulations():
        status = info.get("statut")
        span = f"{date.fromisoformat(info['debut']):%d/%m/%Y} → {date.fromisoformat(info['fin']):%d/%m/%Y}"
        if status in ("preparation", "en_cours", "en_attente"):
            p = float(info.get("progression") or 0)
            t = datetime.fromisoformat(info["heure_simulee"]) if info.get("heure_simulee") else None
            where = f"nous sommes le {t.day} {MOIS[t.month - 1]} {t.year}" if t else "préparation"
            spent = float(info.get("duree_s") or 0) / 60
            left = f" · reste ≈ {_human(spent * (1 - p) / p)} si le PC reste allumé" if 0.002 < p < 1 else ""
            lines.append(f"▶️ Simulation {span} : {where} · {p:.0%}{left}")
        elif status == "terminee" and not info.get("fin_annoncee"):
            conn = open_sim(path)
            try:
                best = conn.execute(
                    "SELECT p.name, s.value_eur / p.initial_cash - 1 AS perf FROM portfolios p JOIN snapshots s"
                    " ON s.portfolio_id = p.id WHERE s.rowid = (SELECT MAX(rowid) FROM snapshots WHERE portfolio_id = p.id)"
                    " ORDER BY perf DESC LIMIT 1").fetchone()
                lead = f" · en tête : {best[0]} ({best[1] * 100:+.1f} %)" if best else ""
                lines.append(f"✅ Simulation {span} terminée{lead}")
                set_info(conn, fin_annoncee=True)
            finally:
                conn.close()
        elif status == "erreur" and not info.get("fin_annoncee"):
            lines.append(f"⚠️ Simulation {span} : erreur ({info.get('message', '')[:80]})")
            conn = open_sim(path)
            set_info(conn, fin_annoncee=True)
            conn.close()
    return lines


def any_running() -> bool:
    return any(info.get("statut") in ("en_attente", "preparation", "en_cours", "arret_demande")
               for _, info in list_simulations())


def _keep_awake() -> None:
    """Empêche la mise en veille automatique du PC tant que la simulation tourne (Windows)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # CONTINUOUS | SYSTEM_REQUIRED


def _reset(conn: sqlite3.Connection) -> None:
    """Départ de zéro : efface ce qu'un démarrage interrompu aurait pu laisser."""
    for table in ("orders", "snapshots", "journal", "alerts", "articles", "portfolios", "eco_docs", "eco_fts"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass   # table pas encore créée
    conn.commit()


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
    import os
    has_eco = any(uses_eco(p) for p in settings["paper_trading"]["portefeuilles"])
    # Reprise au quart d'heure près : seulement avec Éco (sans lui, une simulation dure quelques minutes et
    # recommence simplement du début).
    resume_from = datetime.fromisoformat(info["dernier_pas"]) if has_eco and info.get("dernier_pas") else None
    timing = {"total": float(info.get("duree_s") or 0), "last": time.monotonic()}

    def worked() -> float:
        """Temps de calcul cumulé (reprises comprises). Un trou de plus d'une heure = PC en veille : non compté,
        pour que le temps restant affiché soit celui d'un PC allumé."""
        now_m = time.monotonic()
        delta, timing["last"] = now_m - timing["last"], now_m
        if delta < 3600:
            timing["total"] += delta
        return timing["total"]
    ecarts: list[str] = list(info.get("ecarts") or []) if resume_from else []
    set_info(conn, pid=os.getpid())
    _keep_awake()

    def status(**values):
        set_info(conn, **values)

    from bourse.strategies.eco import EcoStrategy
    EcoStrategy.on_wait = staticmethod(lambda text: status(message=text))
    for p in settings["paper_trading"]["portefeuilles"]:
        if uses_eco(p):   # Éco lit la bibliothèque de la simulation, jamais celle du présent
            p.setdefault("parametres", {})["bibliotheque_db"] = str(path)

    if any(str(p.get("strategie", "")).startswith("labo_") for p in settings["paper_trading"]["portefeuilles"]):
        from bourse import labo
        labo.register()
    # 1. Les cours
    status(statut="preparation", message="Chargement des cours historiques…")
    market = build_market(settings)
    ecarts += [f"Cours corrigé : {n}" for n in market.notes]
    missing = [u["nom"] for u in settings["analyse"]["univers"] if not market.listed_at(u["ticker"], start)]
    if missing and not resume_from:
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

    # 3. Départ (ou reprise là où la simulation s'était arrêtée)
    first = resume_from + STEP if resume_from else start
    if not resume_from:
        _reset(conn)
    clock.set_simulated(first)
    watch = SimWatch(settings)
    earliest = datetime.combine(news_from, datetime.min.time(), timezone.utc)
    if resume_from:   # la veille ne regarde que les dernières 24 h : on lui rend ce qu'elle avait sous les yeux
        earliest = max(earliest, first - timedelta(hours=settings["veille"]["fenetre_heures"] + 2))
        ecarts.append(f"Simulation interrompue puis reprise d'elle-même à l'heure simulée {first:%d/%m/%Y %H:%M} UTC "
                      f"(le {datetime.now():%d/%m/%Y à %H:%M}, heure réelle). Rien n'est perdu ; au pire, le dernier "
                      "quart d'heure simulé a été rejoué.")
    # Avec Éco (simulation longue), on ne dépasse jamais le téléchargement des archives des flux : chaque jour
    # simulé attend que ses archives soient là. Sans Éco (quelques minutes), on prend ce qui est déjà téléchargé.
    gate = {"days": set(), "off": not has_eco}

    def archives_for(day: date) -> None:
        if gate["off"] or day in gate["days"]:
            return
        gate["days"].add(day)
        if not wait_for_archives(archive, settings, day, lambda text: status(message=text)):
            gate["off"] = True
            ecarts.append(f"Le téléchargement des archives BBC, CNBC, Le Monde… s'est arrêté : à partir du "
                          f"{day:%d/%m/%Y}, la simulation a continué avec les seules archives déjà téléchargées.")
            status(ecarts=ecarts)
    for k in range((first.date() - earliest.date()).days + 1):
        archives_for(earliest.date() + timedelta(days=k))
    before = actualites.articles_between(archive, datetime(1990, 1, 1, tzinfo=timezone.utc), first)
    before = [(a, seen) for a, seen in before if seen >= earliest]
    _save_articles(conn, before)
    watch.add([a for a, _ in before])
    library = None
    if has_eco:
        library = SimLibrary(conn)
        library.add_until(first)
    portfolios = {p["name"]: p for p in ensure_portfolios(conn, settings)}
    configs = portfolio_configs(settings)
    fees = settings["paper_trading"]["frais"]
    brokers = {name: SimBroker(conn, p["id"], fees) for name, p in portfolios.items()}

    # quand la veille doit tourner : nouveaux articles, ou nouveaux cours des indicateurs surveillés
    indicators = [i["ticker"] for i in settings["veille"]["indicateurs_marche"]]
    watch_times = {_ceil(t) for t in market.event_times(indicators, start, end)}
    snapshot_times = {_ceil(t) for t in market.event_times([settings["general"]["indice_reference"]], start, end)}

    all_steps = _grid(start, end)
    steps = [t for t in all_steps if t >= first]
    done = len(all_steps) - len(steps)
    status(statut="en_cours", message="Les robots sont au travail", ecarts=ecarts, heure_simulee=first.isoformat())
    last_seen = first
    view_cache: dict = {}
    n_alerts = int(info.get("alertes") or 0) if resume_from else 0
    for i, t in enumerate(steps, start=done):
        clock.set_simulated(t)
        archives_for(t.date())
        # 1. nouveaux articles visibles (et nouveaux textes d'économistes)
        new = actualites.articles_between(archive, last_seen, t)
        last_seen = t
        if new:
            _save_articles(conn, new)
            watch.add([a for a, _ in new])
        if library:
            library.add_until(t)
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
            if t in snapshot_times or i == 0 or i == len(all_steps) - 1:
                _snapshot(conn, row, broker)
        # 4. avancement (et arrêt demandé depuis l'interface ?). Avec Éco, un passage peut durer des minutes :
        # l'état est alors enregistré à chaque passage (reprise possible au quart d'heure près).
        if library or i % 96 == 0 or i == len(all_steps) - 1:
            stop = conn.execute("SELECT valeur FROM simulation WHERE cle = 'statut'").fetchone()
            if stop and json.loads(stop[0]) == "arret_demande":
                status(statut="arretee", message=f"Arrêtée à la demande le {t:%d/%m/%Y}", heure_simulee=t.isoformat(),
                       duree_s=worked(), dernier_pas=t.isoformat())
                return
            status(progression=(i + 1) / len(all_steps), heure_simulee=t.isoformat(), alertes=n_alerts,
                   duree_s=worked(), dernier_pas=t.isoformat(), message="Les robots sont au travail")
    status(statut="terminee", progression=1.0, message="Simulation terminée", heure_simulee=end.isoformat(),
           alertes=n_alerts, duree_s=worked())
    if has_eco:   # simulation longue : on prévient qu'elle est finie
        try:
            from bourse.alerts.notifier import notify
            notify(["🧠 Projet Bourse · le passé", *status_lines()])
        except Exception:
            log.exception("Notification de fin impossible")


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
