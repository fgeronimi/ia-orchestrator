"""Store des restos — source de vérité : data/restos.json.

Toutes les lectures/écritures de la liste passent par ici. Le pipeline image
(perso_resto) et le pipeline conversationnel (perso_miamiton) partagent donc
le même schéma et la même dédup.

Un resto :
    {"nom", "adresse", "quartier", "statut": "a_faire"|"fait",
     "tags": [...], "note", "avis", "ajoute_le", "fait_le", "lat", "lon"}

lat/lon sont remplis best-effort par lib/geo (Nominatim) : un resto sans
coordonnées reste valide, il n'apparaît simplement pas sur la carte.

Les deux services (bot + server) écrivent dans ce fichier depuis des process
distincts : chaque écriture prend un verrou fichier et remplace le JSON de
façon atomique.
"""

import fcntl
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date
from pathlib import Path

RESTOS_JSON = Path(__file__).parent.parent / "data" / "restos.json"
_LOCK = RESTOS_JSON.with_suffix(".lock")

CHAMPS = ("nom", "adresse", "quartier", "statut", "tags", "note", "avis",
          "ajoute_le", "fait_le", "lat", "lon")


class RestoIntrouvable(LookupError):
    pass


class RestoAmbigu(LookupError):
    """Plusieurs restos matchent le nom donné — la liste est dans .matches."""

    def __init__(self, matches: list[dict]):
        self.matches = matches
        noms = ", ".join(r["nom"] for r in matches)
        super().__init__(f"Plusieurs restos correspondent : {noms}")


def normaliser(texte: str) -> str:
    """Minuscules sans accents ni ponctuation, pour comparer des noms."""
    texte = unicodedata.normalize("NFKD", texte or "")
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texte.lower()).strip()


@contextmanager
def _verrou():
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def charger() -> list[dict]:
    if not RESTOS_JSON.exists():
        return []
    return json.loads(RESTOS_JSON.read_text())


def _sauver(restos: list[dict]) -> None:
    RESTOS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESTOS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(restos, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, RESTOS_JSON)


def _index(restos: list[dict], nom: str) -> int:
    """Indice du resto correspondant à `nom`. Exact d'abord, sinon sous-chaîne."""
    cible = normaliser(nom)
    if not cible:
        raise RestoIntrouvable("nom vide")

    exacts = [i for i, r in enumerate(restos) if normaliser(r["nom"]) == cible]
    if len(exacts) == 1:
        return exacts[0]

    partiels = [
        i for i, r in enumerate(restos)
        if cible in normaliser(r["nom"]) or normaliser(r["nom"]) in cible
    ]
    if not partiels:
        raise RestoIntrouvable(nom)
    if len(partiels) > 1:
        raise RestoAmbigu([restos[i] for i in partiels])
    return partiels[0]


def ajouter(data: dict) -> tuple[dict, bool]:
    """Ajoute un resto. Retourne (entrée, créé) — créé=False si déjà présent."""
    if not (data.get("nom") or "").strip():
        raise ValueError("resto sans nom")

    with _verrou():
        restos = charger()
        try:
            existant = restos[_index(restos, data["nom"])]
        except (RestoIntrouvable, RestoAmbigu):
            pass
        else:
            return existant, False

        entree = {
            "nom": data["nom"].strip(),
            "adresse": data.get("adresse"),
            "quartier": data.get("quartier"),
            "statut": data.get("statut") or "a_faire",
            "tags": data.get("tags") or [],
            "note": data.get("note"),
            "avis": None,
            "ajoute_le": date.today().isoformat(),
            "fait_le": None,
            "lat": data.get("lat"),
            "lon": data.get("lon"),
        }
        restos.append(entree)
        _sauver(restos)
        return entree, True


def marquer_fait(nom: str, avis: str | None = None,
                 quand: str | None = None) -> dict:
    with _verrou():
        restos = charger()
        resto = restos[_index(restos, nom)]
        resto["statut"] = "fait"
        resto["fait_le"] = quand or date.today().isoformat()
        if avis:
            resto["avis"] = avis
        _sauver(restos)
        return resto


def supprimer(nom: str) -> dict:
    with _verrou():
        restos = charger()
        resto = restos.pop(_index(restos, nom))
        _sauver(restos)
        return resto


def corriger(nom: str, champs: dict) -> dict:
    """Met à jour les champs fournis (les clés inconnues sont ignorées)."""
    with _verrou():
        restos = charger()
        resto = restos[_index(restos, nom)]
        for cle, valeur in champs.items():
            if cle in CHAMPS:
                resto[cle] = valeur
        _sauver(restos)
        return resto


def _matche_lieu(resto: dict, lieu: str) -> bool:
    """Quartier, adresse, ou arrondissement écrit « 11e » / « 11ème »."""
    champs = normaliser(f"{resto.get('quartier') or ''} {resto.get('adresse') or ''}")
    arrdt = re.fullmatch(r"(\d{1,2})\s*(?:e|eme|er)?", normaliser(lieu))
    if arrdt:
        return f"750{int(arrdt.group(1)):02d}" in champs
    return normaliser(lieu) in champs


def filtrer(statut: str | None = None, lieu: str | None = None,
            tags: list[str] | None = None, texte: str | None = None) -> list[dict]:
    resultat = charger()
    if statut:
        resultat = [r for r in resultat if r.get("statut") == statut]
    if lieu:
        resultat = [r for r in resultat if _matche_lieu(r, lieu)]
    if tags:
        cibles = {normaliser(t) for t in tags}
        resultat = [
            r for r in resultat
            if cibles & {normaliser(t) for t in (r.get("tags") or [])}
        ]
    if texte:
        cible = normaliser(texte)
        resultat = [
            r for r in resultat
            if cible in normaliser(" ".join(str(r.get(c) or "") for c in CHAMPS))
        ]
    return resultat


def formater(resto: dict) -> str:
    """Une ligne Discord pour un resto."""
    ligne = f"**{resto['nom']}**"
    lieu = resto.get("quartier") or resto.get("adresse")
    if lieu:
        ligne += f" — {lieu}"
    if resto.get("statut") == "fait":
        ligne += f" ✅ {resto.get('fait_le') or ''}".rstrip()
        if resto.get("avis"):
            ligne += f" _{resto['avis']}_"
    elif resto.get("note"):
        ligne += f" _{resto['note']}_"
    return ligne
