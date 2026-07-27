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
from pathlib import Path

import yaml

from lib import github, notify, state, workspace
from lib.claude import ClaudeQuotaError, ResultatClaude, modele_depuis_label, run_claude

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
{analyse}
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
{note_exclusions}
Si `.claude/skills/bakaa-brutal-reviewer/SKILL.md` existe dans le repo, lis-le et
applique sa grille de lecture (sans complaisance) en plus de la checklist :
1. Bugs : cas limites, erreurs non gérées, effets de bord hors du scope.
2. Sécurité : secret en clair, injection (shell/SQL), entrée non validée.
3. Fidélité au ticket : tout est couvert, rien en trop.
4. Conventions du repo : CLAUDE.md, style des fichiers voisins.
5. Tests : lancés ? auraient-ils dû l'être ?

Tu es en LECTURE SEULE : tu ne peux pas exécuter les tests, le lint ni les
types — c'est le rôle de la CI de la PR, qui les exécute systématiquement.
Ne signale jamais que tu n'as pas pu les lancer ; concentre-toi sur ce que
la CI ne voit pas (logique, contrats, sécurité, conventions).

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

# Défauts de modèle par type de tâche (data/modeles.yaml) — voir _modele().
MODELES_YAML = Path(__file__).parent.parent / "data" / "modeles.yaml"


def _modeles_defaut() -> dict:
    if not MODELES_YAML.exists():
        return {}
    return yaml.safe_load(MODELES_YAML.read_text()) or {}


def _modele(etape: str, labels: list[str]) -> str | None:
    """Modèle à utiliser pour cette étape de tâche.

    Priorité : label `model:<alias>` du ticket (liste blanche dans lib.claude)
    > défaut de data/modeles.yaml pour cette étape > None (modèle par défaut
    de l'abonnement, comportement actuel).
    """
    return modele_depuis_label(labels) or _modeles_defaut().get(etape) or None


def _labels_ticket(repo: str, n: int) -> list[str]:
    """Labels de l'issue #n (pour l'override `model:<alias>`) — [] si illisible."""
    try:
        return github.get_issue(repo, n)["labels"]
    except github.GitHubError:
        return []


def _tracer_conso(repo: str, n: int, etape: str, r: ResultatClaude,
                  modele: str | None = None) -> str:
    """Enregistre la conso d'un appel et retourne son résumé pour les notifs."""
    state.enregistrer_conso(repo, n, etape, r.tokens_entree, r.tokens_cache,
                            r.tokens_sortie, r.cout_usd, modele)
    lus = (r.tokens_entree + r.tokens_cache) / 1000
    prefixe = f"🪙 {etape}" + (f" ({modele})" if modele else "")
    return f"{prefixe} : {lus:.0f}k lus / {r.tokens_sortie/1000:.1f}k générés (~{r.cout_usd:.2f} $)"


def _bloquer_quota(exc: ClaudeQuotaError) -> str:
    """Mémorise le blocage (le poller saute les actions lourdes jusqu'à la
    reprise, sans spammer) et retourne l'heure de reprise pour la notif."""
    reprise = exc.reset_epoch or int(time.time()) + QUOTA_ATTENTE_DEFAUT
    state.bloquer_quota(reprise)
    return datetime.fromtimestamp(reprise).strftime("%H:%M")


async def _auto_review(repo: str, path, base: str, n: int, titre: str, pr: dict,
                       labels: list[str] | None = None) -> None:
    """Relecture du diff par Claude (lecture seule), postée en commentaire de PR.

    Un échec ici ne fait pas échouer le run : la PR est déjà ouverte.
    """
    try:
        diff, exclus = workspace.diff_filtre(path, base)
        if len(diff) > DIFF_MAX:
            diff = diff[:DIFF_MAX] + "\n[… diff tronqué …]"
        note_exclusions = ""
        if exclus:
            liste = "\n".join(f"- {e}" for e in exclus)
            note_exclusions = f"\nAssets exclus du diff (trop volumineux) :\n{liste}\n"
        modele = _modele("reviewer", labels or [])
        resultat = await run_claude(
            PROMPT_REVIEW.format(n=n, titre=titre, base=base, diff=diff,
                                 note_exclusions=note_exclusions),
            cwd=str(path),
            allowed_tools=["Read"],
            timeout=300,
            model=modele,
        )
        conso = _tracer_conso(repo, n, "auto-review", resultat, modele)
        # comment_issue marche pour les PR : même endpoint issues/commentaires.
        github.comment_issue(repo, pr["number"], f"🤖 **Auto-review**\n\n{resultat.texte}")
        await notify.notify(f"🧐 #{n} : auto-review postée sur la PR #{pr['number']}\n{conso}")
    except ClaudeQuotaError as exc:
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ #{n} : {exc} — auto-review sautée (PR #{pr['number']} "
                            f"ouverte), reprise des actions vers {reprise}")
    except Exception as exc:
        await notify.notify(f"⚠️ #{n} : auto-review échouée (PR #{pr['number']} ouverte) — {exc}")


