"""Endpoint HTTP de l'orchestrateur.

Réduit à /health pour l'instant. Le pipeline dev (GitHub) fonctionne en
polling, sans endpoint entrant — voir docs/plan-orchestrateur-dev.md. Ce
serveur reste en place pour la supervision et un éventuel futur webhook.

Accessible uniquement via Tailscale (pas de port exposé sur internet).
"""

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # host=0.0.0.0 : joignable via l'IP Tailscale, reste injoignable depuis
    # internet tant qu'aucun port forwarding n'est configuré sur la box.
    app.run(host="0.0.0.0", port=5000)
