"""Archives d'actualités pour les simulations dans le passé.

Les robots doivent lire, à la date simulée, les mêmes sources qu'au présent (config : veille.sources) :
  1. Google Actualités : mêmes recherches qu'au présent, limitées au jour voulu (after:/before:).
     Google ne donne alors que la DATE (pas l'heure) : l'article n'est rendu visible qu'à la fin de
     cette journée (heure de Californie, celle de Google), pour ne jamais le montrer trop tôt.
  2. Les flux RSS des médias (BBC, CNBC, Le Monde…) tels qu'archivés par la Wayback Machine
     (archive.org). Un article n'est visible qu'à partir de l'heure où l'archive l'a VU dans le flux
     (le titre a pu être modifié après sa publication : on ne prend aucun risque).
  3. Communiqués officiels : la Fed (avec l'heure exacte) et la BCE (date seule → visible le soir).

Chaque article garde deux heures :
  - `available` : à partir de quand le robot peut le voir (jamais avant sa publication réelle) ;
  - `published` : l'heure de publication (ou sa meilleure estimation), qui sert à juger sa fraîcheur.
"""
import calendar
import html
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests

from bourse.config import PROJECT_ROOT
from bourse.news.models import Article

log = logging.getLogger(__name__)

# BOURSE_ARCHIVE permet d'utiliser une copie figée des archives (vérifications)
ARCHIVE_DB = Path(os.environ.get("BOURSE_ARCHIVE") or PROJECT_ROOT / "data" / "historique" / "actualites.db")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ProjetBourse/0.1 (simulation historique)"
PACIFIC, PARIS, NEW_YORK = ZoneInfo("America/Los_Angeles"), ZoneInfo("Europe/Paris"), ZoneInfo("America/New_York")
TAG_RE = re.compile(r"<[^>]+>")
WAYBACK_SPACING = timedelta(hours=6)   # au plus une archive toutes les 6 h par flux (4 par jour)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_articles (
    id         TEXT PRIMARY KEY,   -- empreinte (source + titre)
    title      TEXT NOT NULL,
    url        TEXT,
    source     TEXT,
    lang       TEXT,
    official   INTEGER DEFAULT 0,
    summary    TEXT,
    published  TEXT NOT NULL,      -- UTC ; estimation si seule la date est connue
    available  TEXT NOT NULL,      -- UTC : le robot ne peut pas le voir avant
    origin     TEXT                -- google / wayback / fed / bce
);
CREATE INDEX IF NOT EXISTS hist_articles_available ON hist_articles(available);
CREATE TABLE IF NOT EXISTS hist_coverage (   -- journées déjà téléchargées, par source
    origin  TEXT NOT NULL,
    day     TEXT NOT NULL,
    n       INTEGER,
    PRIMARY KEY (origin, day)
);
CREATE TABLE IF NOT EXISTS hist_snapshots (  -- liste des archives Wayback de chaque flux
    feed    TEXT NOT NULL,
    year    INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (feed, year, ts)
);
CREATE TABLE IF NOT EXISTS hist_snapshot_years (feed TEXT, year INTEGER, PRIMARY KEY (feed, year));
"""


def connect_archive() -> sqlite3.Connection:
    ARCHIVE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ARCHIVE_DB, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _clean(text: str, max_len: int = 400) -> str:
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:max_len]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _store(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Enregistre des articles ; un article déjà connu garde sa première apparition."""
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO hist_articles (id, title, url, source, lang, official, summary, published, available, origin)"
        " VALUES (:id, :title, :url, :source, :lang, :official, :summary, :published, :available, :origin)"
        " ON CONFLICT(id) DO UPDATE SET available = MIN(available, excluded.available),"
        " published = MIN(published, excluded.published)", rows)
    conn.commit()
    return conn.total_changes - before


