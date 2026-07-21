#!/usr/bin/env bash
#
# sync.sh — synchronisation git du Pi, anti-drift.
#
# Le Pi écrit en continu dans data/ (restos ajoutés via Discord et l'iPhone)
# pendant que le code est édité depuis le Mac. Sans synchronisation régulière,
# les deux côtés divergent et le `git pull` finit en conflit.
#
# Ce script, lancé par le timer systemd orchestrator-sync :
#   1. committe les changements de data/ (le Pi en est le seul auteur)
#   2. rebase sur origin/main
#   3. pousse
#   4. redémarre les services si du code Python a changé
#
# Silencieux quand il n'y a rien à faire ; notifie Discord en cas de conflit
# ou de mise à jour de code. Idempotent, sans effet de bord si tout est à jour.
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PYTHON="$REPO/.venv/bin/python"
BRANCHE="$(git rev-parse --abbrev-ref HEAD)"
# Marqueur de conflit non résolu : évite de renotifier toutes les 10 minutes
# tant que l'humain n'est pas passé.
MARQUEUR="$REPO/state/sync-conflit"

notifier() {
  echo "[sync] $*"
  [ -x "$PYTHON" ] && "$PYTHON" -m lib.notify "$*" >/dev/null 2>&1
  return 0
}

echoue() {
  if [ -e "$MARQUEUR" ]; then
    echo "[sync] $* (déjà notifié)"
  else
    mkdir -p "$(dirname "$MARQUEUR")" && touch "$MARQUEUR"
    notifier "⚠️ sync git : $*"
  fi
  exit 1
}

if [ "$BRANCHE" != "main" ]; then
  echoue "branche courante « $BRANCHE » (attendu : main). Sync ignoré."
fi

# --- 1. Committer les données produites par le Pi -------------------------
if [ -n "$(git status --porcelain -- data/)" ]; then
  git add data/ || echoue "git add a échoué"
  git commit -q -m "data: sync auto $(date +%Y-%m-%dT%H:%M)" \
    || echoue "commit des données impossible"
fi

# Du code modifié à la main sur le Pi n'est pas un cas nominal : on ne le
# committe pas à l'aveugle. --autostash le préservera. Simple trace dans les
# logs plutôt qu'une notif, sinon c'est un rappel toutes les 10 minutes.
if [ -n "$(git status --porcelain | grep -v '^.. data/')" ]; then
  echo "[sync] modifications locales hors data/ (préservées, non commitées)"
fi

# --- 2. Rebase sur origin -------------------------------------------------
AVANT="$(git rev-parse HEAD)"

git fetch -q origin "$BRANCHE" || echoue "fetch impossible (réseau ? credentials ?)"

if ! git pull -q --rebase --autostash origin "$BRANCHE"; then
  # Conflit : on ne tranche pas à la place de l'humain — perdre des restos
  # silencieusement est pire qu'une alerte.
  FICHIERS="$(git diff --name-only --diff-filter=U | tr '\n' ' ')"
  git rebase --abort 2>/dev/null
  echoue "conflit sur : ${FICHIERS:-?} — à résoudre à la main sur le Pi"
fi

# --- 3. Pousser -----------------------------------------------------------
if [ -n "$(git log origin/"$BRANCHE"..HEAD --oneline)" ]; then
  git push -q origin "$BRANCHE" || echoue "push refusé (repousser au prochain tour)"
fi

# Arrivé ici, le repo est aligné : un éventuel conflit précédent est résolu.
if [ -e "$MARQUEUR" ]; then
  rm -f "$MARQUEUR"
  notifier "✅ sync git de nouveau opérationnel"
fi

# --- 4. Redémarrer si le code a changé ------------------------------------
APRES="$(git rev-parse HEAD)"
[ "$AVANT" = "$APRES" ] && exit 0

MODIFIES="$(git diff --name-only "$AVANT" "$APRES")"
if ! grep -qE '\.py$' <<<"$MODIFIES"; then
  exit 0
fi

if sudo -n systemctl restart orchestrator-bot orchestrator-server 2>/dev/null; then
  notifier "♻️ code mis à jour ($(git log --oneline -1 --format=%s)) — services redémarrés"
else
  notifier "⚠️ code mis à jour mais restart impossible (sudo -n refusé) — \`make restart\` requis"
fi
