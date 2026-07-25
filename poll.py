"""Poller — détecte les issues `ai-ready`, notifie, et lance l'exécutant.

Un tour : lit les issues taggées `ai-ready` du repo, notifie les nouvelles sur
Discord (dédup SQLite via lib/state), puis traite la première en inline via
pipelines/dev_executor — UN ticket par tour, sous verrou fichier.

Idempotence de l'exécution : l'exécutant pose `ai-working` (et retire
`ai-ready`) dès la prise en charge, donc l'issue sort du filtre au tour
suivant. Le verrou (flock sur state/executor.lock) empêche deux exécutants
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
from pipelines import dev_executor

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

    if not issues:
        print(f"[poll] aucune issue '{LABEL}' à traiter")
        return

    # --- Increment 1c : exécution inline, un ticket par tour ----------------
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[poll] exécutant déjà en cours (verrou pris), on repassera")
            return
        issue = issues[0]
        print(f"[poll] exécution de #{issue['number']} {issue['title']}")
        await dev_executor.executer(repo, issue)


if __name__ == "__main__":
    load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WATCHED_REPO", "")
    if not repo:
        sys.exit("Usage : python poll.py <owner/repo>  (ou définir WATCHED_REPO)")
    asyncio.run(poll(repo))
