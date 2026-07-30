#!/usr/bin/env bash
#
# purge.sh — un passage de purge des workspaces (branches locales des PR
# mergées), lancé par orchestrator-purge.timer.
#
# Toute la logique est dans purge.py / pipelines/purge.py. Les repos viennent
# de data/repos.yaml. Le verrou est celui de l'exécutant (state/executor.lock) :
# la purge attend qu'aucun agent ne code.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

exec "$REPO_DIR/.venv/bin/python" purge.py
