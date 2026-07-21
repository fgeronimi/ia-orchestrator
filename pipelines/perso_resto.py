"""Pipeline Perso — screenshot → liste restos (.md) ou agenda partagé.

v0 : la partie "restos.md" est complète.
La partie agenda Google Calendar est un stub (lib/gcal.py) en attendant
la config OAuth Google Cloud — voir le TODO dans ce fichier.
"""

import json
import re
from pathlib import Path

from lib.claude import run_claude

RESTOS_MD = Path(__file__).parent.parent / "data" / "restos.md"

PROMPT_TEMPLATE = """Regarde l'image à ce chemin : {image_path}
Utilise l'outil Read pour l'ouvrir.

C'est soit :
- un restaurant (post Instagram, site, recommandation) → type "resto"
- une confirmation de réservation (resto, hôtel, activité) → type "reservation"
- autre chose → type "autre"

Réponds UNIQUEMENT avec un objet JSON valide, rien d'autre, format exact :

Si type "resto":
{{"type": "resto", "nom": "...", "adresse": "... (ou null si inconnue)", "note": "... (contexte utile en une phrase, ou null)"}}

Si type "reservation":
{{"type": "reservation", "lieu": "...", "date": "YYYY-MM-DD (ou null si illisible)", "heure": "HH:MM (ou null)", "note": "... (détails utiles, ou null)"}}

Si type "autre":
{{"type": "autre", "raison": "..."}}

Pas de texte avant/après le JSON, pas de balises markdown."""


def _extract_json(raw: str) -> dict:
    """Claude peut entourer le JSON de ```json ... ``` malgré la consigne."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Pas de JSON trouvé dans la réponse : {raw[:200]}")
    return json.loads(match.group(0))


def _append_resto(data: dict) -> str:
    RESTOS_MD.parent.mkdir(parents=True, exist_ok=True)
    if not RESTOS_MD.exists():
        RESTOS_MD.write_text("# Restos à faire\n\n")

    ligne = f"- **{data['nom']}**"
    if data.get("adresse"):
        ligne += f" — {data['adresse']}"
    if data.get("note"):
        ligne += f" _{data['note']}_"
    ligne += "\n"

    with RESTOS_MD.open("a") as f:
        f.write(ligne)

    return f"🍽️ Ajouté à la liste restos : **{data['nom']}**"


def _handle_reservation(data: dict) -> str:
    # TODO : brancher lib/gcal.py une fois l'OAuth Google Calendar configuré.
    # En attendant, on renvoie l'info extraite pour que tu puisses l'ajouter
    # à la main, et on la log quand même dans data/restos.md pour ne rien perdre.
    resume = (
        f"📅 Réservation détectée : **{data.get('lieu', '?')}** "
        f"le {data.get('date', '?')} à {data.get('heure', '?')}\n"
        f"_agenda pas encore branché — ajoute-la à la main pour l'instant "
        f"({data.get('note') or 'pas de détail supplémentaire'})_"
    )
    return resume


async def handle_image(image_path: str) -> str:
    raw = await run_claude(
        PROMPT_TEMPLATE.format(image_path=image_path),
        allowed_tools=["Read"],
        timeout=120,
    )
    data = _extract_json(raw)

    if data["type"] == "resto":
        return _append_resto(data)
    if data["type"] == "reservation":
        return _handle_reservation(data)
    return f"🤷 Image non classée comme resto/réservation : {data.get('raison', 'raison inconnue')}"