def _row(title, url, source, lang, official, summary, published: datetime, available: datetime, origin) -> dict:
    return {"id": sha1(f"{source}|{title.strip().lower()}".encode("utf-8")).hexdigest(), "title": title,
            "url": url, "source": source, "lang": lang, "official": int(bool(official)), "summary": summary,
            "published": _iso(min(published, available)), "available": _iso(available), "origin": origin}


def _mark(conn, origin: str, day: date, n: int) -> None:
    conn.execute("INSERT OR REPLACE INTO hist_coverage (origin, day, n) VALUES (?, ?, ?)", (origin, day.isoformat(), n))
    conn.commit()


def covered(conn, origin: str) -> set[str]:
    return {r["day"] for r in conn.execute("SELECT day FROM hist_coverage WHERE origin = ?", (origin,))}


def _get(url: str, params=None, timeout: int = 40, tries: int = 4) -> requests.Response:
    """Requête patiente : en cas de refus (trop de demandes), on attend puis on réessaie."""
    wait = 20
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            if resp.status_code in (429, 503, 502, 504):
                raise requests.HTTPError(f"{resp.status_code}")
            return resp
        except (requests.RequestException, OSError) as exc:
            if attempt == tries - 1:
                raise
            log.info("%s : %s, nouvel essai dans %d s", url[:60], exc, wait)
            time.sleep(wait)
            wait *= 2
    raise RuntimeError("inaccessible")


# ---------------------------------------------------------------- 1. Google Actualités

def google_origin(feed: dict) -> str:
    return f"google:{feed['nom']}"


def fetch_google_day(conn, feed: dict, day: date) -> int:
    """Une journée (heure de Californie) d'une recherche Google Actualités des réglages."""
    parsed = urlparse(feed["url"])
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    query = re.sub(r"\s*when:\S+", "", params["q"]).strip()
    params["q"] = f"{query} after:{day.isoformat()} before:{(day + timedelta(days=1)).isoformat()}"
    resp = _get(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", params=params)
    resp.raise_for_status()
    rows = []
    for entry in feedparser.parse(resp.content).entries:
        title = _clean(entry.get("title", ""), 300)
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not title or not stamp:
            continue
        source = feed["nom"]
        if " - " in title:
            title, source = title.rsplit(" - ", 1)
        pub = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc)
        # Google ne donne que la date (minuit, heure de Californie) : visible à la FIN de cette journée
        local_day = pub.astimezone(PACIFIC).date()
        end_of_day = datetime(local_day.year, local_day.month, local_day.day, tzinfo=PACIFIC) + timedelta(days=1)
        rows.append(_row(title, entry.get("link", ""), source, feed.get("langue", "en"), False, "",
                         published=end_of_day - timedelta(hours=12), available=end_of_day, origin="google"))
    _store(conn, rows)
    _mark(conn, google_origin(feed), day, len(rows))
    return len(rows)


# ---------------------------------------------------------------- 2. Wayback Machine (flux RSS archivés)

def wayback_origin(feed: dict) -> str:
    return f"wayback:{feed['nom']}"


