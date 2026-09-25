"""Bibliothèque d'Éco 1 : les textes d'économistes, rangés dans la base et cherchables.

Chaque source est un flux RSS (réglages, section eco1). On garde le texte complet de chaque article
(lu sur le site quand le flux n'en donne qu'un résumé), découpé en passages indexés en plein texte
(SQLite FTS5, accents ignorés). Éco 1 y cherche avec ses propres mots-clés, en français et en anglais.

Usage personnel uniquement : les textes restent sur ce PC et ne sont jamais republiés.
"""
import calendar
import html
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ProjetBourse/0.1 (usage personnel)"
POLITENESS = 1.0          # secondes entre deux pages lues sur un même site
CHUNK_CHARS = 1800        # taille d'un passage indexé
MIN_FEED_TEXT = 1500      # en dessous, le flux ne donne qu'un résumé

SCHEMA = """
CREATE TABLE IF NOT EXISTS eco_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    author      TEXT,
    source      TEXT NOT NULL,      -- nom de la source dans les réglages
    lang        TEXT,
    published   TEXT,               -- date ISO, en UTC
    fetched     TEXT,
    text        TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS eco_fts USING fts5(
    title, author, body, source UNINDEXED, doc_id UNINDEXED, chunk UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS eco_sources (
    name        TEXT PRIMARY KEY,
    last_run    TEXT,
    archived    INTEGER DEFAULT 0,  -- 1 = archives déjà parcourues
    last_error  TEXT
);
CREATE TABLE IF NOT EXISTS eco_chat (      -- conversations avec Éco 1
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conv        INTEGER NOT NULL,
    time        TEXT NOT NULL,
    role        TEXT NOT NULL,      -- user / assistant
    content     TEXT NOT NULL,      -- contenu exact envoyé à l'API (JSON)
    display     TEXT,               -- texte affiché
    model       TEXT
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- lecture des sources

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _published(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc).isoformat(timespec="seconds")
    return None


def _html_to_text(markup: str) -> str:
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup(["script", "style", "figure", "iframe", "form", "button"]):
        tag.decompose()
    blocks = [b.get_text(" ", strip=True) for b in soup.find_all(["p", "h2", "h3", "h4", "li", "blockquote"])]
    text = "\n\n".join(b for b in blocks if b) or soup.get_text(" ", strip=True)
    return html.unescape(re.sub(r"[ \t ]+", " ", text)).strip()


def _page_text(url: str, session: requests.Session) -> tuple[str, str | None]:
    """Texte principal d'une page d'article : le bloc qui contient le plus de paragraphes."""
    resp = session.get(url, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    author = None
    meta = soup.find("meta", attrs={"name": "author"})
    if meta and meta.get("content"):
        author = meta["content"].strip()
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    best, best_len = None, 0
    for el in soup.find_all(["article", "main", "section", "div"]):
        length = sum(len(p.get_text(" ", strip=True)) for p in el.find_all("p", recursive=False))
        if length > best_len:
            best, best_len = el, length
    if best is None or best_len < 300:   # paragraphes imbriqués : on prend l'article entier
        best = soup.find("article") or soup.find("main") or soup.body
    return (_html_to_text(str(best)) if best else ""), author


def _feed_urls(source: dict, archives: bool) -> list[str]:
    url, pages = source["url"], source.get("pages_archives", 1) if archives else 1
    kind = source.get("pagination")
    if kind == "wordpress":
        return [url] + [f"{url}{'&' if '?' in url else '?'}paged={n}" for n in range(2, pages + 1)]
    if kind == "blogger":
        return [f"{url}?max-results=50&start-index={1 + 50 * n}" for n in range(pages)]
    return [url]


def _chunks(text: str) -> list[str]:
    """Découpe en passages d'environ CHUNK_CHARS caractères, sans couper les paragraphes."""
    parts, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > CHUNK_CHARS * 1.5:            # paragraphe géant : on le coupe
            parts.append((current + "\n\n" + para[:CHUNK_CHARS]).strip())
            current, para = "", para[CHUNK_CHARS:]
        if len(current) + len(para) > CHUNK_CHARS and current:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}".strip()
    if current:
        parts.append(current)
    return parts


