"""Purge des workspaces — branches locales dont la PR est mergée.

`dev_followup.traiter_merges` supprime la branche **distante** dès qu'une PR
d'agent est mergée, mais la branche **locale** reste dans le workspace
(`state/workspaces/<owner>-<nom>`) : au fil des tickets, les `ai/<n>` mortes
s'empilent et retiennent des objets git que `gc` ne peut pas libérer tant
qu'une ref les atteint.

Ce pipeline supprime, pour chaque repo surveillé, les branches locales
`ai/*` dont la PR est **mergée** — puis lance un `git gc --prune=now` qui
transforme ces suppressions en octets réellement rendus.

Périmètre volontairement étroit, par prudence :
- **seulement** les branches préfixées `ai/` (celles de l'agent) — jamais une
  branche humaine, même à l'abandon ;
- **seulement** si la PR correspondante est mergée. Une PR fermée sans merge
  ou une branche sans PR peut porter du travail non repris : on n'y touche pas ;
- **jamais pendant qu'un agent code** : le tour prend `state/executor.lock`
  (voir `purge.py` à la racine). Sans ça, on supprimerait la branche sous les
  pieds de l'exécutant.

Ce que ce pipeline ne fait **pas** : supprimer les artefacts de build
(`node_modules`, `.turbo`, `dist`…). Ils sont gitignorés, donc invisibles pour
git, et pèsent bien plus lourd que l'historique — mais ils ne sont liés à
aucune PR, et les rebâtir coûte du temps de CI sur le Pi. C'est une décision
séparée.
"""

import shutil
import subprocess
from pathlib import Path

import yaml

from lib import notify
from pipelines.sante import octets

RACINE = Path(__file__).parent.parent
WORKSPACES = RACINE / "state" / "workspaces"
REPOS_YAML = RACINE / "data" / "repos.yaml"

BRANCHE_PREFIX = "ai/"


def _charger_repos() -> list[str]:
    """Repos surveillés (mêmes entrées que poll, config ignorée ici)."""
    if not REPOS_YAML.exists():
        return []
    config = yaml.safe_load(REPOS_YAML.read_text()) or {}
    entrees = config.get("repos") or []
    return [e if isinstance(e, str) else e["repo"] for e in entrees]


def _git(path: Path, *args: str) -> tuple[int, str]:
    """git dans le workspace → (code retour, sortie). Ne lève jamais.

    La purge est du confort : elle ne doit jamais faire échouer son tour ni
    masquer une vraie alerte. On remonte le code et l'appelant décide.
    """
    r = subprocess.run(["git", "-C", str(path), *args],
                       capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout + r.stderr).strip()


def _taille(path: Path) -> int:
    """Poids d'un répertoire en octets (du -sk), 0 si illisible."""
    try:
        r = subprocess.run(["du", "-sk", str(path)],
                           capture_output=True, text=True, timeout=120)
        return int(r.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def branches_locales(path: Path) -> list[str]:
    """Branches locales `ai/*` du workspace."""
    code, sortie = _git(path, "branch", "--format=%(refname:short)")
    if code != 0:
        return []
    return [b.strip() for b in sortie.splitlines()
            if b.strip().startswith(BRANCHE_PREFIX)]


def branches_mergees(repo: str, github) -> set[str]:
    """Noms de branches des PR d'agent **mergées** de ce repo.

    `github` est injecté pour que les tests n'aient pas à monkeypatcher un
    import de module.
    """
    return {
        pr["head"]
        for pr in github.list_pulls(repo, state="closed")
        # head_repo : jamais une PR de fork (même garde que dev_followup).
        if pr["merged_at"] and pr["head_repo"] == repo
        and pr["head"].startswith(BRANCHE_PREFIX)
    }


def purger_repo(repo: str, github) -> dict:
    """Purge le workspace d'un repo. → {branches: [...], recupere: octets}."""
    path = WORKSPACES / repo.replace("/", "-")
    if not (path / ".git").exists():
        return {"branches": [], "recupere": 0}

    locales = branches_locales(path)
    if not locales:
        return {"branches": [], "recupere": 0}

    a_purger = sorted(set(locales) & branches_mergees(repo, github))
    if not a_purger:
        return {"branches": [], "recupere": 0}

    avant = _taille(path)

    # Impossible de supprimer la branche courante : on détache HEAD d'abord.
    # `preparer()` refera un `checkout -B <base>` au prochain usage, donc un
    # HEAD détaché ne gêne personne.
    code, courante = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and courante in a_purger:
        code, sortie = _git(path, "checkout", "--detach")
        if code != 0:
            print(f"[purge] {repo} : HEAD non détachable ({sortie[:120]}) — "
                  f"branche {courante} conservée")
            a_purger = [b for b in a_purger if b != courante]

    purgees = []
    for branche in a_purger:
        # -D et non -d : la branche est mergée en amont, pas forcément
        # localement (l'agent n'a jamais mergé dans sa copie).
        code, sortie = _git(path, "branch", "-D", branche)
        if code == 0:
            purgees.append(branche)
        else:
            print(f"[purge] {repo} : {branche} non supprimée ({sortie[:120]})")

    if purgees:
        # Sans gc, supprimer les refs ne rend aucun octet : les objets restent
        # dans le pack et dans le reflog.
        _git(path, "reflog", "expire", "--expire=now", "--all")
        _git(path, "gc", "--prune=now", "--quiet")

    return {"branches": purgees, "recupere": max(0, avant - _taille(path))}


async def purger(repos: list[str] | None = None, github=None) -> str:
    """Purge tous les repos surveillés. Renvoie un résumé (pour les logs)."""
    if github is None:  # import tardif : garde le module testable sans réseau
        from lib import github as _github
        github = _github
    if repos is None:
        repos = _charger_repos()

    total_branches, total_octets, details = 0, 0, []
    for repo in repos:
        try:
            r = purger_repo(repo, github)
        except Exception as exc:  # noqa: BLE001 — un repo qui casse n'arrête rien
            print(f"[purge] {repo} : purge en échec ({exc}) — repo sauté")
            continue
        if r["branches"]:
            total_branches += len(r["branches"])
            total_octets += r["recupere"]
            details.append(f"{repo} : {len(r['branches'])} branche(s) "
                           f"({', '.join(r['branches'])})")
            print(f"[purge] {repo} : {len(r['branches'])} branche(s) purgée(s), "
                  f"{octets(r['recupere'])} rendus")

    if not total_branches:
        return "rien à purger (aucune branche locale de PR mergée)"

    reste = shutil.disk_usage("/")
    await notify.notify(
        f"🧹 Purge des workspaces : {total_branches} branche(s) de PR mergées "
        f"supprimée(s), {octets(total_octets)} rendus.\n"
        + "\n".join(details)
        + f"\nDisque : {octets(reste.free)} libres."
    )
    return f"{total_branches} branche(s) purgée(s), {octets(total_octets)} rendus"


async def handle() -> str:
    """Point d'entrée du pipeline (timer systemd)."""
    return await purger()
