"""Pipeline Dev v0 — Discord → (bientôt) ticket Jira.

Étape actuelle : valider la chaîne Discord → Claude Code de bout en bout.
Claude reformule l'idée en brouillon de ticket structuré, sans encore
appeler l'API Jira. Une fois la chaîne validée, on branchera lib/jira.py.
"""

from lib.claude import run_claude

PROMPT_TEMPLATE = """Tu es un assistant qui transforme des idées brutes en brouillons de tickets.

Idée reçue : {idea}

Réponds en français, format exact :
**Titre** : (une ligne, impératif)
**Description** : (2-4 phrases, contexte et objectif)
**Critères d'acceptation** :
- (2 à 4 critères testables)
**Estimation IA-ready** : OUI/NON + une phrase de justification
(IA-ready = scope clair, critères testables, pas de décision produit ouverte)

Ne réponds rien d'autre que ce format."""


async def handle(text: str, message) -> str:
    draft = await run_claude(
        PROMPT_TEMPLATE.format(idea=text),
        allowed_tools=[],  # pur raisonnement, aucun outil nécessaire
        timeout=120,
    )
    return f"📝 Brouillon de ticket :\n\n{draft}\n\n_(v0 : pas encore envoyé à Jira)_"
