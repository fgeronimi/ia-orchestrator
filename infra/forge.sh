#!/usr/bin/env bash
#
# forge.sh — un passage de la forge (conditions déclaratives des repos
# surveillés), lancé par orchestrator-forge.timer.
#
# Toute la logique est dans forge.py / pipelines/forge.py. Les repos viennent
# de data/repos.yaml, les conditions de data/forge.yaml.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

exec "$REPO_DIR/.venv/bin/python" forge.py
