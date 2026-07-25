# Plan — Orchestrateur de dev piloté par GitHub

> Plan d'attaque pour transformer l'orchestrateur en assistant de code : tu crées
> des tickets GitHub, le Pi les implémente, ouvre des PR, se relit, et gère la
> suite après ton merge. Document de travail, mis à jour au fil des phases.
> Créé le 2026-07-25 · dernière mise à jour 2026-07-25.

## État d'avancement

| Phase | État |
|---|---|
| **Phase 0** — poller (lecture GitHub → notif Discord, dédup) | ✅ **fait & déployé** |
| **Phase 1** — l'exécutant (issue → code → PR draft + auto-review) | ✅ **fait & validé live** (1a, 1b, 1c, auto-review) |
| **Phase 2** — suite après merge (nettoyage + boucle de révision) | ✅ **fait & validé live** |
| **Phase 3** — élargissement (multi-repos, CI, review affinée) | ✅ **fait & validé live** |

**Ce qui tourne aujourd'hui :** un timer systemd (`orchestrator-poll`) lance
`poll.py` toutes les 5 min sur le Pi ; il lit les issues taggées `ai-ready` du
repo surveillé, notifie les **nouvelles** dans `#orchestrateur` (dédup SQLite),
puis nettoie les PR d'agent mergées (`dev_followup` : branche supprimée, label
retiré), puis lance **une action lourde** sous verrou `flock`
(`state/executor.lock`), révision prioritaire :
- nouveaux commentaires humains sur une PR d'agent → `dev_executor.reviser()`
  (Claude corrige sur la branche, repush, répond sur la PR) ;
- sinon première issue `ai-ready` → `dev_executor.executer()` : label
  `ai-working`, workspace, branche `ai/<n>`, Claude implémente, commit/push,
  PR draft, auto-review en commentaire, notif à chaque étape.

Validé live : issue #3 → PR #4 (mergée + nettoyée), issue #5 → PR #6
(+ révision sur commentaire). Voir §7 pour la Phase 0.

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
| `lib/github.py` | wrapper API GitHub (lecture : `list_issues`). PAT scopé. | ✅ fait (lecture) |
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
Déployé sur le Pi avec un timer 5 min. Détails d'implémentation en §7.

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

## 6. Prochaine action

Les phases 0 à 3 sont faites : le pipeline vit en autonomie. La suite se
décide à l'usage :
- config par repo dans `repos.yaml` (tests, déploiement) quand un second
  repo sera surveillé ;
- sandbox des tests (`systemd-run --scope`, §5) avant tout repo tiers ;
- retirer/recycler `pipelines/dev_jira.py` (legacy d'avant le pivot).

---

## 7. Implémentation actuelle (Phase 0)

### Fichiers
- **`lib/github.py`** — wrapper API REST GitHub, lecture seule.
  `list_issues(repo, labels=None, state="open")` → `[{number, title, labels, url}]`,
  exclut les PR. Auth via `GITHUB_TOKEN` (env) ; optionnel pour un repo public,
  requis pour un privé. Lève `GitHubError` sur 401/404/erreur HTTP.
- **`lib/state.py`** — idempotence SQLite (`state/orchestrator.db`, gitignored).
  Table `issues_notifiees(repo, numero, notifiee_le)`, clé primaire `(repo, numero)`.
  `deja_notifiee(repo, numero)` / `marquer_notifiee(repo, numero)`.
- **`poll.py`** (racine) — un tour de polling : `list_issues(repo, labels="ai-ready")`,
  filtre les non-vues via `lib/state`, notifie chaque nouvelle via `lib/notify`,
  la marque. Repo = `argv[1]` sinon `WATCHED_REPO` du `.env`.
- **`infra/poll.sh`** — wrapper systemd : `cd` repo, lance `.venv/bin/python poll.py "$WATCHED_REPO"`.
- **`infra/systemd/orchestrator-poll.{service,timer}`** — oneshot, toutes les 5 min.

### Convention de label
Le label déclencheur est **`ai-ready`** (anglais). Piège vécu : ne pas le confondre
avec `ia-ready` (français) — comme `idées`≠`idees`, GitHub matche à la lettre.

### Config requise (`.env`)
- `GITHUB_TOKEN` — PAT (classic ou fine-grained), scope lecture Issues+Metadata.
- `WATCHED_REPO` — `owner/nom` du repo surveillé (défaut `fgeronimi/ia-orchestrator`).
- `DISCORD_WEBHOOK_URL` — webhook du canal `#orchestrateur` (les notifs du poller
  passent par là, c'est un process hors-bot).

### Lancer / observer
```bash
# manuel (Pi) :
.venv/bin/python poll.py fgeronimi/ia-orchestrator
# service : make install-timer   puis   journalctl -u orchestrator-poll -f
# état DB :  sqlite3 state/orchestrator.db "SELECT * FROM issues_notifiees;"
```

### Limites connues (à traiter plus tard)
- `poll.py` marque une issue notifiée **même si le webhook a échoué** (`notify`
  loggue mais ne remonte pas de statut) → pas de re-tentative.
- Pas de gestion de la pagination GitHub (`per_page=100`, suffisant au volume).
- Retirer puis re-tagger une issue ne la re-notifie pas (déjà en base).
