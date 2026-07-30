#!/usr/bin/env bash
#
# sante.sh — un tour de surveillance de la machine (disque, RAM, charge),
# lancé par orchestrator-sante.timer.
#
# Toute la logique est dans sante.py / pipelines/sante.py. Le seuil d'alerte
# disque vient de SEUIL_DISQUE (EnvironmentFile=.env dans le .service),
# défaut 80%.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

exec "$REPO_DIR/.venv/bin/python" sante.py
