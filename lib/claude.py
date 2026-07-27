"""Wrapper subprocess autour de Claude Code CLI.

Toute interaction avec Claude passe par ici : un seul endroit à modifier
si l'on change de CLI, de flags ou de méthode d'auth.
Auth : CLAUDE_CODE_OAUTH_TOKEN doit être présent dans l'environnement
(abonnement Pro/Max via `claude setup-token`, pas de clé API).

Sortie en JSON (`--output-format json`) pour récupérer, en plus du texte,
la consommation de tokens et le coût estimé de chaque appel — les pipelines
les enregistrent par ticket via lib/state.

Quota épuisé (limite d'usage de l'abonnement) → ClaudeQuotaError, avec
l'heure de reprise si le CLI l'annonce. Les pipelines la rattrapent pour
remettre le ticket en file au lieu de le laisser en échec.
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass


# Alias acceptés par `claude --model`. Vit ici (pas dans data/modeles.yaml) :
# un label ou un yaml ne doit jamais pouvoir injecter une valeur arbitraire
# dans la commande CLI.
MODELES_AUTORISES = {"haiku", "sonnet", "opus"}


class ClaudeError(RuntimeError):
    pass


class ClaudeQuotaError(ClaudeError):
    """Limite d'usage de l'abonnement atteinte (plus de crédit)."""

    def __init__(self, message: str, reset_epoch: int | None = None):
        super().__init__(message)
        self.reset_epoch = reset_epoch  # reprise annoncée par le CLI (epoch), si connue


@dataclass
class ResultatClaude:
    texte: str
    tokens_entree: int = 0  # entrée hors cache
    tokens_cache: int = 0   # cache créé + relu
    tokens_sortie: int = 0
    cout_usd: float = 0.0


# Le CLI annonce la limite sous la forme « Claude AI usage limit reached|<epoch> ».
_MOTIF_QUOTA = re.compile(r"usage limit reached\|?(\d+)?", re.IGNORECASE)


def _detecter_quota(texte: str) -> None:
    m = _MOTIF_QUOTA.search(texte)
    if m:
        reset = int(m.group(1)) if m.group(1) else None
        raise ClaudeQuotaError("quota Claude épuisé (limite d'usage de l'abonnement)", reset)
    if "credit balance is too low" in texte.lower():
        raise ClaudeQuotaError("crédit Claude épuisé")


def modele_depuis_label(labels: list[str]) -> str | None:
    """Premier alias valide trouvé dans un label `model:<alias>`.

    Un label hors liste blanche (MODELES_AUTORISES) est ignoré (log) plutôt
    que transmis tel quel à la CLI.
    """
    for label in labels:
        if not label.startswith("model:"):
            continue
        alias = label.removeprefix("model:").strip().lower()
        if alias in MODELES_AUTORISES:
            return alias
        print(f"[claude] label ignoré (alias hors liste blanche) : {label!r}")
    return None


def _parser_sortie(sortie: str) -> ResultatClaude:
    """Parse la sortie JSON du CLI. Lève ClaudeQuotaError/ClaudeError si is_error."""
    try:
        d = json.loads(sortie)
    except ValueError:
        # Sortie inattendue non-JSON : on garde le texte brut plutôt que planter.
        return ResultatClaude(texte=sortie.strip())

    texte = (d.get("result") or "").strip()
    if d.get("is_error"):
        _detecter_quota(texte)
        raise ClaudeError(texte[:2000] or "erreur Claude sans message")

    usage = d.get("usage") or {}
    return ResultatClaude(
        texte=texte,
        tokens_entree=usage.get("input_tokens", 0),
        tokens_cache=(usage.get("cache_creation_input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0)),
        tokens_sortie=usage.get("output_tokens", 0),
        cout_usd=d.get("total_cost_usd") or 0.0,
    )


async def run_claude(
    prompt: str,
    cwd: str | None = None,
    timeout: int = 600,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
) -> ResultatClaude:
    """Exécute `claude -p <prompt>` et retourne texte + conso (ResultatClaude).

    - cwd : répertoire de travail (repo du pipeline concerné)
    - allowed_tools : restreint les outils de Claude Code
      (ex: ["Read", "Grep"] pour un agent en lecture seule)
    - model : alias passé à `--model` (ex: "haiku"). None = modèle par défaut
      de l'abonnement (comportement actuel, inchangé).
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise ClaudeError("CLAUDE_CODE_OAUTH_TOKEN manquant dans l'environnement")

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]
    if model:
        cmd += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or os.path.expanduser("~"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeError(f"Timeout après {timeout}s")

    sortie = stdout.decode(errors="replace")
    erreurs = stderr.decode(errors="replace")
    if proc.returncode != 0:
        _detecter_quota(sortie + erreurs)
        raise ClaudeError((erreurs or sortie)[:2000] or "Erreur inconnue")
    return _parser_sortie(sortie)
