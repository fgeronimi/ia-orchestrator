"""Poller — un tour du pipeline dev GitHub.

Un tour :
  1. notifie les nouvelles issues `ai-ready` (dédup SQLite via lib/state)
  2. suite après merge : nettoie les PR d'agent mergées (dev_followup, léger)
  3. UNE action lourde (Claude) sous verrou fichier, révision prioritaire :
     - nouveaux commentaires humains sur une PR d'agent → dev_executor.reviser
     - sinon première issue `ai-ready` → dev_executor.executer

Idempotence : l'exécutant pose `ai-working` (et retire `ai-ready`) dès la
prise en charge ; les commentaires et PR mergées traités sont mémorisés en
SQLite. Le verrou (flock sur state/executor.lock) empêche deux exécutants
simultanés ; il est libéré automatiquement à la mort du process, donc pas de
verrou orphelin après un crash.

Usage :
    .venv/bin/python poll.py fgeronimi/ia-orchestrator
    # ou repo lu dans WATCHED_REPO du .env
"""

import asyncio
import fcntl
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from lib import github, notify, state
from pipelines import dev_executor, dev_followup

LABEL = "ai-ready"
VERROU = Path(__file__).parent / "state" / "executor.lock"


async def poll(repo: str) -> None:
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
        print(f"[poll] notifié #{issue['number']} {issue['title']}")

    # --- Phase 2 : suite après merge (API seulement, pas de Claude) ----------
    await dev_followup.traiter_merges(repo)

    revision = dev_executor.chercher_revision(repo)
    if not issues and revision is None:
        print(f"[poll] rien à traiter (ni issue '{LABEL}', ni révision)")
        return

    # --- Une action lourde par tour, sous verrou -----------------------------
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[poll] exécutant déjà en cours (verrou pris), on repassera")
            return
        # Révision prioritaire : débloquer une review en cours passe avant
        # entamer un nouveau ticket.
        if revision is not None:
            pr, commentaires = revision
            print(f"[poll] révision de la PR #{pr['number']} "
                  f"({len(commentaires)} commentaire(s))")
            await dev_executor.reviser(repo, pr, commentaires)
        else:
            issue = issues[0]
            print(f"[poll] exécution de #{issue['number']} {issue['title']}")
            await dev_executor.executer(repo, issue)


if __name__ == "__main__":
    load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WATCHED_REPO", "")
    if not repo:
        sys.exit("Usage : python poll.py <owner/repo>  (ou définir WATCHED_REPO)")
    asyncio.run(poll(repo))
