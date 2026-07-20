# ia-orchestrator

Orchestration d'agents Claude Code pilotée par Discord, sur Raspberry Pi (ou VPS).
Architecture complète : [`docs/architecture-mini-serveur-ia.md`](docs/architecture-mini-serveur-ia.md)

## Démarrage rapide

```bash
# 1. Installer le serveur (Pi ou VPS Debian)
bash infra/setup.sh

# 2. Configurer les secrets
cp .env.example .env && chmod 600 .env
# → remplir DISCORD_BOT_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, NOTIFY_CHANNEL_ID

# 3. Tester en direct
.venv/bin/python bot.py
# → dans Discord, canal #idees : @Orchestrator ajoute un cache Redis sur l'API users

# 4. Installer en service (démarrage auto + restart)
sudo cp infra/systemd/orchestrator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-bot
journalctl -u orchestrator-bot -f
```

## Test en local (Mac)

Identique : mêmes fichiers, même `.env` (avec un bot Discord de *test* séparé
pour ne pas interférer avec celui du Pi).

```bash
python3 -m venv .venv
.venv/bin/pip install discord.py python-dotenv requests
.venv/bin/python bot.py
```

## Structure

```
bot.py                  # routeur Discord (aucune logique métier)
pipelines/dev_jira.py   # pipeline dev : idée → brouillon ticket (v0)
lib/claude.py           # wrapper subprocess Claude Code
lib/notify.py           # notifications (Discord)
infra/setup.sh          # install serveur idempotente
infra/systemd/          # service systemd
```

## Ajouter un pipeline

1. Créer `pipelines/mon_pipeline.py` avec `async def handle(text, message) -> str`
2. L'enregistrer dans `PIPELINES` de `bot.py` (canal → handler)
3. Secrets éventuels dans `.env`, scope minimal
