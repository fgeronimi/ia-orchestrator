# ia-orchestrator

Orchestrateur d'agents Claude Code sur Raspberry Pi (ou VPS). Tu crées des
tickets dans **GitHub**, le Pi les implémente, ouvre des PR, se relit, et gère
la suite après ton merge. Notifications sur Discord.

- **Plan du pipeline dev (cap actuel)** : [`docs/plan-orchestrateur-dev.md`](docs/plan-orchestrateur-dev.md)
- **Architecture & état réel** : [`docs/architecture-mini-serveur-ia.md`](docs/architecture-mini-serveur-ia.md)

> État : le pipeline dev GitHub est **en construction** (voir le plan). Le
> routeur Discord (`bot.py`) et l'infra (auto-update, services systemd) sont en
> place. `pipelines/dev_jira.py` est un vestige d'avant le pivot (à retirer ou
> recycler).

## Démarrage rapide (Pi ou VPS Debian)

```bash
bash infra/setup.sh                         # install idempotente
cp .env.example .env && chmod 600 .env      # puis remplir les secrets
sudo cp infra/systemd/*.service infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-bot orchestrator-server
make install-timer                          # auto-update git toutes les 10 min
```

## Exploitation

Tout passe par le `Makefile` — `make` seul liste les cibles (sur le Pi vs depuis
le Mac). Les plus courantes :

```bash
make deploy          # (Mac) push le code + met le Pi à jour
make remote-logs     # (Mac) suit les logs du Pi
make status / logs   # (Pi) état et logs des services
```

Le Pi se met à jour tout seul : un push sur `main` est récupéré et les services
redémarrés dans les 10 min (`infra/sync.sh` + `orchestrator-sync.timer`).

## Conventions

- **Un pipeline = un fichier** dans `pipelines/`, point d'entrée `async def handle(...)`.
- **Tout appel à Claude** passe par `lib/claude.run_claude()` (`allowed_tools` scope les droits).
- **Toute notif** passe par `lib/notify.notify()`.
- **Secrets** via `.env` uniquement, jamais commités.
- Détails et règles d'or : [`CLAUDE.md`](CLAUDE.md).
