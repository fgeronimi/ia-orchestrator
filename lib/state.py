"""Mémoire d'idempotence du poller — SQLite (state/orchestrator.db).

Le poller tourne en boucle ; sans mémoire il re-notifierait les mêmes tickets,
re-nettoierait les mêmes PR mergées ou re-traiterait les mêmes commentaires à
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prs_suivies ("  # PR fermées déjà traitées (dev_followup)
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"
        "  traitee_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, numero)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS commentaires_vus ("  # commentaires de review déjà traités
        "  repo TEXT NOT NULL,"
        "  cle TEXT NOT NULL,"  # 'issue-<id>' ou 'review-<id>' (deux espaces d'ids distincts)
        "  vu_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, cle)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ci_notifiee ("  # statuts CI déjà notifiés (par sha)
        "  repo TEXT NOT NULL,"
        "  sha TEXT NOT NULL,"
        "  notifiee_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, sha)"
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


def pr_deja_suivie(repo: str, numero: int) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM prs_suivies WHERE repo = ? AND numero = ?",
            (repo, numero),
        )
        return cur.fetchone() is not None


def marquer_pr_suivie(repo: str, numero: int) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO prs_suivies (repo, numero) VALUES (?, ?)",
            (repo, numero),
        )


def ci_deja_notifiee(repo: str, sha: str) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM ci_notifiee WHERE repo = ? AND sha = ?",
            (repo, sha),
        )
        return cur.fetchone() is not None


def marquer_ci_notifiee(repo: str, sha: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ci_notifiee (repo, sha) VALUES (?, ?)",
            (repo, sha),
        )


def commentaire_deja_vu(repo: str, cle: str) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM commentaires_vus WHERE repo = ? AND cle = ?",
            (repo, cle),
        )
        return cur.fetchone() is not None


def marquer_commentaire(repo: str, cle: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO commentaires_vus (repo, cle) VALUES (?, ?)",
            (repo, cle),
        )
