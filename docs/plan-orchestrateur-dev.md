# Plan — Orchestrateur de dev piloté par GitHub

> Plan d'attaque pour transformer l'orchestrateur en assistant de code : tu crées
> des tickets GitHub, le Pi les implémente, ouvre des PR, se relit, et gère la
> suite après ton merge. Document de travail, mis à jour au fil des phases.
> Créé le 2026-07-25 · dernière mise à jour 2026-07-30.

## État d'avancement

| Phase | État |
|---|---|
| **Phase 0** — poller (lecture GitHub → notif Discord, dédup) | ✅ **fait & déployé** |
| **Phase 1** — l'exécutant (issue → code → PR draft + auto-review) | ✅ **fait & validé live** (1a, 1b, 1c, auto-review) |
| **Phase 2** — suite après merge (nettoyage + boucle de révision) | ✅ **fait & validé live** |
| **Phase 3** — élargissement (multi-repos, CI, review affinée) | ✅ **fait & validé live** |

**Ce qui tourne aujourd'hui — 5 timers systemd sur le Pi**, sur **6 repos
surveillés** (`data/repos.yaml`) :

| Timer | Cadence | Rôle |
|---|---|---|
| `orchestrator-poll` | 5 min | tour léger (notifs, followup, CI, triage) + **une** action lourde sous `state/executor.lock` |
| `orchestrator-sync` | 10 min | auto-update git + restart si le code a changé |
| `orchestrator-sante` | 15 min | surveillance machine, alerte disque par palier (80/90/95%) |
| `orchestrator-forge` | 1x/jour | conformité déclarative des repos (`data/forge.yaml`) |
| `orchestrator-purge` | 1x/jour | purge des branches locales `ai/*` devenues inutiles |

Plus deux services persistants : `orchestrator-bot` (Discord) et
`orchestrator-server` (Flask `/health`, `/conso`).

L'action lourde du poller suit une chaîne de priorité : révision d'une PR
commentée > review demandée (`ai-review`) > résolution de conflit > réparation
de CI rouge > première issue `ai-ready`. Chaque recherche de cette chaîne est
isolée repo par repo — un timeout GitHub saute le repo, il ne tue plus le tour
(correctif du 2026-07-30, cf. architecture §5).

**Au-delà des phases 0–3**, ajouté depuis : forge (conformité), triage des
tickets (labels `size:*`/`model:*`), modèle Claude configurable, surveillance
machine, purge des workspaces. `lib/github.py` fait désormais lecture **et**
écriture (branches, PR, commentaires, labels, rulesets, check runs).

Validé live : issue #3 → PR #4 (mergée + nettoyée), issue #5 → PR #6
(+ révision sur commentaire). **104 tests** (`make test`).

---

## 0. Décisions actées

| Sujet | Choix |
|---|---|
| Forge | **GitHub** (issues, PR, Actions, PAT scopé par repo) |
| Déclenchement | **Polling** — timer systemd interroge l'API, aucun port exposé |
| Autonomie | **PR en draft, merge humain** — rien n'atterrit sur `main` sans toi |
| Notifications | **Discord** (`lib/notify` déjà en place) |

**Point d'entrée = les issues GitHub.** Tu écris tes tickets directement dans
GitHub et tu les tagges `ai-ready` ; l'orchestrateur ne crée pas de ticket, il
les consomme. Pas de saisie via Discord ni ailleurs.

Le principe fondateur ne change pas : Discord/HTTP = bus d'événements, un pipeline
= un fichier, cloisonnement des droits **au niveau des tokens**.

---

## 1. Ce qui est nouveau par rapport aux pipelines existants

Les pipelines actuels sont **one-shot sans état**. Ici, trois nouveautés :

1. **Un cycle de vie** — un ticket traverse des étapes (à faire → en cours → PR
   ouverte → mergée → suite). Nécessite un état persistant.
2. **Du polling idempotent** — le Pi interroge GitHub en boucle et ne doit
   **jamais** retraiter deux fois le même ticket ou commentaire.
3. **Claude écrit vraiment du code** dans un repo tiers et lance des tests —
   extension de confiance que l'archi cible (Annexe A du doc d'archi) anticipe
   comme le moment où le cloisonnement des droits devient critique.

---

