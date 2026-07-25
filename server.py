"""Endpoint HTTP de l'orchestrateur.

Réduit à /health pour l'instant. Le pipeline dev (GitHub) fonctionne en
polling, sans endpoint entrant — voir docs/plan-orchestrateur-dev.md. Ce
serveur reste en place pour la supervision et un éventuel futur webhook.

Accessible uniquement via Tailscale (pas de port exposé sur internet).
"""

from dotenv import load_dotenv
from flask import Flask, jsonify

from lib import state

load_dotenv()

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/conso", methods=["GET"])
def conso():
    return jsonify([
        {
            "ticket": ticket,
            "appels": appels,
            "tokens_lus": tokens_lus,
            "tokens_generes": tokens_generes,
            "cout_usd": cout_usd,
        }
        for ticket, appels, tokens_lus, tokens_generes, cout_usd in state.conso_par_ticket()
    ])


if __name__ == "__main__":
    # host=0.0.0.0 : joignable via l'IP Tailscale, reste injoignable depuis
    # internet tant qu'aucun port forwarding n'est configuré sur la box.
    app.run(host="0.0.0.0", port=5000)
