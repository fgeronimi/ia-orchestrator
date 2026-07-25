# Plan — Orchestrateur de dev piloté par GitHub

> Plan d'attaque pour transformer l'orchestrateur en assistant de code : tu crées
> des tickets GitHub, le Pi les implémente, ouvre des PR, se relit, et gère la
> suite après ton merge. Document de travail, mis à jour au fil des phases.
> Créé le 2026-07-25.

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

| Brique | Rôle | Modèle existant |
|---|---|---|
| `lib/github.py` | wrapper API : issues, PR, commentaires, statut de merge. PAT scopé. | `lib/claude`, `lib/notify` |
| `state/orchestrator.db` (SQLite) | mémoire d'idempotence : ticket/commentaire déjà traité | déjà prévu dans `.gitignore` |
| `state/workspaces/<repo>/` | clones des repos surveillés où Claude code | nouveau |
| `infra/poll.sh` + `orchestrator-poll.timer` | boucle de polling, dispatch vers pipelines | `sync.sh` + son timer |
| `data/repos.yaml` | repos surveillés + config par repo (tests, déploiement) | nouveau, versionné |

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

---

## 4. Plan par phases

### Phase 0 — Plomberie & confiance *(à faire en premier, petit)*
`lib/github.py` en **lecture seule** + `poll.sh` qui se contente de **notifier**
« issue #12 taggée ai-ready » sur Discord. Valide : auth PAT, polling, dédup
SQLite, notify. Zéro risque, entièrement testable en local. Dérisque tout le reste.

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

Construire la **Phase 0** : `lib/github.py` lecture seule + le poller qui notifie.
Petit, testable sur le Mac, dérisque l'ensemble.