## 2. Briques à créer

| Brique | Rôle | État |
|---|---|---|
| `lib/github.py` | wrapper API GitHub. PAT scopé par repo. | ✅ fait (lecture **et** écriture) |
| `lib/state.py` + `state/orchestrator.db` | mémoire d'idempotence (issues déjà notifiées) | ✅ fait |
| `poll.py` + `infra/poll.sh` + `orchestrator-poll.timer` | boucle de polling → notif | ✅ fait |
| `pipelines/dev_executor.py` | l'exécutant : issue → code → PR + auto-review | ✅ fait |
| `state/workspaces/<repo>/` | clones des repos surveillés où Claude code (`lib/workspace`) | ✅ fait |
| `data/repos.yaml` | repos surveillés (liste ; config par repo au besoin) | ✅ fait |
| `lib/github.py` (écriture : branches, PR, commentaires) | Phase 1 | ✅ fait |

> `lib/github.py` et le poller sont **développables et testables en local depuis
> le Mac** contre un vrai repo GitHub — seule l'exécution de Claude a besoin du
> token OAuth (le CLI local est déjà authentifié, il manque juste le `.env`).

---

## 3. Cycle de vie d'un ticket (état porté par les labels GitHub)

Les labels servent de machine à états — lisibles dans l'UI GitHub, pas seulement
en base.

```
[toi] tu crées une issue + label  ai-ready
        │  (poll détecte)
        ▼
  ai-working   ← l'agent prend, clone/pull le workspace, code, lance les tests,
        │        ouvre la PR draft + auto-review en commentaire → notif Discord
        │  (poll détecte tes nouveaux commentaires sur la PR)
        ├──▶ l'agent révise, push, re-notifie   (boucle tant que tu commentes)
        │
   [TOI] tu merges la PR
        │  (poll détecte le merge)
        ▼
  post-merge   ← cleanup : branche ai/* supprimée, label retiré → notif
                 (Phase 3 : CI/déploiement selon repos.yaml)
```

- `ai-working` empêche un second tour de polling de reprendre un ticket en cours.
  (Pas de label `ai-review` distinct : la PR draft s'ouvre sous `ai-working`.)
- `ai-failed` = échec dur, en attente d'humain (remettre `ai-ready` relance).
  Quota épuisé ≠ échec : le ticket repasse en `ai-ready` tout seul.
- La SQLite dédoublonne les commentaires (un `comment_id` ne se rejoue pas) et
  les PR mergées déjà nettoyées.
- L'agent ne pousse que sur des branches `ai/*`, jamais sur `main`.

### Notifications — une par étape

Chaque transition envoie une notif Discord, pour suivre l'avancement en direct :

| Étape | Notif |
|---|---|
| ticket pris | 🎫 « #12 pris en charge — *titre* » |
| code en cours | 🔨 « #12 : implémentation… » (+ résumé du plan) |
| PR ouverte | 🔍 « #12 : PR draft #34 prête à relire » + lien + auto-review |
| révision | ✏️ « #12 : commentaires pris en compte, repush » |
| mergée | ✅ « PR #34 mergée » |
| suite | 🚀 « déploiement lancé » / 🧹 « branche nettoyée » + résultat |
| échec | ⚠️ à toute étape qui casse (tests rouges, conflit, erreur API) |

**Canal dédié** (ex. `#orchestrateur`), séparé de `#idees` : le flux par étape
est verbeux et ne doit pas noyer le reste. Les notifs viennent du **poller** (un
process distinct du bot), donc via **webhook** — `DISCORD_WEBHOOK_URL` pointe sur
ce canal (`lib/notify` l'utilise déjà pour les process hors-bot).

---

## 4. Plan par phases

### Phase 0 — Plomberie & confiance ✅ FAIT
`lib/github.py` en lecture seule + `poll.py` qui notifie les issues `ai-ready`
sur Discord, avec dédup SQLite. Valide : auth PAT, polling, dédup, notify.
Déployé sur le Pi avec un timer 5 min. Structure du code : architecture §3.

### Phase 1 — L'exécutant *(le cœur, ~80 % de la valeur)*
`pipelines/dev_executor.py` : issue `ai-ready` → clone/pull workspace →
`run_claude(cwd=workspace, allowed_tools=["Read","Edit","Write","Bash"])` pour
implémenter + lancer les tests → branche `ai/<issue>` → **PR draft** →
auto-review postée en commentaire → notif. Tu merges à la main.

**Décisions actées (2026-07-25) :** même PAT passé en écriture (Contents+Pull
requests+Issues=write, scopé au repo) ; bac à sable = timeout simple (repos
perso) ; Claude **auto-détecte** les commandes de test/build (pas de repos.yaml
au début) ; exécution **inline dans le poller avec un verrou** fichier, un
ticket à la fois.

**Construit incrémentalement :**
- *1a — chemin d'écriture* ✅ **validé live**. `lib/github` (écriture :
  `get_default_branch`, `get_issue`, `add_labels`, `remove_label`,
  `comment_issue`, `find_open_pull`, `create_pull`), `lib/workspace`
  (clone/branche/commit/push, token jamais persisté ni loggué),
  `dev_executor.executer()`.
- *1b — Claude implémente* ✅ **validé live** : `run_claude(cwd=workspace,
  allowed_tools=["Read","Edit","Write","Bash"], timeout=600)` implémente le
  ticket, auto-détecte les tests. Testé sur l'issue #1 → PR #2 réelle (Claude a
  ajouté `make env-push` au README). PR réutilisée si déjà ouverte.
  Lancer un ticket à la main : `python -m pipelines.dev_executor <repo> <n>`.
