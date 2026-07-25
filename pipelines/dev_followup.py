"""Suivi des PR d'agent — Phase 2 : nettoyage post-merge ; Phase 3 : CI.

Appelé à chaque tour de poll (léger : API seulement, pas de Claude).

traiter_merges — pour chaque PR fermée dont la branche est `ai/*` :
  - mergée   → supprime la branche, retire `ai-working` de l'issue liée
               (fermée d'elle-même par le « Closes #n » du corps de la PR),
               notifie ✅🧹
  - fermée sans merge → marquée vue sans action ni notif (ticket abandonné :
    la branche et les labels restent, décision humaine)

surveiller_ci — pour chaque PR d'agent ouverte : notifie le résultat des
check runs (GitHub Actions) une fois par sha — un repush (nouveau sha)
relance le suivi. Pas de CI sur le repo → silencieux.

Idempotence : state.prs_suivies / state.ci_notifiee.
"""

from lib import github, notify, state

BRANCHE_PREFIX = "ai/"
LABEL_WORKING = "ai-working"


async def traiter_merges(repo: str) -> None:
    for pr in github.list_pulls(repo, state="closed"):
        if not pr["head"].startswith(BRANCHE_PREFIX):
            continue
        if state.pr_deja_suivie(repo, pr["number"]):
            continue

        if not pr["merged_at"]:
            state.marquer_pr_suivie(repo, pr["number"])
            continue

        github.delete_branch(repo, pr["head"])
        try:
            n = int(pr["head"].removeprefix(BRANCHE_PREFIX))
            github.remove_label(repo, n, LABEL_WORKING)
        except ValueError:
            n = None  # branche ai/* non numérotée : rien à délabelliser
        state.marquer_pr_suivie(repo, pr["number"])
        await notify.notify(
            f"✅ PR #{pr['number']} mergée — 🧹 branche {pr['head']} supprimée"
            + (f" (issue #{n} close)" if n else "")
        )
        print(f"[followup] PR #{pr['number']} mergée, branche {pr['head']} nettoyée")


async def surveiller_ci(repo: str) -> None:
    for pr in github.list_pulls(repo, state="open"):
        if not pr["head"].startswith(BRANCHE_PREFIX):
            continue
        if state.ci_deja_notifiee(repo, pr["sha"]):
            continue
        runs = github.list_check_runs(repo, pr["sha"])
        if not runs:
            continue  # pas de CI sur ce repo
        if any(r["status"] != "completed" for r in runs):
            continue  # encore en cours, on repassera au tour suivant
        echecs = [r for r in runs
                  if r["conclusion"] not in ("success", "neutral", "skipped")]
        state.marquer_ci_notifiee(repo, pr["sha"])
        if echecs:
            noms = ", ".join(f"{r['name']} ({r['conclusion']})" for r in echecs)
            await notify.notify(f"❌ CI rouge — PR #{pr['number']} : {noms}\n{pr['html_url']}")
        else:
            await notify.notify(f"✅ CI verte — PR #{pr['number']}")
        print(f"[followup] CI de la PR #{pr['number']} : "
              f"{'rouge' if echecs else 'verte'} ({pr['sha'][:7]})")
