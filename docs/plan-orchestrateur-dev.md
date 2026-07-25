# Plan — Orchestrateur de dev piloté par GitHub

> Plan d'attaque pour transformer l'orchestrateur en assistant de code : tu crées
> des tickets GitHub, le Pi les implémente, ouvre des PR, se relit, et gère la
> suite après ton merge. Document de travail, mis à jour au fil des phases.
> Créé le 2026-07-25 · dernière mise à jour 2026-07-25.

## État d'avancement

| Phase | État |
|---|---|
| **Phase 0** — poller (lecture GitHub → notif Discord, dédup) | ✅ **fait & déployé** |
| Phase 1 — l'exécutant (issue → code → PR draft) | 🚧 **en cours (prochaine)** |
| Phase 2 — suite après merge (déploiement / révision) | ⬜ à venir |
| Phase 3 — élargissement (multi-repos, CI, garde-fous) | ⬜ à venir |

**Ce qui tourne aujourd'hui :** un timer systemd (`orchestrator-poll`) lance
`poll.py` toutes les 5 min sur le Pi ; il lit les issues taggées `ai-ready` du
repo surveillé et notifie les **nouvelles** dans `#orchestrateur`. La dédup
(SQLite) évite de re-signaler un ticket déjà vu. Voir §7 pour les détails
d'implémentation.

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
| `pipelines/dev_executor.py` | l'exécutant : issue → code → PR | 🚧 Phase 1 |
| `state/workspaces/<repo>/` | clones des repos surveillés où Claude code | ⬜ Phase 1 |
| `data/repos.yaml` | repos surveillés + config par repo (tests, déploiement) | ⬜ Phase 1/2 |
| `lib/github.py` (écriture : branches, PR, commentaires) | Phase 1 | ⬜ |

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
  ai-working   ← l'agent prend, clone/pull le workspace, code, lance les tests
        │
        ▼
  ai-review    ← PR draft ouverte + auto-review en commentaire → notif Discord
        │  (poll détecte tes nouveaux commentaires de review)
        ├──▶ l'agent révise, push, re-notifie   (boucle tant que tu commentes)
        │
   [TOI] tu merges la PR
        │  (poll détecte le merge)
        ▼
  post-merge   ← selon repos.yaml : déclenche CI/déploiement OU cleanup branche
                 → notif du résultat
```

- `ai-working` empêche un second tour de polling de reprendre un ticket en cours.
- La SQLite dédoublonne les commentaires (un `comment_id` ne se rejoue pas).
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

### Phase 2 — La suite après merge
- `pipelines/dev_followup.py` : PR mergée → selon `repos.yaml`, déclenche le
  déploiement/CI ou nettoie la branche.
- Boucle de révision : nouveaux commentaires de review sur une PR d'agent →
  l'agent corrige et repush.

### Phase 3 — Élargissement
Plusieurs repos, review plus fine (checklist, diff-aware), intégration CI
concrète (statut GitHub Actions), garde-fous supplémentaires.

---

## 5. Points de vigilance

- **Permissions = le vrai sujet.** L'exécutant combine un **PAT
  `contents:write` + `pull_requests:write`** (jamais admin, scopé aux repos
  surveillés) *et* `Bash` côté Claude pour lancer les tests. Mitigations :
  PAT dédié à cet usage, `main` en **branche protégée** côté GitHub, push
  restreint aux branches `ai/*`.
- **Sandbox des tests.** `Bash` exécute le code du repo cloné sur un Pi partagé
  avec les autres pipelines. Au minimum un `timeout` ; idéalement
  `systemd-run --scope` avec limites CPU/mémoire. À cadrer en Phase 1.
- **Secrets** : nouveau `GITHUB_TOKEN` (ou `GH_PAT`) → `.env` + `.env.example`.
- **Auto-update du Pi** : déjà en place (`infra/sync.sh` + `orchestrator-sync.timer`).
  Un push sur `main` est récupéré et les services redémarrés dans les 10 min.

---

## 6. Prochaine action

Construire la **Phase 1 — l'exécutant** (`pipelines/dev_executor.py`) : voir §4.
Le poller (§7) appellera l'exécutant au lieu de juste notifier, quand une issue
`ai-ready` est détectée.

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
