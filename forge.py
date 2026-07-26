"""forge.py — un passage de vérification des conditions déclaratives des repos
surveillés (data/forge.yaml). Lancé une fois par jour par le timer systemd
`orchestrator-forge` ; toute la logique est dans `pipelines/forge.py` (API
pure, aucun appel Claude).

Verrou fichier (state/forge.lock) : même principe que le verrou de l'action
lourde de poll.py — évite deux passages simultanés (timer + lancement manuel).

Usage :
    .venv/bin/python forge.py
"""

import asyncio
import fcntl
from pathlib import Path

from dotenv import load_dotenv

from pipelines import forge

RACINE = Path(__file__).parent
VERROU = RACINE / "state" / "forge.lock"


def main() -> None:
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[forge] passage déjà en cours (verrou pris), on repassera")
            return
        resultat = asyncio.run(forge.handle())
        print(f"[forge] {resultat}")


if __name__ == "__main__":
    load_dotenv()
    main()
