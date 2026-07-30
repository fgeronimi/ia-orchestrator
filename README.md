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
- **Modèle Claude configurable** : défaut par type de tâche dans
  `data/modeles.yaml`, ou override par ticket avec un label `model:<alias>`
  (`haiku`/`sonnet`/`opus`) ; le modèle utilisé est tracé avec la conso.
- **Notifications Discord** à chaque étape (🎫 🔨 🔍 ✏️ 🔧 ✅ 🧹 ⚠️ ⏳ 🚨), et un
  bot interrogeable : `@bot conso`, `@bot statut`, `@bot santé`.
- **Surveillance machine** : toutes les 15 min, alerte Discord dès que le disque
  atteint `SEUIL_DISQUE` (défaut 80%), puis à chaque palier franchi (90%, 95%),
  une seule fois par palier ; notif de retour à la normale en repassant dessous.
- **Purge des workspaces** : 1x/jour, supprime les branches locales `ai/*` dont
  la PR est **mergée** *ou* dont les commits sont **déjà dans la branche par
  défaut**, puis `git gc`. Ne touche jamais une branche humaine, ni une `ai/*`
  qui porterait du travail absent de `main`.
- **Robustesse** : quota d'abonnement géré (remise en file + reprise), échecs
  durs visibles (`ai-failed`), crash du poller notifié (`OnFailure=`),
  verrou anti-concurrence libéré même après un kill, hoquets réseau isolés repo
  par repo (tour léger **et** chaîne de priorité) — un timeout GitHub ne fait
  jamais échouer le tour.

## Architecture

```
poll.py                    # un tour : notifs + followup + CI, puis 1 action lourde (verrou)
forge.py                   # un passage : conformité déclarative des repos surveillés (verrou)
sante.py                   # un tour de surveillance machine (disque : alerte par palier)
purge.py                   # un passage : purge des branches locales devenues inutiles (verrou)
pipelines/
├── dev_executor.py        # exécute, révise, répare la CI (les appels Claude)
├── dev_followup.py        # suivi léger : nettoyage post-merge, CI (notif + détection)
├── dev_statut.py          # @bot conso / statut / santé depuis Discord
├── forge.py               # vérifie data/forge.yaml sur chaque repo, ticket par écart
├── sante.py               # mesures machine (disque, RAM, charge, temp) + alerte disque
└── purge.py               # supprime les branches locales ai/* mergées ou déjà dans main + git gc
lib/
├── claude.py              # wrapper claude -p (JSON : texte + tokens + coût, quota détecté)
├── github.py              # API GitHub (issues, PR, labels, rulesets, check runs, logs de jobs)
├── workspace.py           # clones locaux : branche, commit, push (token jamais persisté)
├── state.py               # idempotence SQLite (notifs, PR, commentaires, CI, conso, quota, forge)
└── notify.py              # notifs : bot Discord si dispo, sinon webhook
bot.py / server.py         # bot Discord (notifs + statut) / HTTP (/health, /conso)
infra/                     # setup idempotent, systemd (timers poll + sync + forge + santé + purge, OnFailure), sync auto
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
make install-timer                          # timers : poller (5 min) + auto-update (10 min) + forge (1x/j) + santé (15 min) + purge (1x/j)
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
| `DISCORD_BOT_TOKEN` | bot Discord (notifs + `@bot conso/statut/santé`) |
| `DISCORD_WEBHOOK_URL` | notifs des process hors-bot (poller, timers) → `#orchestrateur` |
| `NOTIFY_CHANNEL_ID` | canal de notif du bot |
| `SEUIL_DISQUE` | *(optionnel)* seuil d'alerte disque en %, défaut 80, borné à [50, 99] |

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

Modèle Claude par défaut, par type de tâche (`data/modeles.yaml`) — vide =
modèle par défaut de l'abonnement, aucun changement de comportement tant que
le fichier n'est pas édité :

```yaml
executer:
reviewer:
reviser:
corriger_ci:
resoudre_conflit:
```

## Au quotidien

```bash
make                 # liste toutes les cibles (Pi vs Mac)
make deploy          # (Mac) push + mise à jour du Pi
make remote-poll     # (Mac) déclenche un tour de poll (⚠️ peut exécuter un ticket)
make remote-conso    # (Mac) conso Claude par ticket (tokens, coût)
make remote-sante    # (Mac) santé du Pi (disque, RAM, charge, température)
make remote-purge    # (Mac) purge les workspaces du Pi (branches ai/* devenues inutiles)
make remote-logs     # (Mac) logs du Pi en continu
make conso / poll / forge / sante / purge  # (Pi) équivalents locaux
```

