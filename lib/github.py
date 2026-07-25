"""Wrapper minimal de l'API GitHub REST.

Toute interaction avec GitHub passe par ici — même principe que lib/claude et
lib/notify : un seul endroit à modifier si l'auth ou l'API change.

Auth : GITHUB_TOKEN (PAT fine-grained) dans l'environnement. Optionnel pour les
repos publics (API anonyme, 60 req/h) ; requis pour les repos privés.

Phase 0 : lecture seule (lister les issues). L'écriture (branches, PR,
commentaires) viendra en phase 1.
"""

import os

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _headers() -> dict:
    entetes = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        entetes["Authorization"] = f"Bearer {token}"
    return entetes


def _request(method: str, path: str, payload: dict | None = None,
             params: dict | None = None):
    r = requests.request(method, f"{API}{path}", headers=_headers(),
                         json=payload, params=params, timeout=15)
    if r.status_code == 401:
        raise GitHubError("401 — GITHUB_TOKEN invalide ou manquant")
    if r.status_code == 403:
        raise GitHubError(f"403 — permission refusée (scope du token ?) : {r.text[:200]}")
    if r.status_code == 404:
        raise GitHubError(f"404 — introuvable : {path} (repo privé sans token ?)")
    if r.status_code >= 300:
        raise GitHubError(f"HTTP {r.status_code} : {r.text[:200]}")
    return r.json() if r.text else {}


def _get(path: str, params: dict | None = None):
    return _request("GET", path, params=params)


def list_issues(repo: str, labels: str | list[str] | None = None,
                state: str = "open") -> list[dict]:
    """Issues d'un repo `owner/nom`, filtrables par label(s).

    Retourne une liste allégée (number, title, labels, url). Exclut les PR :
    l'endpoint /issues de GitHub les mélange avec les vraies issues.
    """
    params = {"state": state, "per_page": 100}
    if labels:
        params["labels"] = labels if isinstance(labels, str) else ",".join(labels)

    brut = _get(f"/repos/{repo}/issues", params)
    return [
        {
            "number": i["number"],
            "title": i["title"],
            "labels": [lbl["name"] for lbl in i["labels"]],
            "url": i["html_url"],
        }
        for i in brut
        if "pull_request" not in i  # exclure les PR
    ]


# --- Écriture (Phase 1) : nécessite un PAT avec Contents+Pull requests+Issues=write

def get_default_branch(repo: str) -> str:
    return _get(f"/repos/{repo}")["default_branch"]


def add_labels(repo: str, numero: int, labels: list[str]) -> None:
    _request("POST", f"/repos/{repo}/issues/{numero}/labels", {"labels": labels})


def remove_label(repo: str, numero: int, label: str) -> None:
    """Retire un label. Ignore le 404 (label déjà absent)."""
    try:
        _request("DELETE", f"/repos/{repo}/issues/{numero}/labels/{label}")
    except GitHubError as exc:
        if "404" not in str(exc):
            raise


def comment_issue(repo: str, numero: int, body: str) -> dict:
    return _request("POST", f"/repos/{repo}/issues/{numero}/comments", {"body": body})


def create_pull(repo: str, head: str, base: str, title: str, body: str,
                draft: bool = True) -> dict:
    """Ouvre une PR (draft par défaut). Retourne le dict PR (number, html_url…)."""
    return _request("POST", f"/repos/{repo}/pulls", {
        "title": title, "head": head, "base": base, "body": body, "draft": draft,
    })
