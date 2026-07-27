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

COMPLEXITES = {"S", "M", "L"}

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
        await notify.notify(f"⏳ #{n} : {exc} — triage reporté vers {reprise}")
        return
    except Exception as exc:
        # Échec dur : marqué triée (pas de retry en boucle sur une issue cassée).
        state.marquer_issue_triee(repo, n)
        print(f"[triage] #{n} : échec — {exc}")
        return

    state.marquer_issue_triee(repo, n)
    state.enregistrer_conso(repo, n, "triage", resultat.tokens_entree, resultat.tokens_cache,
                            resultat.tokens_sortie, resultat.cout_usd, modele)

    analyse = _parser_analyse(resultat.texte)
    if analyse is None:
        print(f"[triage] #{n} : sortie JSON invalide, ignorée")
        return

    labels = [f"size:{analyse['complexite']}", f"model:{analyse['modele_suggere']}"]
    if not analyse["clair"]:
        labels.append(LABEL_QUESTIONS)
    github.add_labels(repo, n, labels)

    if analyse["clair"]:
        commentaire = (f"🤖 **Triage** — taille {analyse['complexite']}, "
                       f"modèle suggéré {analyse['modele_suggere']}\n\n{analyse['resume']}")
        if analyse["fichiers_probables"]:
            commentaire += "\n\n**Fichiers probables :**\n" + "\n".join(
                f"- `{f}`" for f in analyse["fichiers_probables"])
    else:
        questions = "\n".join(f"- {q}" for q in analyse["questions"])
        commentaire = (f"🤖 **Triage** — précisions nécessaires avant implémentation\n\n"
                       f"{analyse['resume']}\n\n{questions}")
    github.comment_issue(repo, n, commentaire)

    await notify.notify(
        f"🔎 #{n} triée — {'clair' if analyse['clair'] else 'questions'} "
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
