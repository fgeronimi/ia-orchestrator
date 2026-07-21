"""Géocodage des restos via Nominatim (OpenStreetMap).

Gratuit et sans clé, ce qui suffit largement au volume ici (quelques dizaines
d'adresses). Sa politique d'usage impose en contrepartie un User-Agent
identifiant et 1 requête/seconde maximum : `backfill()` respecte ce délai.

Le géocodage est toujours **best-effort** : un resto sans coordonnées reste un
resto valide, il n'apparaîtra simplement pas sur la carte.
"""

import os
import time

import requests

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ia-orchestrator/1.0 (usage personnel; github.com/fgeronimi/ia-orchestrator)"

# Ville par défaut quand un resto n'a qu'un nom, sans adresse.
VILLE_DEFAUT = os.environ.get("VILLE_DEFAUT", "Paris, France")

DELAI_NOMINATIM = 1.1  # secondes entre deux appels (politique d'usage OSM)


def geocoder(nom: str, adresse: str | None = None) -> dict | None:
    """Retourne {lat, lon, adresse, quartier} ou None si rien de trouvé."""
    requete = adresse or f"{nom}, {VILLE_DEFAUT}"
    try:
        reponse = requests.get(
            NOMINATIM,
            params={"q": requete, "format": "jsonv2", "limit": 1,
                    "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        reponse.raise_for_status()
        resultats = reponse.json()
    except (requests.RequestException, ValueError):
        return None

    if not resultats:
        return None

    lieu = resultats[0]
    detail = lieu.get("address") or {}
    quartier = (detail.get("neighbourhood") or detail.get("suburb")
                or detail.get("city_district") or detail.get("village"))

    return {
        "lat": float(lieu["lat"]),
        "lon": float(lieu["lon"]),
        "adresse": lieu.get("display_name"),
        "quartier": quartier,
    }


def enrichir(data: dict) -> dict:
    """Complète lat/lon (et adresse/quartier si absents) avant l'ajout au store.

    Ne remplace jamais une valeur déjà renseignée par Claude, et n'échoue
    jamais : sans réseau, le resto est simplement enregistré sans coordonnées.
    """
    if data.get("lat") is not None:
        return data

    trouve = geocoder(data.get("nom", ""), data.get("adresse"))
    if trouve is None:
        return data

    data["lat"] = trouve["lat"]
    data["lon"] = trouve["lon"]
    data.setdefault("adresse", None)
    if not data.get("adresse"):
        data["adresse"] = trouve["adresse"]
    if not data.get("quartier"):
        data["quartier"] = trouve["quartier"]
    return data


def backfill(verbeux: bool = True) -> tuple[int, int]:
    """Géocode les restos existants qui n'ont pas encore de coordonnées.

    Retourne (géocodés, échecs). Appelé par `make geocode`.
    """
    from lib import restos  # import tardif : évite un cycle geo <-> restos

    a_traiter = [r for r in restos.charger() if r.get("lat") is None]
    ok = ko = 0

    for i, resto in enumerate(a_traiter):
        if i:
            time.sleep(DELAI_NOMINATIM)
        trouve = geocoder(resto["nom"], resto.get("adresse"))
        if trouve is None:
            ko += 1
            if verbeux:
                print(f"  ✗ {resto['nom']}")
            continue

        champs = {"lat": trouve["lat"], "lon": trouve["lon"]}
        if not resto.get("adresse"):
            champs["adresse"] = trouve["adresse"]
        if not resto.get("quartier"):
            champs["quartier"] = trouve["quartier"]
        restos.corriger(resto["nom"], champs)
        ok += 1
        if verbeux:
            print(f"  ✓ {resto['nom']} — {trouve['lat']:.5f}, {trouve['lon']:.5f}")

    return ok, ko


if __name__ == "__main__":
    ok, ko = backfill()
    print(f"Géocodage terminé : {ok} ok, {ko} en échec.")
