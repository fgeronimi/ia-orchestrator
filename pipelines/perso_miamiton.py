"""Pipeline Perso conversationnel — discuter de la liste restos dans #miamiton.

Claude ne fait qu'une chose ici : traduire le message en une intention JSON.
Toute la lecture/écriture est ensuite du Python déterministe (lib/restos),
pour que « les restos pas faits dans le 11e » donne une vraie réponse et pas
une reconstitution de mémoire.

Exemples :
    @bot les restos pas faits près de Bastille
    @bot ajoute Septime (80 rue de Charonne) et Le Chateaubriand
    @bot j'ai fait Bofinger hier, très bon mais bruyant
    @bot enlève Le Chateaubriand
"""

import json
import re
from datetime import date

from lib import geo, restos
from lib.claude import run_claude

LIMITE_AFFICHAGE = 30

PROMPT_TEMPLATE = """Tu traduis un message en intention JSON pour une liste de restaurants.

Date du jour : {aujourdhui}
Restos déjà en liste : {noms}

Message de l'utilisateur :
\"\"\"{texte}\"\"\"

Réponds UNIQUEMENT avec un objet JSON valide, sans texte ni balises autour.
Une seule de ces formes :

Consulter la liste :
{{"action": "lister", "statut": "a_faire"|"fait"|null, "lieu": "quartier, ville ou arrondissement type 11e (ou null)", "tags": ["cuisine ou type, ex: italien, brasserie"], "texte": "mot-clé libre (ou null)"}}

Ajouter un ou plusieurs restos (déduis quartier et tags de ce que tu sais) :
{{"action": "ajouter", "restos": [{{"nom": "...", "adresse": "... (ou null)", "quartier": "... (ou null)", "tags": [...], "note": "... (ou null)"}}]}}

Marquer un resto comme fait (utilise un nom de la liste ci-dessus) :
{{"action": "marquer_fait", "nom": "...", "avis": "son ressenti (ou null)", "date": "YYYY-MM-DD (ou null)"}}

Supprimer :
{{"action": "supprimer", "nom": "..."}}

Corriger des champs :
{{"action": "corriger", "nom": "...", "champs": {{"adresse": "...", "quartier": "...", "note": "..."}}}}

Rien de tout ça (question générale, discussion) :
{{"action": "discuter", "reponse": "ta réponse en français, 3 phrases max"}}"""


def _extraire_json(brut: str) -> dict:
    match = re.search(r"\{.*\}", brut, re.DOTALL)
    if not match:
        raise ValueError(f"Pas de JSON dans la réponse : {brut[:200]}")
    return json.loads(match.group(0))


def _lister(intention: dict) -> str:
    trouves = restos.filtrer(
        statut=intention.get("statut"),
        lieu=intention.get("lieu"),
        tags=intention.get("tags"),
        texte=intention.get("texte"),
    )
    if not trouves:
        return "🍽️ Rien qui corresponde dans la liste."

    entete = f"🍽️ {len(trouves)} resto{'s' if len(trouves) > 1 else ''}"
    if intention.get("lieu"):
        entete += f" vers {intention['lieu']}"
    if intention.get("statut") == "a_faire":
        entete += " (pas encore faits)"
    elif intention.get("statut") == "fait":
        entete += " (déjà faits)"

    lignes = [restos.formater(r) for r in trouves[:LIMITE_AFFICHAGE]]
    if len(trouves) > LIMITE_AFFICHAGE:
        lignes.append(f"_… et {len(trouves) - LIMITE_AFFICHAGE} autres_")
    return entete + " :\n" + "\n".join(f"- {ligne}" for ligne in lignes)


def _ajouter(intention: dict) -> str:
    ajoutes, doublons, erreurs = [], [], []
    for data in intention.get("restos") or []:
        try:
            entree, cree = restos.ajouter(geo.enrichir(data))
        except ValueError as exc:
            erreurs.append(str(exc))
            continue
        (ajoutes if cree else doublons).append(entree["nom"])

    if not ajoutes and not doublons:
        return "🤷 Aucun resto identifiable dans ce message."

    parties = []
    if ajoutes:
        parties.append(f"🍽️ Ajouté{'s' if len(ajoutes) > 1 else ''} : "
                       + ", ".join(f"**{n}**" for n in ajoutes))
    if doublons:
        parties.append(f"_Déjà en liste : {', '.join(doublons)}_")
    if erreurs:
        parties.append(f"_Ignoré(s) : {'; '.join(erreurs)}_")
    return "\n".join(parties)


def _marquer_fait(intention: dict) -> str:
    resto = restos.marquer_fait(
        intention["nom"], intention.get("avis"), intention.get("date")
    )
    reponse = f"✅ **{resto['nom']}** marqué comme fait le {resto['fait_le']}."
    if resto.get("avis"):
        reponse += f"\n_{resto['avis']}_"
    return reponse


def _supprimer(intention: dict) -> str:
    resto = restos.supprimer(intention["nom"])
    return f"🗑️ **{resto['nom']}** retiré de la liste."


def _corriger(intention: dict) -> str:
    resto = restos.corriger(intention["nom"], intention.get("champs") or {})
    return f"✏️ Mis à jour : {restos.formater(resto)}"


ACTIONS = {
    "lister": _lister,
    "ajouter": _ajouter,
    "marquer_fait": _marquer_fait,
    "supprimer": _supprimer,
    "corriger": _corriger,
}


async def handle(text: str, message=None) -> str:
    noms = [r["nom"] for r in restos.charger()]
    brut = await run_claude(
        PROMPT_TEMPLATE.format(
            aujourdhui=date.today().isoformat(),
            noms=", ".join(noms) if noms else "(liste vide)",
            texte=text,
        ),
        allowed_tools=[],  # raisonnement pur : aucun accès disque pour Claude
        timeout=120,
    )
    intention = _extraire_json(brut)

    action = ACTIONS.get(intention.get("action"))
    if action is None:
        return intention.get("reponse") or "🤷 Je n'ai pas compris la demande."

    try:
        return action(intention)
    except restos.RestoAmbigu as exc:
        propositions = ", ".join(r["nom"] for r in exc.matches)
        return f"🤔 Plusieurs restos correspondent : {propositions}. Lequel ?"
    except restos.RestoIntrouvable as exc:
        return f"🤷 Pas trouvé dans la liste : {exc}"
