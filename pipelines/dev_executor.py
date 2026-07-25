"""Exécutant — Phase 1 : implémente une issue ai-ready et ouvre une PR draft.

Flux pour une issue `ai-ready` :
  1. label ai-working (retire ai-ready) — évite une reprise au tour suivant
  2. workspace à jour sur la branche de base, crée ai/<n>
  3. Claude implémente le ticket (Read/Edit/Write/Bash, auto-détecte les tests)
  4. commit + push (rien à committer → on s'arrête là)
  5. ouvre (ou réutilise) une PR draft
  6. commente l'issue avec le lien PR
  7. auto-review : Claude relit son diff (Read seul) → commentaire sur la PR
  8. notifie chaque étape

Phase 2 — boucle de révision : chercher_revision() repère les nouveaux
commentaires humains sur les PR d'agent ouvertes, reviser() les applique
(Claude sur la branche de la PR) et repush. Dédup des commentaires en SQLite.

Appelé par le poller (poll.py, une action lourde par tour sous verrou) ;
lancement manuel possible :

Usage manuel (test live d'un ticket) :
    .venv/bin/python -m pipelines.dev_executor fgeronimi/ia-orchestrator 1
"""

from lib import github, notify, state, workspace
from lib.claude import run_claude

LABEL_READY = "ai-ready"
LABEL_WORKING = "ai-working"
BRANCHE_PREFIX = "ai/"
# Les commentaires de l'orchestrateur partent avec le même PAT que l'humain
# (même login) : le préfixe 🤖 est ce qui les distingue des vrais retours.
PREFIX_BOT = "🤖"

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

PROMPT_REVIEW = """Tu relis une PR que tu viens d'écrire pour le ticket #{n} : {titre}.
Tu es dans le dépôt, sur la branche de la PR : tu peux lire les fichiers pour
avoir le contexte autour du diff.

Diff de la PR (contre {base}) :

```diff
{diff}
```

Rédige directement le commentaire de review (markdown), en français, concis :
- bugs, risques ou effets de bord éventuels ;
- écarts avec le ticket ou les conventions du repo ;
- termine par un verdict clair : « ✅ RAS » ou « ⚠️ points à vérifier avant merge ».
Pas de préambule, pas de répétition du diff."""

PROMPT_REVISION = """Tu es dans un dépôt git, sur la branche de la PR #{pr}
(ticket #{n} : {titre}). Des commentaires de review demandent des corrections.
Applique-les, rien de plus.

Commentaires :
{commentaires}

Consignes :
- Modifie directement les fichiers du dépôt courant.
- Reste minimal et scopé aux commentaires. Respecte les conventions du repo.
- Si tu repères des tests, lance-les et assure-toi qu'ils passent.
- Ne touche pas à git (pas de commit/push) : l'orchestrateur s'en charge.
- Termine par un résumé de 2-3 lignes de ce que tu as changé."""

# Au-delà, le diff est tronqué dans le prompt (l'agent Read complète au besoin).
DIFF_MAX = 40_000


async def _auto_review(repo: str, path, base: str, n: int, titre: str, pr: dict) -> None:
    """Relecture du diff par Claude (lecture seule), postée en commentaire de PR.

    Un échec ici ne fait pas échouer le run : la PR est déjà ouverte.
    """
    try:
        diff = workspace.diff_contre(path, base)
        if len(diff) > DIFF_MAX:
            diff = diff[:DIFF_MAX] + "\n[… diff tronqué …]"
        review = await run_claude(
            PROMPT_REVIEW.format(n=n, titre=titre, base=base, diff=diff),
            cwd=str(path),
            allowed_tools=["Read"],
            timeout=300,
        )
        # comment_issue marche pour les PR : même endpoint issues/commentaires.
        github.comment_issue(repo, pr["number"], f"🤖 **Auto-review**\n\n{review}")
        await notify.notify(f"🧐 #{n} : auto-review postée sur la PR #{pr['number']}")
    except Exception as exc:
        await notify.notify(f"⚠️ #{n} : auto-review échouée (PR #{pr['number']} ouverte) — {exc}")


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

        await _auto_review(repo, path, base, n, titre, pr)

    except Exception as exc:
        await notify.notify(f"⚠️ #{n} : échec de l'exécutant — {exc}")
        raise


# --- Phase 2 : boucle de révision -----------------------------------------

def chercher_revision(repo: str) -> tuple[dict, list[dict]] | None:
    """Première PR d'agent ouverte ayant de nouveaux commentaires humains.

    Balaye les PR ouvertes sur une branche ai/*, agrège commentaires de
    conversation et commentaires de diff, écarte ceux de l'orchestrateur
    (préfixe 🤖) et ceux déjà traités (state.commentaires_vus).
    """
    for pr in github.list_pulls(repo, state="open"):
        if not pr["head"].startswith(BRANCHE_PREFIX):
            continue
        commentaires = []
        for genre, liste in (
            ("issue", github.list_comments(repo, pr["number"])),
            ("review", github.list_review_comments(repo, pr["number"])),
        ):
            for c in liste:
                cle = f"{genre}-{c['id']}"  # deux espaces d'ids distincts
                if (c["body"] or "").startswith(PREFIX_BOT):
                    continue
                if state.commentaire_deja_vu(repo, cle):
                    continue
                commentaires.append({**c, "cle": cle})
        if commentaires:
            return pr, commentaires
    return None


async def reviser(repo: str, pr: dict, commentaires: list[dict]) -> None:
    """Applique les commentaires de review d'une PR d'agent et repush.

    Les commentaires ne sont marqués vus qu'après succès : un échec (timeout
    Claude, push refusé) sera retenté au tour suivant.
    """
    num_pr = pr["number"]
    branche = pr["head"]
    n = int(branche.removeprefix(BRANCHE_PREFIX))

    try:
        await notify.notify(
            f"✏️ PR #{num_pr} : révision demandée ({len(commentaires)} commentaire(s))"
        )
        base = github.get_default_branch(repo)
        path = workspace.preparer(repo, base)
        workspace.basculer_sur(path, repo, branche)

        texte = "\n\n".join(
            f"- {c['body']}" + (f"\n  (sur le fichier {c['path']})" if c.get("path") else "")
            for c in commentaires
        )
        resume = await run_claude(
            PROMPT_REVISION.format(pr=num_pr, n=n, titre=pr["title"], commentaires=texte),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=600,
        )

        if workspace.commit_tout(path, f"ai: #{n} révision (PR #{num_pr})"):
            workspace.pousser(path, repo, branche)
            github.comment_issue(repo, num_pr, f"🤖 Commentaires pris en compte.\n\n{resume}")
            await notify.notify(f"✏️ #{n} : commentaires pris en compte, repush → {pr['html_url']}")
        else:
            github.comment_issue(
                repo, num_pr,
                f"🤖 Commentaires lus mais aucune modification produite.\n\n{resume}",
            )
            await notify.notify(f"🤷 #{n} : révision sans modification (voir la PR)")

        for c in commentaires:
            state.marquer_commentaire(repo, c["cle"])

    except Exception as exc:
        await notify.notify(f"⚠️ PR #{num_pr} : échec de la révision — {exc}")
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
