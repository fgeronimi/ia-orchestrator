"""purge.py — un passage de purge des workspaces (branches locales des PR
mergées). Lancé une fois par jour par le timer systemd `orchestrator-purge` ;
toute la logique est dans `pipelines/purge.py` (git local + API GitHub en
lecture, aucun appel Claude).

Verrou : **`state/executor.lock`**, celui de l'action lourde de `poll.py` — et
non un verrou propre. C'est volontaire : la purge touche aux branches des
workspaces où l'exécutant code, donc les deux ne doivent jamais tourner en même
temps. Si l'exécutant travaille, on repasse demain.

Usage :
    .venv/bin/python purge.py
"""

import asyncio
import fcntl
from pathlib import Path

from dotenv import load_dotenv

from pipelines import purge

RACINE = Path(__file__).parent
VERROU = RACINE / "state" / "executor.lock"


def main() -> None:
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    with open(VERROU, "w") as verrou:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[purge] exécutant en cours (verrou pris) — on repassera")
            return
        print(f"[purge] {asyncio.run(purge.handle())}")


if __name__ == "__main__":
    load_dotenv()
    main()
