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


#: Un ticket ai-working sans PR depuis plus d'une heure = exécutant mort en
#: plein vol (l'exécution la plus longue configurée est de 20 min, marge ×3).
#: Les erreurs ATTRAPABLES remettent en file elles-mêmes (ClaudeQuotaError) ;
#: ici on rattrape les arrêts DURS (process tué, OOM) — post-mortem vécu sur
#: fgeronimi.github.io#8, voir le README.
SEUIL_ORPHELIN_S = 60 * 60


async def remettre_orphelins_en_file(repo: str) -> None:
    """Détecte les `ai-working` orphelins (crash dur) et les remet en file.

    Orphelin = issue ouverte `ai-working`, SANS PR ouverte sur sa branche
    ai/<n>, dont le label a été posé il y a plus de SEUIL_ORPHELIN_S.
    Dédup par (repo, numero, horodatage de pose) : un incident n'est signalé
    qu'une fois, un NOUVEAU crash (nouvel horodatage) l'est à nouveau.
    Au 2e orphelinage du même ticket : ai-failed (un crash déterministe
    bouclerait — même philosophie que MAX_TENTATIVES_CI).
    """
    import datetime as _dt

    for issue in github.list_issues(repo, labels=LABEL_WORKING):
        n = issue["number"]
        if github.find_open_pull(repo, f"{BRANCHE_PREFIX}{n}") is not None:
            continue  # exécution légitime : la PR existe, le suivi normal gère

        pose_le = github.label_pose_le(repo, n, LABEL_WORKING)
        if pose_le is None:
            continue  # pas de trace datée : ne rien décider sur du vide
        pose = _dt.datetime.fromisoformat(pose_le.replace("Z", "+00:00"))
        age = (_dt.datetime.now(_dt.UTC) - pose).total_seconds()
        if age < SEUIL_ORPHELIN_S:
            continue  # peut-être une exécution longue encore en cours
        if state.orphelin_deja_signale(repo, n, pose_le):
            continue

        deja = state.compter_orphelinages(repo, n)
        state.marquer_orphelin(repo, n, pose_le)
        github.remove_label(repo, n, LABEL_WORKING)
        if deja >= 1:
            github.add_labels(repo, n, ["ai-failed"])
            github.comment_issue(repo, n, (
                "🤖 Exécution interrompue détectée une seconde fois (aucune PR "
                f"après {SEUIL_ORPHELIN_S // 60} min) — `ai-failed` posé : un crash "
                "répété ne se relance pas en boucle, intervention humaine requise."
            ))
            await notify.notify(f"🛑 #{n} ({repo}) : 2e crash dur de l'exécutant — ai-failed")
        else:
            github.add_labels(repo, n, ["ai-ready"])
            github.comment_issue(repo, n, (
                "🤖 Exécution interrompue détectée (label `ai-working` orphelin, "
                f"aucune PR après {SEUIL_ORPHELIN_S // 60} min) — ticket remis en file."
            ))
            await notify.notify(f"♻️ #{n} ({repo}) : exécutant mort en plein vol, ticket remis en file")
        print(f"[followup] orphelin #{n} ({repo}) : "
              f"{'ai-failed (2e crash)' if deja >= 1 else 'remis en file'}")


COMMANDE_TICKET = "/ticket"


async def traiter_commandes(repo: str) -> None:
    """Commandes en commentaire de PR — v1 : `/ticket titre\\ncorps` crée l'issue.

    Réservées au propriétaire du repo, sur toute PR ouverte du repo (agent ou
    humaine, jamais un fork), commentaires de conversation uniquement.
    L'issue est créée SANS label : poser `ai-ready` reste un geste humain.
    Dédup partagée avec la révision (state.commentaires_vus) ; les
    commentaires commençant par « / » ne déclenchent jamais de révision.
    """
    proprietaire = repo.split("/")[0]
    for pr in github.list_pulls(repo, state="open"):
        if pr["head_repo"] != repo:
            continue
        for c in github.list_comments(repo, pr["number"]):
            corps_brut = (c["body"] or "").strip()
            if c["user"] != proprietaire or not corps_brut.startswith(COMMANDE_TICKET):
                continue
            cle = f"issue-{c['id']}"
            if state.commentaire_deja_vu(repo, cle):
                continue

            texte = corps_brut[len(COMMANDE_TICKET):].strip()
            lignes = texte.splitlines() or [""]
            titre = (lignes[0] or "").strip() or f"Ticket depuis la PR #{pr['number']}"
            corps = "\n".join(lignes[1:]).strip()
            corps = (f"{corps}\n\n" if corps else "") + (
                f"_Créé par `{COMMANDE_TICKET}` depuis un commentaire de la "
                f"PR #{pr['number']} : {pr['html_url']}_"
            )
            issue = github.create_issue(repo, titre, corps)
            state.marquer_commentaire(repo, cle)
            github.comment_issue(repo, pr["number"],
                                 f"🤖 Ticket créé : #{issue['number']} — {issue['html_url']}")
            await notify.notify(f"🎫 Ticket #{issue['number']} créé depuis la "
                                f"PR #{pr['number']} : {titre}")
            print(f"[followup] {COMMANDE_TICKET} → issue #{issue['number']} ({repo})")


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


def chercher_conflit(repo: str) -> dict | None:
    """Première PR d'agent ouverte en conflit avec sa base (non mergeable).

    Arrive quand deux tickets voisins partent du même main et que l'un est
    mergé avant l'autre. Un sha n'est tenté qu'une fois (state.conflits_tentes,
    marqué par l'exécutant) ; `mergeable` à None = GitHub calcule encore, on
    repassera. Jamais les PR de forks.
    """
    for pr in github.list_pulls(repo, state="open"):
        if not pr["head"].startswith(BRANCHE_PREFIX) or pr["head_repo"] != repo:
            continue
        if state.conflit_deja_tente(repo, pr["sha"]):
            continue
        detail = github.get_pull(repo, pr["number"])
        if detail["mergeable"] is False:
            return detail
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
