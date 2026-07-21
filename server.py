"""Endpoint HTTP pour le raccourci iOS "Partager vers".

Reçoit un screenshot, le sauvegarde temporairement, le passe au pipeline
perso_resto pour classification, notifie le résultat sur Discord.

Accessible uniquement via Tailscale (pas de port exposé sur internet).
Protégé par un token simple dans le header X-Shortcut-Token.
"""

import asyncio
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from lib import notify
from pipelines import perso_resto

load_dotenv()

app = Flask(__name__)

INCOMING_DIR = Path(__file__).parent / "state" / "incoming"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)

SHORTCUT_TOKEN = os.environ.get("IOS_SHORTCUT_TOKEN", "")


def _run_async(coro):
    """Flask est synchrone ; on exécute les coroutines ponctuellement."""
    return asyncio.run(coro)


@app.route("/upload", methods=["POST"])
def upload():
    if not SHORTCUT_TOKEN or request.headers.get("X-Shortcut-Token") != SHORTCUT_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    image = request.files.get("image")
    if image is None:
        return jsonify({"error": "champ 'image' manquant"}), 400

    ext = Path(image.filename or "screenshot.jpg").suffix or ".jpg"
    dest = INCOMING_DIR / f"{uuid.uuid4().hex}{ext}"
    image.save(dest)

    try:
        result = _run_async(perso_resto.handle_image(str(dest)))
        _run_async(notify.notify(result))
        return jsonify({"status": "ok", "result": result})
    except Exception as exc:
        _run_async(notify.notify(f"❌ Erreur traitement screenshot : {exc}"))
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        dest.unlink(missing_ok=True)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # host=0.0.0.0 : nécessaire pour être joignable via l'IP Tailscale.
    # Reste néanmoins injoignable depuis internet tant que le Pi n'a pas
    # de port forwarding explicite sur la box (qu'on ne fait pas).
    app.run(host="0.0.0.0", port=5000)
