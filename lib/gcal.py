"""Google Calendar — STUB.

À implémenter une fois l'OAuth Google Cloud configuré :
1. console.cloud.google.com → nouveau projet → activer "Google Calendar API"
2. Écran de consentement OAuth (type "Externe", mode test suffit pour un usage perso)
3. Identifiants → Créer → ID client OAuth → type "Application de bureau"
4. Télécharger le JSON, le placer dans state/gcal_credentials.json (gitignored)
5. Premier lancement : flux OAuth interactif pour générer un token réutilisable

Une fois fait, create_event() remplace le TODO dans pipelines/perso_resto.py.
"""


def create_event(summary: str, date: str, time: str | None, notes: str | None) -> str:
    raise NotImplementedError(
        "Google Calendar pas encore configuré — voir le TODO en haut de lib/gcal.py"
    )
