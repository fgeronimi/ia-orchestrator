#!/usr/bin/env bash
#
# poll.sh — un tour du poller GitHub, lancé par orchestrator-poll.timer.
#
# Lit les issues taggées `ai-ready` du repo surveillé et notifie les NOUVELLES
# sur Discord (dédup dans state/orchestrator.db). Toute la logique est dans
# poll.py ; ce wrapper ne fait que fixer le repo et lancer le venv.
#
# Le repo vient de WATCHED_REPO (EnvironmentFile=.env dans le .service), avec un
# défaut si absent.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

REPO="${WATCHED_REPO:-fgeronimi/ia-orchestrator}"
exec "$REPO_DIR/.venv/bin/python" poll.py "$REPO"
