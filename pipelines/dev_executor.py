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

import time
from datetime import datetime

from lib import github, notify, state, workspace
from lib.claude import ClaudeQuotaError, ResultatClaude, run_claude

LABEL_READY = "ai-ready"
LABEL_WORKING = "ai-working"
LABEL_FAILED = "ai-failed"  # échec dur : visible dans GitHub, relance humaine
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

Passe le diff au crible de cette checklist :
1. Bugs : cas limites, erreurs non gérées, effets de bord hors du scope.
2. Sécurité : secret en clair, injection (shell/SQL), entrée non validée.
3. Fidélité au ticket : tout est couvert, rien en trop.
4. Conventions du repo : CLAUDE.md, style des fichiers voisins.
5. Tests : lancés ? auraient-ils dû l'être ?

Rédige directement le commentaire de review (markdown), en français, concis :
cite uniquement les points de la checklist qui méritent une remarque (tais les
points RAS), et termine par un verdict clair : « ✅ RAS » ou « ⚠️ points à
vérifier avant merge ». Pas de préambule, pas de répétition du diff."""

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

PROMPT_CI = """Tu es dans un dépôt git, sur la branche de la PR #{pr}
(ticket #{n} : {titre}). La CI de cette PR est en échec ({noms}).
Répare-la, rien de plus.

Fin du log du job en échec :

```
{log}
```

Consignes :
- Modifie directement les fichiers du dépôt courant.
- Reste minimal et scopé à la réparation de la CI. Respecte les conventions du repo.
- Reproduis l'échec en local si possible (mêmes commandes que la CI) et
  vérifie que c'est réparé.
- Ne touche pas à git (pas de commit/push) : l'orchestrateur s'en charge.
- Termine par un résumé de 2-3 lignes de ce que tu as changé."""

# Au-delà, le diff est tronqué dans le prompt (l'agent Read complète au besoin).
DIFF_MAX = 40_000

# Sans heure de reprise annoncée par le CLI, on retente au bout de 30 min.
QUOTA_ATTENTE_DEFAUT = 30 * 60


def _tracer_conso(repo: str, n: int, etape: str, r: ResultatClaude) -> str:
    """Enregistre la conso d'un appel et retourne son résumé pour les notifs."""
    state.enregistrer_conso(repo, n, etape, r.tokens_entree, r.tokens_cache,
                            r.tokens_sortie, r.cout_usd)
    lus = (r.tokens_entree + r.tokens_cache) / 1000
    return f"🪙 {etape} : {lus:.0f}k lus / {r.tokens_sortie/1000:.1f}k générés (~{r.cout_usd:.2f} $)"


def _bloquer_quota(exc: ClaudeQuotaError) -> str:
    """Mémorise le blocage (le poller saute les actions lourdes jusqu'à la
    reprise, sans spammer) et retourne l'heure de reprise pour la notif."""
    reprise = exc.reset_epoch or int(time.time()) + QUOTA_ATTENTE_DEFAUT
    state.bloquer_quota(reprise)
    return datetime.fromtimestamp(reprise).strftime("%H:%M")


async def _auto_review(repo: str, path, base: str, n: int, titre: str, pr: dict) -> None:
    """Relecture du diff par Claude (lecture seule), postée en commentaire de PR.

    Un échec ici ne fait pas échouer le run : la PR est déjà ouverte.
    """
    try:
        diff = workspace.diff_contre(path, base)
        if len(diff) > DIFF_MAX:
            diff = diff[:DIFF_MAX] + "\n[… diff tronqué …]"
        resultat = await run_claude(
            PROMPT_REVIEW.format(n=n, titre=titre, base=base, diff=diff),
            cwd=str(path),
            allowed_tools=["Read"],
            timeout=300,
        )
        conso = _tracer_conso(repo, n, "auto-review", resultat)
        # comment_issue marche pour les PR : même endpoint issues/commentaires.
        github.comment_issue(repo, pr["number"], f"🤖 **Auto-review**\n\n{resultat.texte}")
        await notify.notify(f"🧐 #{n} : auto-review postée sur la PR #{pr['number']}\n{conso}")
    except ClaudeQuotaError as exc:
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ #{n} : {exc} — auto-review sautée (PR #{pr['number']} "
                            f"ouverte), reprise des actions vers {reprise}")
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
        github.remove_label(repo, n, LABEL_FAILED)  # relance après échec

        base = github.get_default_branch(repo)
        path = workspace.preparer(repo, base)
        workspace.creer_branche(path, branche)

        # --- Increment 1b : Claude implémente le ticket -------------------
        corps = github.get_issue(repo, n)["body"]
        await notify.notify(f"🔨 #{n} : implémentation par Claude…")
        resultat = await run_claude(
            PROMPT_IMPL.format(n=n, titre=titre, corps=corps or "(pas de description)"),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=600,
        )
        conso = _tracer_conso(repo, n, "implementation", resultat)
        resume = resultat.texte

        if not workspace.commit_tout(path, f"ai: #{n} {titre}"):
            await notify.notify(f"🤷 #{n} : Claude n'a produit aucune modification\n{conso}")
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
            await notify.notify(f"🔍 #{n} : PR draft ouverte → {pr['html_url']}\n{conso}")
        else:
            await notify.notify(f"🔄 #{n} : PR #{pr['number']} mise à jour → {pr['html_url']}\n{conso}")

        await _auto_review(repo, path, base, n, titre, pr)

    except ClaudeQuotaError as exc:
        # Ticket remis en file : il sera repris quand le quota reviendra.
        github.add_labels(repo, n, [LABEL_READY])
        github.remove_label(repo, n, LABEL_WORKING)
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ #{n} : {exc} — ticket remis en file, reprise vers {reprise}")
    except Exception as exc:
        # Échec dur : label ai-failed pour l'état visible dans GitHub. Pas de
        # retry auto (un échec déterministe bouclerait) : remettre ai-ready à
        # la main pour relancer. Best-effort si c'est GitHub qui est en panne.
        try:
            github.add_labels(repo, n, [LABEL_FAILED])
            github.remove_label(repo, n, LABEL_WORKING)
        except Exception:
            pass
        await notify.notify(f"⚠️ #{n} : échec de l'exécutant — {exc}\n"
                            f"Label `{LABEL_FAILED}` posé ; remets `{LABEL_READY}` pour retenter.")


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
        resultat = await run_claude(
            PROMPT_REVISION.format(pr=num_pr, n=n, titre=pr["title"], commentaires=texte),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=600,
        )
        conso = _tracer_conso(repo, n, "revision", resultat)
        resume = resultat.texte

        if workspace.commit_tout(path, f"ai: #{n} révision (PR #{num_pr})"):
            workspace.pousser(path, repo, branche)
            github.comment_issue(repo, num_pr, f"🤖 Commentaires pris en compte.\n\n{resume}")
            await notify.notify(f"✏️ #{n} : commentaires pris en compte, repush → "
                                f"{pr['html_url']}\n{conso}")
        else:
            github.comment_issue(
                repo, num_pr,
                f"🤖 Commentaires lus mais aucune modification produite.\n\n{resume}",
            )
            await notify.notify(f"🤷 #{n} : révision sans modification (voir la PR)\n{conso}")

        for c in commentaires:
            state.marquer_commentaire(repo, c["cle"])

    except ClaudeQuotaError as exc:
        # Commentaires non marqués vus : la révision repartira avec le quota.
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ PR #{num_pr} : {exc} — révision reportée vers {reprise}")
    except Exception as exc:
        # Échec dur : marquer les commentaires vus, sinon un échec déterministe
        # relancerait la même révision (10 min de Claude) tous les 5 min.
        for c in commentaires:
            state.marquer_commentaire(repo, c["cle"])
        await notify.notify(f"⚠️ PR #{num_pr} : échec de la révision — {exc}\n"
                            f"Commentaires abandonnés : re-commente la PR pour retenter.")


