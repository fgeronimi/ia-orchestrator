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
        requests.post(webhook, json={"content": f"🔔 {message}"}, timeout=10)
        return

    # Dernier recours : au moins une trace dans les logs systemd
    print(f"[notify] {message}")
