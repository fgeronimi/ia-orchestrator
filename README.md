# ia-orchestrator

[![CI](https://github.com/fgeronimi/ia-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/fgeronimi/ia-orchestrator/actions/workflows/ci.yml)

**Un agent de dev autonome sur Raspberry Pi, piloté par GitHub.** Tu écris un
ticket, tu poses le label `ai-ready` — le Pi le développe, ouvre une PR draft,
relit son propre code, répare sa CI, applique tes commentaires de review, et
nettoie tout après ton merge. Toi, tu ne fais que deux choses : écrire des
tickets et merger des PR.

```
issue + ai-ready ──▶ code ──▶ PR draft + auto-review ──▶ [TOI: review/merge] ──▶ cleanup
                                   ▲        │
                                   └────────┘  révisions & réparations CI, en boucle
```

## Comment ça marche

Un timer systemd fait tourner un poller toutes les 5 minutes. À chaque tour,
pour chaque repo surveillé : notification des nouveaux tickets, nettoyage des
PR mergées, suivi de la CI — puis **une seule action lourde** (un appel à
Claude Code), par priorité :

1. **Réviser** — tu as commenté une PR d'agent ? Claude applique tes retours et repushe.
2. **Réparer** — la CI d'une PR d'agent est rouge ? Claude lit le log du job et corrige (2 tentatives max, puis il te passe la main).
3. **Développer** — un ticket `ai-ready` attend ? Claude clone, code, teste, ouvre la PR draft et poste une auto-review sans complaisance sur son propre diff.

L'état vit dans les **labels GitHub** de l'issue :

```
[toi] issue + label  ai-ready
        │  (poll détecte)
        ▼
  ai-working ── l'agent code, teste, ouvre la PR draft + auto-review → notif
        │         ├──▶ tu commentes → il révise et repushe    (en boucle)
        │         └──▶ CI rouge     → il répare et repushe    (2 essais max)
        ▼
 [TOI] merge de la PR
        │  (poll détecte)
        ▼
  cleanup ── branche ai/* supprimée, labels retirés, issue close → notif ✅
```

- `ai-failed` : échec dur, l'agent te passe la main (remets `ai-ready` pour relancer).
- Quota Claude épuisé ≠ échec : le ticket repasse en file tout seul et le
  poller attend l'heure de reprise sans spammer.

## Fonctionnalités

- **Exécution de tickets** : workspace cloné, branche `ai/<n>`, auto-détection
  des tests, commit + push, PR draft (réutilisée si déjà ouverte), commentaire
  sur l'issue.
- **Auto-review** : Claude relit son propre diff (outil `Read` seul) avec la
  grille senior de `.claude/skills/bakaa-brutal-reviewer` et la poste en
  commentaire de PR.
- **Boucle de révision** : tes commentaires (conversation ou diff) déclenchent
  une révision ; ceux de l'orchestrateur (préfixe 🤖) sont ignorés.
- **CI intégrée** : GitHub Actions (`make test`) sur chaque PR et sur `main` ;
  résultat notifié par sha ; CI rouge réparée automatiquement.
- **Multi-repos** : liste dans `data/repos.yaml`, un PAT scopé sur chacun.
- **Forge** : conditions déclaratives de conformité (`data/forge.yaml` — labels,
  fichiers requis, protection de `main`), vérifiées 1x/jour sur chaque repo
  surveillé ; chaque écart ouvre un ticket `forge:` **sans label** (poser
  `ai-ready` reste un geste humain).
- **Conso tracée par ticket** : tokens et coût estimé de chaque appel Claude,
  par étape (`implementation`, `auto-review`, `revision`, `ci-fix`).
- **Notifications Discord** à chaque étape (🎫 🔨 🔍 ✏️ 🔧 ✅ 🧹 ⚠️ ⏳ 🚨), et un
  bot interrogeable : `@bot conso`, `@bot statut`.
- **Robustesse** : quota d'abonnement géré (remise en file + reprise), échecs
  durs visibles (`ai-failed`), crash du poller notifié (`OnFailure=`),
  verrou anti-concurrence libéré même après un kill.

## Architecture

```
poll.py                    # un tour : notifs + followup + CI, puis 1 action lourde (verrou)
forge.py                   # un passage : conformité déclarative des repos surveillés (verrou)
pipelines/
├── dev_executor.py        # exécute, révise, répare la CI (les appels Claude)
├── dev_followup.py        # suivi léger : nettoyage post-merge, CI (notif + détection)
├── dev_statut.py          # @bot conso / statut depuis Discord
└── forge.py                # vérifie data/forge.yaml sur chaque repo, ticket par écart
lib/
├── claude.py              # wrapper claude -p (JSON : texte + tokens + coût, quota détecté)
├── github.py              # API GitHub (issues, PR, labels, rulesets, check runs, logs de jobs)
├── workspace.py           # clones locaux : branche, commit, push (token jamais persisté)
├── state.py               # idempotence SQLite (notifs, PR, commentaires, CI, conso, quota, forge)
└── notify.py              # notifs : bot Discord si dispo, sinon webhook
bot.py / server.py         # bot Discord (notifs + statut) / HTTP (/health, /conso)
infra/                     # setup idempotent, systemd (timers poll + sync + forge, OnFailure), sync auto
data/repos.yaml            # repos surveillés
data/forge.yaml            # conditions de conformité des repos surveillés
```

Principes : **Discord/HTTP = bus d'événements** (zéro logique métier dans
`bot.py`/`server.py`), un pipeline = un fichier, tout appel Claude via
`lib/claude.run_claude()` avec des droits scopés (`allowed_tools`), toute
notif via `lib/notify.notify()`, cloisonnement par tokens.

Docs détaillées :
[`docs/architecture-mini-serveur-ia.md`](docs/architecture-mini-serveur-ia.md)
(environnement, exploitation, état réel) ·
[`docs/plan-orchestrateur-dev.md`](docs/plan-orchestrateur-dev.md)
(décisions, phases, implémentation) · [`CLAUDE.md`](CLAUDE.md) (règles pour
les sessions Claude Code).

## Installation (Pi ou VPS Debian)

```bash
bash infra/setup.sh                         # système, node/nvm, Claude Code, venv (idempotent)
cp .env.example .env && chmod 600 .env      # puis remplir les secrets (voir Configuration)
sudo cp infra/systemd/*.service infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-bot orchestrator-server
make install-timer                          # timers : poller (5 min) + auto-update (10 min) + forge (1x/j)
```

Côté GitHub, une fois par repo surveillé :
- créer les labels `ai-ready`, `ai-working`, `ai-failed`, `ai-review` ;
- un **PAT fine-grained** scopé au repo : Contents, Pull requests, Issues en
  write (+ Actions en read pour les logs de CI, + Administration en read pour
  la forge) ;
- protéger `main` (l'agent ne pousse que sur `ai/*`, mais ceinture et bretelles
  — et c'est justement ce que la forge vérifie).

## Configuration

| Clé `.env` | Rôle |
|---|---|
| `GITHUB_TOKEN` | PAT scopé aux repos surveillés (voir ci-dessus) |
| `WATCHED_REPO` | repo surveillé — secours si `data/repos.yaml` absent |
| `CLAUDE_CODE_OAUTH_TOKEN` | auth Claude Code (abonnement, via `claude setup-token`) |
| `DISCORD_BOT_TOKEN` | bot Discord (notifs + `@bot conso/statut`) |
| `DISCORD_WEBHOOK_URL` | notifs des process hors-bot (poller, timers) → `#orchestrateur` |
| `NOTIFY_CHANNEL_ID` | canal de notif du bot |

Repos surveillés (`data/repos.yaml`) :

```yaml
repos:
  - fgeronimi/ia-orchestrator
```

Conditions de conformité des repos surveillés (`data/forge.yaml`) — voir le
fichier pour le détail, à incrémenter (`version`) quand elles changent :

```yaml
version: 1
labels: [ai-ready, ai-working, ai-failed, ai-review]
fichiers: [CLAUDE.md]
protection_main: true
```

## Au quotidien

```bash
make                 # liste toutes les cibles (Pi vs Mac)
make deploy          # (Mac) push + mise à jour du Pi
make remote-poll     # (Mac) déclenche un tour de poll (⚠️ peut exécuter un ticket)
make remote-conso    # (Mac) conso Claude par ticket (tokens, coût)
make remote-logs     # (Mac) logs du Pi en continu
make conso / poll / forge  # (Pi) équivalents locaux (forge : vérification de conformité)
```

Depuis Discord (`#orchestrateur`) : `@bot statut` (tickets en file / en cours /
en échec, PR ouvertes), `@bot conso` (tableau par ticket). En HTTP :
`GET /health`, `GET /conso` (port 5000, non exposé sur internet).

Le Pi s'auto-entretient : un push sur `main` est récupéré et les services
redémarrés dans les 10 minutes ; le poller notifie tout ce qu'il fait dans
`#orchestrateur` ; un crash du poller envoie un 🚨.

## Exemple de bout en bout (vécu : issue #11 → PR #12)

Le ticket qui a ajouté l'endpoint `/conso` de ce repo, déroulé réel :

1. **Issue [#11](https://github.com/fgeronimi/ia-orchestrator/issues/11)** créée
   avec le label `ai-ready` : *« server.py : endpoint /conso (conso Claude par
   ticket, JSON) — réutiliser `lib.state.conso_par_ticket()`, ne pas dupliquer
   le SQL, rester dans le style de `/health`. »*
2. **Tour de poll suivant** — sur Discord, en direct :
   ```
   🔔 🎫 #11 pris en charge — server.py : endpoint /conso …
   🔔 🔨 #11 : implémentation par Claude…
   🔔 🔍 #11 : PR draft ouverte → …/pull/12
        🪙 implementation : 266k lus / 1.6k générés (~0.21 $)
   🔔 🧐 #11 : auto-review postée sur la PR #12
        🪙 auto-review : 228k lus / 2.6k générés (~0.27 $)
   ```
3. **La PR [#12](https://github.com/fgeronimi/ia-orchestrator/pull/12)** : 14
   lignes, l'endpoint réutilise bien `state.conso_par_ticket()`. L'auto-review
   (grille `bakaa-brutal-reviewer`) épingle un vrai point — *« Docstring
   désynchronisée du diff (severity: MEDIUM) : le docstring dit toujours
   “Réduit à `/health` pour l'instant” »* — et conclut *« ⚠️ un point à
   vérifier avant merge, le reste est solide »*.
4. **Merge humain** (seule intervention manuelle du déroulé, avec l'écriture du
   ticket).
5. **Tour de poll suivant** : branche `ai/11` supprimée, issue #11 close et
   délabellisée, `🔔 ✅ PR #12 mergée — 🧹 branche ai/11 supprimée`. Le Pi
   s'auto-met à jour et l'endpoint est en production :
   ```bash
   $ curl -s http://ia-orchestrator.home:5000/conso
   [{"appels":2,"cout_usd":0.477,"ticket":"fgeronimi/ia-orchestrator#11",
     "tokens_generes":4177,"tokens_lus":494731}, …]
   ```
   L'agent a donc écrit, relu, et mis en production l'endpoint qui mesure… son
   propre coût : **0,48 $** pour ce ticket.

## Garde-fous

- **Rien n'atteint `main` sans toi** : PR draft systématique, merge humain.
- **Seul le propriétaire du repo pilote l'agent** : les commentaires de tiers
  sur les PR sont ignorés (un commentaire = des instructions exécutées sur le
  Pi), et les PR de forks sont invisibles pour l'orchestrateur, même avec une
  branche nommée `ai/*`. Les labels déclencheurs restent réservés aux
  collaborateurs (mécanique GitHub).
- L'agent ne pousse que sur des branches `ai/*` ; le PAT est scopé par repo,
  jamais admin ; le token n'est ni persisté dans les remotes git ni loggué.
- Appels Claude bornés (timeout 600 s) et scopés par `allowed_tools`
  (l'auto-review n'a que `Read`).
- CI rouge : 2 tentatives de réparation max, ensuite intervention humaine.
- Quota d'abonnement épuisé : remise en file automatique, reprise à l'heure
  annoncée, une seule notif.
- Conso visible partout (notifs, `make conso`, `/conso`, `@bot conso`) — pas
  de coût silencieux.

## Licence

[MIT](LICENSE).
