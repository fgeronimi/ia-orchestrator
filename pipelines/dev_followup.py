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

chercher_ci_rouge — détecte la première PR d'agent dont la CI est rouge et
réparable ; l'exécutant (dev_executor.corriger_ci) tentera de la réparer
avec le log du job en prompt. Max MAX_TENTATIVES_CI corrections par ticket,
ensuite abandon notifié (une fois). Un sha déjà traité n'est jamais repris.

Idempotence : state.prs_suivies / state.ci_notifiee / state.ci_fix_tentees.
"""

import re

from lib import github, notify, state

BRANCHE_PREFIX = "ai/"
LABEL_WORKING = "ai-working"
LABEL_REVIEW = "ai-review"  # review à la demande d'une PR humaine
MAX_TENTATIVES_CI = 2
LOG_MAX = 4_000  # extrait de fin de log passé dans le prompt


async def traiter_merges(repo: str) -> None:
    for pr in github.list_pulls(repo, state="closed"):
        # head_repo : jamais les PR de forks (une branche ai/* d'un fork n'est
        # pas une PR d'agent — sinon on délabelliserait une issue au hasard).
        if not pr["head"].startswith(BRANCHE_PREFIX) or pr["head_repo"] != repo:
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
        if not pr["head"].startswith(BRANCHE_PREFIX) or pr["head_repo"] != repo:
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


def chercher_review_demandee(repo: str) -> dict | None:
    """Première PR ouverte portant le label `ai-review`.

    Usage : les grosses sessions se font en local (quota du Pi préservé) et
    poussent des PR ; poser `ai-review` fait relire le diff par le Pi
    (dev_executor.reviewer_pr), qui retire le label après coup. Jamais les
    PR de forks.
    """
    for pr in github.list_pulls(repo, state="open"):
        if LABEL_REVIEW in pr["labels"] and pr["head_repo"] == repo:
            return pr
    return None


def _queue_de_log(brut: str) -> str:
    """Fin du log d'un job, débarrassée des horodatages (économie de tokens)."""
    lignes = [re.sub(r"^\S+Z ", "", l) for l in brut.splitlines()]
    return "\n".join(lignes)[-LOG_MAX:]


async def chercher_ci_rouge(repo: str):
    """Première PR d'agent à CI rouge réparable → (pr, echecs, extrait de log).

    Un sha n'est considéré qu'une fois (state.ci_fix_tentees, marqué par
    l'exécutant après tentative). Au-delà de MAX_TENTATIVES_CI corrections sur
    le ticket : abandon notifié une fois, intervention humaine.
    """
    for pr in github.list_pulls(repo, state="open"):
        if not pr["head"].startswith(BRANCHE_PREFIX) or pr["head_repo"] != repo:
            continue
        try:
            n = int(pr["head"].removeprefix(BRANCHE_PREFIX))
        except ValueError:
            continue
        if state.ci_fix_deja_tentee(repo, pr["sha"]):
            continue
        runs = github.list_check_runs(repo, pr["sha"])
        if not runs or any(r["status"] != "completed" for r in runs):
            continue
        echecs = [r for r in runs
                  if r["conclusion"] not in ("success", "neutral", "skipped")]
        if not echecs:
            continue
        if state.compter_conso(repo, n, "ci-fix") >= MAX_TENTATIVES_CI:
            state.marquer_ci_fix_tentee(repo, pr["sha"])  # ne plus re-notifier
            await notify.notify(
                f"🛑 PR #{pr['number']} : CI toujours rouge après "
                f"{MAX_TENTATIVES_CI} corrections — intervention humaine requise\n"
                f"{pr['html_url']}"
            )
            continue
        log = _queue_de_log(github.get_job_log(repo, echecs[0]["id"]))
        return pr, echecs, log
    return None
