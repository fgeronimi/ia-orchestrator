"""Exécutant — Phase 1 : implémente une issue ai-ready et ouvre une PR draft.

Increment 1a (hello-world de l'écriture) : valide tout le chemin d'écriture
GitHub (branche, push, PR draft, labels, commentaire) avec un changement TRIVIAL
et déterministe, AVANT de brancher Claude dessus (increment 1b).

Flux pour une issue :
  1. label ai-working (retire ai-ready) — évite une reprise au tour suivant
  2. workspace à jour sur la branche de base, crée ai/<n>
  3. [1a] écrit un fichier trivial   →   [1b] Claude implémente + tests
  4. commit + push
  5. ouvre une PR draft
  6. commente l'issue avec le lien PR
  7. notifie chaque étape

Usage manuel (test live d'un ticket) :
    .venv/bin/python -m pipelines.dev_executor fgeronimi/ia-orchestrator 1
"""

from lib import github, notify, workspace

LABEL_READY = "ai-ready"
LABEL_WORKING = "ai-working"


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

        # --- Increment 1a : changement trivial déterministe ---------------
        # (sera remplacé par un appel run_claude en 1b)
        note = path / ".ai" / f"ticket-{n}.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text(
            f"# Ticket #{n}\n\n{titre}\n\n_(ébauche générée par l'exécutant)_\n"
        )

        if not workspace.commit_tout(path, f"ai: amorce ticket #{n} — {titre}"):
            await notify.notify(f"⚠️ #{n} : rien à committer")
            return
        workspace.pousser(path, repo, branche)

        pr = github.create_pull(
            repo, head=branche, base=base,
            title=f"[IA] #{n} {titre}",
            body=f"Ébauche automatique pour #{n}.\n\nCloses #{n}",
            draft=True,
        )
        github.comment_issue(repo, n, f"🤖 PR ouverte : {pr['html_url']}")
        await notify.notify(f"🔍 #{n} : PR draft ouverte → {pr['html_url']}")

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
