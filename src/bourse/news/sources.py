"""Lecture des flux RSS d'actualité."""
import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from .models import Article

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ProjetBourse/0.1"
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str, max_len: int = 400) -> str:
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:max_len]


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_feed(feed: dict) -> list[Article]:
    """Télécharge un flux RSS et le convertit en liste d'Article."""
    resp = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    articles = []
    for entry in parsed.entries:
        title = _clean(entry.get("title", ""), 300)
        if not title:
            continue
        source = feed["nom"]
        # Google Actualités : le titre se termine par « - Nom du média »
        if feed.get("agregateur") and " - " in title:
            title, source = title.rsplit(" - ", 1)
        articles.append(
            Article(
                title=title,
                url=entry.get("link", ""),
                source=source,
                published=_published(entry),
                summary="" if feed.get("agregateur") else _clean(entry.get("summary", "")),
                lang=feed.get("langue", "en"),
                official=bool(feed.get("officielle")),
            )
        )
    return articles


def fetch_all(feeds: list[dict], max_age_hours: int) -> tuple[list[Article], list[str]]:
    """Lit toutes les sources. Une source en panne n'empêche pas les autres.

    Renvoie (articles récents, liste des sources en erreur).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    articles, errors = [], []
    for feed in feeds:
        try:
            articles += [a for a in fetch_feed(feed) if a.published >= cutoff]
        except Exception as exc:  # réseau, site indisponible, flux invalide…
            log.warning("Source %s indisponible : %s", feed["nom"], exc)
            errors.append(feed["nom"])

    # Un même article peut apparaître dans plusieurs flux : on dédoublonne.
    unique = {a.id: a for a in articles}
    return list(unique.values()), errors
