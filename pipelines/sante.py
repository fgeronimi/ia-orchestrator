"""Santé de la machine — surveillance du disque et état du Pi.

Deux usages, une seule source de mesures (`mesurer()`) :

- **Surveillance** (`surveiller()`, appelée par le timer `orchestrator-sante`
  via `sante.py` à la racine) : alerte Discord dès que le disque atteint
  `SEUIL_DISQUE` (défaut 80%), puis à chaque palier franchi (90%, 95%).
- **Consultation** (`resume()`, appelée par `dev_statut` sur `@bot santé`) :
  photo lisible de l'état du Pi, sans rien alerter.

Anti-spam : le dernier palier alerté est mémorisé dans la table `meta` de
SQLite (`lib/state`). Tant que le disque reste dans le même palier, plus rien
n'est envoyé ; en repassant sous le seuil, une notif de retour à la normale
part et la mémoire est effacée. Sans ça, un disque à 81% alerterait toutes
les 15 minutes indéfiniment.

Tout est lu via le stdlib et `df`/`systemctl` : aucune dépendance ajoutée.
Les mesures propres à Linux (/proc, /sys) renvoient None ailleurs (Mac), pour
que le module reste importable et testable hors du Pi.
"""

import os
import shutil
import subprocess
from pathlib import Path

from lib import notify, state

RACINE = Path(__file__).parent.parent
WORKSPACES = RACINE / "state" / "workspaces"

# Palier d'alerte de base, surchargeable par SEUIL_DISQUE dans .env.
SEUIL_DEFAUT = 80
# Paliers d'escalade au-delà du seuil : un disque qui passe de 81% à 91% doit
# re-alerter, c'est une aggravation.
ESCALADES = (90, 95)
CLE_PALIER = "sante_disque_palier"  # dernier palier alerté (table meta)

SERVICES = ("orchestrator-bot", "orchestrator-server")
# À tenir à jour avec infra/systemd/ : un timer absent d'ici est un timer dont
# `@bot santé` ne dira jamais qu'il est mort.
TIMERS = ("orchestrator-poll.timer", "orchestrator-sync.timer",
          "orchestrator-forge.timer", "orchestrator-sante.timer",
          "orchestrator-purge.timer")


def seuil() -> int:
    """Seuil d'alerte disque en %, borné à [50, 99]."""
    try:
        valeur = int(os.environ.get("SEUIL_DISQUE", SEUIL_DEFAUT))
    except ValueError:
        return SEUIL_DEFAUT
    return max(50, min(99, valeur))


def octets(n: float) -> str:
    """1234567890 → '1.1G' (unité binaire, une décimale sous 10).

    Public : `pipelines/purge.py` s'en sert pour que les tailles annoncées
    dans ses notifs se lisent comme celles de `@bot santé`.
    """
    for unite in ("o", "K", "M", "G", "T"):
        if abs(n) < 1024 or unite == "T":
            return f"{n:.1f}{unite}" if n < 10 and unite != "o" else f"{n:.0f}{unite}"
        n /= 1024
    return f"{n:.0f}T"


def _lire(chemin: str) -> str | None:
    """Contenu d'un fichier de /proc ou /sys, None s'il n'existe pas (Mac)."""
    try:
        return Path(chemin).read_text().strip()
    except OSError:
        return None


