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
    """Un ruleset actif ciblant la branche par défaut, exigeant une pull request.

    Cibles reconnues : la branche nommée, `~DEFAULT_BRANCH` et `~ALL`
    (toutes les branches — inclut la branche par défaut). Un ruleset qui
    l'exclut explicitement ne compte pas.
    """
    cible_branche = f"refs/heads/{branche_defaut}"
    cibles = {cible_branche, "~DEFAULT_BRANCH", "~ALL"}
    for ruleset in github.list_rulesets(repo):
        if ruleset["enforcement"] != "active":
            continue
        detail = github.get_ruleset(repo, ruleset["id"])
        ref_name = detail["conditions"].get("ref_name", {})
        include = set(ref_name.get("include", []))
        exclude = set(ref_name.get("exclude", []))
        if not (include & cibles):
            continue
        if exclude & {cible_branche, "~DEFAULT_BRANCH"}:
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

    if conditions.get("protection_main"):
        try:
            protege = _protection_main_active(repo, branche_defaut)
        except github.GitHubError as exc:
            # Repos privés sous plan gratuit : l'API rulesets répond 403
            # (« Upgrade to GitHub Pro »). La condition est insatisfiable —
            # exiger un geste que GitHub refuse serait absurde. On saute,
            # en le disant dans les logs.
            if "403" in str(exc):
                print(f"[forge] {repo} : rulesets non vérifiables (403 — "
                      f"plan/permissions), condition protection_main sautée")
                protege = True
            else:
                raise
        if not protege:
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
    sautes: list[str] = []
    for repo in _charger_repos():
        # Un repo en erreur (403, réseau, rate limit) ne doit pas masquer la
        # vérification des suivants : on isole, on continue, on notifie.
        try:
            ecarts = _ecarts(repo, conditions)
        except Exception as exc:  # noqa: BLE001 — frontière d'isolation par repo
            print(f"[forge] {repo} : vérification impossible ({exc}) — repo sauté")
            sautes.append(repo)
            continue
        for condition, description in ecarts:
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

    if total or sautes:
        morceaux = []
        if total:
            morceaux.append(f"{total} écart(s) de conformité signalé(s) — "
                            f"voir les nouvelles issues `forge:`")
        if sautes:
            morceaux.append(f"⚠️ repo(s) non vérifié(s) : {', '.join(sautes)}")
        await notify.notify("🛠️ forge : " + " · ".join(morceaux))

    bilan = f"{total} écart(s) signalé(s)" if total else "aucun écart"
    if sautes:
        bilan += f", {len(sautes)} repo(s) sauté(s)"
    return bilan