def _analyse_triage(repo: str, n: int) -> str:
    """Dernière analyse du triage (🤖 Raffinement/Clarification) en bloc de
    prompt, ou chaîne vide. Des HYPOTHÈSES pour orienter l'agent — le prompt
    le dit explicitement pour qu'il ne les prenne pas pour des faits
    (post-mortem « fichiers hallucinés », README)."""
    try:
        commentaires = github.list_comments(repo, n)
    except Exception:
        return ""  # le triage est un bonus, jamais un bloqueur
    analyses = [
        c["body"] for c in commentaires
        if (c["body"] or "").startswith(("🤖 **Raffinement**", "🤖 **Clarification**"))
    ]
    if not analyses:
        return ""
    return (
        "\nAnalyse préalable du ticket (triage automatique — des hypothèses "
        "pour t'orienter, à VÉRIFIER dans le dépôt, pas des faits) :\n"
        f"{analyses[-1].removeprefix('🤖 ').strip()}\n"
    )


async def executer(repo: str, issue: dict, timeout: int | None = None) -> None:
    n = issue["number"]
    titre = issue["title"]
    branche = f"ai/{n}"
    timeout = timeout or 600  # surchageable par repo (data/repos.yaml)

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
        labels = issue.get("labels", [])
        modele = _modele("executer", labels)
        await notify.notify(f"🔨 #{n} : implémentation par Claude…")
        resultat = await run_claude(
            PROMPT_IMPL.format(n=n, titre=titre, corps=corps or "(pas de description)",
                               analyse=_analyse_triage(repo, n)),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=timeout,
            model=modele,
        )
        conso = _tracer_conso(repo, n, "implementation", resultat, modele)
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

        await _auto_review(repo, path, base, n, titre, pr, labels)

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
    """Première PR d'agent ouverte ayant de nouveaux commentaires du proprio.

    Balaye les PR ouvertes sur une branche ai/* DE CE REPO (jamais un fork),
    agrège commentaires de conversation et de diff, et n'écoute que le
    propriétaire du repo : sur un repo public, n'importe qui peut commenter,
    et un commentaire pilote un agent Bash sur le Pi — seuls les tiens font
    foi. Écarte aussi ceux de l'orchestrateur (préfixe 🤖, même login) et
    ceux déjà traités (state.commentaires_vus).
    """
    proprietaire = repo.split("/")[0]
    for pr in github.list_pulls(repo, state="open"):
        if not pr["head"].startswith(BRANCHE_PREFIX) or pr["head_repo"] != repo:
            continue
        commentaires = []
        for genre, liste in (
            ("issue", github.list_comments(repo, pr["number"])),
            ("review", github.list_review_comments(repo, pr["number"])),
        ):
            for c in liste:
                cle = f"{genre}-{c['id']}"  # deux espaces d'ids distincts
                if c["user"] != proprietaire:
                    continue
                corps = (c["body"] or "").strip()
                if corps.startswith(PREFIX_BOT):
                    continue
                if corps.startswith("/"):
                    continue  # espace de noms des commandes (/ticket…) : pas une révision
                if state.commentaire_deja_vu(repo, cle):
                    continue
                commentaires.append({**c, "cle": cle})
        if commentaires:
            return pr, commentaires
    return None


async def reviser(repo: str, pr: dict, commentaires: list[dict],
                  timeout: int | None = None) -> None:
    """Applique les commentaires de review d'une PR d'agent et repush.

    Les commentaires ne sont marqués vus qu'après succès : un échec (timeout
    Claude, push refusé) sera retenté au tour suivant.
    """
    num_pr = pr["number"]
    branche = pr["head"]
    n = int(branche.removeprefix(BRANCHE_PREFIX))
    timeout = timeout or 600

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
        modele = _modele("reviser", _labels_ticket(repo, n))
        resultat = await run_claude(
            PROMPT_REVISION.format(pr=num_pr, n=n, titre=pr["title"], commentaires=texte),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=timeout,
            model=modele,
        )
        conso = _tracer_conso(repo, n, "revision", resultat, modele)
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


# --- Review à la demande (label ai-review sur une PR humaine) ---------------

LABEL_REVIEW = "ai-review"


async def reviewer_pr(repo: str, pr: dict) -> None:
    """Relit une PR écrite en local (grosse session Mac) et retire le label.

    Réutilise l'auto-review (Read seul, l'étape la moins gourmande en quota).
    Le label est retiré dans tous les cas pour ne pas boucler : si la review a
    été sautée (quota) ou a échoué, la notif l'explique — reposer le label
    relance.
    """
    num = pr["number"]
    try:
        base = github.get_default_branch(repo) if not pr.get("base") else pr["base"]
        path = workspace.preparer(repo, base)
        workspace.basculer_sur(path, repo, pr["head"])
        await _auto_review(repo, path, base, num, pr["title"], pr, pr.get("labels"))
    except Exception as exc:
        await notify.notify(f"⚠️ PR #{num} : review à la demande échouée — {exc}")
    finally:
        github.remove_label(repo, num, LABEL_REVIEW)