- *1c — câblage poller* ✅ **validé live** : après les notifs, `poll.py` traite
  la **première** issue `ai-ready` en inline via `dev_executor.executer()` —
  un ticket par tour, sous verrou `flock` sur `state/executor.lock`
  (non-bloquant : déjà pris → skip du tour ; libéré automatiquement à la mort
  du process, pas de verrou orphelin après crash). Le label `ai-working` sert
  d'idempotence (l'issue n'est plus `ai-ready` donc pas reprise). Piège vécu :
  l'unité **installée** dans `/etc/systemd/system` n'avait pas le PATH node du
  repo → `FileNotFoundError: claude`. Après toute modif d'un `.service`,
  refaire `make install-timer`.
- *auto-review* ✅ **validé live** : après la PR, le diff `base...HEAD` est
  calculé en Python (`workspace.diff_contre`, tronqué à 40k chars) et injecté
  dans le prompt ; Claude le relit depuis le workspace
  (`allowed_tools=["Read"]`) et le commentaire est posté sur la PR
  (`comment_issue` : même endpoint pour les PR). Un échec d'auto-review
  notifie ⚠️ mais ne fait pas échouer le run (la PR est déjà ouverte).
  Validé live : issue #3 → PR #4 + auto-review pertinente en commentaire.

### Phase 2 — La suite après merge ✅ FAIT & validé live
- `pipelines/dev_followup.py` : à chaque tour de poll (léger, API seulement),
  les PR fermées sur une branche `ai/*` sont traitées une seule fois
  (`state.prs_suivies`) : mergée → branche supprimée + label `ai-working`
  retiré + notif ✅🧹 ; fermée sans merge → marquée vue, sans action (décision
  humaine). Validé live sur la PR #4 (issue #3).
- Boucle de révision (`dev_executor`) : `chercher_revision()` balaye les PR
  d'agent ouvertes et agrège les nouveaux commentaires humains — conversation
  ET diff (deux espaces d'ids, clés `issue-`/`review-` dans
  `state.commentaires_vus`). Les commentaires de l'orchestrateur (même login,
  même PAT) sont écartés par leur **préfixe 🤖**. `reviser()` : workspace sur
  la branche de la PR, Claude applique les commentaires
  (Read/Edit/Write/Bash), commit, repush, répond sur la PR, notif. Les
  commentaires ne sont marqués vus qu'après succès (retente au tour suivant).
  Une révision est **prioritaire** sur un nouveau ticket (une seule action
  lourde par tour, même verrou). Validé live sur la PR #6 (issue #5) :
  commentaire → correction exacte → repush + réponse.