def _store(conn: sqlite3.Connection, source: dict, url: str, title: str, author: str | None,
           published: str | None, text: str) -> None:
    # OR IGNORE : la veille automatique peut avoir ajouté le même article entre-temps
    cur = conn.execute("INSERT OR IGNORE INTO eco_docs (url, title, author, source, lang, published, fetched, text)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (url, title, author, source["nom"], source.get("langue"), published, _now(), text))
    if not cur.rowcount:
        return
    conn.executemany("INSERT INTO eco_fts (title, author, body, source, doc_id, chunk) VALUES (?, ?, ?, ?, ?, ?)",
                     [(title, author or "", chunk, source["nom"], cur.lastrowid, i)
                      for i, chunk in enumerate(_chunks(text))])


def update_source(conn: sqlite3.Connection, source: dict, max_new: int, progress=None) -> int:
    """Lit une source ; renvoie le nombre d'articles ajoutés. La première fois, remonte les archives."""
    row = conn.execute("SELECT archived FROM eco_sources WHERE name = ?", (source["nom"],)).fetchone()
    archives = not (row and row["archived"])
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    added, error, done = 0, None, False
    try:
        for feed_url in _feed_urls(source, archives):
            resp = session.get(feed_url, timeout=25)
            if resp.status_code == 404 and feed_url != source["url"]:
                break                                    # plus d'archives
            resp.raise_for_status()
            entries = feedparser.parse(resp.content).entries
            if not entries:
                break
            for entry in entries:
                url = entry.get("link", "")
                if not url or conn.execute("SELECT 1 FROM eco_docs WHERE url = ?", (url,)).fetchone():
                    continue
                if added >= max_new:
                    break
                title = html.unescape(entry.get("title", "")).strip() or url
                content = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""
                text = _html_to_text(content or entry.get("summary", ""))
                author = entry.get("author")
                if source.get("lire_page") or len(text) < MIN_FEED_TEXT:
                    try:
                        page, page_author = _page_text(url, session)
                        if len(page) > len(text):
                            text = page
                        author = author or page_author
                    except requests.RequestException as exc:
                        log.info("Page illisible %s : %s", url, exc)
                    time.sleep(POLITENESS)
                if len(text) < 200:
                    continue
                if author:   # « Aswath Damodaran (noreply@blogger.com) » → « Aswath Damodaran »
                    author = re.sub(r"\s*\([^)]*@[^)]*\)", "", author).strip()
                _store(conn, source, url, title, author, _published(entry), text)
                conn.commit()
                added += 1
                if progress:
                    progress(source["nom"], title)
            if added >= max_new:
                break
            time.sleep(POLITENESS)
        # archives parcourues jusqu'au bout ? sinon on reprendra au prochain passage
        done = added < max_new
    except Exception as exc:     # une source en panne n'empêche pas les autres
        log.warning("Source Éco 1 %s indisponible : %s", source["nom"], exc)
        error = str(exc)[:300]
    conn.execute("INSERT INTO eco_sources (name, last_run, archived, last_error) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run,"
                 " archived = MAX(archived, excluded.archived), last_error = excluded.last_error",
                 (source["nom"], _now(), int(done), error))
    conn.commit()
    return added


def update_all(conn: sqlite3.Connection, settings: dict, progress=None,
               max_seconds: float | None = None) -> dict[str, int]:
    """Met à jour toutes les sources (les moins récemment lues d'abord). Au-delà de max_seconds, on s'arrête :
    les sources restantes passeront en premier la fois suivante."""
    init(conn)
    cfg = settings["eco1"]
    last = {r["name"]: r["last_run"] or "" for r in conn.execute("SELECT name, last_run FROM eco_sources")}
    start, added = time.monotonic(), {}
    for s in sorted(cfg["sources"], key=lambda s: last.get(s["nom"], "")):
        if max_seconds and time.monotonic() - start > max_seconds:
            break
        added[s["nom"]] = update_source(conn, s, cfg.get("articles_max_par_source", 40), progress)
    return added


def update_if_due(conn: sqlite3.Connection, settings: dict) -> dict[str, int] | None:
    """Pour la veille automatique : met à jour seulement si la dernière mise à jour est assez ancienne."""
    init(conn)
    last = conn.execute("SELECT MIN(last_run) FROM eco_sources").fetchone()[0]
    names = {s["nom"] for s in settings["eco1"]["sources"]}
    known = {r[0] for r in conn.execute("SELECT name FROM eco_sources")}
    hours = settings["eco1"].get("mise_a_jour_heures", 12)
    if last and names <= known and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=hours):
        return None
    return update_all(conn, settings, max_seconds=300)   # la veille ne doit pas dépasser ses 10 minutes


# ---------------------------------------------------------------- recherche