# --- Phase 3+ : réparation de CI rouge -------------------------------------

async def corriger_ci(repo: str, pr: dict, echecs: list[dict], log: str) -> None:
    """Tente de réparer la CI rouge d'une PR d'agent (détectée par
    dev_followup.chercher_ci_rouge) et repush.

    Le sha est marqué traité après la tentative (succès ou échec dur) pour ne
    jamais boucler dessus ; pas marqué sur un quota épuisé (retenté après la
    reprise). Un repush crée un nouveau sha → nouveau cycle CI surveillé.
    """
    num_pr = pr["number"]
    branche = pr["head"]
    n = int(branche.removeprefix(BRANCHE_PREFIX))
    noms = ", ".join(f"{r['name']} ({r['conclusion']})" for r in echecs)

    try:
        tentative = state.compter_conso(repo, n, "ci-fix") + 1
        await notify.notify(f"🔧 PR #{num_pr} : CI rouge ({noms}), "
                            f"correction par Claude (tentative {tentative})…")
        base = github.get_default_branch(repo)
        path = workspace.preparer(repo, base)
        workspace.basculer_sur(path, repo, branche)

        resultat = await run_claude(
            PROMPT_CI.format(pr=num_pr, n=n, titre=pr["title"], noms=noms,
                             log=log or "(log indisponible)"),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=600,
        )
        conso = _tracer_conso(repo, n, "ci-fix", resultat)

        if workspace.commit_tout(path, f"ai: #{n} répare la CI (PR #{num_pr})"):
            workspace.pousser(path, repo, branche)
            github.comment_issue(repo, num_pr, f"🤖 CI réparée (tentative {tentative}).\n\n{resultat.texte}")
            await notify.notify(f"🔧 #{n} : correctif CI repushé, CI relancée → "
                                f"{pr['html_url']}\n{conso}")
        else:
            github.comment_issue(repo, num_pr,
                                 f"🤖 CI rouge analysée mais aucune modification produite.\n\n{resultat.texte}")
            await notify.notify(f"🤷 #{n} : pas de correctif CI produit (voir la PR)\n{conso}")
        state.marquer_ci_fix_tentee(repo, pr["sha"])

    except ClaudeQuotaError as exc:
        # Sha non marqué : la correction repartira avec le quota.
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ PR #{num_pr} : {exc} — correction CI reportée vers {reprise}")
    except Exception as exc:
        state.marquer_ci_fix_tentee(repo, pr["sha"])
        await notify.notify(f"⚠️ PR #{num_pr} : échec de la correction CI — {exc}")


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
