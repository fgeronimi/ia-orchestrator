# Makefile — exploitation de l'orchestrateur.
#
# Deux familles de cibles :
#   - LOCAL  : à lancer sur le Pi (systemd, services, git)
#   - DEPUIS LE MAC : passent par SSH/scp vers le Pi (préfixe remote-, env-, deploy)
#
# Le host du Pi est surchargeable :  make deploy PI_HOST=<ip-tailscale-du-pi>
# (utile hors du LAN : passer par l'IP Tailscale)

# Le user est explicite : sans lui, ssh utilise le login du Mac (francois.geronimi)
# et le Pi refuse la connexion.
PI_USER ?= fgeronimi
PI_HOST ?= ia-orchestrator.home
PI      := $(PI_USER)@$(PI_HOST)
PI_DIR  ?= /home/fgeronimi/ia-orchestrator
PYTHON  := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
SERVICES := orchestrator-bot orchestrator-server
HORODATAGE := $(shell date +%Y%m%d-%H%M%S)

.DEFAULT_GOAL := help
.PHONY: help sync pull push restart status logs test poll conso install-timer \
        deploy remote-logs remote-status remote-poll remote-conso \
        env-pull env-push env-diff

help: ## Affiche cette aide
	@echo "Sur le Pi :"
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | grep -v '^\(deploy\|remote-\|env-\)' \
		| awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo "\nDepuis le Mac (via SSH vers $(PI)) :"
	@grep -hE '^(deploy|remote-[a-z]+|env-[a-z]+):.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- sur le Pi

sync: ## Auto-update : pull, rebase, redémarre si le code a changé
	@bash infra/sync.sh

pull: ## Récupère le code et redémarre les services
	@git pull --rebase --autostash
	@$(MAKE) restart

push: ## Pousse d'éventuelles modifs faites sur le Pi
	@git push

restart: ## Redémarre les deux services
	@sudo systemctl restart $(SERVICES)
	@sleep 2 && $(MAKE) --no-print-directory status

status: ## État des services
	@# boucle POSIX : make exécute les recettes avec /bin/sh (dash sur le Pi),
	@# donc pas de process substitution bash <(...) ici.
	@for s in $(SERVICES); do printf '%s: %s\n' "$$s" "$$(systemctl is-active $$s)"; done

logs: ## Suit les logs des deux services (Ctrl-C pour sortir)
	@journalctl -u orchestrator-bot -u orchestrator-server -f

test: ## Vérifie que les modules importent + lance les tests unitaires
	@$(PYTHON) -c "import bot, server, poll, forge, pipelines.dev_executor, \
		pipelines.dev_followup, pipelines.dev_statut, pipelines.dev_triage, pipelines.forge, \
		lib.claude, lib.github, lib.notify, lib.state, lib.workspace" && echo "imports OK"
	@$(PYTHON) -m unittest discover -s tests -v

poll: ## Lance un tour du poller GitHub (WATCHED_REPO du .env) ; peut déclencher l'exécution réelle d'un ticket ai-ready
	@$(PYTHON) poll.py

forge: ## Vérifie les conditions déclaratives des repos surveillés (data/forge.yaml)
	@$(PYTHON) forge.py

conso: ## Conso Claude par ticket (tokens lus/générés, coût estimé)
	@test -f state/orchestrator.db || { echo "Pas encore de données."; exit 0; }
	@sqlite3 -column -header state/orchestrator.db \
		"SELECT repo || '#' || numero AS ticket, COUNT(*) AS appels, \
		 printf('%.0fk', SUM(tokens_entree + tokens_cache)/1000.0) AS lus, \
		 printf('%.1fk', SUM(tokens_sortie)/1000.0) AS generes, \
		 printf('%.2f $$', SUM(cout_usd)) AS cout, \
		 COALESCE(GROUP_CONCAT(DISTINCT modele), '(défaut)') AS modele \
		 FROM conso_claude GROUP BY repo, numero ORDER BY MAX(le) DESC"

install-timer: ## Installe les timers systemd (sync git + poll GitHub + forge quotidienne)
	@sudo cp infra/systemd/*.service infra/systemd/*.timer /etc/systemd/system/
	@sudo systemctl daemon-reload
	@sudo systemctl enable --now orchestrator-sync.timer orchestrator-poll.timer orchestrator-forge.timer
	@systemctl list-timers 'orchestrator-*' --no-pager

# ------------------------------------------------------------ depuis le Mac

deploy: ## Pousse le code puis déclenche un sync sur le Pi
	@git push
	@ssh $(PI) 'cd $(PI_DIR) && make sync'

remote-logs: ## Suit les logs du Pi depuis le Mac
	@ssh -t $(PI) 'journalctl -u orchestrator-bot -u orchestrator-server -f'

remote-status: ## État des services du Pi depuis le Mac
	@ssh $(PI) 'cd $(PI_DIR) && make status'

remote-poll: ## Déclenche un tour de poll GitHub sur le Pi depuis le Mac ; peut déclencher l'exécution réelle d'un ticket ai-ready
	@ssh $(PI) 'sudo -n systemctl start orchestrator-poll.service'

remote-conso: ## Conso Claude par ticket, lue sur le Pi depuis le Mac
	@ssh $(PI) 'cd $(PI_DIR) && make conso'

env-diff: ## Compare les CLÉS du .env local et du Pi (jamais les valeurs)
	@ssh $(PI) 'grep -oE "^[A-Z_]+=" $(PI_DIR)/.env | sort' > /tmp/env-pi.keys
	@grep -oE '^[A-Z_]+=' .env | sort > /tmp/env-local.keys
	@diff /tmp/env-local.keys /tmp/env-pi.keys \
		&& echo "Mêmes clés des deux côtés (valeurs non comparées)." \
		|| echo "  (« < » = seulement en local, « > » = seulement sur le Pi)"
	@rm -f /tmp/env-pi.keys /tmp/env-local.keys

env-pull: ## Récupère le .env du Pi (sauvegarde le local avant écrasement)
	@test ! -f .env || cp .env .env.bak.$(HORODATAGE)
	@test ! -f .env.bak.$(HORODATAGE) || echo "Sauvegarde : .env.bak.$(HORODATAGE)"
	@scp -q $(PI):$(PI_DIR)/.env .env
	@chmod 600 .env
	@echo "Récupéré depuis $(PI) : $$(grep -cE '^[A-Z_]+=' .env) clés."

env-push: ## Envoie le .env local vers le Pi et redémarre les services
	@test -f .env || { echo "Pas de .env local."; exit 1; }
	@ssh $(PI) 'test ! -f $(PI_DIR)/.env || cp $(PI_DIR)/.env $(PI_DIR)/.env.bak.$(HORODATAGE)'
	@echo "Sauvegarde côté Pi : .env.bak.$(HORODATAGE)"
	@scp -q .env $(PI):$(PI_DIR)/.env
	@ssh $(PI) 'chmod 600 $(PI_DIR)/.env'
	@ssh $(PI) 'cd $(PI_DIR) && make restart'
