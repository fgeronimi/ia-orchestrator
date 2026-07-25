"""Suite après merge — Phase 2 : nettoyage des PR d'agent mergées.

Appelé à chaque tour de poll (léger : API seulement, pas de Claude). Pour
chaque PR fermée dont la branche est `ai/*` et pas encore traitée :
  - mergée   → supprime la branche, retire `ai-working` de l'issue liée
               (fermée d'elle-même par le « Closes #n » du corps de la PR),
               notifie ✅🧹
  - fermée sans merge → marquée vue sans action ni notif (ticket abandonné :
    la branche et les labels restent, décision humaine)

Idempotence : state.prs_suivies (une PR fermée ne se retraite jamais).
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