# --- Résolution de conflits de merge ----------------------------------------

PROMPT_CONFLIT = """Tu es dans un dépôt git EN PLEIN MERGE de `{base}` dans la
branche de la PR #{pr} (ticket #{n} : {titre}). Des marqueurs de conflit
(<<<<<<< ======= >>>>>>>) sont présents dans :

{fichiers}

Résous chaque conflit SÉMANTIQUEMENT : les changements des deux côtés doivent
coexister quand c'est possible (souvent deux fonctionnalités indépendantes qui
fusionnent) — ne jette le travail d'aucun côté sans raison.

Consignes :
- Retire tous les marqueurs de conflit.
- Si le repo a des tests/lint (lis CLAUDE.md et/ou README), lance-les et
  assure-toi qu'ils passent.
- Ne touche pas à git (pas d'add/commit/push/merge --abort) : l'orchestrateur
  finalise le merge.
- Termine par un résumé de 2-3 lignes de tes choix de résolution."""


async def resoudre_conflit(repo: str, pr: dict, timeout: int | None = None) -> None:
    """Résout le conflit d'une PR d'agent avec sa base et repush.

    Merge de la base dans la branche : propre → push direct (pas de Claude) ;
    conflits → Claude les résout, on vérifie qu'aucun marqueur ne survit avant
    de committer le merge. Échec → merge abandonné (branche intacte), sha
    marqué (une tentative), intervention humaine notifiée.
    """
    num_pr = pr["number"]
    branche = pr["head"]
    n = int(branche.removeprefix(BRANCHE_PREFIX))

    try:
        base = pr["base"]
        path = workspace.preparer(repo, base)
        workspace.basculer_sur(path, repo, branche)

        if workspace.merger_base(path, base):
            # Le conflit s'est résorbé tout seul (main a encore bougé) : push.
            workspace.pousser(path, repo, branche)
            await notify.notify(f"🔀 PR #{num_pr} : plus de conflit, branche mise à niveau")
            return

        fichiers = workspace.fichiers_en_conflit(path)
        await notify.notify(f"🔀 PR #{num_pr} : conflit avec {base} "
                            f"({len(fichiers)} fichier(s)), résolution par Claude…")
        modele = _modele("resoudre_conflit", _labels_ticket(repo, n))
        resultat = await run_claude(
            PROMPT_CONFLIT.format(base=base, pr=num_pr, n=n, titre=pr["title"],
                                  fichiers="\n".join(f"- {f}" for f in fichiers)),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=timeout or 600,
            model=modele,
        )
        conso = _tracer_conso(repo, n, "conflit", resultat, modele)

        restants = workspace.marqueurs_restants(path, fichiers)
        if restants:
            workspace.abandonner_merge(path)
            state.marquer_conflit_tente(repo, pr["sha"])
            await notify.notify(f"🛑 PR #{num_pr} : marqueurs de conflit restants dans "
                                f"{', '.join(restants)} — merge abandonné, résolution humaine requise\n{conso}")
            return

        workspace.commit_tout(path, f"ai: #{n} résout le conflit avec {base} (PR #{num_pr})")
        workspace.pousser(path, repo, branche)
        state.marquer_conflit_tente(repo, pr["sha"])
        github.comment_issue(repo, num_pr,
                             f"🤖 Conflit avec `{base}` résolu.\n\n{resultat.texte}")
        await notify.notify(f"🔀 #{n} : conflit résolu, repush (CI relancée) → "
                            f"{pr['html_url']}\n{conso}")

    except ClaudeQuotaError as exc:
        # Sha non marqué : la résolution repartira avec le quota.
        try:
            workspace.abandonner_merge(path)
        except Exception:
            pass
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ PR #{num_pr} : {exc} — résolution reportée vers {reprise}")
    except Exception as exc:
        try:
            workspace.abandonner_merge(path)
        except Exception:
            pass
        state.marquer_conflit_tente(repo, pr["sha"])
        await notify.notify(f"⚠️ PR #{num_pr} : échec de la résolution de conflit — {exc}")


# --- Phase 3+ : réparation de CI rouge -------------------------------------

async def corriger_ci(repo: str, pr: dict, echecs: list[dict], log: str,
                      timeout: int | None = None) -> None:
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

        modele = _modele("corriger_ci", _labels_ticket(repo, n))
        resultat = await run_claude(
            PROMPT_CI.format(pr=num_pr, n=n, titre=pr["title"], noms=noms,
                             log=log or "(log indisponible)"),
            cwd=str(path),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            timeout=timeout or 600,
            model=modele,
        )
        conso = _tracer_conso(repo, n, "ci-fix", resultat, modele)

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
