"""Poller — hello world de la phase 0.

Lit les issues taggées `ai-ready` d'un repo GitHub et les notifie sur Discord.
C'est la plus petite tranche qui prouve la chaîne GitHub → notif.

⚠️ Pas encore de dédup ni d'état : à chaque exécution il re-notifie toutes les
issues taggées. La mémoire d'idempotence (SQLite) est l'étape suivante.

Usage :
    .venv/bin/python poll.py fgeronimi/ia-orchestrator
    # ou repo lu dans WATCHED_REPO du .env
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from lib import github, notify

LABEL = "ai-ready"


async def poll(repo: str) -> None:
    issues = github.list_issues(repo, labels=LABEL)
    if not issues:
        print(f"[poll] aucune issue '{LABEL}' sur {repo}")
        return
    for issue in issues:
        await notify.notify(
            f"🎫 Ticket à faire — {repo}#{issue['number']} : {issue['title']}\n"
            f"{issue['url']}"
        )
        print(f"[poll] notifié #{issue['number']} {issue['title']}")


if __name__ == "__main__":
    load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WATCHED_REPO", "")
    if not repo:
        sys.exit("Usage : python poll.py <owner/repo>  (ou définir WATCHED_REPO)")
    asyncio.run(poll(repo))
