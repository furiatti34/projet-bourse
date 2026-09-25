"""Chaîne complète de la veille : actualités → sujets → score → traduction → notification."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bourse import clock
from bourse.config import db_path
from bourse.database import connect
from bourse.news.models import Article
from bourse.news.sources import fetch_all
from bourse.news.translate import translate

from .market import Move, market_moves
from .notifier import notify
from .scoring import Story, cluster, same_story, score_story, tokens

log = logging.getLogger(__name__)


@dataclass
class WatchReport:
    n_articles: int = 0
    failed_sources: list[str] = field(default_factory=list)
    market_ok: bool = True
    stories: list[Story] = field(default_factory=list)  # triés du plus au moins important
    alerts: list[Story] = field(default_factory=list)   # sujets notifiés lors de ce passage


def run_watch(settings: dict, send_notifications: bool = True) -> WatchReport:
    cfg = settings["veille"]
    report = WatchReport()

    articles, report.failed_sources = fetch_all(cfg["sources"], cfg["fenetre_heures"])
    report.n_articles = len(articles)

    try:
        moves = market_moves(cfg["indicateurs_marche"])
    except Exception as exc:  # pas bloquant : on juge alors sans les marchés
        log.warning("Données de marché indisponibles : %s", exc)
        moves, report.market_ok = {}, False

    stories = [score_story(s, moves, cfg["seuil_alerte_forte"], cfg["seuil_alerte"])
               for s in cluster(articles)]
    report.stories = sorted(stories, key=lambda s: -s.score)

    with connect(db_path(settings)) as conn:
        _save_articles(conn, articles)
        already = _recent_alert_tokens(conn, hours=48)
        for story in report.stories:
            if len(report.alerts) >= cfg["max_alertes_par_passage"] or not story.level:
                break
            if any(same_story(tokens(a.title), t) for a in story.articles for t in already):
                continue  # sujet déjà signalé récemment
            if send_notifications:  # en mode « sans notif », on n'enregistre rien non plus
                _raise_alert(conn, story, cfg["langue_cible"])
            report.alerts.append(story)
    return report


def french_title(story: Story, target_lang: str = "fr") -> str:
    """Titre en français : un article déjà en français s'il y en a un, sinon traduction."""
    native = [a for a in story.articles if a.lang == target_lang]
    if native:
        return native[0].title
    return translate(story.lead.title, story.lead.lang, target_lang)


def _raise_alert(conn, story: Story, target_lang: str, is_test: bool = False) -> None:
    """Garde une trace de l'alerte dans la base, puis envoie la notification."""
    title_fr = story.title_fr = french_title(story, target_lang)
    moves = [{"ticker": m.ticker, "name": m.name, "change_pct": m.change_pct, "zscore": m.zscore}
             for m in story.moves]
    cur = conn.execute(
        "INSERT INTO alerts (created, published, score, level, title, title_fr, url, sources, reasons,"
        " severe_terms, moves, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clock.now().isoformat(), story.published.isoformat(), story.score, story.level,
         story.lead.title, title_fr, story.lead.url, ", ".join(story.sources), " | ".join(story.reasons),
         json.dumps(story.severe_terms), json.dumps(moves, ensure_ascii=False), int(is_test)),
    )
    conn.commit()  # l'alerte doit être lisible par scripts/rouvrir_alerte.py dès le premier clic
    show_alert(conn, cur.lastrowid)


def show_alert(conn, alert_id: int, silent: bool = False) -> bool:
    """Affiche (ou remet, sans pop-up, dans Windows+N) la notification d'une alerte de la base."""
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return False
    icon = "🔴" if row["level"] == "FORTE" else "🟠"
    prefix = "[TEST] " if row["is_test"] else ""
    published = datetime.fromisoformat(row["published"])
    age = format_age(datetime.fromisoformat(row["created"]) - published)
    return notify(
        [f"{prefix}{icon} Alerte {row['level']} · {row['score']}/100 · publiée il y a {age}",
         row["title_fr"] or row["title"],
         " · ".join((row["reasons"] or "").split(" | ")[:2])],
        url=row["url"], alert_id=alert_id, silent=silent,
        timestamp=datetime.fromisoformat(row["created"]),
    )


def format_age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} jours"


def _save_articles(conn, articles: list[Article]) -> None:
    now = clock.now().isoformat()
    conn.executemany(
        "INSERT OR IGNORE INTO articles (id, title, url, source, published, summary, lang, first_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(a.id, a.title, a.url, a.source, a.published.isoformat(), a.summary, a.lang, now)
         for a in articles],
    )


def _recent_alert_tokens(conn, hours: int) -> list[set[str]]:
    since = (clock.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("SELECT title FROM alerts WHERE is_test = 0 AND created >= ?", (since,))
    return [tokens(row["title"]) for row in rows]


# --- Alerte fictive de test ---

def send_test_alert(settings: dict) -> Story:
    """Simule une crise du yen et la fait passer par le VRAI système de score et de notification."""
    cfg = settings["veille"]
    now = clock.now()
    fake_titles = [
        ("Yen plunges to record low as Bank of Japan signals emergency intervention", "Agence fictive A"),
        ("Japanese yen collapse sparks fears of carry trade crisis", "Agence fictive B"),
        ("Yen plunges past record low, Bank of Japan intervention expected", "Agence fictive C"),
        ("Bank of Japan emergency meeting as yen plunges to record low", "Agence fictive D"),
        ("Le yen s'effondre, la Banque du Japon prépare une intervention d'urgence", "Journal fictif FR"),
    ]
    story = Story([
        Article(title=t, url="https://www.boj.or.jp/en/", source=src, published=now,
                lang="fr" if "FR" in src else "en")
        for t, src in fake_titles
    ])
    fake_moves = {
        "JPY=X": Move("JPY=X", "Dollar/Yen", ["yen", "japan", "bank of japan"], change_pct=4.1, zscore=5.2),
        "^VIX": Move("^VIX", "VIX", [], change_pct=18.0, zscore=2.6),
    }
    score_story(story, fake_moves, cfg["seuil_alerte_forte"], cfg["seuil_alerte"])
    with connect(db_path(settings)) as conn:
        _raise_alert(conn, story, cfg["langue_cible"], is_test=True)
    return story
