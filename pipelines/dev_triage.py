"""Triage — premier lecteur des nouveaux tickets, avant l'humain.

Pour chaque ticket nouvellement ouvert par le propriétaire du repo (jamais
le portail `ai-ready` : ça reste un geste humain), une passe de raisonnement
pur (`allowed_tools=[]`, modèle léger — `triage:` dans data/modeles.yaml,
défaut haiku) évalue si un agent pourrait l'implémenter tel quel :
  - clair  → labels `size:S|M|L` + `model:<suggestion>`, commentaire 🤖 avec
             le résumé de ce qu'un agent comprendrait et les fichiers probables ;
  - flou   → même labels de taille/modèle (best effort) + `triage:questions`,
             commentaire 🤖 avec au plus 3 questions bloquantes.

Owner-only (repo public = spam possible) : ignore les issues d'un tiers,
celles de la forge (titre `forge:`, pipelines/forge.py) et celles déjà
`ai-ready`/`ai-working` (déjà lues par un humain ou en cours). Dédup SQLite
(state.issues_triees, même principe que state.issues_notifiees) : chaque
issue n'est triée qu'une fois.

Sortie JSON stricte validée ; un parse raté est loggué et ignoré, jamais de
commentaire poubelle sur le ticket.

Phase 2 — boucle de clarification : quand le propriétaire répond sur une
issue `triage:questions`, `clarifier_nouveaux()` re-triage avec le corps et
le fil complet des commentaires. Questions levées → label retiré, taille/
modèle mis à jour si l'estimation change, commentaire « prêt pour ai-ready » ;
sinon nouvelles questions. Plafond de 2 tours de clarification : au-delà, le
triage se tait définitivement pour cette issue (state.triage_epuise).

Appelé par le poller (poll.py), après la détection des nouvelles issues et
avant la chaîne de priorité des actions lourdes : léger, pas sous le verrou
`state/executor.lock`.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from lib import github, notify, state
from lib.claude import MODELES_AUTORISES, ClaudeQuotaError, modele_depuis_label, run_claude

LABEL_READY = "ai-ready"
LABEL_WORKING = "ai-working"
LABEL_QUESTIONS = "triage:questions"
PREFIX_FORGE = "forge:"  # titre des issues créées par pipelines/forge.py
# Les commentaires de l'orchestrateur partent avec le même PAT que l'humain
# (même login) : le préfixe 🤖 est ce qui les distingue des vrais retours
# (même convention que dev_executor.PREFIX_BOT).
PREFIX_BOT = "🤖"

COMPLEXITES = {"S", "M", "L"}

ETAPE_CLARIFICATION = "triage-clarification"
# Au-delà, un ticket qui résiste à deux clarifications se discute ailleurs.
MAX_TOURS_CLARIFICATION = 2

# Sans heure de reprise annoncée par le CLI, on retente au bout de 30 min
# (même défaut que dev_executor, dupliqué pour rester un fichier autonome).
QUOTA_ATTENTE_DEFAUT = 30 * 60

MODELES_YAML = Path(__file__).parent.parent / "data" / "modeles.yaml"

PROMPT_TRIAGE = """Tu es le premier lecteur d'un nouveau ticket, avant qu'un agent de
développement ne l'implémente sans supervision humaine. Tu n'as pas accès au
dépôt (raisonnement pur, uniquement le texte du ticket ci-dessous) : évalue si
un agent raisonnable pourrait l'implémenter tel quel.

Ticket #{n} : {titre}

{corps}

Consignes :
- Pose des questions UNIQUEMENT si elles sont BLOQUANTES — impossible pour un
  agent raisonnable d'avancer sans réponse. 3 maximum. Si l'agent peut faire
  des choix par défaut sensés, considère le ticket clair (clair=true,
  questions=[]).
- complexite : "S" (quelques lignes, un fichier), "M" (plusieurs fichiers,
  logique modérée), "L" (architecture, plusieurs composants, migration).
- modele_suggere : "haiku" (mécanique, très simple), "sonnet" (défaut, la
  majorité des tickets), "opus" (conception délicate, fort risque).
- resume : 2-3 phrases — ce qu'un agent comprendrait du ticket et
  implémenterait, en signalant les risques ou zones d'ombre non bloquantes.
