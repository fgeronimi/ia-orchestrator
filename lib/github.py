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


def list_labels(repo: str) -> list[str]:
    """Noms des labels existants sur le repo."""
    brut = _get(f"/repos/{repo}/labels", {"per_page": 100})
    return [lbl["name"] for lbl in brut]


def fichier_existe(repo: str, chemin: str, ref: str | None = None) -> bool:
    """Un fichier existe-t-il à ce chemin (ref = branche, défaut : branche par défaut) ?"""
    try:
        _get(f"/repos/{repo}/contents/{chemin}", {"ref": ref} if ref else None)
        return True
    except GitHubError as exc:
        if "404" in str(exc):
            return False
        raise


def list_rulesets(repo: str) -> list[dict]:
    """Rulesets du repo (id, enforcement) — le détail (branches ciblées, règles)
    n'est pas dans cet endpoint de liste, voir get_ruleset."""
    brut = _get(f"/repos/{repo}/rulesets")
    return [{"id": r["id"], "name": r["name"], "enforcement": r["enforcement"]}
            for r in brut]


def get_ruleset(repo: str, ruleset_id: int) -> dict:
    """Détail d'un ruleset : branches ciblées (conditions) et règles actives (rules)."""
    r = _get(f"/repos/{repo}/rulesets/{ruleset_id}")
    return {"conditions": r.get("conditions", {}), "rules": r.get("rules", [])}


def get_issue(repo: str, numero: int) -> dict:
    """Issue complète, avec son corps (title, body, labels, url)."""
    i = _get(f"/repos/{repo}/issues/{numero}")
    return {
        "number": i["number"],
        "title": i["title"],
        "body": i.get("body") or "",
        "labels": [lbl["name"] for lbl in i["labels"]],
        "url": i["html_url"],
    }


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


def create_issue(repo: str, title: str, body: str,
                 labels: list[str] | None = None) -> dict:
    charge: dict = {"title": title, "body": body}
    if labels:
        charge["labels"] = labels
    return _request("POST", f"/repos/{repo}/issues", charge)


def find_open_pull(repo: str, head_branch: str) -> dict | None:
    """PR ouverte pour cette branche head, ou None."""
    owner = repo.split("/")[0]
    prs = _get(f"/repos/{repo}/pulls",
               {"head": f"{owner}:{head_branch}", "state": "open"})
    return prs[0] if prs else None


def create_pull(repo: str, head: str, base: str, title: str, body: str,
                draft: bool = True) -> dict:
    """Ouvre une PR (draft par défaut). Retourne le dict PR (number, html_url…)."""
    return _request("POST", f"/repos/{repo}/pulls", {
        "title": title, "head": head, "base": base, "body": body, "draft": draft,
    })


# --- Phase 2 : suivi post-merge et boucle de révision

def list_pulls(repo: str, state: str = "open") -> list[dict]:
    """PR d'un repo, liste allégée. `merged_at` est None si non mergée."""
    brut = _get(f"/repos/{repo}/pulls",
                {"state": state, "per_page": 100, "sort": "updated", "direction": "desc"})
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "head": p["head"]["ref"],
            "base": p["base"]["ref"],
            "sha": p["head"]["sha"],
            # Repo d'où vient la branche head (None si fork supprimé) : permet
            # d'écarter les PR de forks, qui ne sont jamais des PR d'agent.
            "head_repo": (p["head"]["repo"] or {}).get("full_name"),
            "labels": [lbl["name"] for lbl in p["labels"]],
            "merged_at": p["merged_at"],
            "html_url": p["html_url"],
        }
        for p in brut
    ]


def get_pull(repo: str, numero: int) -> dict:
    """Détail d'une PR — seul cet endpoint expose `mergeable`.

    `mergeable` vaut None pendant que GitHub calcule (réessayer au tour
    suivant), True si le merge est propre, False en cas de conflit.
    """
    p = _get(f"/repos/{repo}/pulls/{numero}")
    return {
        "number": p["number"],
        "title": p["title"],
        "head": p["head"]["ref"],
        "base": p["base"]["ref"],
        "sha": p["head"]["sha"],
        "head_repo": (p["head"]["repo"] or {}).get("full_name"),
        "mergeable": p["mergeable"],
        "html_url": p["html_url"],
    }


def list_check_runs(repo: str, ref: str) -> list[dict]:
    """Check runs (GitHub Actions & co) d'un commit. Vide si pas de CI."""
    brut = _get(f"/repos/{repo}/commits/{ref}/check-runs", {"per_page": 100})
    return [{"id": r["id"], "name": r["name"], "status": r["status"],
             "conclusion": r["conclusion"]}
            for r in brut["check_runs"]]


def get_job_log(repo: str, job_id: int) -> str:
    """Log brut d'un job GitHub Actions (l'id de check run d'Actions = id de job).

    Chaîne vide si le log n'est pas/plus disponible (expiré, permissions) :
    l'appelant se débrouille sans.
    """
    r = requests.get(f"{API}/repos/{repo}/actions/jobs/{job_id}/logs",
                     headers=_headers(), timeout=30)
    return r.text if r.status_code < 300 else ""


def list_comments(repo: str, numero: int) -> list[dict]:
    """Commentaires de conversation d'une issue ou PR (même endpoint)."""
    brut = _get(f"/repos/{repo}/issues/{numero}/comments", {"per_page": 100})
    return [{"id": c["id"], "body": c["body"], "user": c["user"]["login"]}
            for c in brut]


def list_review_comments(repo: str, numero: int) -> list[dict]:
    """Commentaires de review posés sur le diff d'une PR (avec le fichier visé)."""
    brut = _get(f"/repos/{repo}/pulls/{numero}/comments", {"per_page": 100})
    return [{"id": c["id"], "body": c["body"], "user": c["user"]["login"],
             "path": c.get("path")}
            for c in brut]


def delete_branch(repo: str, branche: str) -> None:
    """Supprime une branche distante. Ignore le 422 (déjà supprimée)."""
    try:
        _request("DELETE", f"/repos/{repo}/git/refs/heads/{branche}")
    except GitHubError as exc:
        if "422" not in str(exc) and "404" not in str(exc):
            raise
