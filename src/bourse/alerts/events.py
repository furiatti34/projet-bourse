"""Alertes telles que les robots les lisent dans la base.

Les robots ne dépendent pas du passage qui a créé l'alerte : à chaque cycle, chacun lit les
alertes qu'il n'a pas encore traitées. Une alerte détectée au rallumage du PC n'est donc pas perdue,
et le robot sait depuis combien de temps l'information est publique (`published`).
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class AlertEvent:
    id: int
    created: datetime      # quand le logiciel a vu l'information
    published: datetime    # quand les médias l'ont publiée
    level: str
    score: int
    title: str
    severe_terms: list[str]
    moves: list[dict]      # marchés liés : ticker, name, change_pct, zscore

    def age(self, now: datetime) -> timedelta:
        return now - self.published


def _event(row: sqlite3.Row) -> AlertEvent:
    created = datetime.fromisoformat(row["created"])
    return AlertEvent(
        id=row["id"], created=created,
        published=datetime.fromisoformat(row["published"]) if row["published"] else created,
        level=row["level"], score=row["score"], title=row["title_fr"] or row["title"],
        severe_terms=json.loads(row["severe_terms"] or "[]"), moves=json.loads(row["moves"] or "[]"),
    )


def new_alerts(conn: sqlite3.Connection, after_id: int) -> list[AlertEvent]:
    rows = conn.execute("SELECT * FROM alerts WHERE is_test = 0 AND id > ? ORDER BY id", (after_id,))
    return [_event(r) for r in rows]


def last_alert_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0]
