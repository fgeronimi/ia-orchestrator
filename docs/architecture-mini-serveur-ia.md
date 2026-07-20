# Mini-serveur IA — Architecture d'orchestration (v2, révisée)

## 0. Contexte & objectif

Plateforme personnelle d'orchestration d'agents IA (Claude Code) capable de :
- Exécuter des tâches asynchrones / planifiées
- Gérer plusieurs **pipelines indépendants** (dev, perso, futurs)
- Cloisonner les secrets par usage
- Démarrer simple et complexifier **uniquement quand un besoin réel apparaît**

**Philosophie v2 :** un pipeline = un agent au départ. Le découpage multi-agents
(Créateur / Vérificateur / Exécutant) est une *cible* documentée en annexe,
pas le point de départ.

---

## 1. Décisions d'architecture

| Sujet | Décision | Pourquoi |
|---|---|---|
| Hébergement | Pi 4 **ou** VPS (voir §2) — archi identique dans les deux cas | Migration triviale si besoin |
| Bot Discord | **Fait maison, minimal** (~200-300 lignes, discord.py ou discord.js) | Il manipule les tokens OAuth Claude + Jira + GitHub : code 100% maîtrisé et auditable > framework communautaire non audité |
| Agent engine | Claude Code CLI, auth abonnement Pro/Max (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`) | Pas de coût API |
| State | SQLite | Suffisant, zéro infra |
| Queue | Aucune au départ (appels séquentiels) | Redis seulement si concurrence réelle un jour |
| Scheduling | systemd timers | Plus robuste que cron brut, logs intégrés |
| Accès distant | Tailscale | Aucun port exposé sur internet |
| Secrets | Un `.env` par pipeline, permissions strictes, jamais commité | Scope minimal par usage |
| Notifications | **Discord uniquement pour le moment** (DM ou canal via le bot, push iOS par l'app Discord) | Zéro brique en plus ; ntfy envisagé plus tard pour le pipeline perso (copine sans Discord) |

## 2. Pipeline Dev (v0 : un seul agent)

```
Discord #idees  ──(mention ou commande)──▶  Agent unique
                                             ├─ crée le ticket Jira
                                             └─ (plus tard) prend un ticket
                                                taggé "ai-ready" → branche → MR
```

- La "vérification IA-ready" du design initial devient une **checklist dans le
  prompt** de l'agent, pas un agent séparé.
- Le cloisonnement des droits se fait **au niveau des tokens** (un token Jira
  write-only tickets, un PAT GitHub scoped au repo), pas en multipliant les process.
- Évolution vers le découpage 3 agents : voir Annexe A, à activer seulement si
  le mono-agent montre ses limites (conflits, dérives, besoin d'approbation humaine).

## 3. Pipeline Perso (resto / réservations)

**Interface : raccourci iOS "Partager vers", pas Discord.**
Vous avez tous les deux des iPhones ; partager un screenshot doit prendre deux taps
depuis n'importe quelle app, sans adopter une nouvelle messagerie.

```
Screenshot iPhone
  → Raccourci iOS "Partager" (toi + copine)
  → POST HTTP vers le serveur via Tailscale (aucun port public)
  → Agent Classifieur (vision)
       ├─ resto      → append dans restos.md (repo git perso, consultable)
       └─ réservation → event dans l'agenda Google partagé
```

- Le raccourci iOS embarque un token simple (header) pour identifier qui envoie.
- Tailscale doit être installé sur les deux iPhones (app gratuite, une fois).
- Fallback si Tailscale gêne : mini endpoint exposé via Cloudflare Tunnel.

## 4. Structure de repo (plate, évolutive)

```
ia-orchestrator/
├── README.md
├── .gitignore                # .env*, *.db, __pycache__...
├── .env.example              # variables attendues, sans valeurs
├── bot.py                    # routeur Discord minimal (fait maison)
├── server.py                 # endpoint HTTP pour le raccourci iOS
├── pipelines/
│   ├── dev_jira.py           # agent unique pipeline dev
│   └── perso_resto.py        # agent classifieur
├── lib/
│   ├── claude.py             # wrapper subprocess Claude Code
│   ├── notify.py             # notifications (Discord pour l'instant, backend interchangeable)
│   ├── jira.py               # appels API Jira
│   ├── github.py             # appels API GitHub
│   └── gcal.py               # appels API Google Calendar
├── state/                    # SQLite, gitignored
├── infra/
│   ├── systemd/              # .service / .timer
│   └── setup.sh              # install serveur (Pi ou VPS, même script)
└── docs/
    └── architecture-mini-serveur-ia.md
```

Pas de dossiers vides "pour plus tard" : la structure émerge avec le besoin.

## 5. Roadmap

1. Serveur opérationnel (Pi ou VPS) : OS, Node, Claude Code, auth headless, Tailscale
2. `bot.py` minimal : un canal, une commande, une réponse de Claude Code
3. Pipeline dev v0 : Discord → ticket Jira
4. Pipeline perso v0 : raccourci iOS → restos.md
5. Extensions : MR automatiques, agenda partagé, puis seulement si besoin :
   découpage multi-agents (Annexe A), Redis, monitoring

## 6. Points de vigilance

- **Si Pi** : SD 16GB limite (prévoir boot SSD/USB pour du 24/7), refroidissement
  passif (throttling possible), usure microSD en écriture (SQLite + logs)
- **Secrets** : jamais en clair dans le code, un token par usage, scope minimal
- **Bot maison** : rester minimal ; chaque feature ajoutée au bot est du code à
  maintenir — la logique va dans `pipelines/`, pas dans `bot.py`

---

## Annexe A — Architecture cible multi-agents (si besoin avéré)

```
Discord #idees
   → Agent "Créateur"     (token Jira write-only)     → crée le ticket
Webhook Jira / timer
   → Agent "Vérificateur" (tokens read-only)          → tague "ai-ready"
Trigger "ai-ready"
   → Agent "Exécutant"    (PAT GitHub scoped repo)    → branche + MR liée
```

Critères de déclenchement du découpage :
- Besoin d'un point d'approbation humaine entre création et exécution
- Volume de tickets rendant utile un tri automatisé en amont
- Incident où un agent unique a eu trop de droits au mauvais moment
