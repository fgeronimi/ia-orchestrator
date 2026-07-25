"""Mémoire d'idempotence du poller — SQLite (state/orchestrator.db).

Le poller tourne en boucle ; sans mémoire il re-notifierait les mêmes tickets à
chaque tour. On enregistre ici ce qui a déjà été traité.

state/ est gitignored : la base est locale au Pi, jamais versionnée. Le poller
est un process unique (timer oneshot), pas de concurrence en écriture.
"""

import sqlite3
from pathlib import Path

DB = Path(__file__).parent.parent / "state" / "orchestrator.db"


def _connexion() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS issues_notifiees ("
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"
        "  notifiee_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, numero)"
        ")"
    )
    return conn


def deja_notifiee(repo: str, numero: int) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM issues_notifiees WHERE repo = ? AND numero = ?",
            (repo, numero),
        )
        return cur.fetchone() is not None


def marquer_notifiee(repo: str, numero: int) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO issues_notifiees (repo, numero) VALUES (?, ?)",
            (repo, numero),
        )