def _snapshot_list(conn, feed: dict, year: int) -> list[datetime]:
    done = conn.execute("SELECT 1 FROM hist_snapshot_years WHERE feed = ? AND year = ?", (feed["nom"], year)).fetchone()
    if not done:
        url = feed["url"].split("://", 1)[1]
        resp = _get("http://web.archive.org/cdx/search/cdx", timeout=120, params={
            "url": url, "from": f"{year}0101", "to": f"{year}1231", "output": "json",
            "filter": "statuscode:200", "fl": "timestamp", "collapse": "timestamp:10"})
        rows = resp.json()[1:] if resp.text.strip() else []
        conn.executemany("INSERT OR IGNORE INTO hist_snapshots (feed, year, ts) VALUES (?, ?, ?)",
                         [(feed["nom"], year, r[0]) for r in rows])
        if year < datetime.now(timezone.utc).year:   # l'année en cours peut encore s'enrichir
            conn.execute("INSERT OR IGNORE INTO hist_snapshot_years (feed, year) VALUES (?, ?)", (feed["nom"], year))
        conn.commit()
    rows = conn.execute("SELECT ts FROM hist_snapshots WHERE feed = ? AND year = ? ORDER BY ts", (feed["nom"], year))
    return [datetime.strptime(r["ts"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) for r in rows]


def fetch_wayback_day(conn, feed: dict, day: date) -> int:
    """Les archives d'un flux RSS pour une journée (UTC), au plus une toutes les 6 heures."""
    snaps = [s for s in _snapshot_list(conn, feed, day.year) if s.date() == day]
    chosen, last = [], None
    for s in snaps:
        if last is None or s - last >= WAYBACK_SPACING:
            chosen.append(s)
            last = s
    n, failed = 0, 0
    for snap in chosen:
        try:
            resp = _get(f"https://web.archive.org/web/{snap:%Y%m%d%H%M%S}id_/{feed['url']}", timeout=60, tries=3)
        except Exception as exc:
            log.info("Archive %s %s illisible : %s", feed["nom"], snap, exc)
            failed += 1
            time.sleep(60)   # archive.org refuse les connexions quand on va trop vite : on patiente
            continue
        rows = []
        for entry in feedparser.parse(resp.content).entries:
            title = _clean(entry.get("title", ""), 300)
            if not title:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            pub = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc) if stamp else snap
            if pub < snap - timedelta(days=3):
                continue   # vieil article encore dans le flux : inutile
            rows.append(_row(title, entry.get("link", ""), feed["nom"], feed.get("langue", "en"),
                             feed.get("officielle"), _clean(entry.get("summary", "")),
                             published=pub, available=snap, origin="wayback"))
        _store(conn, rows)
        n += len(rows)
        time.sleep(2.0)   # politesse envers archive.org
    if failed:   # journée incomplète : elle sera retentée au prochain téléchargement
        raise RuntimeError(f"{failed} archive(s) illisible(s) sur {len(chosen)}, journée à retenter")
    _mark(conn, wayback_origin(feed), day, n)
    return n


# ---------------------------------------------------------------- 3. Communiqués officiels

FED_NAME, ECB_NAME = "Réserve fédérale (Fed)", "BCE"


def fetch_fed(conn) -> int:
    """Tous les communiqués de la Fed depuis 2006, avec leur heure exacte (heure de New York)."""
    resp = _get("https://www.federalreserve.gov/json/ne-press.json", timeout=90)
    import json
    rows = []
    for item in json.loads(resp.content.decode("utf-8-sig")):
        try:
            when = datetime.strptime(item["d"], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=NEW_YORK)
        except (KeyError, ValueError):
            continue
        rows.append(_row(_clean(item.get("t", ""), 300), "https://www.federalreserve.gov" + item.get("l", ""),
                         FED_NAME, "en", True, "", published=when, available=when, origin="fed"))
    _store(conn, rows)
    _mark(conn, "fed", date.today(), len(rows))
    return len(rows)


def fetch_ecb_year(conn, year: int) -> int:
    """Communiqués de la BCE d'une année. Date seule : visibles à 22 h (heure de Francfort) ce jour-là."""
    from bs4 import BeautifulSoup
    resp = _get(f"https://www.ecb.europa.eu/press/pr/date/{year}/html/index_include.en.html", timeout=60)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        title_div = dd.find("div", class_="title") if dd else None
        if not title_div:
            continue
        try:
            day = datetime.strptime(dt.get_text(" ", strip=True), "%d %B %Y")
        except ValueError:
            continue
        link = title_div.find("a")
        evening = datetime(day.year, day.month, day.day, 22, 0, tzinfo=PARIS)
        rows.append(_row(title_div.get_text(" ", strip=True), "https://www.ecb.europa.eu" + (link["href"] if link else ""),
                         ECB_NAME, "en", True, "", published=evening - timedelta(hours=8), available=evening,
                         origin="bce"))
    _store(conn, rows)
    _mark(conn, f"bce:{year}", date(year, 1, 1), len(rows))
    return len(rows)


