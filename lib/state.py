"""Mémoire d'idempotence du poller — SQLite (state/orchestrator.db).

Le poller tourne en boucle ; sans mémoire il re-notifierait les mêmes tickets,
re-nettoierait les mêmes PR mergées ou re-traiterait les mêmes commentaires à
chaque tour. On enregistre ici ce qui a déjà été traité.

state/ est gitignored : la base est locale au Pi, jamais versionnée. Le poller
est un process unique (timer oneshot), pas de concurrence en écriture.
"""

import sqlite3
import time
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conso_claude ("  # conso tokens par appel Claude
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"  # numéro du ticket (issue)
        "  etape TEXT NOT NULL,"      # implementation | auto-review | revision
        "  tokens_entree INTEGER NOT NULL,"
        "  tokens_cache INTEGER NOT NULL,"
        "  tokens_sortie INTEGER NOT NULL,"
        "  cout_usd REAL NOT NULL,"
        "  le TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta ("  # clé/valeur (ex: quota_jusqua)
        "  cle TEXT PRIMARY KEY,"
        "  valeur TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ci_fix_tentees ("  # shas dont la CI rouge a été traitée
        "  repo TEXT NOT NULL,"
        "  sha TEXT NOT NULL,"
        "  le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, sha)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conflits_tentes ("  # shas dont le conflit a été traité
        "  repo TEXT NOT NULL,"
        "  sha TEXT NOT NULL,"
        "  le TEXT NOT NULL DEFAULT (datetime('now')),"
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


def enregistrer_conso(repo: str, numero: int, etape: str,
                      entree: int, cache: int, sortie: int, cout: float) -> None:
    """Trace la conso d'un appel Claude, rattachée à un ticket et une étape."""
    with _connexion() as conn:
        conn.execute(
            "INSERT INTO conso_claude"
            "  (repo, numero, etape, tokens_entree, tokens_cache, tokens_sortie, cout_usd)"
            "  VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo, numero, etape, entree, cache, sortie, cout),
        )


def conso_par_ticket(limite: int = 15) -> list[tuple]:
    """Conso agrégée par ticket, du plus récent au plus ancien :
    (ticket, appels, tokens lus, tokens générés, coût $)."""
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT repo || '#' || numero, COUNT(*),"
            "       SUM(tokens_entree + tokens_cache), SUM(tokens_sortie), SUM(cout_usd)"
            "  FROM conso_claude GROUP BY repo, numero ORDER BY MAX(le) DESC LIMIT ?",
            (limite,),
        )
        return cur.fetchall()


def ci_fix_deja_tentee(repo: str, sha: str) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM ci_fix_tentees WHERE repo = ? AND sha = ?",
            (repo, sha),
        )
        return cur.fetchone() is not None


def marquer_ci_fix_tentee(repo: str, sha: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ci_fix_tentees (repo, sha) VALUES (?, ?)",
            (repo, sha),
        )


def conflit_deja_tente(repo: str, sha: str) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM conflits_tentes WHERE repo = ? AND sha = ?",
            (repo, sha),
        )
        return cur.fetchone() is not None


def marquer_conflit_tente(repo: str, sha: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conflits_tentes (repo, sha) VALUES (?, ?)",
            (repo, sha),
        )


def compter_conso(repo: str, numero: int, etape: str) -> int:
    """Nombre d'appels Claude déjà tracés pour ce ticket et cette étape."""
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM conso_claude WHERE repo = ? AND numero = ? AND etape = ?",
            (repo, numero, etape),
        )
        return cur.fetchone()[0]


def bloquer_quota(jusqua_epoch: int) -> None:
    """Mémorise que le quota Claude est épuisé jusqu'à cette date (epoch)."""
    with _connexion() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (cle, valeur) VALUES ('quota_jusqua', ?)",
            (str(jusqua_epoch),),
        )


def quota_bloque_jusqua() -> int | None:
    """Epoch de reprise si le quota est encore bloqué, sinon None."""
    with _connexion() as conn:
        cur = conn.execute("SELECT valeur FROM meta WHERE cle = 'quota_jusqua'")
        ligne = cur.fetchone()
    if ligne is None:
        return None
    jusqua = int(ligne[0])
    return jusqua if jusqua > time.time() else None


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
