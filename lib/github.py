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


def _get(path: str, params: dict | None = None):
    r = requests.get(f"{API}{path}", headers=_headers(), params=params, timeout=15)
    if r.status_code == 401:
        raise GitHubError("401 — GITHUB_TOKEN invalide ou manquant")
    if r.status_code == 404:
        raise GitHubError(f"404 — introuvable : {path} (repo privé sans token ?)")
    if r.status_code >= 300:
        raise GitHubError(f"HTTP {r.status_code} : {r.text[:200]}")
    return r.json()


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