# ---------------------------------------------------------------- téléchargement d'une période

def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def prefetch(settings: dict, start: date, end: date, kinds=("officiel", "google", "wayback"),
             progress=None) -> None:
    """Télécharge ce qui manque entre deux dates. `progress(texte)` est appelé au fil de l'eau."""
    conn = connect_archive()
    feeds = settings["veille"]["sources"]
    say = progress or (lambda text: log.info(text))
    end = min(end, date.today())
    if "officiel" in kinds:
        if not covered(conn, "fed"):
            say(f"Communiqués de la Fed : {fetch_fed(conn)}")
        for year in range(start.year, end.year + 1):
            if not covered(conn, f"bce:{year}") or year == date.today().year:
                try:
                    say(f"Communiqués de la BCE {year} : {fetch_ecb_year(conn, year)}")
                except Exception as exc:
                    say(f"BCE {year} indisponible : {exc}")
    todo = [(kind, feed) for kind in ("google", "wayback") if kind in kinds
            for feed in feeds if bool(feed.get("agregateur")) == (kind == "google")]
    for day in days_between(start, end):
        for kind, feed in todo:
            origin = google_origin(feed) if kind == "google" else wayback_origin(feed)
            if conn.execute("SELECT 1 FROM hist_coverage WHERE origin = ? AND day = ?",
                            (origin, day.isoformat())).fetchone():
                continue
            try:
                n = (fetch_google_day if kind == "google" else fetch_wayback_day)(conn, feed, day)
                say(f"{day:%d/%m/%Y} · {feed['nom']} : {n} articles")
            except Exception as exc:
                say(f"{day:%d/%m/%Y} · {feed['nom']} indisponible : {exc}")
            if kind == "google":
                time.sleep(1.5)   # Google refuse les rafales


def articles_between(conn, after: datetime, until: datetime) -> list[tuple[Article, datetime]]:
    """Articles devenus visibles dans l'intervalle ]after, until], avec leur heure de visibilité."""
    rows = conn.execute("SELECT * FROM hist_articles WHERE available > ? AND available <= ? ORDER BY available",
                        (_iso(after), _iso(until)))
    return [(Article(title=r["title"], url=r["url"] or "", source=r["source"],
                     published=datetime.fromisoformat(r["published"]), summary=r["summary"] or "",
                     lang=r["lang"] or "en", official=bool(r["official"])),
             datetime.fromisoformat(r["available"])) for r in rows]


def coverage_report(conn, settings: dict, start: date, end: date) -> dict[str, float]:
    """Part des journées téléchargées, par source (pour prévenir l'utilisateur des trous)."""
    days = {d.isoformat() for d in days_between(start, min(end, date.today()))}
    report = {}
    for feed in settings["veille"]["sources"]:
        origin = google_origin(feed) if feed.get("agregateur") else wayback_origin(feed)
        rows = conn.execute("SELECT day, n FROM hist_coverage WHERE origin = ?", (origin,)).fetchall()
        done = {r["day"] for r in rows if r["day"] in days}
        with_articles = {r["day"] for r in rows if r["day"] in days and r["n"]}
        report[feed["nom"]] = {"telecharge": len(done) / len(days) if days else 0,
                               "avec_articles": len(with_articles) / len(days) if days else 0}
    return report


if __name__ == "__main__":   # téléchargement en tâche de fond : python -m bourse.backtest.actualites 2016-09-01 2026-09-25 google
    import sys

    from bourse.config import load_settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start_day, end_day = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    prefetch(load_settings(), start_day, end_day, kinds=tuple(sys.argv[3].split(",")) if len(sys.argv) > 3
             else ("officiel", "google", "wayback"))
