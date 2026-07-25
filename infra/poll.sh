#!/usr/bin/env bash
#
# poll.sh — un tour du poller GitHub, lancé par orchestrator-poll.timer.
#
# Toute la logique est dans poll.py ; ce wrapper ne fait que lancer le venv.
# Les repos surveillés viennent de data/repos.yaml, sinon de WATCHED_REPO
# (EnvironmentFile=.env dans le .service).
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

exec "$REPO_DIR/.venv/bin/python" poll.py
