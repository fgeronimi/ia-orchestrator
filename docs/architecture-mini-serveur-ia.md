# ia-orchestrator — Architecture & état du système (v5)

> Document de référence, tenu à jour pour servir de contexte Claude Code.
> Reflète l'état **réellement déployé**, pas seulement l'intention.
> Dernière mise à jour : 2026-07-25.

---

## 0. Objectif

Plateforme personnelle d'orchestration d'agents Claude Code, tournant sur un
Raspberry Pi 4 en autonomie (services systemd). Pilotée par Discord, et — en
construction — par GitHub : tu crées des tickets, l'orchestrateur les
implémente, ouvre des PR, se relit, et gère la suite après ton merge.

**Principe fondateur :** Discord/HTTP = bus d'événements. Le routeur ne contient
aucune logique métier ; toute la logique vit dans `pipelines/*.py`.

> **Cap actuel :** pipeline dev GitHub — voir `docs/plan-orchestrateur-dev.md`.
> Le pipeline perso (restos, carte, endpoint iOS) a été retiré (pivot du projet ;
> récupérable dans l'historique git avant le 2026-07-25).

---

## 1. Environnement réel

| Élément | Valeur |
|---|---|
| Hôte | Raspberry Pi 4, 4GB RAM |
| OS | Raspberry Pi OS Lite 64-bit (Debian, kernel 6.18 aarch64) |
| User | `fgeronimi` |
| Hostname LAN | `ia-orchestrator.home` (SSH local) |
| IP Tailscale | `<ip-tailscale>` (accès distant — ⚠️ non installé sur le Mac, bloqué par la politique du poste) |
| Compte Tailscale | `<compte>` |
| Node | v20 LTS via nvm (`~/.nvm/versions/node/`) |
| Python | venv à `~/ia-orchestrator/.venv` |
| Projet | `~/ia-orchestrator` |
| Repo | GitHub `fgeronimi/ia-orchestrator` (push HTTPS + PAT, credential.helper store) |
| Auth Claude | `CLAUDE_CODE_OAUTH_TOKEN` (abonnement, pas d'API key) |

Accès distant : Tailscale est installé **sur le Pi**. Sur le Mac il est bloqué
par le politique du poste (`politique du poste`) — le
dev distant depuis le Mac passe donc par le LAN (`ia-orchestrator.home`).
Raspberry Pi Connect dispo en secours (shell navigateur).

---

## 2. État des pipelines

### Pipeline Dev (idée → brouillon) — `pipelines/dev_jira.py` — ✅ v0
```
Discord #idees  ──@mention──▶  bot.py route vers dev_jira.handle
                               └─ Claude génère un brouillon de ticket structuré
                                  (titre, description, critères, verdict IA-ready)
```
- **État :** répond en direct dans Discord. Brouillon uniquement, rien n'est créé.
- **À repositionner :** avec le pivot GitHub, ce pipeline devient soit « idée
  Discord → issue GitHub créée » (rôle Créateur), soit retiré. À trancher au
  démarrage du plan dev.

### Pipeline Dev GitHub — 🚧 à construire
Cœur du projet désormais. Tu crées des issues GitHub et les tagges ; le Pi les
implémente et ouvre des PR. Spécifié en détail dans
**`docs/plan-orchestrateur-dev.md`** (décisions, briques, phases). Rien n'est
encore codé.

---

## 3. Structure réelle du repo

```
ia-orchestrator/
├── README.md
├── .gitignore                 # .env*, state/
├── .env / .env.example        # .env NON versionné (chmod 600)
├── bot.py                     # routeur Discord (mention → pipeline mappé par nom de canal)
├── server.py                  # endpoint Flask /health (port 5000)
├── pipelines/
│   └── dev_jira.py            # ✅ idée Discord → brouillon de ticket
├── lib/
│   ├── claude.py              # wrapper subprocess `claude -p` (timeout, allowed_tools)
│   └── notify.py              # notif : bot si dispo, sinon webhook, sinon print
├── state/                     # runtime, gitignored (futurs .db, workspaces)
├── Makefile                   # exploitation : sync, deploy, env-push/pull, logs
├── infra/
│   ├── setup.sh               # install idempotente (Pi ou VPS Debian)
│   ├── sync.sh                # auto-update git + restart (appelé par le timer)
│   └── systemd/
│       ├── orchestrator-bot.service      # bot.py
│       ├── orchestrator-server.service   # server.py
│       ├── orchestrator-sync.service     # sync.sh (oneshot)
│       └── orchestrator-sync.timer       # toutes les 10 min
└── docs/
    ├── architecture-mini-serveur-ia.md   # ce fichier
    └── plan-orchestrateur-dev.md         # plan du pipeline dev GitHub
```

> À créer pour le pipeline dev (voir le plan) : `lib/github.py`,
> `pipelines/dev_executor.py`, `infra/poll.sh`, `data/repos.yaml`.

---

## 4. Conventions (à respecter pour toute extension)

- **Un pipeline = un fichier** dans `pipelines/`, exposant un point d'entrée
  `async def handle(...) -> str`. Pour un pipeline Discord, l'enregistrer dans
  `PIPELINES` de `bot.py` avec le **nom de canal exact** (minuscules, sans
  accent — piège vécu : `idée` ≠ `idees`).
- **Tout appel à Claude** passe par `lib/claude.run_claude()`. Jamais de
  subprocess Claude ailleurs. `allowed_tools` scope les droits (`[]` pour du
  raisonnement pur, `["Read","Edit","Write","Bash"]` pour un agent qui code).
- **Toute notif** passe par `lib/notify.notify()`. Ne pas poster en dur.
- **Secrets** : uniquement via `.env` (chargé par `python-dotenv`), jamais en
  clair, jamais commités. Nouveau secret → l'ajouter à `.env.example`.
- **Cloisonnement des droits = au niveau des tokens**, pas en multipliant les
  process. Un token par usage, scope minimal.

---

## 5. Exploitation (commandes réelles)

**Tout passe par le `Makefile`** — `make` seul liste les cibles, séparées en
« sur le Pi » et « depuis le Mac ».

| Depuis le Mac | Effet |
|---|---|
| `make deploy` | push du code + `make sync` sur le Pi (restart inclus si `.py` modifié) |
| `make remote-logs` / `remote-status` | logs et état du Pi via SSH |
| `make env-pull` / `env-push` | récupère/envoie le `.env` (sauvegarde horodatée avant écrasement, restart après push) |
| `make env-diff` | compare les **clés** des deux `.env` — jamais les valeurs |

**Prérequis SSH (une fois) :** les cibles distantes passent par
`fgeronimi@ia-orchestrator.home`. Le user est explicite dans le Makefile
(`PI_USER`) — sans lui, ssh tente le login du Mac (`francois.geronimi`) et le Pi
répond `Permission denied (publickey)`. Déposer la clé : `ssh-copy-id
fgeronimi@ia-orchestrator.home`. Bloc `Host ia-orchestrator*` dans
`~/.ssh/config` (Mac) pour le trousseau. Hors LAN, Tailscale serait requis mais
il est bloqué sur le Mac (voir §1).

| Sur le Pi | Effet |
|---|---|
| `make sync` | pull, rebase, push, restart si code changé |
| `make pull` / `push` / `restart` / `status` / `logs` / `test` | opérations unitaires |
| `make install-timer` | active l'auto-update toutes les 10 min |

### Auto-update git (`infra/sync.sh` + `orchestrator-sync.timer`)

Le Pi se met à jour tout seul : toutes les 10 min, le timer lance `infra/sync.sh`
→ `git pull --rebase` sur `main`, et **restart des services si un `.py` a
changé**. Un push sur `main` depuis le Mac est donc pris en compte dans les 10
minutes, sans exposer le Pi (polling, pas de webhook). Silencieux quand il n'y a
rien à faire.

- **En cas de conflit** (édition locale sur le Pi vs remote), le script
  `rebase --abort` — jamais de résolution automatique — et notifie Discord
  **une seule fois** via le marqueur `state/sync-conflit`, puis repart seul
  (« ✅ de nouveau opérationnel ») une fois résolu à la main.
- Le restart auto demande `sudo -n systemctl restart` sans mot de passe. Sinon
  le script notifie que `make restart` est requis. Drop-in :
  ```
  # /etc/sudoers.d/orchestrator  (via visudo -f)
  fgeronimi ALL=(root) NOPASSWD: /bin/systemctl restart orchestrator-bot orchestrator-server
  ```

**Services (autonomes, restart auto, survivent au reboot) :**
```bash
sudo systemctl status orchestrator-bot        # Discord
sudo systemctl status orchestrator-server     # HTTP /health
journalctl -u orchestrator-bot -f             # logs live
sudo systemctl restart orchestrator-bot       # après modif de code/-env
```
> ⚠️ Les process ne rechargent pas à chaud : après du nouveau code, restart du
> service concerné (l'auto-update s'en charge pour un push sur `main`).

**Piège systemd connu :** nvm n'existe pas dans le contexte systemd. Les
`.service` ont le chemin node en dur dans `Environment=PATH=...` — le mettre à
jour si la version de Node change (`ls ~/.nvm/versions/node/`).

---

## 6. Variables d'environnement (`.env`)

| Clé | Usage | État |
|---|---|---|
| `DISCORD_BOT_TOKEN` | bot.py | ✅ configuré |
| `NOTIFY_CHANNEL_ID` | canal notif du bot (#idees) | ✅ |
| `DISCORD_WEBHOOK_URL` | notif hors-bot (timers systemd) | ✅ |
| `CLAUDE_CODE_OAUTH_TOKEN` | auth Claude Code | ✅ |
| `GITHUB_TOKEN` | pipeline dev GitHub (PAT scopé) | ⬜ à venir (phase 0 du plan) |

---

## 7. Reste à faire

**Cap principal**
- **Pipeline dev GitHub** — dérouler `docs/plan-orchestrateur-dev.md`, en
  commençant par la Phase 0 (`lib/github.py` lecture seule + poller qui notifie).

**Court terme**
- **VS Code Remote-SSH** depuis le Mac (via LAN) pour du dev confortable.
- Déployer l'auto-update sur le Pi (`make install-timer` + drop-in sudoers).

**Si besoin avéré (pas avant)**
- Notifs par canal distinct (aujourd'hui un seul `DISCORD_WEBHOOK_URL`).
- Migration VPS (Hetzner ~4,50 €/mois) : rejouer `infra/setup.sh` + copier
  `.env` et l'état.

**Vigilance matériel (Pi)**
- microSD 16GB : limite pour du 24/7 long terme (usure écriture SQLite/logs) —
  envisager boot SSD/USB. Refroidissement passif : surveiller le throttling.
