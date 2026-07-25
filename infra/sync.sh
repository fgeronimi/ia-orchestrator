#!/usr/bin/env bash
#
# sync.sh — auto-update git du Pi.
#
# Le code est édité et poussé depuis le Mac ; le Pi doit se mettre à jour tout
# seul. Ce script, lancé par le timer systemd orchestrator-sync, fait :
#   1. rebase sur origin/main
#   2. redémarre les services si du code Python a changé
#
# Polling (pas de webhook), donc rien n'est exposé sur internet. Silencieux
# quand il n'y a rien à faire ; notifie Discord en cas de conflit ou de mise à
# jour de code. Idempotent, sans effet de bord si tout est déjà à jour.
#
# NB : l'état runtime vit dans state/ (gitignored) — le Pi n'écrit rien de
# versionné, donc pas de commit remontant depuis le Pi en fonctionnement nominal.
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

# Du code modifié à la main sur le Pi n'est pas un cas nominal : on ne le
# committe pas à l'aveugle. --autostash le préservera. Simple trace dans les
# logs plutôt qu'une notif, sinon c'est un rappel toutes les 10 minutes.
if [ -n "$(git status --porcelain)" ]; then
  echo "[sync] modifications locales sur le Pi (préservées via autostash, non commitées)"
fi

# --- 1. Rebase sur origin -------------------------------------------------
AVANT="$(git rev-parse HEAD)"

git fetch -q origin "$BRANCHE" || echoue "fetch impossible (réseau ? credentials ?)"

if ! git pull -q --rebase --autostash origin "$BRANCHE"; then
  # Conflit : on ne tranche pas à la place de l'humain.
  FICHIERS="$(git diff --name-only --diff-filter=U | tr '\n' ' ')"
  git rebase --abort 2>/dev/null
  echoue "conflit sur : ${FICHIERS:-?} — à résoudre à la main sur le Pi"
fi

# Aligné : un éventuel conflit précédent est résolu.
if [ -e "$MARQUEUR" ]; then
  rm -f "$MARQUEUR"
  notifier "✅ sync git de nouveau opérationnel"
fi

# --- 2. Redémarrer si le code a changé ------------------------------------
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
