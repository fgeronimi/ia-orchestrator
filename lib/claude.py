"""Wrapper subprocess autour de Claude Code CLI.

Toute interaction avec Claude passe par ici : un seul endroit à modifier
si l'on change de CLI, de flags ou de méthode d'auth.
Auth : CLAUDE_CODE_OAUTH_TOKEN doit être présent dans l'environnement
(abonnement Pro/Max via `claude setup-token`, pas de clé API).
"""

import asyncio
import os


class ClaudeError(RuntimeError):
    pass


async def run_claude(
    prompt: str,
    cwd: str | None = None,
    timeout: int = 600,
    allowed_tools: list[str] | None = None,
) -> str:
    """Exécute `claude -p <prompt>` et retourne la sortie texte.

    - cwd : répertoire de travail (repo du pipeline concerné)
    - allowed_tools : restreint les outils de Claude Code
      (ex: ["Read", "Grep"] pour un agent en lecture seule)
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise ClaudeError("CLAUDE_CODE_OAUTH_TOKEN manquant dans l'environnement")

    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if allowed_tools:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]

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

    if proc.returncode != 0:
        raise ClaudeError(stderr.decode(errors="replace")[:2000] or "Erreur inconnue")
    return stdout.decode(errors="replace").strip()
