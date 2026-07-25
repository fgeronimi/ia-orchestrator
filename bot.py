"""Routeur Discord minimal → Claude Code.

Principe : le bot ne contient AUCUNE logique métier.
Il route les messages vers le pipeline associé au canal, c'est tout.
La logique vit dans pipelines/*.py.

Usage : mentionner le bot dans un canal configuré.
  @Orchestrator crée un ticket pour ajouter un cache Redis sur l'API users
"""

import asyncio
import os

import discord
from dotenv import load_dotenv

from lib import notify
from pipelines import dev_jira

load_dotenv()

# Lu au lancement (bloc __main__), pas à l'import : le module doit rester
# importable sans .env (make test, CI).
NOTIFY_CHANNEL_ID = int(os.environ.get("NOTIFY_CHANNEL_ID", "0"))

# Mapping canal → handler de pipeline.
# Chaque handler : async def handle(text: str, message) -> str
PIPELINES = {
    "idees": dev_jira.handle,
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _chunks(text: str, size: int = 1990):
    """Discord limite les messages à 2000 caractères."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


@client.event
async def on_ready():
    if NOTIFY_CHANNEL_ID:
        notify.set_bot(client, NOTIFY_CHANNEL_ID)
    print(f"Connecté en tant que {client.user}")


@client.event
async def on_message(message: discord.Message):
    # Ignorer ses propres messages et ceux des autres bots
    if message.author.bot:
        return
    # Ne réagir qu'aux mentions explicites
    if client.user not in message.mentions:
        return

    handler = PIPELINES.get(message.channel.name)
    if handler is None:
        await message.reply(
            f"Aucun pipeline configuré pour #{message.channel.name}."
        )
        return

    text = message.clean_content.replace(f"@{client.user.name}", "").strip()
    if not text:
        await message.reply("Dis-moi quoi faire après la mention.")
        return

    async with message.channel.typing():
        try:
            result = await handler(text, message)
        except Exception as exc:  # remonter l'erreur plutôt que silence
            result = f"❌ Erreur : {exc}"

    for chunk in _chunks(result):
        await message.reply(chunk)


if __name__ == "__main__":
    client.run(os.environ["DISCORD_BOT_TOKEN"])
