# ia-orchestrator — Architecture & état du système (v6)

> Document de référence, tenu à jour pour servir de contexte Claude Code.
> Reflète l'état **réellement déployé**, pas seulement l'intention.
> Dernière mise à jour : 2026-07-25 (Phases 0 à 3 du pipeline dev GitHub validées live).

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
| Disque | carte SD 15 Go (~14 Go utilisables hors réserve root) — surveillé, alerte à 80% |
| User | `fgeronimi` |
| Hostname LAN | `ia-orchestrator.home` (SSH local) |
| Accès distant | Tailscale installé sur le Pi (`tailscale ip` pour l'adresse) |
| Node | v22 LTS via nvm (`~/.nvm/versions/node/`) |
| Python | venv à `~/ia-orchestrator/.venv` |
| Projet | `~/ia-orchestrator` |
| Repo | GitHub `fgeronimi/ia-orchestrator` (push HTTPS + PAT, credential.helper store) |
| Auth Claude | `CLAUDE_CODE_OAUTH_TOKEN` (abonnement, pas d'API key) |

Accès distant : Tailscale est installé **sur le Pi** ; le dev depuis le Mac
passe par le LAN (`ia-orchestrator.home`). Raspberry Pi Connect dispo en
secours (shell navigateur).

---

## 2. État des pipelines

### Pipeline Dev GitHub — 🚧 en construction — **cœur du projet**
**Point d'entrée = les issues GitHub.** Tu écris tes tickets directement dans
GitHub et tu les tagges `ai-ready` ; le Pi les implémente, ouvre des PR, se
relit, et gère la suite après ton merge. Spécifié en détail dans
**`docs/plan-orchestrateur-dev.md`** (décisions, briques, machine à états,
phases 0→3, implémentation actuelle).

- **Phase 0 ✅ faite & déployée** : le poller. Timer `orchestrator-poll` (5 min)
  → `poll.py` lit les issues `ai-ready` du repo surveillé (`WATCHED_REPO`) et
  notifie les **nouvelles** dans `#orchestrateur`. Dédup via SQLite
  (`lib/state`, `state/orchestrator.db`). Lecture GitHub via `lib/github`.
- **Phase 1 ✅ faite & validée live** : l'exécutant. Le poller traite la
  première issue `ai-ready` en inline (un ticket par tour, verrou `flock` sur
  `state/executor.lock`) via `pipelines/dev_executor` : label `ai-working` →
  workspace cloné (`lib/workspace`) → branche `ai/<n>` → Claude implémente
  (`run_claude`, Read/Edit/Write/Bash) → commit/push → PR draft → auto-review
  (Claude relit son diff, Read seul) en commentaire de PR → notif par étape.
  Validé live : issue #3 → PR #4 + auto-review.
- **Phase 2 ✅ faite & validée live** : la suite après merge.
  `pipelines/dev_followup.py` nettoie les PR d'agent mergées (branche `ai/*`
  supprimée, label retiré, notif ; dédup SQLite). Boucle de révision : les
  nouveaux commentaires humains sur une PR d'agent (conversation + diff,
  ceux de l'orchestrateur écartés par leur préfixe 🤖) déclenchent
  `dev_executor.reviser()` — Claude corrige sur la branche, repush, répond
  sur la PR. Révision prioritaire sur nouveau ticket, une action lourde par
  tour. Validé live : PR #4 nettoyée, PR #6 révisée sur commentaire.
- **Phase 3 ✅ faite & validée live** : multi-repos via `data/repos.yaml`
  (poll balaye tous les repos, une action lourde par tour tous repos
  confondus) ; auto-review à checklist ; CI GitHub Actions
  (`.github/workflows/ci.yml` : `make test` sur PR et main) avec
  surveillance des PR d'agent (`dev_followup.surveiller_ci`, notif ✅/❌ une
  fois par sha, repush → nouveau suivi). Validé sur la PR #8.

> **Repos surveillés** (`data/repos.yaml`) : `ia-orchestrator` (ce repo),
> `havre-data` et `havre-app` (projet havre — assistant hyperacousie,
> timeouts dédiés). Chaque repo surveillé documente dans son CLAUDE.md
> (§0.1) les règles que les agents doivent y respecter ; le mode d'emploi
> côté humain est dans leurs README (« Workflow de développement »).
>
> `pipelines/dev_jira.py` (idée Discord → brouillon de ticket, antérieur au
> pivot) a été retiré le 2026-07-25 — récupérable dans l'historique git.
> Côté Discord, le bot **notifie** (`lib/notify`) et répond aux requêtes de
> suivi dans `#orchestrateur` : `@bot conso` / `@bot statut`
> (`pipelines/dev_statut.py`, lecture seule).