- fichiers_probables : chemins probables dans le dépôt, d'après le seul texte
  du ticket (best effort, [] si tu n'as aucune idée).

Réponds UNIQUEMENT par un objet JSON strict, sans texte autour, sans balises
markdown, exactement dans ce format :
{{"clair": bool, "resume": "...", "complexite": "S|M|L", "modele_suggere": "haiku|sonnet|opus", "fichiers_probables": ["..."], "questions": ["..."]}}"""

PROMPT_CLARIFICATION = """Tu avais jugé ce ticket flou et posé des questions bloquantes. Le
propriétaire a répondu. Réévalue si un agent raisonnable pourrait maintenant
l'implémenter, à la lumière de tout le fil ci-dessous.

Ticket #{n} : {titre}

Corps initial :
{corps}

Fil des commentaires (du plus ancien au plus récent) :
{fil}

Consignes :
- Si les réponses lèvent les points bloquants, considère le ticket clair
  (clair=true, questions=[]).
- Sinon, questions UNIQUEMENT sur ce qui reste bloquant. 3 maximum.
- complexite et modele_suggere : réévalue-les à la lumière du fil (même
  échelle qu'au premier triage : S/M/L, haiku/sonnet/opus).
- resume : 2-3 phrases sur l'état de compréhension actuel du ticket.
- fichiers_probables : chemins probables (best effort, [] si aucune idée).

Réponds UNIQUEMENT par un objet JSON strict, sans texte autour, sans balises
markdown, exactement dans ce format :
{{"clair": bool, "resume": "...", "complexite": "S|M|L", "modele_suggere": "haiku|sonnet|opus", "fichiers_probables": ["..."], "questions": ["..."]}}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _modeles_defaut() -> dict:
    if not MODELES_YAML.exists():
        return {}
    return yaml.safe_load(MODELES_YAML.read_text()) or {}


def _modele(labels: list[str]) -> str | None:
    """Modèle de l'étape triage : label `model:<alias>` du ticket > défaut
    de data/modeles.yaml > None (modèle par défaut de l'abonnement)."""
    return modele_depuis_label(labels) or _modeles_defaut().get("triage") or None


def _bloquer_quota(exc: ClaudeQuotaError) -> str:
    reprise = exc.reset_epoch or int(time.time()) + QUOTA_ATTENTE_DEFAUT
    state.bloquer_quota(reprise)
    return datetime.fromtimestamp(reprise).strftime("%H:%M")


def _parser_analyse(texte: str) -> dict | None:
    """Parse + valide strictement la sortie JSON attendue. None si invalide."""
    brut = _FENCE.sub("", texte).strip()
    try:
        d = json.loads(brut)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if not isinstance(d.get("clair"), bool):
        return None
    if not isinstance(d.get("resume"), str) or not d["resume"].strip():
        return None
    if d.get("complexite") not in COMPLEXITES:
        return None
    if d.get("modele_suggere") not in MODELES_AUTORISES:
        return None
    fichiers = d.get("fichiers_probables")
    if not isinstance(fichiers, list) or not all(isinstance(f, str) for f in fichiers):
        return None
    questions = d.get("questions")
    if (not isinstance(questions, list) or len(questions) > 3
            or not all(isinstance(q, str) for q in questions)):
        return None
    return d


async def trier(repo: str, issue: dict) -> None:
    """Triage d'une issue — une passe de raisonnement pur, puis labels + commentaire."""
    n = issue["number"]
    titre = issue["title"]

    try:
        corps = github.get_issue(repo, n)["body"]
        modele = _modele(issue.get("labels", []))
        resultat = await run_claude(
            PROMPT_TRIAGE.format(n=n, titre=titre, corps=corps or "(pas de description)"),
            allowed_tools=[],
            timeout=120,
            model=modele,
        )
    except ClaudeQuotaError as exc:
        # Pas marqué triée : retentée quand le quota reviendra.
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ #{n} : {exc} — raffinement reporté vers {reprise}")
        return
    except Exception as exc:
        # Échec dur : marqué triée (pas de retry en boucle sur une issue cassée).
        state.marquer_issue_triee(repo, n)
        print(f"[triage] #{n} : échec — {exc}")
        return

    state.enregistrer_conso(repo, n, "triage", resultat.tokens_entree, resultat.tokens_cache,
                            resultat.tokens_sortie, resultat.cout_usd, modele)

    analyse = _parser_analyse(resultat.texte)
    if analyse is None:
        state.marquer_issue_triee(repo, n)
        print(f"[triage] #{n} : sortie JSON invalide, ignorée")
        return

    labels = [f"size:{analyse['complexite']}", f"model:{analyse['modele_suggere']}"]
    if not analyse["clair"]:
        labels.append(LABEL_QUESTIONS)

    if analyse["clair"]:
        commentaire = (f"🤖 **Raffinement** — taille {analyse['complexite']}, "
                       f"modèle suggéré {analyse['modele_suggere']}\n\n{analyse['resume']}")
        if analyse["fichiers_probables"]:
            commentaire += "\n\n**Fichiers probables :**\n" + "\n".join(
                f"- `{f}`" for f in analyse["fichiers_probables"])
    else:
        questions = "\n".join(f"- {q}" for q in analyse["questions"])
        commentaire = (f"🤖 **Raffinement** — précisions nécessaires avant implémentation\n\n"
                       f"{analyse['resume']}\n\n{questions}")

    try:
        github.add_labels(repo, n, labels)
        github.comment_issue(repo, n, commentaire)
    except Exception as exc:
        # Pas marqué triée : retentée au prochain tour (ex. label pas encore créé sur le repo).
        await notify.notify(f"⚠️ #{n} : raffinement — échec écriture GitHub (labels/commentaire) — {exc}")
        return

    state.marquer_issue_triee(repo, n)

    await notify.notify(
        f"🔎 #{n} raffinée — {'clair' if analyse['clair'] else 'questions'} "
        f"(taille {analyse['complexite']}, modèle {analyse['modele_suggere']})"
    )


async def trier_nouveaux(repo: str) -> None:
    """Triage des nouveaux tickets d'un repo : owner-only, dédup, léger.

    Appelée à chaque tour de poll, avant la chaîne de priorité des actions
    lourdes. Sautée pendant un blocage de quota (les appels échoueraient de
    toute façon) — pas de notif supplémentaire, poll.py notifie déjà l'état.
    """
    if state.quota_bloque_jusqua() is not None:
        return
    proprietaire = repo.split("/")[0]
    for issue in github.list_issues(repo, state="open"):
        if state.issue_deja_triee(repo, issue["number"]):
            continue
        if issue["user"] != proprietaire:
            continue
        if issue["title"].startswith(PREFIX_FORGE):
            continue
        if LABEL_READY in issue["labels"] or LABEL_WORKING in issue["labels"]:
            continue
        await trier(repo, issue)


# --- Phase 2 : boucle de clarification --------------------------------------

def _label_par_prefixe(labels: list[str], prefixe: str) -> str | None:
    return next((l for l in labels if l.startswith(prefixe)), None)


def _maj_label_taille_modele(repo: str, n: int, labels_actuels: list[str], analyse: dict) -> None:
    """Retire/pose size:/model: seulement s'ils changent (garde l'historique propre)."""
    nouvelle_taille = f"size:{analyse['complexite']}"
    nouveau_modele = f"model:{analyse['modele_suggere']}"

    taille_actuelle = _label_par_prefixe(labels_actuels, "size:")
    if taille_actuelle != nouvelle_taille:
        if taille_actuelle:
            github.remove_label(repo, n, taille_actuelle)
        github.add_labels(repo, n, [nouvelle_taille])

    modele_actuel = _label_par_prefixe(labels_actuels, "model:")
    if modele_actuel != nouveau_modele:
        if modele_actuel:
            github.remove_label(repo, n, modele_actuel)
        github.add_labels(repo, n, [nouveau_modele])


async def clarifier(repo: str, issue: dict, nouveaux: list[dict]) -> None:
    """Re-triage d'une issue `triage:questions` suite à une réponse du propriétaire.

    `nouveaux` : commentaires du propriétaire pas encore vus (dédup
    state.commentaires_vus). Le fil complet (corps + tous les commentaires)
    est donné à Claude pour réévaluer la clarté du ticket.
    """
    n = issue["number"]
    titre = issue["title"]

    if state.triage_epuise(repo, n):
        for c in nouveaux:
            state.marquer_commentaire(repo, c["cle"])
        return

    try:
        issue_fraiche = github.get_issue(repo, n)
        corps = issue_fraiche["body"]
        labels_actuels = issue_fraiche["labels"]
        fil = "\n\n".join(f"{c['user']} : {c['body']}" for c in github.list_comments(repo, n))
        modele = _modele(labels_actuels)
        resultat = await run_claude(
            PROMPT_CLARIFICATION.format(n=n, titre=titre, corps=corps or "(pas de description)", fil=fil),
            allowed_tools=[],
            timeout=120,
            model=modele,
        )
    except ClaudeQuotaError as exc:
        # Pas marqués vus : la clarification repartira avec le quota.
        reprise = _bloquer_quota(exc)
        await notify.notify(f"⏳ #{n} : {exc} — clarification reportée vers {reprise}")
        return
    except Exception as exc:
        for c in nouveaux:
            state.marquer_commentaire(repo, c["cle"])
        print(f"[triage] #{n} : clarification — échec {exc}")
        return

    state.enregistrer_conso(repo, n, ETAPE_CLARIFICATION, resultat.tokens_entree,
                            resultat.tokens_cache, resultat.tokens_sortie,
                            resultat.cout_usd, modele)

    analyse = _parser_analyse(resultat.texte)
    if analyse is None:
        for c in nouveaux:
            state.marquer_commentaire(repo, c["cle"])
        print(f"[triage] #{n} : clarification — sortie JSON invalide, ignorée")
        return

    tours = state.compter_conso(repo, n, ETAPE_CLARIFICATION)  # inclut cet appel

    try:
        if analyse["clair"]:
            _maj_label_taille_modele(repo, n, labels_actuels, analyse)
            github.remove_label(repo, n, LABEL_QUESTIONS)
            commentaire = (f"🤖 **Clarification** — prêt pour `{LABEL_READY}` "
                           f"(taille {analyse['complexite']}, modèle {analyse['modele_suggere']})"
                           f"\n\n{analyse['resume']}")
            github.comment_issue(repo, n, commentaire)
            await notify.notify(f"✅ #{n} : questions levées, prêt pour {LABEL_READY}")
        elif tours >= MAX_TOURS_CLARIFICATION:
            state.marquer_triage_epuise(repo, n)
            await notify.notify(
                f"🔇 #{n} : toujours flou après {MAX_TOURS_CLARIFICATION} tours de "
                f"clarification — triage silencieux, à discuter autrement"
            )
        else:
            questions = "\n".join(f"- {q}" for q in analyse["questions"])
            commentaire = (f"🤖 **Clarification** — encore des précisions nécessaires "
                           f"({tours}/{MAX_TOURS_CLARIFICATION})\n\n{analyse['resume']}\n\n{questions}")
            github.comment_issue(repo, n, commentaire)
            await notify.notify(f"🔎 #{n} : nouvelles questions ({tours}/{MAX_TOURS_CLARIFICATION})")
    except Exception as exc:
        await notify.notify(f"⚠️ #{n} : clarification — échec écriture GitHub (labels/commentaire) — {exc}")
        return

    for c in nouveaux:
        state.marquer_commentaire(repo, c["cle"])


async def clarifier_nouveaux(repo: str) -> None:
    """Repère les issues `triage:questions` avec une réponse neuve du propriétaire.

    Owner-only (même contrainte que dev_executor.chercher_revision : sur un
    repo public, seul le propriétaire pilote un agent via commentaire).
    Écarte les commentaires de l'orchestrateur (préfixe 🤖) et les commandes
    (`/…`). Dédup partagée avec la boucle de révision (state.commentaires_vus,
    clé `issue-<id>` : les ids de commentaires sont uniques dans tout le repo).
    """
    if state.quota_bloque_jusqua() is not None:
        return
    proprietaire = repo.split("/")[0]
    for issue in github.list_issues(repo, labels=LABEL_QUESTIONS, state="open"):
        n = issue["number"]
        if state.triage_epuise(repo, n):
            continue
        nouveaux = []
        for c in github.list_comments(repo, n):
            if c["user"] != proprietaire:
                continue
            corps = (c["body"] or "").strip()
            if corps.startswith(PREFIX_BOT) or corps.startswith("/"):
                continue
            cle = f"issue-{c['id']}"
            if state.commentaire_deja_vu(repo, cle):
                continue
            nouveaux.append({**c, "cle": cle})
        if nouveaux:
            await clarifier(repo, issue, nouveaux)


if __name__ == "__main__":
    import asyncio
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) != 3:
        sys.exit("Usage : python -m pipelines.dev_triage <owner/repo> <numéro>")
    repo_arg, numero = sys.argv[1], int(sys.argv[2])
    issue_arg = github.get_issue(repo_arg, numero)
    asyncio.run(trier(repo_arg, issue_arg))
