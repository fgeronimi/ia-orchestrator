"""Poller — un tour du pipeline dev GitHub, sur tous les repos surveillés.

Repos surveillés : `data/repos.yaml` (clé `repos`), sinon `WATCHED_REPO` du
.env, sinon l'argument CLI (qui prime sur tout, pratique pour un test ciblé).

Un tour, pour CHAQUE repo :
  1. notifie les nouvelles issues `ai-ready` (dédup SQLite via lib/state)
  2. suite après merge : nettoie les PR d'agent mergées (dev_followup, léger)
  3. CI : notifie le résultat des check runs des PR d'agent (une fois par sha)
puis UNE SEULE action lourde (Claude) tous repos confondus, sous verrou
fichier, par ordre de priorité :
  1. nouveaux commentaires humains sur une PR d'agent → dev_executor.reviser
  2. CI rouge réparable sur une PR d'agent → dev_executor.corriger_ci
  3. première issue `ai-ready` → dev_executor.executer

Idempotence : l'exécutant pose `ai-working` (et retire `ai-ready`) dès la
prise en charge ; commentaires, PR mergées et statuts CI traités sont
mémorisés en SQLite. Le verrou (flock sur state/executor.lock) empêche deux
exécutants simultanés ; il est libéré automatiquement à la mort du process,
donc pas de verrou orphelin après un crash.

Usage :
    .venv/bin/python poll.py                          # repos.yaml / WATCHED_REPO
    .venv/bin/python poll.py fgeronimi/ia-orchestrator  # un repo précis
"""

import asyncio
import fcntl
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from lib import github, notify, state
from pipelines import dev_executor, dev_followup

LABEL = "ai-ready"
RACINE = Path(__file__).parent
VERROU = RACINE / "state" / "executor.lock"
REPOS_YAML = RACINE / "data" / "repos.yaml"


def charger_repos() -> list[str]:
    """Repos surveillés : data/repos.yaml, sinon WATCHED_REPO du .env."""
    if REPOS_YAML.exists():
        config = yaml.safe_load(REPOS_YAML.read_text()) or {}
        if config.get("repos"):
            return config["repos"]
    repo = os.environ.get("WATCHED_REPO", "")
    return [repo] if repo else []


async def poll(repos: list[str]) -> None:
    a_faire: list[tuple[str, dict]] = []  # (repo, issue) candidats à l'exécution

    for repo in repos:
        issues = github.list_issues(repo, labels=LABEL)
        nouveaux = [i for i in issues if not state.deja_notifiee(repo, i["number"])]

        for issue in nouveaux:
            await notify.notify(
                f"🎫 Ticket à faire — {repo}#{issue['number']} : {issue['title']}\n"
                f"{issue['url']}"
            )
            # Marqué après la notif. Limite connue : si le webhook est down, notify
            # loggue l'erreur mais l'issue est quand même marquée (pas de re-tentative).
            state.marquer_notifiee(repo, issue["number"])
            print(f"[poll] notifié {repo}#{issue['number']} {issue['title']}")

        # --- Phase 2/3 : suivi post-merge et CI (API seulement, pas de Claude)
        await dev_followup.traiter_merges(repo)
        await dev_followup.surveiller_ci(repo)

        a_faire += [(repo, i) for i in issues]

    # Quota Claude épuisé (mémorisé par l'exécutant) : notifs et suivi ont eu
    # lieu, mais on saute les actions lourdes jusqu'à la reprise, sans spammer.
    reprise = state.quota_bloque_jusqua()
    if reprise is not None:
        print("[poll] quota Claude épuisé — action lourde sautée, reprise vers "
              + time.strftime("%H:%M", time.localtime(reprise)))
        return

    # Première PR d'agent avec de nouveaux commentaires, tous repos confondus.
    revision = None
    for repo in repos:
        trouvee = dev_executor.chercher_revision(repo)
        if trouvee is not None:
            revision = (repo, *trouvee)
            break

    # À défaut, première PR d'agent dont la CI est rouge et réparable.
    ci_rouge = None
    if revision is None:
        for repo in repos:
            trouvee = await dev_followup.chercher_ci_rouge(repo)
            if trouvee is not None:
                ci_rouge = (repo, *trouvee)
                break

    if not a_faire and revision is None and ci_rouge is None:
        print(f"[poll] rien à traiter (ni issue '{LABEL}', ni révision, ni CI rouge)")
        return

    # --- Une action lourde par tour, sous verrou -----------------------------
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[poll] exécutant déjà en cours (verrou pris), on repassera")
            return
        # Priorité : révision (débloquer ta review) > CI rouge (réparer
        # l'existant) > nouveau ticket.
        if revision is not None:
            repo, pr, commentaires = revision
            print(f"[poll] révision de la PR {repo}#{pr['number']} "
                  f"({len(commentaires)} commentaire(s))")
            await dev_executor.reviser(repo, pr, commentaires)
        elif ci_rouge is not None:
            repo, pr, echecs, log = ci_rouge
            print(f"[poll] correction CI de la PR {repo}#{pr['number']}")
            await dev_executor.corriger_ci(repo, pr, echecs, log)
        else:
            repo, issue = a_faire[0]
            print(f"[poll] exécution de {repo}#{issue['number']} {issue['title']}")
            await dev_executor.executer(repo, issue)


if __name__ == "__main__":
    load_dotenv()
    repos = [sys.argv[1]] if len(sys.argv) > 1 else charger_repos()
    if not repos:
        sys.exit("Usage : python poll.py [owner/repo]  "
                 "(ou data/repos.yaml, ou WATCHED_REPO)")
    asyncio.run(poll(repos))
