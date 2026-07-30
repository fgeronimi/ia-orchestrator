"""Statut & conso depuis Discord — mentionner le bot dans #orchestrateur.

Commandes (lecture seule, aucun appel Claude) :
  @bot conso   → conso Claude agrégée par ticket (tokens, coût estimé)
  @bot statut  → tickets en file / en cours / en échec + PR d'agent ouvertes
  autre chose  → aide

Le repo interrogé est le premier de data/repos.yaml (via poll.charger_repos),
comme le poller.

⚠️ Ce module tourne DANS la boucle asyncio du bot : tout appel bloquant
(réseau, subprocess) doit passer par `asyncio.to_thread`, sinon le heartbeat
Discord saute — vécu le 2026-07-27, `heartbeat blocked for more than 10s`.
"""

import asyncio

from lib import github, state

AIDE = ("Commandes : `conso` (tokens/coût par ticket) ou `statut` "
        "(tickets et PR en cours).")


def _conso() -> str:
    lignes = state.conso_par_ticket()
    if not lignes:
        return "Aucune conso enregistrée pour le moment."
    corps = "\n".join(
        f"{ticket:<32} {appels:>2} appel(s)  {lus/1000:>6.0f}k lus  "
        f"{sortie/1000:>5.1f}k générés  {cout:>5.2f} $"
        for ticket, appels, lus, sortie, cout in lignes
    )
    etapes = "\n".join(
        f"{etape:<22} {appels:>3} appel(s)  {cout:>6.2f} $"
        for etape, appels, _lus, _sortie, cout in state.conso_par_etape()
    )
    return f"```\n{corps}\n\nPar étape :\n{etapes}\n```"


def _statut(repo: str) -> str:
    sections = []
    for label, titre in (("ai-ready", "🎫 En file"),
                         ("ai-working", "🔨 En cours"),
                         ("ai-failed", "⚠️ En échec")):
        issues = github.list_issues(repo, labels=label)
        if issues:
            sections.append(f"**{titre}** : " + ", ".join(
                f"#{i['number']} {i['title']}" for i in issues))
    prs = [p for p in github.list_pulls(repo, state="open")
           if p["head"].startswith("ai/") and p["head_repo"] == repo]
    if prs:
        sections.append("**🔍 PR d'agent ouvertes** : " + ", ".join(
            f"#{p['number']} ({p['head']})" for p in prs))
    return "\n".join(sections) if sections else "Rien en cours — tout est calme. ✅"


async def handle(text: str, message) -> str:
    # Import local : poll charge dev_executor/dev_followup, inutile de tirer
    # tout ça au démarrage du bot.
    from poll import charger_repos

    demande = text.strip().lower()
    if "conso" in demande:
        return _conso()
    if "statut" in demande or "status" in demande or "état" in demande:
        repos = [e["repo"] for e in charger_repos()]
        if not repos:
            return "Aucun repo surveillé (data/repos.yaml vide et WATCHED_REPO absent)."
        # _statut() fait des appels GitHub synchrones : hors boucle asyncio.
        blocs = [await asyncio.to_thread(_statut, repo) for repo in repos]
        return "\n\n".join(f"__{repo}__\n{bloc}"
                           for repo, bloc in zip(repos, blocs))
    return AIDE
