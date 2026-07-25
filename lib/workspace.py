"""Workspaces : clones locaux des repos où l'exécutant travaille.

Un workspace par repo sous state/workspaces/<owner>-<nom>. Cloné à la première
utilisation, remis à jour ensuite. L'auth git passe par le token dans l'URL
d'accès (state/ est local au Pi et gitignored : le token ne sort jamais du Pi,
et n'est pas persisté dans le remote — on le passe explicitement aux commandes
réseau).
"""

import os
import subprocess
from pathlib import Path

WORKSPACES = Path(__file__).parent.parent / "state" / "workspaces"

# Identité des commits produits par l'orchestrateur (distincte de l'humain).
BOT_NAME = "ia-orchestrator"
BOT_EMAIL = "bot@ia-orchestrator.local"


class WorkspaceError(RuntimeError):
    pass


def _url(repo: str) -> str:
    """URL HTTPS authentifiée. Isolée pour être monkeypatchable en test."""
    token = os.environ.get("GITHUB_TOKEN", "")
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _scrub(texte: str) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    return texte.replace(token, "***") if token else texte


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise WorkspaceError(f"git {args[0]} : {_scrub(r.stderr).strip()[:300]}")
    return r.stdout.strip()


def preparer(repo: str, base: str) -> Path:
    """Clone (ou met à jour) le repo, se place sur `base` propre et à jour."""
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    path = WORKSPACES / repo.replace("/", "-")

    if not (path / ".git").exists():
        r = subprocess.run(["git", "clone", _url(repo), str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise WorkspaceError(f"clone : {_scrub(r.stderr).strip()[:300]}")

    _git(path, "fetch", _url(repo), base)
    _git(path, "checkout", "-B", base, "FETCH_HEAD")
    _git(path, "reset", "--hard", "FETCH_HEAD")
    _git(path, "clean", "-fd")
    return path


def creer_branche(path: Path, branche: str) -> None:
    _git(path, "checkout", "-B", branche)


def basculer_sur(path: Path, repo: str, branche: str) -> None:
    """Se place sur une branche qui existe déjà sur le remote (révision de PR)."""
    _git(path, "fetch", _url(repo), branche)
    _git(path, "checkout", "-B", branche, "FETCH_HEAD")


def commit_tout(path: Path, message: str) -> bool:
    """Commit tous les changements. Retourne False si rien à committer."""
    _git(path, "add", "-A")
    if not _git(path, "status", "--porcelain"):
        return False
    _git(path, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
         "commit", "-m", message)
    return True


def diff_contre(path: Path, base: str) -> str:
    """Diff de la branche courante contre la base (pour l'auto-review)."""
    return _git(path, "diff", f"{base}...HEAD")


def merger_base(path: Path, base: str) -> bool:
    """Merge la base (déjà fetchée par preparer) dans la branche courante.

    Retourne True si le merge est propre (commit de merge créé), False si des
    conflits restent à résoudre (marqueurs en place dans l'arbre de travail).
    """
    r = subprocess.run(
        ["git", "-C", str(path), "-c", f"user.name={BOT_NAME}",
         "-c", f"user.email={BOT_EMAIL}", "merge", base,
         "-m", f"ai: merge {base}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return True
    if "CONFLICT" in r.stdout + r.stderr:
        return False
    raise WorkspaceError(f"git merge : {_scrub(r.stderr).strip()[:300]}")


def fichiers_en_conflit(path: Path) -> list[str]:
    """Fichiers encore en conflit dans un merge en cours."""
    sortie = _git(path, "diff", "--name-only", "--diff-filter=U")
    return [f for f in sortie.splitlines() if f]


def marqueurs_restants(path: Path, fichiers: list[str]) -> list[str]:
    """Fichiers de la liste contenant encore des marqueurs de conflit."""
    restants = []
    for f in fichiers:
        contenu = (path / f).read_text(errors="replace") if (path / f).exists() else ""
        if "<<<<<<< " in contenu or ">>>>>>> " in contenu:
            restants.append(f)
    return restants


def abandonner_merge(path: Path) -> None:
    """Abandonne un merge en cours — la branche redevient propre."""
    _git(path, "merge", "--abort")


def pousser(path: Path, repo: str, branche: str) -> None:
    # force : la branche ai/<n> appartient à l'agent, on la réécrit sans risque.
    _git(path, "push", "-f", _url(repo), f"{branche}:{branche}")
