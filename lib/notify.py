"""Notifications — backend Discord pour le moment.

Les pipelines appellent notify() sans savoir comment la notif part.
Le jour où l'on ajoute ntfy/Pushover, seul ce fichier change.

Deux modes :
- via le bot en cours d'exécution (set_bot() appelé par bot.py)
- via webhook Discord (DISCORD_WEBHOOK_URL) pour les process hors bot
  (ex: systemd timer qui tourne sans le bot)
"""

import os

import requests

_bot = None
_channel_id: int | None = None


def set_bot(bot, channel_id: int) -> None:
    """Appelé par bot.py au démarrage : notifs via le bot lui-même."""
    global _bot, _channel_id
    _bot = bot
    _channel_id = channel_id


async def notify(message: str) -> None:
    """Envoie une notification (canal Discord des notifs)."""
    if _bot is not None and _channel_id is not None:
        channel = _bot.get_channel(_channel_id)
        if channel is not None:
            await channel.send(f"🔔 {message}")
            return

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        # Ne pas avaler les échecs : un webhook invalide/tronqué renvoie un
        # 4xx sans lever d'exception — sinon la notif est perdue en silence.
        try:
            r = requests.post(webhook, json={"content": f"🔔 {message}"}, timeout=10)
            if r.status_code >= 300:
                print(f"[notify] échec webhook Discord : HTTP {r.status_code} "
                      f"{r.text[:200]}")
        except requests.RequestException as exc:
            print(f"[notify] webhook injoignable : {exc}")
        return

    # Dernier recours : au moins une trace dans les logs systemd
    print(f"[notify] {message}")


if __name__ == "__main__":
    # Entrée CLI pour les scripts shell (infra/sync.sh) : leurs notifs passent
    # par ici aussi, plutôt que par un curl en dur vers le webhook.
    #   .venv/bin/python -m lib.notify "message"
    import asyncio
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(notify(" ".join(sys.argv[1:]) or "(message vide)"))