### Forge — conformité déclarative des repos surveillés — ✅ v1 faite
Les repos surveillés doivent tous respecter les mêmes conditions (labels
`ai-*`, fichiers requis, protection de `main`) ; la forge les rend
déclaratives et versionnées dans `data/forge.yaml`. Timer `orchestrator-forge`
(1x/jour) → `forge.py` → `pipelines/forge.py` vérifie chaque repo de
`data/repos.yaml` **par API pure** (aucun appel Claude : conditions
objectives) et ouvre une issue `forge: <condition>` **sans label** par écart
(dédup SQLite par repo/condition/version, `lib/state`). Hors périmètre v1 :
correction automatique des écarts (l'humain décide) et conditions floues
nécessitant un agent LLM (v2 éventuelle).

### Surveillance machine — ✅ v1 faite
Le Pi tourne sur une carte SD de 15 Go que les workspaces de l'exécutant
remplissent au fil des tickets : il faut le savoir avant la saturation. Timer
`orchestrator-sante` (15 min) → `sante.py` → `pipelines/sante.py` mesure le
disque (`shutil.disk_usage`, pourcentage calculé comme `df` : `utilise /
(utilise + libre)`, la réserve root étant exclue), la RAM (`/proc/meminfo`,
`MemAvailable` et non `MemFree`), la charge, la température et l'état des
services/timers — **aucun appel Claude, aucun réseau** hors notification.

Alerte Discord dès le seuil `SEUIL_DISQUE` (défaut 80%), puis à chaque palier
franchi (90%, 95%). Anti-spam par palier mémorisé dans la table `meta` de
SQLite : tant que le disque reste dans le même palier, silence ; en repassant
sous le seuil, une notif de retour à la normale part et la mémoire est effacée.
Sans ça, un disque à 81% alerterait toutes les 15 minutes indéfiniment.

Les mêmes mesures alimentent `@bot santé` dans Discord (via `dev_statut`).
Hors périmètre v1 : purge automatique des workspaces (l'humain décide), et
surveillance d'autres axes que le disque en alerte (RAM/température sont
affichées mais n'alertent pas — le Pi swappe sans mourir, et 66 °C est normal).

---

## 3. Structure réelle du repo

```
ia-orchestrator/
├── README.md
├── .gitignore                 # .env*, state/
├── .env / .env.example        # .env NON versionné (chmod 600)
├── bot.py                     # routeur Discord (notifs + @bot conso/statut/santé dans #orchestrateur)
├── server.py                  # endpoint Flask /health (port 5000)
├── poll.py                    # ✅ poller multi-repos : notifs + followup + CI + 1 action lourde/tour
├── forge.py                   # ✅ 1 passage/jour : conformité déclarative des repos surveillés
├── sante.py                   # ✅ 1 tour/15 min : surveillance machine (alerte disque par palier)
├── data/
│   ├── repos.yaml             # ✅ repos surveillés (fallback WATCHED_REPO)
│   └── forge.yaml             # ✅ conditions de conformité (labels, fichiers, protection main)
├── pipelines/
│   ├── dev_executor.py        # ✅ l'exécutant : issue → code → PR + auto-review + révision + fix CI
│   ├── dev_followup.py        # ✅ suivi : nettoyage post-merge, CI (notif + détection rouge)
│   ├── dev_statut.py          # ✅ @bot conso / statut / santé depuis Discord (lecture seule)
│   ├── forge.py               # ✅ vérifie data/forge.yaml sur chaque repo, ticket par écart
│   └── sante.py               # ✅ mesures machine (disque, RAM, charge, temp) + alerte disque par palier
├── lib/
│   ├── claude.py              # wrapper subprocess `claude -p` (timeout, allowed_tools)
│   ├── notify.py              # notif : bot si dispo, sinon webhook, sinon print
│   ├── github.py              # ✅ wrapper API GitHub (lecture + écriture : PR, labels, rulesets, commentaires)
│   ├── workspace.py           # ✅ clones locaux où l'exécutant code (branche, commit, push)
│   └── state.py               # ✅ idempotence SQLite (issues déjà notifiées, écarts de forge signalés)
├── state/                     # runtime, gitignored (orchestrator.db, workspaces/, executor.lock, forge.lock)
├── Makefile                   # exploitation : sync, deploy, env-push/pull, logs, install-timer
├── infra/
│   ├── setup.sh               # install idempotente (Pi ou VPS Debian)
│   ├── sync.sh                # auto-update git + restart (appelé par le timer sync)
│   ├── poll.sh                # ✅ un tour du poller (appelé par le timer poll)
│   ├── forge.sh                # ✅ un passage de la forge (appelé par le timer forge)
│   ├── sante.sh               # ✅ un tour de surveillance machine (appelé par le timer santé)
│   └── systemd/
│       ├── orchestrator-bot.service      # bot.py
│       ├── orchestrator-server.service   # server.py
│       ├── orchestrator-sync.{service,timer}   # auto-update git, toutes les 10 min
│       ├── orchestrator-poll.{service,timer}   # poller GitHub, toutes les 5 min
│       ├── orchestrator-forge.{service,timer}  # forge (conformité), 1x/jour
│       ├── orchestrator-sante.{service,timer}  # surveillance machine, toutes les 15 min
│       └── orchestrator-fail-notify@.service   # OnFailure → notif Discord 🚨
└── docs/
    ├── architecture-mini-serveur-ia.md   # ce fichier
    └── plan-orchestrateur-dev.md         # plan + implémentation du pipeline dev GitHub
```

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
| `make remote-poll` / `remote-conso` | déclenche un tour de poll / affiche la conso Claude par ticket |
| `make remote-sante` | santé du Pi (disque, RAM, charge, température, services) |

**Prérequis SSH (une fois) :** les cibles distantes passent par
`fgeronimi@ia-orchestrator.home`. Le user est explicite dans le Makefile
(`PI_USER`) — sans lui, ssh tente le login du Mac (`francois.geronimi`) et le Pi
répond `Permission denied (publickey)`. Déposer la clé : `ssh-copy-id
fgeronimi@ia-orchestrator.home`. Bloc `Host ia-orchestrator*` dans
`~/.ssh/config` (Mac) pour le trousseau. Hors LAN : passer par l'IP Tailscale
du Pi (`make deploy PI_HOST=<ip-tailscale>`).

| Sur le Pi | Effet |
|---|---|
| `make sync` | pull, rebase, push, restart si code changé |
| `make pull` / `push` / `restart` / `status` / `logs` / `test` | opérations unitaires |
| `make install-timer` | installe les timers systemd (auto-update 10 min + poller GitHub 5 min + forge 1x/j + santé 15 min) |
| `make poll` / `conso` / `forge` / `sante` | tour de poll / conso Claude par ticket / vérification de conformité / surveillance machine, à la main |

### Poller GitHub (`orchestrator-poll.timer` → `poll.py`)
Toutes les 5 min, `poll.py`, pour **chaque repo** de `data/repos.yaml`
(secours : `WATCHED_REPO`) : notifie les **nouvelles** issues `ai-ready`
(dédup SQLite, `state/orchestrator.db`), nettoie les PR d'agent mergées et
suit leur CI (`dev_followup`), puis lance **une action lourde** tous repos
confondus sous verrou (`state/executor.lock`) — révision d'une PR commentée
(prioritaire) ou exécution de la première issue `ai-ready`. Un run peut donc
durer plusieurs minutes (Claude implémente + auto-review). Détails et
limites : `docs/plan-orchestrateur-dev.md` §4 et §7.

**Isolation des hoquets réseau** — chaque appel GitHub du tour est protégé
repo par repo, dans le tour léger (`_tour_leger`) **comme dans la chaîne de
priorité** (`_premier`) : un `ReadTimeout` de l'API ne fait échouer ni le
service ni les repos suivants, il saute le repo et le tour d'après rattrape.
Le repo sauté est signalé par une notif ⚠️ (une info, pas une alerte). Les
deux moitiés ont été corrigées séparément : le tour léger le 2026-07-27 (rafale
de merges), la chaîne de priorité le 2026-07-30 après un `ReadTimeout` dans
`chercher_revision` à 02:03 qui avait tué le service et déclenché un 🚨.
```bash
journalctl -u orchestrator-poll -f                         # logs du poller
.venv/bin/python poll.py fgeronimi/ia-orchestrator         # un tour à la main
sqlite3 state/orchestrator.db "SELECT * FROM issues_notifiees;"  # état dédup
```

### Forge — conformité déclarative (`orchestrator-forge.timer` → `forge.py`)
Une fois par jour, `forge.py` (verrou `state/forge.lock`, même principe que
l'action lourde de `poll.py`) charge les conditions de `data/forge.yaml`
(version, labels requis, fichiers requis sur la branche par défaut,
protection de `main` par ruleset actif exigeant une pull request) et les
vérifie sur chaque repo de `data/repos.yaml`, **par API pure** (aucun appel
Claude — ce sont des conditions objectives). Chaque écart ouvre une issue
`forge: <condition>` **sans label** sur le repo concerné (poser `ai-ready`
reste un geste humain), dédupliquée par (repo, condition, version) dans
`state/orchestrator.db` (table `forge_signale`) — incrémenter `version` dans
`data/forge.yaml` relance le signalement des écarts déjà connus.
```bash
journalctl -u orchestrator-forge -f                        # logs de la forge
.venv/bin/python forge.py                                  # un passage à la main
sqlite3 state/orchestrator.db "SELECT * FROM forge_signale;"  # état dédup
```

### Surveillance machine (`orchestrator-sante.timer` → `sante.py`)
Toutes les 15 min, `sante.py` mesure le disque, la RAM, la charge, la
température et l'état des services/timers — mesures purement locales (`/proc`,
`/sys`, `df`, `systemctl`), aucun appel Claude ni GitHub. Alerte Discord 🔴 dès
`SEUIL_DISQUE` (défaut 80%), puis aux paliers 90% et 95%, **une seule fois par
palier** (dernier palier mémorisé dans la table `meta`) ; notif ✅ de retour à
la normale en repassant sous le seuil, qui réarme l'alerte. Le pourcentage est
calculé comme `df` (`utilise / (utilise + libre)`) et non `utilise / total` :
la réserve root d'ext4 (~5%) n'est pas utilisable, la compter fausserait le
seuil de ~3 points.
```bash
journalctl -u orchestrator-sante -f                        # logs de la surveillance
make sante                                                 # un tour à la main
SEUIL_DISQUE=60 .venv/bin/python sante.py                  # tester l'alerte (⚠️ notifie pour de vrai)
sqlite3 state/orchestrator.db "SELECT * FROM meta WHERE cle='sante_disque_palier';"
```

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

**Échec d'un tour de poll :** `orchestrator-poll.service` a
`OnFailure=orchestrator-fail-notify@%n.service` — un crash non géré envoie
🚨 sur Discord (les cas prévus — quota, échec d'un ticket — sont notifiés
plus finement par les pipelines et ne font pas échouer l'unité).

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
jour si la version de Node change (`ls ~/.nvm/versions/node/`). Et après toute
modif d'un `.service`/`.timer`, refaire `make install-timer` : l'unité active
est la **copie** dans `/etc/systemd/system`, pas le fichier du repo (piège
vécu : PATH node ajouté au repo mais unité installée jamais rafraîchie →
`FileNotFoundError: claude`).

---

## 6. Variables d'environnement (`.env`)

| Clé | Usage | État |
|---|---|---|
| `DISCORD_BOT_TOKEN` | bot.py | ✅ configuré |
| `NOTIFY_CHANNEL_ID` | canal notif du bot (#idees) | ✅ |
| `DISCORD_WEBHOOK_URL` | notif hors-bot (timers, poller) → `#orchestrateur` | ✅ |
| `CLAUDE_CODE_OAUTH_TOKEN` | auth Claude Code | ✅ |
| `GITHUB_TOKEN` | poller + forge GitHub (PAT : Issues+Metadata en lecture, Contents/Pull requests/Issues en écriture, Administration en lecture pour les rulesets) | ✅ |
| `WATCHED_REPO` | repo surveillé `owner/nom` — secours si `data/repos.yaml` absent | ✅ |

---

## 7. Reste à faire

**Cap principal**
- **Pipeline dev GitHub** — dérouler `docs/plan-orchestrateur-dev.md` :
  Phases 0 et 1 faites, prochaine étape = Phase 2 (suite après merge :
  déploiement/nettoyage + boucle de révision).

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