Depuis Discord (`#orchestrateur`) : `@bot statut` (tickets en file / en cours /
en échec, PR ouvertes), `@bot conso` (tableau par ticket), `@bot santé`
(disque, RAM, charge, température, uptime, état des services et timers).
En HTTP : `GET /health`, `GET /conso` (port 5000, non exposé sur internet).

Le Pi s'auto-entretient : un push sur `main` est récupéré et les services
redémarrés dans les 10 minutes ; le poller notifie tout ce qu'il fait dans
`#orchestrateur` ; un crash du poller envoie un 🚨 ; un disque qui se remplit
alerte à 80% (puis 90%, 95%), une seule fois par palier ; les branches locales
`ai/*` devenues inutiles sont purgées chaque nuit.

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

## Post-mortems — les erreurs qu'on a identifiées (et ce qu'elles enseignent)

Un orchestrateur qui code seul fait des erreurs de conception subtiles.
Les documenter vaut mieux que les oublier : chaque entrée décrit le
problème, sa source, ce qui se serait passé, et la correction.

### 2026-07 — la boucle de clarification consommait son plafond toute seule (PR #31)

**Le problème.** La phase 2 du triage (#25) re-analyse un ticket
`triage:questions` quand le propriétaire répond, avec un plafond de
2 tours. Dans les chemins de *succès* de `clarifier()` (questions levées,
nouvelles questions, plafond atteint), les commentaires traités n'étaient
jamais marqués vus (`state.marquer_commentaire`) — seuls les chemins
d'*erreur* le faisaient.

**La source.** Une asymétrie classique d'agent : les chemins d'erreur ont
été écrits avec soin (quota, JSON invalide, échec GitHub — chacun décide
explicitement de marquer ou non), et le chemin nominal a hérité de
l'hypothèse implicite « le travail est fait, rien à nettoyer ». La dédup
était bien *conçue* (clé partagée avec la boucle de révision), juste
jamais *appelée* là où tout se passe bien. Les tests vérifiaient chaque
comportement isolément — aucun ne rejouait le passage suivant du poller.

**Les conséquences (évitées).** Le poll tourne toutes les 5 minutes : la
même réponse humaine aurait été retraitée à chaque passage. Concrètement :
tu réponds une fois → questions au tour 1 → cinq minutes plus tard le
même commentaire re-déclenche un tour 2 sur ta *même* réponse → plafond
atteint → « triage silencieux » sans que tu aies jamais pu répondre aux
secondes questions. En prime : un appel Claude gaspillé par passage, et
des commentaires en double sur le ticket.

**La résolution.** Marquer les commentaires vus dès que l'appel Claude est
consommé (parse réussi), *avant* les écritures GitHub : une écriture qui
échoue ensuite notifie, mais ne rejoue pas le quota. Et un test de
régression qui vérifie l'état de dédup *après* l'appel — le test qui
manquait : celui qui simule le temps qui passe, pas seulement l'action.

**La leçon générale.** Dans un système qui se re-déclenche périodiquement,
« traiter » n'est pas fini tant que l'événement n'est pas marqué consommé —
et c'est le chemin nominal qui oublie, jamais les chemins d'erreur.
L'auto-review du Pi n'avait pas vu ce bug (elle relit le diff, pas le
comportement au tour suivant) ; c'est une relecture humaine/locale avec la
question « et dans 5 minutes ? » qui l'a attrapé.

### 2026-07 — le triage citait des fichiers inventés (PR #28/#30)

**Le problème.** Le premier triage (#24) annonçait des « fichiers
probables » plausibles mais inexistants (`triage/orchestrator.py`,
`.github/workflows/triage.yml`…).

**La source.** Le triage tourne en raisonnement pur (`allowed_tools=[]`,
c'est ce qui le rend quasi gratuit) : sans accès au dépôt, le champ ne
pouvait être que de la spéculation habillée en précision. L'erreur de
conception était de demander au modèle une information qu'il n'avait
aucun moyen d'avoir.

**Les conséquences (évitées).** Trompeur en soi, et dangereux en aval :
la phase 3 (#26) injectera cette analyse dans le prompt de l'exécutant —
des chemins hallucinés seraient devenus une désorientation active.

**La résolution (#29).** Ancrer : l'arborescence réelle du repo (un appel
API `git/trees`, zéro token) est injectée dans le prompt avec la consigne
« ne cite que ces chemins », **et** filtrée côté code après la réponse —
la consigne seule ne suffit jamais (défense en profondeur).

**La leçon générale.** Ne jamais demander à un modèle un champ dont il n'a
pas la source : soit on lui donne la source (ancrage), soit on retire le
champ. Et toute contrainte de prompt se double d'une validation côté code.

## Licence

[MIT](LICENSE).
