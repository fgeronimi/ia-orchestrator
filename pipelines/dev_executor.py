"""Exécutant — Phase 1 : implémente une issue ai-ready et ouvre une PR draft.

Flux pour une issue `ai-ready` :
  1. label ai-working (retire ai-ready) — évite une reprise au tour suivant
  2. workspace à jour sur la branche de base, crée ai/<n>
  3. Claude implémente le ticket (Read/Edit/Write/Bash, auto-détecte les tests)
  4. commit + push (rien à committer → on s'arrête là)
  5. ouvre (ou réutilise) une PR draft
  6. commente l'issue avec le lien PR
  7. notifie chaque étape

Increment 1c à venir : appel depuis le poller (verrou, 1 ticket/tour) au lieu
du lancement manuel.

Usage manuel (test live d'un ticket) :
    .venv/bin/python -m pipelines.dev_executor fgeronimi/ia-orchestrator 1
"""

from lib import github, notify, workspace
from lib.claude import run_claude

LABEL_READY = "ai-ready"
LABEL_WORKING = "ai-working"

PROMPT_IMPL = """Tu es dans un dépôt git, sur une branche dédiée à ce ticket.
Implémente-le, rien de plus.

Ticket #{n} : {titre}

{corps}

Consignes :
- Modifie directement les fichiers du dépôt courant.
- Reste minimal et scopé au ticket. Respecte les conventions du repo
  (lis CLAUDE.md et/ou README s'ils existent).
- Si tu repères des tests, lance-les et assure-toi qu'ils passent.
- Ne touche pas à git (pas de commit/push) : l'orchestrateur s'en charge.
- Termine par un résumé de 2-3 lignes de ce que tu as changé."""


async def executer(repo: str, issue: dict) -> None:
    n = issue["number"]
    titre = issue["title"]
    branche = f"ai/{n}"

    try:
        await notify.notify(f"🎫 #{n} pris en charge — {titre}")
        github.add_labels(repo, n, [LABEL_WORKING])
        github.remove_label(repo, n, LABEL_READY)

        base = github.get_default_branch(repo)
        path = workspace.preparer(repo, base)
        workspace.creer_branche(path, branche)

        # --- Increment 1b : Claude implémente le ticket -------------------
        corps = github.get_issue(repo, n)["body"]
        await notify.notify(f"🔨 #{n} : implémentation par Claude…")
        resume = await run_claude(
            PROMPT_IMPL.format(n=n, titre=titre, corps=corps or "(pas de description)"),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=600,
        )

        if not workspace.commit_tout(path, f"ai: #{n} {titre}"):
            await notify.notify(f"🤷 #{n} : Claude n'a produit aucune modification")
            return
        workspace.pousser(path, repo, branche)

        # Idempotent : réutilise la PR si elle existe déjà (re-run, révision).
        pr = github.find_open_pull(repo, branche)
        if pr is None:
            pr = github.create_pull(
                repo, head=branche, base=base,
                title=f"[IA] #{n} {titre}",
                body=f"Implémentation automatique de #{n}.\n\n{resume}\n\nCloses #{n}",
                draft=True,
            )
            github.comment_issue(repo, n, f"🤖 PR ouverte : {pr['html_url']}")
            await notify.notify(f"🔍 #{n} : PR draft ouverte → {pr['html_url']}")
        else:
            await notify.notify(f"🔄 #{n} : PR #{pr['number']} mise à jour → {pr['html_url']}")

    except Exception as exc:
        await notify.notify(f"⚠️ #{n} : échec de l'exécutant — {exc}")
        raise


if __name__ == "__main__":
    import asyncio
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) != 3:
        sys.exit("Usage : python -m pipelines.dev_executor <owner/repo> <numéro>")
    repo_arg, numero = sys.argv[1], int(sys.argv[2])
    issues = github.list_issues(repo_arg, state="all")
    cible = next((i for i in issues if i["number"] == numero), None)
    if cible is None:
        sys.exit(f"Issue #{numero} introuvable sur {repo_arg}")
    asyncio.run(executer(repo_arg, cible))