### Phase 3 — Élargissement ✅ FAIT & validé live
- *Multi-repos* ✅ **validé live** : `data/repos.yaml` (clé `repos`) liste les
  repos surveillés ; `WATCHED_REPO` en secours, argument CLI prioritaire.
  `poll.py` balaye tous les repos (notifs, followup, CI) puis lance UNE action
  lourde tous repos confondus (révision prioritaire, même verrou). Le PAT doit
  être scopé sur chaque repo listé. Dépendance `pyyaml` (setup.sh).
- *Review affinée* ✅ **validée live** : checklist dans le prompt d'auto-review
  (bugs, sécurité, fidélité au ticket, conventions, tests) — validée sur la
  PR #8 (elle a relevé un vrai écart doc/sudoers).
- *CI GitHub Actions* ✅ **validée live** : `.github/workflows/ci.yml` lance
  `make test` (imports de tous les modules) sur chaque PR et sur `main`.
  `dev_followup.surveiller_ci()` notifie le résultat des check runs des PR
  d'agent (une fois par sha, un repush relance le suivi ; silencieux sans
  CI ; dédup `state.ci_notifiee`). Validé sur la PR #8 (révision → repush →
  CI → notif « ✅ CI verte »). Pièges vécus : pousser un workflow exige le
  scope PAT `workflow` (ajouté au PAT du Mac le 2026-07-25) ; et la première
  CI a été rouge — `bot.py` lisait `DISCORD_BOT_TOKEN` à l'import (le `.env`
  local masquait le problème), lecture déplacée dans le bloc `__main__`.
- *Garde-fous* : décision sandbox en §5 (timeout conservé, systemd-run
  reporté). `main` protégée et push limité aux branches `ai/*` inchangés.

