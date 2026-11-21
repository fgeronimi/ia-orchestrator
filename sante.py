"""sante.py — un tour de surveillance de la machine (disque, token GitHub).

Lancé toutes les 15 minutes par le timer systemd `orchestrator-sante` ; toute
la logique est dans `pipelines/sante.py` (mesures locales, aucun appel Claude ;
seuls appels réseau : un GET /user pour valider le token GitHub, et les notifs).

Pas de verrou fichier ici : le tour est court, en lecture seule, et sa seule
écriture (les états d'alerte en SQLite) est idempotente.

Usage :
    .venv/bin/python sante.py
"""

import asyncio

from dotenv import load_dotenv

from pipelines import sante

if __name__ == "__main__":
    load_dotenv()
    print(f"[sante] {asyncio.run(sante.handle())}")