WORD_RE = re.compile(r"\w{2,}", re.UNICODE)
# « or » n'est pas un mot vide : c'est le métal (on cherche aussi « gold » pour les sources anglaises)
STOP = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "en", "au", "aux", "que", "qui", "the",
        "of", "and", "to", "in", "on", "for", "is", "a", "an", "est", "sur", "par", "pour", "dans", "ce", "cette",
        "ces", "il", "elle", "ils", "je", "tu", "vous", "nous", "me", "moi", "mon", "ma", "mes", "quoi", "comment",
        "pense", "pensent", "penser", "avis", "dit", "disent", "explique", "reponds", "réponds", "lignes", "moment",
        "actuellement", "maintenant", "selon", "quel", "quelle", "quels", "quelles", "sont", "fait", "faire", "peux",
        "peut", "what", "does", "do", "think", "about", "how", "why"}
SYNONYMS = {"or": ["gold"], "gold": ["or"], "inflation": ["inflation"], "dette": ["debt"], "debt": ["dette"],
            "taux": ["rates", "yields"], "bourse": ["stocks", "market"], "recession": ["récession"],
            "chomage": ["unemployment"], "chômage": ["unemployment"], "dollar": ["dollar"], "euro": ["euro"]}
GENERIC_NAME_WORDS = {"institut", "economics", "économistes", "economistes", "banque", "règlements", "reglements",
                      "internationaux", "york", "markets", "musings", "investing", "klement", "the", "des", "les",
                      "liberty", "street", "revolution", "marginal", "risk", "calculated", "economist", "grumpy"}


def detect_source(question: str, sources: list[dict]) -> str:
    """Nom de la source citée dans la question (« Charles Gave » → Institut des Libertés), sinon ""."""
    words = {w.lower() for w in WORD_RE.findall(question)}
    for s in sources:
        name_words = {w.lower() for w in WORD_RE.findall(s["nom"]) if len(w) >= 4} - GENERIC_NAME_WORDS
        if words & name_words:
            return s["nom"]
    return ""


def _fts_query(text: str) -> str:
    words = [w for w in WORD_RE.findall(text.lower()) if w not in STOP]
    words += [syn for w in words for syn in SYNONYMS.get(w, [])]
    return " OR ".join(f'"{w}"*' if len(w) > 4 else f'"{w}"' for w in dict.fromkeys(words))


def search(conn: sqlite3.Connection, query: str, source: str = "", limit: int = 8) -> list[dict]:
    """Passages les plus pertinents (au plus 2 par article), meilleurs d'abord."""
    fts = _fts_query(query)
    if not fts:
        return []
    sql = ("SELECT f.doc_id, f.body, bm25(eco_fts, 4.0, 1.0, 1.0) AS rank, d.title, d.author, d.source, d.url,"
           " d.published FROM eco_fts f JOIN eco_docs d ON d.id = f.doc_id WHERE eco_fts MATCH ?")
    params: list = [fts]
    if source:
        sql += " AND d.source LIKE ?"
        params.append(f"%{source}%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit * 4)
    results, per_doc = [], {}
    for r in conn.execute(sql, params):
        if per_doc.get(r["doc_id"], 0) >= 2:
            continue
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        results.append(dict(r))
        if len(results) >= limit:
            break
    return results


def document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM eco_docs WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def catalogue(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    """Chaque source : nombre d'articles, période couverte, auteurs principaux, dernière mise à jour."""
    init(conn)
    out = []
    for s in settings["eco1"]["sources"]:
        stats = conn.execute("SELECT COUNT(*) n, MIN(published) first, MAX(published) last FROM eco_docs"
                             " WHERE source = ?", (s["nom"],)).fetchone()
        authors = [r[0] for r in conn.execute(
            "SELECT author FROM eco_docs WHERE source = ? AND author IS NOT NULL AND author != ''"
            " GROUP BY author ORDER BY COUNT(*) DESC LIMIT 5", (s["nom"],))]
        state = conn.execute("SELECT last_run, last_error FROM eco_sources WHERE name = ?", (s["nom"],)).fetchone()
        out.append({"source": s["nom"], "ecole": s.get("ecole", ""), "langue": s.get("langue", ""),
                    "articles": stats["n"], "du": (stats["first"] or "")[:10], "au": (stats["last"] or "")[:10],
                    "auteurs": authors, "mise_a_jour": state["last_run"] if state else None,
                    "erreur": state["last_error"] if state else None})
    return out
