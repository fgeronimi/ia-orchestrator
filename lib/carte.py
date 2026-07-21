"""Carte des restos — page HTML autonome (Leaflet + tuiles OpenStreetMap).

Générée à la demande depuis le store, donc toujours à jour : `server.py` se
contente de servir le HTML sur GET /carte. Consultable depuis l'iPhone via
Tailscale.

Les restos sans coordonnées sont ignorés (voir lib/geo, géocodage best-effort).
"""

import json

from lib import restos

# Centre par défaut si aucun resto n'est géocodé : Paris.
CENTRE_DEFAUT = (48.8566, 2.3522)

_GABARIT = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Carte des restos</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; }}
  #carte {{ height: 100%; }}
  .leaflet-control-layers {{ font: 14px/1.5 system-ui, sans-serif; }}
  .pastille {{ display: inline-block; width: 10px; height: 10px;
               border-radius: 50%; margin: 0 5px 0 2px; }}
</style>
</head>
<body>
<div id="carte"></div>
<script>
const RESTOS = {donnees};

const carte = L.map('carte').setView({centre}, {zoom});
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap'
}}).addTo(carte);

const echapper = (t) => {{
  const d = document.createElement('div');
  d.textContent = t == null ? '' : String(t);
  return d.innerHTML;
}};

const pastille = (couleur) => L.divIcon({{
  className: '',
  html: `<div style="background:${{couleur}};width:14px;height:14px;
         border-radius:50%;border:2px solid #fff;
         box-shadow:0 0 3px rgba(0,0,0,.5)"></div>`,
  iconSize: [14, 14],
  iconAnchor: [7, 7],
}});

const groupes = {{ a_faire: L.layerGroup(), fait: L.layerGroup() }};

for (const r of RESTOS) {{
  const fait = r.statut === 'fait';
  const lignes = [`<b>${{echapper(r.nom)}}</b>`];
  if (r.adresse) lignes.push(echapper(r.adresse));
  if (r.tags && r.tags.length) lignes.push(`<i>${{echapper(r.tags.join(', '))}}</i>`);
  if (fait && r.avis) lignes.push(`✅ ${{echapper(r.avis)}}`);
  else if (r.note) lignes.push(echapper(r.note));

  L.marker([r.lat, r.lon], {{ icon: pastille(fait ? '#2b8a3e' : '#e8590c') }})
    .bindPopup(lignes.join('<br>'))
    .addTo(groupes[fait ? 'fait' : 'a_faire']);
}}

groupes.a_faire.addTo(carte);
groupes.fait.addTo(carte);

// Compteurs et filtres dans le même bloc : deux encarts superposés en haut à
// droite se masquaient l'un l'autre.
const etiquette = (couleur, texte, n) =>
  `<span class="pastille" style="background:${{couleur}}"></span>${{texte}} (${{n}})`;

L.control.layers(null, {{
  [etiquette('#e8590c', 'À faire', {nb_a_faire})]: groupes.a_faire,
  [etiquette('#2b8a3e', 'Déjà faits', {nb_faits})]: groupes.fait,
}}, {{ collapsed: false }}).addTo(carte);
</script>
</body>
</html>
"""


def generer() -> str:
    """Retourne la page HTML complète de la carte."""
    situes = [r for r in restos.charger()
              if r.get("lat") is not None and r.get("lon") is not None]

    if situes:
        centre = (sum(r["lat"] for r in situes) / len(situes),
                  sum(r["lon"] for r in situes) / len(situes))
        zoom = 13 if len(situes) > 1 else 15
    else:
        centre, zoom = CENTRE_DEFAUT, 12

    return _GABARIT.format(
        donnees=json.dumps(situes, ensure_ascii=False),
        centre=json.dumps(list(centre)),
        zoom=zoom,
        nb_a_faire=sum(1 for r in situes if r.get("statut") != "fait"),
        nb_faits=sum(1 for r in situes if r.get("statut") == "fait"),
    )
