#!/usr/bin/env bash
#
# setup.sh — Installation du serveur d'orchestration IA
# Cible : Raspberry Pi OS Lite 64-bit (fonctionne aussi sur Debian/Ubuntu VPS)
# Usage : bash setup.sh
# Idempotent : peut être relancé sans casser l'existant.
#
set -euo pipefail

log()  { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m/!\\ $*\033[0m"; }

# ------------------------------------------------------------------
# 0. Vérifications préalables
# ------------------------------------------------------------------
if [ "$(uname -m)" != "aarch64" ] && [ "$(uname -m)" != "x86_64" ]; then
  warn "Architecture $(uname -m) détectée. Il faut un OS 64-bit (aarch64/x86_64)."
  warn "Si tu es sur un Pi en 32-bit, reflashe avec Raspberry Pi OS Lite 64-bit."
  exit 1
fi

# ------------------------------------------------------------------
# 1. Système de base
# ------------------------------------------------------------------
log "Mise à jour du système"
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

log "Paquets essentiels"
sudo apt-get install -y -qq \
  git curl build-essential \
  python3 python3-venv python3-pip \
  sqlite3 \
  ca-certificates

# ------------------------------------------------------------------
# 2. Node.js via nvm (les paquets apt sont trop vieux pour Claude Code)
# ------------------------------------------------------------------
export NVM_DIR="$HOME/.nvm"
if [ ! -d "$NVM_DIR" ]; then
  log "Installation de nvm"
  curl -so- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
else
  log "nvm déjà présent"
fi
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

log "Installation de Node.js 20 LTS"
nvm install 20 >/dev/null
nvm alias default 20
node --version

# ------------------------------------------------------------------
# 3. Claude Code (fallback npm, fiable sur ARM64)
# ------------------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  log "Installation de Claude Code"
  npm install -g @anthropic-ai/claude-code
else
  log "Claude Code déjà présent — mise à jour"
  npm update -g @anthropic-ai/claude-code
fi
claude --version || warn "claude --version a échoué, vérifier l'install"

# ------------------------------------------------------------------
# 4. Tailscale (accès distant sans port exposé)
# ------------------------------------------------------------------
if ! command -v tailscale >/dev/null 2>&1; then
  log "Installation de Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
else
  log "Tailscale déjà présent"
fi

# ------------------------------------------------------------------
# 5. Dossier projet + venv Python
# ------------------------------------------------------------------
PROJECT_DIR="$HOME/ia-orchestrator"
if [ ! -d "$PROJECT_DIR" ]; then
  log "Création du dossier projet $PROJECT_DIR"
  mkdir -p "$PROJECT_DIR"/{pipelines,lib,state,infra/systemd,docs}
else
  log "Dossier projet déjà présent"
fi

if [ ! -d "$PROJECT_DIR/.venv" ]; then
  log "Création du venv Python"
  python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet discord.py python-dotenv requests

# ------------------------------------------------------------------
# 6. Récapitulatif des actions manuelles restantes
# ------------------------------------------------------------------
log "Installation terminée. Actions manuelles restantes :"
cat <<'EOF'

  1) AUTH CLAUDE (depuis ton Mac, pas sur le Pi) :
       claude setup-token
     Puis sur le Pi, ajoute dans ~/.bashrc :
       export CLAUDE_CODE_OAUTH_TOKEN="<le token>"
     Recharge : source ~/.bashrc
     Test :   claude -p "réponds juste: ok"

  2) TAILSCALE :
       sudo tailscale up
     (ouvre l'URL affichée pour lier le Pi à ton compte)

  3) BOT DISCORD :
     - discord.com/developers/applications → New Application
     - Bot → Reset Token → copie le token
     - Active "Message Content Intent" dans les réglages du bot
     - Invite le bot sur ton serveur (OAuth2 → URL Generator → bot,
       permissions: Send Messages, Read Message History)
     - Mets le token dans ~/ia-orchestrator/.env :
         DISCORD_BOT_TOKEN=...

  4) Clone ton repo quand il existera :
       git clone <ton-repo> ~/ia-orchestrator
EOF
