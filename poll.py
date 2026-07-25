"""Poller — hello world de la phase 0.

Lit les issues taggées `ai-ready` d'un repo GitHub et les notifie sur Discord.
C'est la plus petite tranche qui prouve la chaîne GitHub → notif.

Dédup : les issues déjà signalées sont mémorisées dans state/orchestrator.db
(via lib/state), donc seuls les NOUVEAUX tickets `ai-ready` sont notifiés. Le
poller peut ainsi tourner en boucle sans spammer.

Usage :
    .venv/bin/python poll.py fgeronimi/ia-orchestrator
    # ou repo lu dans WATCHED_REPO du .env
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from lib import github, notify, state

LABEL = "ai-ready"


async def poll(repo: str) -> None:
    issues = github.list_issues(repo, labels=LABEL)
    nouveaux = [i for i in issues if not state.deja_notifiee(repo, i["number"])]

    if not nouveaux:
        print(f"[poll] rien de nouveau ({len(issues)} issue(s) '{LABEL}' déjà vues)")
        return

    for issue in nouveaux:
        await notify.notify(
            f"🎫 Ticket à faire — {repo}#{issue['number']} : {issue['title']}\n"
            f"{issue['url']}"
        )
        # Marqué après la notif. Limite connue : si le webhook est down, notify
        # loggue l'erreur mais l'issue est quand même marquée (pas de re-tentative).
        state.marquer_notifiee(repo, issue["number"])
        print(f"[poll] notifié #{issue['number']} {issue['title']}")


if __name__ == "__main__":
    load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WATCHED_REPO", "")
    if not repo:
        sys.exit("Usage : python poll.py <owner/repo>  (ou définir WATCHED_REPO)")
    asyncio.run(poll(repo))