def _temperature() -> float | None:
    """Température CPU en °C (millidegrés dans /sys sur le Pi)."""
    brut = _lire("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(brut) / 1000 if brut else None
    except ValueError:
        return None


def _uptime() -> str | None:
    brut = _lire("/proc/uptime")
    if not brut:
        return None
    try:
        secondes = int(float(brut.split()[0]))
    except (ValueError, IndexError):
        return None
    jours, reste = divmod(secondes, 86400)
    heures, minutes = divmod(reste // 60, 60)
    return f"{jours}j {heures:02d}h{minutes:02d}" if jours else f"{heures}h{minutes:02d}"


def _memoire() -> dict | None:
    """RAM et swap depuis /proc/meminfo (valeurs en octets)."""
    brut = _lire("/proc/meminfo")
    if not brut:
        return None
    champs = {}
    try:
        for ligne in brut.splitlines():
            cle, _, valeur = ligne.partition(":")
            champs[cle] = int(valeur.split()[0]) * 1024  # kB → octets
    except (ValueError, IndexError):
        # Format inattendu : on renonce à la RAM plutôt que de faire échouer le
        # tour. Un moniteur ne doit jamais être la cause d'une alerte 🚨.
        return None
    try:
        total = champs["MemTotal"]
        # 'available' est la bonne métrique (le cache est récupérable), pas
        # MemFree : sur le Pi 2.3G de buff/cache font paniquer à tort.
        dispo = champs["MemAvailable"]
        swap_total = champs["SwapTotal"]
        swap_libre = champs["SwapFree"]
    except KeyError:
        return None
    return {
        "total": total,
        "utilise": total - dispo,
        "pct": round((total - dispo) / total * 100, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_utilise": swap_total - swap_libre,
    }


def _taille_workspaces() -> int | None:
    """Poids de state/workspaces — le principal vecteur de remplissage du SD.

    `du` plutôt qu'un parcours Python : plus rapide sur SD card, et borné par
    un timeout pour ne jamais bloquer le tour.
    """
    if not WORKSPACES.exists():
        return 0
    try:
        sortie = subprocess.run(["du", "-sk", str(WORKSPACES)],
                                capture_output=True, text=True, timeout=60)
        return int(sortie.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _etat_units(units: tuple[str, ...]) -> dict[str, str]:
    """{unit: 'active' | 'inactive' | ...} via un seul appel systemctl."""
    try:
        sortie = subprocess.run(["systemctl", "is-active", *units],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {u: "inconnu" for u in units}
    lignes = sortie.stdout.strip().splitlines()
    # is-active renvoie une ligne par unité, dans l'ordre demandé.
    return {u: (lignes[i] if i < len(lignes) else "inconnu")
            for i, u in enumerate(units)}


def mesurer() -> dict:
    """Toutes les mesures d'un coup. Les clés Linux-only valent None ailleurs."""
    usage = shutil.disk_usage("/")
    # `usage.total` inclut la réserve root (~5% sur ext4), que nos écritures ne
    # peuvent PAS utiliser : utilise/total sous-estime le remplissage de ~3
    # points. On calcule le pourcentage comme `df`, sur utilise + libre, sinon
    # un seuil à 80% ne se déclenche qu'à 83% réels.
    utilisable = usage.used + usage.free
    return {
        "disque": {
            "total": usage.total,
            "utilisable": utilisable,
            "utilise": usage.used,
            "libre": usage.free,
            "pct": round(usage.used / utilisable * 100, 1) if utilisable else 0.0,
        },
        "memoire": _memoire(),
        "charge": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "temperature": _temperature(),
        "uptime": _uptime(),
        "workspaces": _taille_workspaces(),
        "services": _etat_units(SERVICES),
        "timers": _etat_units(TIMERS),
    }


def _palier(pct: float) -> int | None:
    """Palier d'alerte franchi par ce pourcentage, None s'il est sous le seuil."""
    base = seuil()
    paliers = sorted({base} | {e for e in ESCALADES if e > base}, reverse=True)
    for p in paliers:
        if pct >= p:
            return p
    return None


def resume() -> str:
    """État de santé lisible pour Discord (bloc de code aligné).

    Bloquant (df, /proc, systemctl) : à appeler via asyncio.to_thread depuis
    la boucle du bot.
    """
    m = mesurer()
    d = m["disque"]
    lignes = [
        f"{'disque':<11}{octets(d['utilise']):>7} / {octets(d['utilisable']):<6}"
        f"({d['pct']}%) {'⚠️' if _palier(d['pct']) else '✅'}",
        f"{'libre':<11}{octets(d['libre']):>7}",
    ]
    if m["memoire"]:
        mem = m["memoire"]
        lignes.append(
            f"{'RAM':<11}{octets(mem['utilise']):>7} / "
            f"{octets(mem['total']):<6}({mem['pct']}%)")
        if mem["swap_total"]:
            lignes.append(
                f"{'swap':<11}{octets(mem['swap_utilise']):>7} / "
                f"{octets(mem['swap_total'])}")
    if m["charge"]:
        lignes.append(f"{'charge':<11}" + "  ".join(f"{c:.2f}" for c in m["charge"]))
    if m["temperature"] is not None:
        lignes.append(f"{'CPU':<11}{m['temperature']:.1f} °C")
    if m["uptime"]:
        lignes.append(f"{'uptime':<11}{m['uptime']}")
    if m["workspaces"] is not None:
        lignes.append(f"{'workspaces':<11}{octets(m['workspaces']):>7}")

    def etats(units: dict[str, str], prefixe: str) -> str:
        return "  ".join(
            f"{u.removeprefix(prefixe).removesuffix('.timer')}"
            f"{' ✅' if e == 'active' else ' ❌ ' + e}"
            for u, e in units.items())

    corps = "\n".join(lignes)
    return (f"🩺 **Santé du Pi**\n```\n{corps}\n```\n"
            f"services : {etats(m['services'], 'orchestrator-')}\n"
            f"timers : {etats(m['timers'], 'orchestrator-')}")


async def surveiller() -> str:
    """Un tour de surveillance disque. Renvoie ce qui a été fait (pour les logs)."""
    m = mesurer()
    d = m["disque"]
    pct = d["pct"]
    palier = _palier(pct)
    memo = state.meta_lire(CLE_PALIER)
    dernier = int(memo) if memo else None

    if palier is None:
        if dernier is not None:
            state.meta_effacer(CLE_PALIER)
            await notify.notify(
                f"✅ Disque revenu sous {seuil()}% — à {pct}% "
                f"({octets(d['libre'])} libres).")
            return f"retour à la normale notifié ({pct}%)"
        return f"disque à {pct}% — sous le seuil de {seuil()}%"

    if dernier is not None and palier <= dernier:
        return f"disque à {pct}% — palier {palier}% déjà alerté, pas de spam"

    state.meta_ecrire(CLE_PALIER, str(palier))
    ws = m["workspaces"]
    piste = f"\nstate/workspaces pèse {octets(ws)}." if ws else ""
    await notify.notify(
        f"🔴 Disque à {pct}% (palier {palier}%) — "
        f"{octets(d['utilise'])} utilisés sur {octets(d['utilisable'])}, "
        f"{octets(d['libre'])} libres.{piste}\n"
        f"`@bot santé` pour le détail.")
    return f"alerte palier {palier}% envoyée ({pct}%)"


async def handle() -> str:
    """Point d'entrée du pipeline (timer systemd)."""
    return await surveiller()
