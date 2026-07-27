"""Poller — un tour du pipeline dev GitHub, sur tous les repos surveillés.

Repos surveillés : `data/repos.yaml` (clé `repos`), sinon `WATCHED_REPO` du
.env, sinon l'argument CLI (qui prime sur tout, pratique pour un test ciblé).

Un tour, pour CHAQUE repo :
  1. notifie les nouvelles issues `ai-ready` (dédup SQLite via lib/state)
  2. suite après merge : nettoie les PR d'agent mergées (dev_followup, léger),
     et remet en file les ai-working orphelins (crash dur de l'exécutant)
  3. CI : notifie le résultat des check runs des PR d'agent (une fois par sha)
  4. triage des nouveaux tickets du propriétaire (dev_triage, léger, modèle
     haiku) : labels `size:*`/`model:*` + commentaire d'analyse ou questions
  5. clarification (dev_triage, léger) : réponse du propriétaire sur une
     issue `triage:questions` → re-triage avec le fil complet, plafonné à
     2 tours
puis UNE SEULE action lourde (Claude) tous repos confondus, sous verrou
fichier, par ordre de priorité :
  1. nouveaux commentaires humains sur une PR d'agent → dev_executor.reviser
  2. PR humaine labellisée `ai-review` → dev_executor.reviewer_pr
  3. PR d'agent en conflit avec sa base → dev_executor.resoudre_conflit
  4. CI rouge réparable sur une PR d'agent → dev_executor.corriger_ci
  5. première issue `ai-ready` → dev_executor.executer

Idempotence : l'exécutant pose `ai-working` (et retire `ai-ready`) dès la
prise en charge ; commentaires, PR mergées et statuts CI traités sont
mémorisés en SQLite. Le verrou (flock sur state/executor.lock) empêche deux
exécutants simultanés ; il est libéré automatiquement à la mort du process,
donc pas de verrou orphelin après un crash.

Usage :
    .venv/bin/python poll.py                          # repos.yaml / WATCHED_REPO
    .venv/bin/python poll.py fgeronimi/ia-orchestrator  # un repo précis
"""

import asyncio
import fcntl
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from lib import github, notify, state
from pipelines import dev_executor, dev_followup, dev_triage

LABEL = "ai-ready"
RACINE = Path(__file__).parent
VERROU = RACINE / "state" / "executor.lock"
REPOS_YAML = RACINE / "data" / "repos.yaml"


def charger_repos() -> list[dict]:
    """Repos surveillés + config par repo, normalisés en dicts.

    Une entrée de data/repos.yaml est soit une chaîne 'owner/nom', soit un
    mapping {repo: 'owner/nom', timeout: <secondes>} — le timeout borne les
    appels Claude de ce repo (défaut : celui de run_claude). Secours si le
    yaml est absent : WATCHED_REPO du .env.
    """
    entrees: list = []
    if REPOS_YAML.exists():
        config = yaml.safe_load(REPOS_YAML.read_text()) or {}
        entrees = config.get("repos") or []
    if not entrees:
        repo = os.environ.get("WATCHED_REPO", "")
        entrees = [repo] if repo else []
    return [
        {"repo": e, "timeout": None} if isinstance(e, str)
        else {"repo": e["repo"], "timeout": e.get("timeout")}
        for e in entrees
    ]


async def _tour_leger(repo: str) -> list[dict]:
    """Le tour léger d'UN repo (notifs, followup, triage) → issues ai-ready.

    Isolé par l'appelant : une erreur ici (hoquet GitHub, course pendant un
    merge) ne doit ni faire échouer le service ni priver les autres repos de
    leur tour — vécu le 2026-07-27 pendant une rafale de merges.
    """
    issues = github.list_issues(repo, labels=LABEL)
    nouveaux = [i for i in issues if not state.deja_notifiee(repo, i["number"])]

    for issue in nouveaux:
        await notify.notify(
            f"🎫 Ticket à faire — {repo}#{issue['number']} : {issue['title']}\n"
            f"{issue['url']}"
        )
        # Marqué après la notif. Limite connue : si le webhook est down, notify
        # loggue l'erreur mais l'issue est quand même marquée (pas de re-tentative).
        state.marquer_notifiee(repo, issue["number"])
        print(f"[poll] notifié {repo}#{issue['number']} {issue['title']}")

    # --- Phase 2/3 : suivi post-merge, CI, commandes (API seulement)
    await dev_followup.traiter_merges(repo)
    await dev_followup.surveiller_ci(repo)
    await dev_followup.traiter_commandes(repo)
    await dev_followup.remettre_orphelins_en_file(repo)

    # --- Triage(1/3) : analyse des nouveaux tickets, avant la chaîne de
    # priorité — léger (raisonnement pur, modèle haiku par défaut).
    await dev_triage.trier_nouveaux(repo)

    # --- Triage(2/3) : clarification suite à une réponse du propriétaire
    # sur une issue `triage:questions` — même tour, même légèreté.
    await dev_triage.clarifier_nouveaux(repo)

    return issues


