"""Forge — conditions déclaratives des repos surveillés (data/forge.yaml).

Vérifie, pour chaque repo de `data/repos.yaml`, que les conditions de
`data/forge.yaml` sont remplies (labels requis, fichiers présents sur la
branche par défaut, protection de `main` par un ruleset actif). Purement API
GitHub, aucun appel Claude : ce sont des conditions objectives, pas de
jugement à porter.

Pour chaque écart : une issue **sans label** sur le repo concerné (poser
`ai-ready` reste un geste humain), dédupliquée par (repo, condition, version)
via `lib/state` — un changement de version (conditions modifiées) relance le
signalement même pour un écart déjà signalé sous l'ancienne version.

Appelé une fois par jour par le timer `orchestrator-forge` (voir forge.py à
la racine) — sans lien avec le poller `poll.py` (5 min, pipeline dev).
"""

from pathlib import Path

import yaml

from lib import github, notify, state

RACINE = Path(__file__).parent.parent
FORGE_YAML = RACINE / "data" / "forge.yaml"
REPOS_YAML = RACINE / "data" / "repos.yaml"


def _charger_repos() -> list[str]:
    if not REPOS_YAML.exists():
        return []
    config = yaml.safe_load(REPOS_YAML.read_text()) or {}
    entrees = config.get("repos") or []
    return [e if isinstance(e, str) else e["repo"] for e in entrees]


def _charger_conditions() -> dict:
    if not FORGE_YAML.exists():
        return {}
    return yaml.safe_load(FORGE_YAML.read_text()) or {}


def _protection_main_active(repo: str, branche_defaut: str) -> bool:
    """Un ruleset actif ciblant la branche par défaut, exigeant une pull request."""
    cible_branche = f"refs/heads/{branche_defaut}"
    for ruleset in github.list_rulesets(repo):
        if ruleset["enforcement"] != "active":
            continue
        detail = github.get_ruleset(repo, ruleset["id"])
        refs = detail["conditions"].get("ref_name", {}).get("include", [])
        if cible_branche not in refs and "~DEFAULT_BRANCH" not in refs:
            continue
        if any(r["type"] == "pull_request" for r in detail["rules"]):
            return True
    return False


def _ecarts(repo: str, conditions: dict) -> list[tuple[str, str]]:
    """Écarts (condition, description) entre ce repo et les conditions de la forge."""
    ecarts: list[tuple[str, str]] = []

    labels_presents = set(github.list_labels(repo))
    for label in conditions.get("labels") or []:
        if label not in labels_presents:
            ecarts.append((
                f"label manquant : {label}",
                f"Le label `{label}` n'existe pas sur ce repo.\n\n"
                f"Geste attendu : créer le label `{label}` "
                f"(Settings → Labels → New label).",
            ))

    branche_defaut = github.get_default_branch(repo)
    for chemin in conditions.get("fichiers") or []:
        if not github.fichier_existe(repo, chemin, ref=branche_defaut):
            ecarts.append((
                f"fichier manquant : {chemin}",
                f"Le fichier `{chemin}` n'existe pas sur `{branche_defaut}`.\n\n"
                f"Geste attendu : ajouter `{chemin}` à la racine du repo.",
            ))

    if conditions.get("protection_main") and not _protection_main_active(repo, branche_defaut):
        ecarts.append((
            "protection_main",
            f"Aucun ruleset actif n'exige de pull request sur `{branche_defaut}`.\n\n"
            f"Geste attendu : créer un ruleset actif (Settings → Rules → "
            f"Rulesets), ciblant `{branche_defaut}`, avec la règle "
            f"« Require a pull request before merging ».",
        ))

    return ecarts


async def handle() -> str:
    conditions = _charger_conditions()
    version = conditions.get("version", 1)

    total = 0
    for repo in _charger_repos():
        for condition, description in _ecarts(repo, conditions):
            if state.forge_deja_signalee(repo, condition, version):
                continue
            github.create_issue(
                repo,
                title=f"forge: {condition}",
                body=(
                    f"{description}\n\n"
                    f"_Condition de `data/forge.yaml` (version {version}) — "
                    f"signalé automatiquement par la forge, aucun label posé._"
                ),
            )
            state.marquer_forge_signalee(repo, condition, version)
            total += 1
            print(f"[forge] écart signalé sur {repo} : {condition}")

    if total:
        await notify.notify(
            f"🛠️ forge : {total} écart(s) de conformité signalé(s) — "
            f"voir les nouvelles issues `forge:` sur les repos concernés"
        )

    return f"{total} écart(s) signalé(s)" if total else "aucun écart"
