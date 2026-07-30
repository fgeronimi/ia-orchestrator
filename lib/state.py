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
        "  modele TEXT,"              # alias --model utilisé, NULL = défaut abonnement
        "  le TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    # Migration douce : bases créées avant l'ajout de la colonne `modele`.
    colonnes = [l[1] for l in conn.execute("PRAGMA table_info(conso_claude)")]
    if "modele" not in colonnes:
        conn.execute("ALTER TABLE conso_claude ADD COLUMN modele TEXT")
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS issues_triees ("  # issues déjà passées au triage
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"
        "  triee_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, numero)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS triage_epuise ("  # issues où la clarification a atteint son plafond
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"
        "  epuise_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, numero)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orphelins_signales ("  # crashs durs de l'exécutant détectés
        "  repo TEXT NOT NULL,"
        "  numero INTEGER NOT NULL,"
        "  label_pose_le TEXT NOT NULL,"  # horodatage de la pose d'ai-working = identité de l'incident
        "  signale_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, numero, label_pose_le)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS forge_signale ("  # écarts de conformité déjà signalés
        "  repo TEXT NOT NULL,"
        "  condition TEXT NOT NULL,"
        "  version INTEGER NOT NULL,"  # version de data/forge.yaml au moment du signalement
        "  signalee_le TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (repo, condition, version)"
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


def issue_deja_triee(repo: str, numero: int) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM issues_triees WHERE repo = ? AND numero = ?",
            (repo, numero),
        )
        return cur.fetchone() is not None


def marquer_issue_triee(repo: str, numero: int) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO issues_triees (repo, numero) VALUES (?, ?)",
            (repo, numero),
        )


def triage_epuise(repo: str, numero: int) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM triage_epuise WHERE repo = ? AND numero = ?",
            (repo, numero),
        )
        return cur.fetchone() is not None


def marquer_triage_epuise(repo: str, numero: int) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO triage_epuise (repo, numero) VALUES (?, ?)",
            (repo, numero),
        )


def enregistrer_conso(repo: str, numero: int, etape: str,
                      entree: int, cache: int, sortie: int, cout: float,
                      modele: str | None = None) -> None:
    """Trace la conso d'un appel Claude, rattachée à un ticket et une étape.

    modele : alias --model utilisé (None = modèle par défaut de l'abonnement).
    """
    with _connexion() as conn:
        conn.execute(
            "INSERT INTO conso_claude"
            "  (repo, numero, etape, tokens_entree, tokens_cache, tokens_sortie, cout_usd, modele)"
            "  VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, numero, etape, entree, cache, sortie, cout, modele),
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


def orphelin_deja_signale(repo: str, numero: int, label_pose_le: str) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM orphelins_signales WHERE repo = ? AND numero = ? AND label_pose_le = ?",
            (repo, numero, label_pose_le),
        )
        return cur.fetchone() is not None


def marquer_orphelin(repo: str, numero: int, label_pose_le: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orphelins_signales (repo, numero, label_pose_le)"
            " VALUES (?, ?, ?)",
            (repo, numero, label_pose_le),
        )


def compter_orphelinages(repo: str, numero: int) -> int:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM orphelins_signales WHERE repo = ? AND numero = ?",
            (repo, numero),
        )
        return int(cur.fetchone()[0])


def conso_par_etape() -> list[tuple]:
    """Conso agrégée par étape (triage, implementation, review…) :
    (etape, appels, tokens lus, tokens générés, coût $)."""
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT etape, COUNT(*),"
            "       SUM(tokens_entree + tokens_cache), SUM(tokens_sortie), SUM(cout_usd)"
            "  FROM conso_claude GROUP BY etape ORDER BY SUM(cout_usd) DESC"
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


def meta_lire(cle: str) -> str | None:
    """Valeur brute d'une clé de la table meta (clé/valeur), None si absente."""
    with _connexion() as conn:
        cur = conn.execute("SELECT valeur FROM meta WHERE cle = ?", (cle,))
        ligne = cur.fetchone()
    return ligne[0] if ligne is not None else None


def meta_ecrire(cle: str, valeur: str) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (cle, valeur) VALUES (?, ?)",
            (cle, valeur),
        )


def meta_effacer(cle: str) -> None:
    with _connexion() as conn:
        conn.execute("DELETE FROM meta WHERE cle = ?", (cle,))


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


def forge_deja_signalee(repo: str, condition: str, version: int) -> bool:
    with _connexion() as conn:
        cur = conn.execute(
            "SELECT 1 FROM forge_signale WHERE repo = ? AND condition = ? AND version = ?",
            (repo, condition, version),
        )
        return cur.fetchone() is not None


def marquer_forge_signalee(repo: str, condition: str, version: int) -> None:
    with _connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO forge_signale (repo, condition, version) VALUES (?, ?, ?)",
            (repo, condition, version),
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
