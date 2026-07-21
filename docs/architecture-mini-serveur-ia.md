# ia-orchestrator — Architecture & état du système (v3)

> Document de référence, tenu à jour pour servir de contexte Claude Code.
> Reflète l'état **réellement déployé**, pas seulement l'intention.
> Dernière mise à jour : 2026-07-21.

---

## 0. Objectif

Plateforme personnelle d'orchestration d'agents Claude Code, pilotée par Discord
et par un raccourci iOS. Tourne sur un Raspberry Pi 4 en autonomie (services
systemd). Plusieurs **pipelines indépendants** ; complexité ajoutée seulement
quand un besoin réel apparaît.

**Principe fondateur :** Discord/HTTP = bus d'événements. Le routeur ne contient
aucune logique métier ; toute la logique vit dans `pipelines/*.py`.

---

## 1. Environnement réel

| Élément | Valeur |
|---|---|
| Hôte | Raspberry Pi 4, 4GB RAM |
| OS | Raspberry Pi OS Lite 64-bit (Debian, kernel 6.18 aarch64) |
| User | `fgeronimi` |
| Hostname LAN | `ia-orchestrator.home` (SSH local) |
| IP Tailscale | `100.122.194.119` (accès distant, stable) |
| Compte Tailscale | `geronimi.francois@` |
| Node | v20 LTS via nvm (`~/.nvm/versions/node/`) |
| Python | venv à `~/ia-orchestrator/.venv` |
| Projet | `~/ia-orchestrator` |
| Repo | GitHub `fgeronimi/ia-orchestrator` (push HTTPS + PAT, credential.helper store) |
| Auth Claude | `CLAUDE_CODE_OAUTH_TOKEN` (abonnement, pas d'API key) |

Accès distant : Tailscale (installé sur le Pi ; **à installer sur les iPhones et
le Mac** pour bosser à distance). Raspberry Pi Connect dispo en secours (shell
navigateur via connect.raspberrypi.com), mais ne rend pas les services HTTP
joignables — Tailscale reste requis pour le raccourci iOS.

---

## 2. État des pipelines

### Pipeline Dev — `pipelines/dev_jira.py` — ✅ v0 fonctionnel
```
Discord #idees  ──@mention──▶  bot.py route vers dev_jira.handle
                               └─ Claude génère un brouillon de ticket structuré
                                  (titre, description, critères, verdict IA-ready)
```
- **État :** répond en direct dans Discord. Brouillon uniquement.
- **Pas encore branché :** création réelle du ticket via l'API Jira
  (`lib/jira.py` n'existe pas encore ; `JIRA_*` réservés dans `.env`).
- **Manque Jira côté user** — repris quand dispo.

### Pipeline Perso — `pipelines/perso_resto.py` + `server.py` — ✅ v0 fonctionnel
```
Screenshot ──▶ POST /upload (Tailscale, header X-Shortcut-Token)
            ──▶ server.py sauve l'image dans state/incoming/
            ──▶ perso_resto.handle_image : Claude (outil Read, vision) classe en JSON
                  ├─ resto       → append data/restos.md          ✅
                  ├─ reservation → résumé + notif (agenda Google = STUB)  ⚠️
                  └─ autre       → notif "non classé"
            ──▶ notify() → webhook Discord #miamiton
            ──▶ image temporaire supprimée
```
- **État :** testé de bout en bout via `curl` (ex. "Bofinger" ajouté à restos.md,
  notif reçue dans #miamiton).
- **STUB :** `lib/gcal.py` (Google Calendar) lève `NotImplementedError`. Une
  réservation détectée renvoie juste l'info par notif pour ajout manuel.
- **Pas encore fait :** le **raccourci iOS** (le flux marche, il manque juste le
  déclencheur "Partager vers" sur les 2 iPhones à la place du `curl`).

---

## 3. Structure réelle du repo

```
ia-orchestrator/
├── README.md
├── .gitignore                 # .env*, state/*.db, state/incoming/, gcal creds
├── .env / .env.example        # .env NON versionné (chmod 600)
├── bot.py                     # routeur Discord (mention → pipeline mappé par nom de canal)
├── server.py                  # endpoint Flask /upload + /health (port 5000)
├── pipelines/
│   ├── dev_jira.py            # ✅ idée → brouillon ticket
│   └── perso_resto.py         # ✅ image → restos.md / (stub) réservation
├── lib/
│   ├── claude.py              # wrapper subprocess `claude -p` (timeout, allowed_tools)
│   ├── notify.py              # notif : bot si dispo, sinon webhook, sinon print
│   └── gcal.py                # ⚠️ STUB Google Calendar
├── data/
│   └── restos.md              # liste restos (versionnée)
├── state/                     # runtime, gitignored (incoming/, futurs .db)
├── infra/
│   ├── setup.sh               # install idempotente (Pi ou VPS Debian)
│   └── systemd/
│       ├── orchestrator-bot.service      # bot.py
│       └── orchestrator-server.service   # server.py
└── docs/
    └── architecture-mini-serveur-ia.md   # ce fichier
```

> Note : `lib/jira.py` et `lib/github.py` sont **prévus mais pas encore créés** —
> ne pas supposer leur existence.

---

## 4. Conventions (à respecter pour toute extension)

- **Un pipeline = un fichier** dans `pipelines/`, exposant
  `async def handle(text, message) -> str` (déclencheur Discord) et/ou
  `async def handle_image(image_path) -> str` (déclencheur HTTP).
- **Enregistrer un pipeline** = ajouter une entrée `"<nom-canal>": module.handle`
  dans le dict `PIPELINES` de `bot.py`. Le nom de canal Discord doit matcher
  **exactement** (minuscules, sans accent — piège vécu : `idée` ≠ `idees`).
- **Tout appel à Claude** passe par `lib/claude.run_claude()`. Jamais de
  subprocess Claude ailleurs. Utiliser `allowed_tools` pour scoper les droits
  (ex. `["Read"]` pour la vision, `[]` pour du raisonnement pur).
- **Toute notif** passe par `lib/notify.notify()`. Ne pas poster en dur.
- **Secrets** : uniquement via `.env` (chargé par `python-dotenv`), jamais en
  clair dans le code, jamais commités. Nouveau secret → l'ajouter à `.env.example`
  (clé sans valeur) pour documenter.
- **Cloisonnement des droits = au niveau des tokens**, pas en multipliant les
  process. Un token par usage, scope minimal.

---

## 5. Exploitation (commandes réelles)

**Services (tournent en autonomie, restart auto, survivent au reboot) :**
```bash
sudo systemctl status orchestrator-bot        # pipeline dev (Discord)
sudo systemctl status orchestrator-server     # pipeline perso (HTTP)
journalctl -u orchestrator-bot -f             # logs live
journalctl -u orchestrator-server -f
sudo systemctl restart orchestrator-bot       # après modif de code/‑env
```
> ⚠️ Après un `git pull` de nouveau code, **redémarrer le service concerné**
> (les process ne rechargent pas à chaud).

**Déploiement d'une modif :**
```bash
cd ~/ia-orchestrator
git pull                          # ou édition directe / scp
sudo systemctl restart orchestrator-bot orchestrator-server
```

**Test manuel du serveur perso :**
```bash
curl http://localhost:5000/health
curl -X POST http://localhost:5000/upload \
  -H "X-Shortcut-Token: $IOS_SHORTCUT_TOKEN" \
  -F "image=@/chemin/screenshot.jpg"
```

**Piège systemd connu :** nvm n'existe pas dans le contexte systemd. Les
`.service` ont le chemin node en dur dans `Environment=PATH=...` — le mettre à
jour si la version de Node change (`ls ~/.nvm/versions/node/`).

---

## 6. Variables d'environnement (`.env`)

| Clé | Usage | État |
|---|---|---|
| `DISCORD_BOT_TOKEN` | bot.py (pipeline dev) | ✅ configuré |
| `NOTIFY_CHANNEL_ID` | canal notif du bot (#idees) | ✅ |
| `CLAUDE_CODE_OAUTH_TOKEN` | auth Claude Code | ✅ |
| `IOS_SHORTCUT_TOKEN` | auth endpoint /upload | ✅ |
| `DISCORD_WEBHOOK_URL` | notif hors-bot → #miamiton | ✅ |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | pipeline dev Jira | ⬜ à venir |

---

## 7. Reste à faire

**Court terme**
1. **Raccourci iOS** (2 iPhones) : "Partager vers" → POST `/upload` sur
   `http://100.122.194.119:5000/upload`, header `X-Shortcut-Token`. Remplace le `curl`.
2. Installer **Tailscale sur les iPhones** (même compte) pour joindre le Pi hors LAN.
3. **VS Code Remote-SSH** depuis le Mac (via Tailscale) pour du dev confortable.

**Moyen terme**
4. **Google Calendar** : implémenter `lib/gcal.py` (projet Google Cloud, OAuth
   "Application de bureau", creds dans `state/gcal_credentials.json`). Débloque
   la branche réservation du pipeline perso.
5. **Jira** : créer `lib/jira.py`, brancher `dev_jira.py` sur la vraie création
   de ticket quand l'accès est dispo.

**Si besoin avéré (pas avant)**
6. Découpage multi-agents du pipeline dev (Créateur / Vérificateur / Exécutant),
   voir Annexe A.
7. Notifs par canal distinct (aujourd'hui un seul `DISCORD_WEBHOOK_URL`).
8. systemd **timers** pour des tâches planifiées (cron-like).
9. Migration VPS (Hetzner ~4,50 €/mois) : rejouer `infra/setup.sh` + copier
   `.env` et l'état. Pertinent surtout pour de la dispo pendant absences
   (ex. voyage Japon).

**Vigilance matériel (Pi)**
- microSD 16GB : limite pour du 24/7 long terme (usure écriture SQLite/logs) —
  envisager boot SSD/USB. Refroidissement passif : surveiller le throttling.

---

## Annexe A — Architecture cible multi-agents (si besoin avéré)

```
Discord #idees
   → Agent "Créateur"     (token Jira write-only)   → crée le ticket
Webhook Jira / timer
   → Agent "Vérificateur" (tokens read-only)        → tague "ai-ready"
Trigger "ai-ready"
   → Agent "Exécutant"    (PAT GitHub scoped repo)  → branche + MR liée
```
Déclencheurs du découpage : besoin d'approbation humaine intercalée ; volume de
tickets justifiant un tri auto ; incident de sur-permission d'un agent unique.