async def poll(repos: list[dict]) -> None:
    a_faire: list[tuple[dict, dict]] = []  # (entrée repo, issue) candidats
    sautes: list[str] = []

    for entree in repos:
        repo = entree["repo"]
        try:
            issues = await _tour_leger(repo)
        except Exception as exc:  # noqa: BLE001 — frontière d'isolation par repo
            print(f"[poll] {repo} : tour léger en échec ({exc}) — repo sauté, "
                  f"le tour suivant rattrape")
            sautes.append(f"{repo} ({type(exc).__name__})")
            continue
        a_faire += [(entree, i) for i in issues]

    if sautes:
        # Une info, pas une alerte : le service n'échoue plus pour un hoquet,
        # et le prochain tour (5 min) reprend naturellement.
        await notify.notify("⚠️ poll : tour léger sauté sur " + ", ".join(sautes))

    # Quota Claude épuisé (mémorisé par l'exécutant) : notifs et suivi ont eu
    # lieu, mais on saute les actions lourdes jusqu'à la reprise, sans spammer.
    reprise = state.quota_bloque_jusqua()
    if reprise is not None:
        print("[poll] quota Claude épuisé — action lourde sautée, reprise vers "
              + time.strftime("%H:%M", time.localtime(reprise)))
        return

    # Première PR d'agent avec de nouveaux commentaires, tous repos confondus.
    revision = None
    for entree in repos:
        trouvee = dev_executor.chercher_revision(entree["repo"])
        if trouvee is not None:
            revision = (entree, *trouvee)
            break

    # À défaut, première PR humaine dont la review est demandée (label ai-review).
    review = None
    if revision is None:
        for entree in repos:
            trouvee = dev_followup.chercher_review_demandee(entree["repo"])
            if trouvee is not None:
                review = (entree, trouvee)
                break

    # À défaut, première PR d'agent en conflit avec sa base.
    conflit = None
    if revision is None and review is None:
        for entree in repos:
            trouvee = dev_followup.chercher_conflit(entree["repo"])
            if trouvee is not None:
                conflit = (entree, trouvee)
                break

    # À défaut, première PR d'agent dont la CI est rouge et réparable.
    ci_rouge = None
    if revision is None and review is None and conflit is None:
        for entree in repos:
            trouvee = await dev_followup.chercher_ci_rouge(entree["repo"])
            if trouvee is not None:
                ci_rouge = (entree, *trouvee)
                break

    if (not a_faire and revision is None and review is None
            and conflit is None and ci_rouge is None):
        print(f"[poll] rien à traiter (ni issue '{LABEL}', ni révision, "
              "ni review demandée, ni conflit, ni CI rouge)")
        return

    # --- Une action lourde par tour, sous verrou -----------------------------
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[poll] exécutant déjà en cours (verrou pris), on repassera")
            return
        # Priorité : révision (débloquer ta review) > CI rouge (réparer
        # l'existant) > nouveau ticket. Le timeout vient de l'entrée repo.
        if revision is not None:
            entree, pr, commentaires = revision
            print(f"[poll] révision de la PR {entree['repo']}#{pr['number']} "
                  f"({len(commentaires)} commentaire(s))")
            await dev_executor.reviser(entree["repo"], pr, commentaires,
                                       timeout=entree["timeout"])
        elif review is not None:
            entree, pr = review
            print(f"[poll] review demandée sur la PR {entree['repo']}#{pr['number']}")
            await dev_executor.reviewer_pr(entree["repo"], pr)
        elif conflit is not None:
            entree, pr = conflit
            print(f"[poll] résolution du conflit de la PR {entree['repo']}#{pr['number']}")
            await dev_executor.resoudre_conflit(entree["repo"], pr,
                                                timeout=entree["timeout"])
        elif ci_rouge is not None:
            entree, pr, echecs, log = ci_rouge
            print(f"[poll] correction CI de la PR {entree['repo']}#{pr['number']}")
            await dev_executor.corriger_ci(entree["repo"], pr, echecs, log,
                                           timeout=entree["timeout"])
        else:
            entree, issue = a_faire[0]
            print(f"[poll] exécution de {entree['repo']}#{issue['number']} {issue['title']}")
            await dev_executor.executer(entree["repo"], issue, timeout=entree["timeout"])


if __name__ == "__main__":
    load_dotenv()
    if len(sys.argv) > 1:
        repos = [{"repo": sys.argv[1], "timeout": None}]
    else:
        repos = charger_repos()
    if not repos:
        sys.exit("Usage : python poll.py [owner/repo]  "
                 "(ou data/repos.yaml, ou WATCHED_REPO)")
    asyncio.run(poll(repos))
