"""Purge des workspaces — branches locales dont la PR est mergée.

`dev_followup.traiter_merges` supprime la branche **distante** dès qu'une PR
d'agent est mergée, mais la branche **locale** reste dans le workspace
(`state/workspaces/<owner>-<nom>`) : au fil des tickets, les `ai/<n>` mortes
s'empilent et retiennent des objets git que `gc` ne peut pas libérer tant
qu'une ref les atteint.

Ce pipeline supprime, pour chaque repo surveillé, les branches locales `ai/*`
qui n'ont plus de raison d'exister, puis lance un `git gc --prune=now` qui
transforme ces suppressions en octets réellement rendus.

Deux critères, **complémentaires** :

1. **La PR est mergée** (`merged_at` non nul). Indispensable pour les merges
   *squash* ou *rebase* : GitHub réécrit les commits, donc la branche n'est
   **pas** un ancêtre de `main` alors que son travail y est bel et bien.
2. **Tous ses commits sont déjà dans la branche par défaut**
   (`git merge-base --is-ancestor <branche> <base>`). Attrape ce que le
   critère 1 laisse passer : branche orpheline sans PR mais mergée à la main,
   branche vide créée puis abandonnée. Si le contenu est dans `main`, la ref ne
   protège plus rien — la supprimer ne perd aucun commit, ils restent
   joignables depuis `main`.

Périmètre volontairement étroit, par prudence :
- **seulement** les branches préfixées `ai/` (celles de l'agent) — jamais une
  branche humaine, même à l'abandon ;
- une branche `ai/*` **sans PR mergée et absente de `main`** est conservée :
  elle peut porter du travail non repris (cas vécu : 3 orphelines épargnées le
  2026-07-30, l'agent ayant crashé avant d'ouvrir la PR) ;
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

from lib import notify, workspace
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
    """git dans le workspace → (code retour, sortie **expurgée**). Ne lève jamais.

    La purge est du confort : elle ne doit jamais faire échouer son tour ni
    masquer une vraie alerte. On remonte le code et l'appelant décide.

    La sortie passe par `workspace._scrub` : un `fetch` en échec recrache l'URL
    authentifiée dans son message d'erreur (« could not read Username for
    https://x-access-token:<token>@github.com/… »), et cette sortie part dans
    les logs systemd. Le token ne doit jamais y apparaître.
    """
    r = subprocess.run(["git", "-C", str(path), *args],
                       capture_output=True, text=True, timeout=300)
    return r.returncode, workspace._scrub((r.stdout + r.stderr).strip())


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


def branches_dans_base(path: Path, repo: str, github) -> set[str]:
    """Branches `ai/*` locales dont tous les commits sont déjà dans la base.

    La base (branche par défaut du repo) est **refetchée** avant comparaison :
    se fier au `main` local suffirait à rater tout ce qui a été mergé depuis le
    dernier passage de l'exécutant. En cas d'échec du fetch, on renvoie un
    ensemble vide — pas de verdict, donc aucune suppression.

    `workspace._url` est réutilisé volontairement : c'est le seul endroit du
    projet qui construit l'URL authentifiée, et dupliquer la manipulation du
    token ici serait pire que d'emprunter un helper privé du même paquet.
    """
    base = github.get_default_branch(repo)
    code, sortie = _git(path, "fetch", workspace._url(repo), base)
    if code != 0:
        print(f"[purge] {repo} : fetch de {base} en échec ({sortie[:120]}) — "
              f"critère « déjà dans {base} » non évalué")
        return set()

    contenues = set()
    for branche in branches_locales(path):
        # --is-ancestor : code 0 si tous les commits de la branche sont
        # joignables depuis FETCH_HEAD. Une branche vide l'est trivialement.
        code, _ = _git(path, "merge-base", "--is-ancestor", branche, "FETCH_HEAD")
        if code == 0:
            contenues.add(branche)
    return contenues


def purger_repo(repo: str, github) -> dict:
    """Purge le workspace d'un repo.

    → {branches: [...], raisons: {branche: raison}, recupere: octets}
    """
    vide = {"branches": [], "raisons": {}, "recupere": 0}
    path = WORKSPACES / repo.replace("/", "-")
    if not (path / ".git").exists():
        return vide

    locales = set(branches_locales(path))
    if not locales:
        return vide

    mergees = locales & branches_mergees(repo, github)
    # Évalué même si `mergees` couvre déjà tout : c'est ce critère qui attrape
    # les orphelines sans PR, et il ne coûte qu'un fetch de la base.
    dans_base = locales & branches_dans_base(path, repo, github)

    raisons = {b: "PR mergée" for b in mergees}
    base = None
    for b in dans_base - mergees:
        if base is None:
            base = github.get_default_branch(repo)
        raisons[b] = f"déjà dans {base}"

    a_purger = sorted(raisons)
    if not a_purger:
        return vide

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
            print(f"[purge] {repo} : {branche} supprimée ({raisons[branche]})")
        else:
            print(f"[purge] {repo} : {branche} non supprimée ({sortie[:120]})")

    if purgees:
        # Sans gc, supprimer les refs ne rend aucun octet : les objets restent
        # dans le pack et dans le reflog.
        _git(path, "reflog", "expire", "--expire=now", "--all")
        _git(path, "gc", "--prune=now", "--quiet")

    return {"branches": purgees,
            "raisons": {b: raisons[b] for b in purgees},
            "recupere": max(0, avant - _taille(path))}


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
            raisons = r.get("raisons", {})
            # La raison figure dans la notif : « déjà dans main » sur une
            # branche sans PR est le cas qu'on veut pouvoir relire après coup.
            details.append(f"{repo} : " + ", ".join(
                f"{b} ({raisons[b]})" if b in raisons else b
                for b in r["branches"]))
            print(f"[purge] {repo} : {len(r['branches'])} branche(s) purgée(s), "
                  f"{octets(r['recupere'])} rendus")

    if not total_branches:
        return "rien à purger (aucune branche mergée ni contenue dans la base)"

    reste = shutil.disk_usage("/")
    await notify.notify(
        f"🧹 Purge des workspaces : {total_branches} branche(s) supprimée(s), "
        f"{octets(total_octets)} rendus.\n"
        + "\n".join(details)
        + f"\nDisque : {octets(reste.free)} libres."
    )
    return f"{total_branches} branche(s) purgée(s), {octets(total_octets)} rendus"


async def handle() -> str:
    """Point d'entrée du pipeline (timer systemd)."""
    return await purger()
