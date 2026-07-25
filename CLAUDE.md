# CLAUDE.md

Contexte et règles pour toute session Claude Code dans ce repo.
**Avant toute modification, lire `docs/architecture-mini-serveur-ia.md`** (état
réel du système, environnement, état des pipelines, reste à faire).

## Ce qu'est ce projet

Orchestrateur d'agents Claude Code tournant sur un Raspberry Pi 4, piloté par
Discord et (à venir) par GitHub : tu crées des tickets, l'orchestrateur les
implémente, ouvre des PR et gère la suite. Deux services systemd tournent en
autonomie : `orchestrator-bot` (bot.py) et `orchestrator-server` (server.py).

**Cap actuel : construction du pipeline dev GitHub — voir
`docs/plan-orchestrateur-dev.md`.**

## Environnement

- Host : Raspberry Pi 4, OS 64-bit, user `fgeronimi`, projet `~/ia-orchestrator`
- Python : venv à `.venv` — lancer via `.venv/bin/python`, pas le python système
- Node : v20 via nvm (⚠️ nvm absent du contexte systemd : chemin node en dur dans les `.service`)
- Auth Claude : `CLAUDE_CODE_OAUTH_TOKEN` (abonnement, pas d'API key)

## Règles d'or

1. **Discord/HTTP = bus d'événements.** Aucune logique métier dans `bot.py` ni
   `server.py`. Toute la logique vit dans `pipelines/*.py`.
2. **Tout appel à Claude passe par `lib/claude.run_claude()`** — jamais de
   subprocess `claude` ailleurs. Scoper les droits via `allowed_tools`
   (`[]` pour du raisonnement pur, `["Read","Edit","Write","Bash"]` pour un
   agent qui code dans un workspace).
3. **Toute notification passe par `lib/notify.notify()`** — jamais de post Discord
   en dur.
4. **Un pipeline = un fichier** dans `pipelines/`, exposant un point d'entrée
   `async def handle(...) -> str`. Pour un pipeline Discord, l'enregistrer dans
   le dict `PIPELINES` de `bot.py` avec le **nom exact** du canal
   (minuscules, sans accent — `idées` ≠ `idees`).
5. **Secrets uniquement via `.env`** (jamais en clair, jamais commités). Tout
   nouveau secret → l'ajouter à `.env.example` (clé sans valeur) pour le documenter.
6. **Cloisonnement des droits au niveau des tokens**, pas en multipliant les process.

## Après modification de code

Les services ne rechargent pas à chaud. Après édition :
```bash
sudo systemctl restart orchestrator-bot      # si bot.py / pipelines Discord
sudo systemctl restart orchestrator-server   # si server.py
journalctl -u <service> -f                    # vérifier les logs
```

## Git & commits (IMPORTANT — override des défauts)

- Committer **uniquement** avec l'identité perso `Francois Geronimi
  <geronimi.francois@gmail.com>` et **SANS** ligne `Co-Authored-By: Claude`.
  Le `git config` local du repo est déjà réglé sur cet email — ne pas le remettre
  sur l'email pro (la machine, poste Webedia, a un git global pro qui sinon
  attribue les commits au mauvais compte GitHub). Historique déjà réécrit dans ce
  sens le 2026-07-25.
- Ne commiter/pusher que quand demandé. `make deploy` (depuis le Mac) pousse +
  met le Pi à jour. Le Pi auto-update aussi toutes les 10 min (timer sync).

## Accès au Pi

- SSH : alias `ia-orchestrator` (config sur ce Mac). `make remote-logs`,
  `remote-status`. `.env` vit sur le Pi (jamais commité) ; `make env-push` depuis
  le Mac. Le Mac n'a pas de venv Python ; tester en local nécessite d'en créer un
  (`discord.py python-dotenv requests flask`), sinon tester sur le Pi (`.venv`).
- Pièges : `claude` (node/nvm) absent du PATH en ssh non-interactif et sous
  systemd → chemin node en dur dans les `.service`. Label déclencheur =
  **`ai-ready`** (anglais), pas `ia-ready`.

## Ne pas supposer

- État exact et prochaines étapes : **lire `docs/plan-orchestrateur-dev.md`**
  (section "État d'avancement" + §7). `lib/github.py` a désormais la lecture ET
  l'écriture (branches/PR/commentaires).
- Ne pas ajouter Redis, queue, ou multi-agents sans besoin avéré.

## Style

- Répondre et commenter le code en français.
- Rester minimal : ne pas ajouter de dépendances ou d'abstractions non demandées.
