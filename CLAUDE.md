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

## Ne pas supposer

- `lib/github.py` existe mais est en **lecture seule** (Phase 0). L'écriture
  (branches, PR, commentaires) est à créer en Phase 1.
- Label déclencheur = **`ai-ready`** (anglais), pas `ia-ready`. GitHub matche à
  la lettre (piège vécu, cf `idées`≠`idees`).
- Ne pas ajouter Redis, queue, ou multi-agents sans besoin avéré (voir la
  section "Reste à faire" du doc d'archi).

## Style

- Répondre et commenter le code en français.
- Rester minimal : ne pas ajouter de dépendances ou d'abstractions non demandées.
