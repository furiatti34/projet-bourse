"""Base de données locale (SQLite : un simple fichier dans data/)."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,   -- empreinte de l'URL
    title       TEXT NOT NULL,
    url         TEXT,
    source      TEXT,
    published   TEXT,               -- date ISO, en UTC
    summary     TEXT,
    lang        TEXT,
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    score       INTEGER NOT NULL,
    level       TEXT NOT NULL,      -- IMPORTANTE / FORTE
    title       TEXT NOT NULL,      -- titre d'origine
    title_fr    TEXT,               -- titre traduit
    url         TEXT,
    sources     TEXT,               -- médias ayant couvert le sujet
    reasons     TEXT,               -- pourquoi le logiciel juge le sujet important
    is_test     INTEGER DEFAULT 0   -- 1 = alerte fictive de test
);

-- ===== Paper trading (argent FICTIF uniquement) =====

CREATE TABLE IF NOT EXISTS portfolios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    strategy        TEXT,               -- NULL = portefeuille manuel
    initial_cash    REAL NOT NULL,      -- en euros
    created         TEXT NOT NULL,
    benchmark       TEXT NOT NULL,      -- indice de référence (ticker)
    benchmark_start REAL,               -- cours de l'indice à la création
    state           TEXT DEFAULT '{}'   -- mémoire de la stratégie (JSON)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
    created       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL,        -- ACHAT / VENTE
    quantity      REAL,                 -- nombre de titres demandé (ou NULL)
    amount_eur    REAL,                 -- ou montant en euros (ou NULL = tout)
    status        TEXT NOT NULL,        -- EN_ATTENTE / EXECUTE / ANNULE / REJETE
    reason        TEXT,                 -- pourquoi cet ordre (manuel, alerte…)
    filled_at     TEXT,
    filled_qty    REAL,
    fill_price    REAL,                 -- dans la devise du titre
    currency      TEXT,
    fx_to_eur     REAL,
    fees_eur      REAL,
    note          TEXT                  -- explication si rejeté / ajusté
);

CREATE TABLE IF NOT EXISTS journal (     -- décisions des robots, expliquées
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
    time          TEXT NOT NULL,
    message       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
    time          TEXT NOT NULL,
    value_eur     REAL NOT NULL,
    cash_eur      REAL NOT NULL,
    benchmark_eur REAL                  -- valeur si tout avait été placé dans l'indice
);
"""


def connect(path: Path) -> sqlite3.Connection:
    # timeout : si la veille automatique écrit en même temps, on attend au lieu d'échouer
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


# Colonnes ajoutées après la création de la base : on les ajoute aux bases existantes
LATER_COLUMNS = {
    "alerts": {"published": "TEXT", "severe_terms": "TEXT DEFAULT '[]'", "moves": "TEXT DEFAULT '[]'"},
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in LATER_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    conn.commit()