### Robustesse & conso (ajouts du 2026-07-25)
- **Quota Claude épuisé** : `run_claude` détecte la limite d'usage du CLI
  (« usage limit reached|<epoch> ») et lève `ClaudeQuotaError` avec l'heure de
  reprise si annoncée. L'exécutant remet alors le ticket en file (`ai-ready`
  reposé, `ai-working` retiré) ; une révision interrompue repart seule (les
  commentaires ne sont marqués vus qu'après succès). Le blocage est mémorisé
  (`state.meta`, clé `quota_jusqua`, 30 min par défaut) : le poller continue
  notifs/followup/CI mais saute les actions lourdes jusqu'à la reprise — une
  seule notif ⏳, pas de spam toutes les 5 min.
- **Conso de tokens par ticket** : chaque appel Claude (étapes
  `implementation`, `auto-review`, `revision`, `ci-fix`) est tracé dans SQLite
  (`conso_claude` : tokens entrée/cache/sortie + coût estimé du CLI). Les
  notifs Discord de chaque étape portent un résumé 🪙 ; `make conso` (Pi),
  `make remote-conso` (Mac) ou `@bot conso` (Discord) affichent le tableau
  agrégé par ticket.
- **Échec dur d'un ticket → label `ai-failed`** (2026-07-25) : l'exécutant
  pose `ai-failed` (retire `ai-working`) et notifie ⚠️ — état visible dans
  GitHub, pas de retry auto (un échec déterministe bouclerait). Remettre
  `ai-ready` relance (l'exécutant retire `ai-failed` à la prise en charge).
  Une révision en échec dur marque ses commentaires vus (sinon elle serait
  relancée tous les 5 min) : re-commenter la PR pour retenter.
- **Crash du poller → notif Discord** (2026-07-25) : unité template
  `orchestrator-fail-notify@.service` branchée en `OnFailure=` sur
  `orchestrator-poll` — un tour qui crashe (hors cas gérés : quota, échec
  ticket) envoie 🚨 sur Discord au lieu de mourir en silence dans journalctl.
- **CI rouge → correction automatique** (2026-07-25) :
  `dev_followup.chercher_ci_rouge()` détecte la première PR d'agent dont la
  CI est rouge (jamais deux fois le même sha — `state.ci_fix_tentees`) ;
  `dev_executor.corriger_ci()` récupère la fin du log du job en échec
  (l'id de check run Actions = id de job), la donne en prompt à Claude sur la
  branche de la PR, commit `ai: #n répare la CI`, repush (nouveau sha →
  nouveau cycle CI surveillé). **Max 2 tentatives par ticket** (comptées via
  `conso_claude`, étape `ci-fix`), ensuite 🛑 intervention humaine (notifié
  une fois). Priorité des actions lourdes du poller : révision > CI rouge >
  nouveau ticket. Un quota épuisé ne consomme pas de tentative.
- **Prêt pour un repo public** (2026-07-25) : la boucle de révision n'écoute
  que les commentaires du **propriétaire du repo** (sur un repo public,
  n'importe qui peut commenter une PR, et un commentaire pilote un agent Bash
  sur le Pi — injection de prompt = exécution de code) ; et toutes les
  détections (révision, CI, merges, statut) écartent les **PR de forks**,
  même si leur branche s'appelle `ai/*` (`head_repo` ≠ repo surveillé).
  Restent sous contrôle humain : les labels (collaborateurs seuls) — sur un
  repo public, ne labelliser `ai-ready` que des issues relues (le corps
  devient un prompt). Historique scanné : aucun secret jamais commité.
- **Review à la demande** (2026-07-25) : le quota Claude de l'abonnement est
  limité — les **grosses sessions se font en local** (Mac) et poussent des
  PR ; poser le label **`ai-review`** sur une PR fait relire son diff par le
  Pi (auto-review, Read seul — l'étape la moins gourmande) qui poste le
  commentaire et retire le label. Priorité des actions lourdes : révision >
  review demandée > CI rouge > nouveau ticket. Jamais les PR de forks.
- **Résolution autonome des conflits** (2026-07-26) : deux tickets voisins
  partent du même `main`, l'un est mergé avant l'autre → la seconde PR
  d'agent devient non mergeable. `dev_followup.chercher_conflit()` détecte
  les PR `ai/*` `mergeable = false` (une tentative par sha,
  `state.conflits_tentes`) ; `dev_executor.resoudre_conflit()` merge la base
  dans le workspace : propre → push direct (zéro Claude) ; conflits → Claude
  résout **sémantiquement** (les deux côtés doivent coexister), vérification
  qu'aucun marqueur ne survit avant de committer le merge, sinon
  `merge --abort` (branche intacte) + 🛑 humain. Priorité : révision >
  review demandée > conflit > CI rouge > ticket. Étape conso `conflit`.
- **Commande `/ticket` en commentaire de PR** (2026-07-26) : un commentaire
  du propriétaire commençant par `/ticket <titre>` (corps optionnel sur les
  lignes suivantes) crée l'issue correspondante avec le lien vers la PR
  d'origine — **sans label** (`ai-ready` reste un geste humain), zéro appel
  Claude, dédup partagée avec la révision. Les commentaires `/…` ne
  déclenchent jamais de révision (espace de noms des commandes).
- **Review en lecture seule, DoD à la CI** (2026-07-26) : le prompt
  d'auto-review précise que l'agent ne peut pas exécuter tests/lint/types —
  c'est la CI de la PR qui les exécute ; la review se concentre sur ce que
  la CI ne voit pas, au lieu de signaler l'évidence à chaque PR.
- **Suivi depuis Discord** (2026-07-25) : `pipelines/dev_statut.py`, branché
  sur le canal `#orchestrateur` (dict `PIPELINES` de `bot.py`) — `@bot conso`
  (tableau par ticket) et `@bot statut` (tickets en file/en cours/en échec,
  PR d'agent ouvertes). Lecture seule, aucun appel Claude.

---

## 5. Points de vigilance

- **Permissions = le vrai sujet.** L'exécutant combine un **PAT
  `contents:write` + `pull_requests:write`** (jamais admin, scopé aux repos
  surveillés) *et* `Bash` côté Claude pour lancer les tests. Mitigations :
  PAT dédié à cet usage, `main` en **branche protégée** côté GitHub, push
  restreint aux branches `ai/*`.
- **Sandbox des tests.** `Bash` exécute le code du repo cloné sur un Pi partagé
  avec les autres pipelines. **Décision (2026-07-25) : on reste au `timeout`**
  (600 s, repos perso uniquement) — `systemd-run --scope` avec limites
  CPU/mémoire est reporté « si besoin avéré » (fragile depuis le contexte
  systemd du poller : pas de user manager, XDG_RUNTIME_DIR absent). À
  reconsidérer avant de surveiller un repo tiers non maîtrisé.
- **Secrets** : nouveau `GITHUB_TOKEN` (ou `GH_PAT`) → `.env` + `.env.example`.
- **Auto-update du Pi** : déjà en place (`infra/sync.sh` + `orchestrator-sync.timer`).
  Un push sur `main` est récupéré et les services redémarrés dans les 10 min.

---

## 6. Reste à faire

Les phases 0 à 3 sont faites : le pipeline vit en autonomie. Rien d'urgent —
liste tenue à jour, du plus mûr au plus spéculatif.

**Déclencheur déjà franchi**
- **Config par repo dans `repos.yaml`** — prévue « quand un second repo sera
  surveillé » ; il y en a **6**. Seul `timeout` est configurable aujourd'hui.
  Manque : commande de tests et de déploiement par repo (aujourd'hui
  l'exécutant auto-détecte les tests).

**Conditionné à un usage qu'on n'a pas encore**
- **Sandbox des tests** (`systemd-run --scope`, cf. §5) — requis **avant de
  surveiller un repo tiers non maîtrisé**. Aujourd'hui `Bash` exécute le code
  cloné avec un simple `timeout` (600 s), assumé pour des repos perso.

**Espace disque** (constaté le 2026-07-30, disque à 66%, 4,6 Go libres)
- **Artefacts de build non purgés** — 1,1 Go de `node_modules` dans le
  workspace `havre-app`, contre 3,9 Mo de `.git`. Gitignorés donc invisibles
  pour git, liés à aucune PR : la purge (`pipelines/purge.py`) ne les touche
  pas. Piste si la marge devient courte : purge ciblée sur les workspaces
  inactifs depuis N jours, pour que le coût du rebuild à froid ne tombe jamais
  sur un ticket en cours. **Décision non prise.**

**Limites connues, jamais traitées**
- `poll.py` marque une issue notifiée **même si le webhook a échoué** (`notify`
  loggue mais ne remonte pas de statut) → pas de re-tentative. Même faiblesse
  dans `pipelines/sante.py` : le palier d'alerte est enregistré *avant* l'envoi,
  donc une alerte disque perdue n'est pas retentée — plus gênant là que sur une
  notif de ticket.
- Pas de gestion de la pagination GitHub (`per_page=100`, suffisant au volume).
- Retirer puis re-poser `ai-ready` ne re-notifie pas (déjà en base).
- **Réseau du Pi par intermittence** : `Temporary failure in name resolution`
  le 2026-07-28, `ReadTimeout` de l'API GitHub le 2026-07-30 à 02:03. Désormais
  encaissé proprement (⚠️ au lieu de 🚨) mais la cause n'a pas été cherchée.

(`pipelines/dev_jira.py`, legacy d'avant le pivot, retiré le 2026-07-25.)

---

## 7. Où regarder dans le code

⚠️ Cette section décrivait l'implémentation de la **Phase 0** (juillet 2026) et
avait divergé du code : elle affirmait notamment que `lib/github.py` était « en
lecture seule », faux depuis la Phase 1. Elle ne duplique plus l'inventaire des
fichiers — **la référence à jour est
[`architecture-mini-serveur-ia.md`](architecture-mini-serveur-ia.md) §3
(structure) et §5 (exploitation, un bloc par timer)**. Ne restent ici que les
éléments qui n'ont pas leur place ailleurs.

### Convention de label — piège vécu
Le label déclencheur est **`ai-ready`** (anglais). Ne pas le confondre avec
`ia-ready` (français) : comme `idées` ≠ `idees`, GitHub matche à la lettre.
Les autres labels du cycle : `ai-working`, `ai-failed`, `ai-review`, plus
`size:*` / `model:*` / `triage:questions` posés par le triage.

### Config `.env`
Toutes les clés sont documentées dans **`.env.example`** (clé sans valeur) —
c'est la source de vérité, `make env-diff` compare les clés Mac/Pi sans jamais
comparer les valeurs. Rappel des scopes du PAT : Contents + Pull requests +
Issues en write, Actions en read (logs de CI), Administration en read (forge).

### Historique des décisions
Les choix structurants (forge GitHub, polling plutôt que webhooks, PR draft +
merge humain, pas de sandbox pour l'instant) sont en §0 et §5 de ce document —
ils restent valides et expliquent le *pourquoi* du code actuel.
