"""sante.py — un tour de surveillance de la machine (disque en priorité).

Lancé toutes les 15 minutes par le timer systemd `orchestrator-sante` ; toute
la logique est dans `pipelines/sante.py` (mesures locales, aucun appel Claude,
aucun appel réseau hors notification Discord).

Pas de verrou fichier ici : le tour est court, en lecture seule, et sa seule
écriture (le palier d'alerte en SQLite) est idempotente.

Usage :
    .venv/bin/python sante.py
"""

import asyncio

from dotenv import load_dotenv

from pipelines import sante

if __name__ == "__main__":
    load_dotenv()
    print(f"[sante] {asyncio.run(sante.handle())}")
