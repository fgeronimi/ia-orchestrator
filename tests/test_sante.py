"""Surveillance machine : seuil, paliers d'escalade et anti-spam.

Lancer : python3 -m unittest tests.test_sante -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lib import state
from pipelines import sante


def _disque(pct: float) -> dict:
    """Jeu de mesures minimal, avec un disque au pourcentage voulu.

    `utilisable` = utilise + libre, comme dans mesurer() : c'est la base du
    pourcentage (df), la réserve root étant exclue.
    """
    utilisable = 14 * 1024**3
    utilise = int(utilisable * pct / 100)
    return {
        "disque": {"total": 15 * 1024**3, "utilisable": utilisable,
                   "utilise": utilise, "libre": utilisable - utilise,
                   "pct": pct},
        "memoire": None, "charge": None, "temperature": None,
        "uptime": None, "workspaces": 0,
        "services": {}, "timers": {},
    }


class SeuilTest(unittest.TestCase):
    def test_defaut_sans_variable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sante.seuil(), 80)

    def test_surcharge_par_env(self):
        with patch.dict(os.environ, {"SEUIL_DISQUE": "70"}):
            self.assertEqual(sante.seuil(), 70)

    def test_valeur_absurde_bornee(self):
        with patch.dict(os.environ, {"SEUIL_DISQUE": "150"}):
            self.assertEqual(sante.seuil(), 99)
        with patch.dict(os.environ, {"SEUIL_DISQUE": "3"}):
            self.assertEqual(sante.seuil(), 50)

    def test_valeur_non_numerique_retombe_sur_le_defaut(self):
        with patch.dict(os.environ, {"SEUIL_DISQUE": "beaucoup"}):
            self.assertEqual(sante.seuil(), 80)


class PalierTest(unittest.TestCase):
    def test_sous_le_seuil(self):
        with patch.dict(os.environ, {"SEUIL_DISQUE": "80"}):
            self.assertIsNone(sante._palier(66.0))
            self.assertIsNone(sante._palier(79.9))

    def test_paliers_successifs(self):
        with patch.dict(os.environ, {"SEUIL_DISQUE": "80"}):
            self.assertEqual(sante._palier(80.0), 80)
            self.assertEqual(sante._palier(89.9), 80)
            self.assertEqual(sante._palier(90.0), 90)
            self.assertEqual(sante._palier(96.0), 95)

    def test_seuil_haut_ne_cree_pas_de_palier_sous_lui(self):
        """Avec SEUIL_DISQUE=95, un disque à 91% ne doit PAS alerter."""
        with patch.dict(os.environ, {"SEUIL_DISQUE": "95"}):
            self.assertIsNone(sante._palier(91.0))
            self.assertEqual(sante._palier(95.0), 95)


class SurveillerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state.DB = Path(self._tmp.name) / "orchestrator.db"
        self._env = patch.dict(os.environ, {"SEUIL_DISQUE": "80"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def test_sous_le_seuil_aucune_notif(self):
        with patch("pipelines.sante.mesurer", return_value=_disque(66.0)), \
             patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            await sante.surveiller()
        mock.assert_not_awaited()

    async def test_franchissement_du_seuil_alerte_une_seule_fois(self):
        with patch("pipelines.sante.mesurer", return_value=_disque(81.0)), \
             patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            await sante.surveiller()
            self.assertEqual(mock.await_count, 1)
            self.assertIn("81", mock.await_args.args[0])
            # deuxième tour dans le même palier : silence
            await sante.surveiller()
            await sante.surveiller()
            self.assertEqual(mock.await_count, 1)

    async def test_aggravation_realerte(self):
        with patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            with patch("pipelines.sante.mesurer", return_value=_disque(81.0)):
                await sante.surveiller()
            with patch("pipelines.sante.mesurer", return_value=_disque(92.0)):
                await sante.surveiller()
            self.assertEqual(mock.await_count, 2)
            self.assertIn("90", mock.await_args.args[0])

    async def test_amelioration_dans_la_zone_ne_realerte_pas(self):
        """Redescendre de 92% à 85% ne doit pas renvoyer d'alerte."""
        with patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            with patch("pipelines.sante.mesurer", return_value=_disque(92.0)):
                await sante.surveiller()
            with patch("pipelines.sante.mesurer", return_value=_disque(85.0)):
                await sante.surveiller()
            self.assertEqual(mock.await_count, 1)

    async def test_retour_sous_le_seuil_notifie_puis_se_taît(self):
        with patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            with patch("pipelines.sante.mesurer", return_value=_disque(81.0)):
                await sante.surveiller()
            with patch("pipelines.sante.mesurer", return_value=_disque(60.0)):
                await sante.surveiller()
                self.assertEqual(mock.await_count, 2)
                self.assertIn("✅", mock.await_args.args[0])
                # la mémoire est effacée : plus rien ensuite
                await sante.surveiller()
                self.assertEqual(mock.await_count, 2)

    async def test_reprise_apres_retour_a_la_normale(self):
        """Après un cycle complet, un nouveau franchissement realerte bien."""
        with patch("pipelines.sante.notify.notify", new_callable=AsyncMock) as mock:
            for pct in (81.0, 60.0, 82.0):
                with patch("pipelines.sante.mesurer", return_value=_disque(pct)):
                    await sante.surveiller()
            self.assertEqual(mock.await_count, 3)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        # Seuil explicite : le rendu du ⚠️ en dépend.
        self._env = patch.dict(os.environ, {"SEUIL_DISQUE": "80"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_resume_lisible_avec_mesures_partielles(self):
        """Hors Pi, /proc et /sys manquent : le résumé ne doit pas casser."""
        with patch("pipelines.sante.mesurer", return_value=_disque(66.0)):
            texte = sante.resume()
        self.assertIn("Santé du Pi", texte)
        self.assertIn("66", texte)
        self.assertIn("disque", texte)

    def test_resume_complet(self):
        mesures = _disque(85.0) | {
            "memoire": {"total": 4 * 1024**3, "utilise": 1024**3, "pct": 25.0,
                        "swap_total": 2 * 1024**3, "swap_utilise": 52 * 1024**2},
            "charge": (0.01, 0.05, 0.1),
            "temperature": 47.2,
            "uptime": "9j 09h38",
            "workspaces": 412 * 1024**2,
            "services": {"orchestrator-bot": "active",
                         "orchestrator-server": "failed"},
            "timers": {"orchestrator-poll.timer": "active"},
        }
        with patch("pipelines.sante.mesurer", return_value=mesures):
            texte = sante.resume()
        self.assertIn("47.2 °C", texte)
        self.assertIn("9j 09h38", texte)
        self.assertIn("bot ✅", texte)
        self.assertIn("server ❌", texte)  # un service mort se voit
        self.assertIn("⚠️", texte)          # disque à 85% avec seuil à 80

    def test_octets_lisibles(self):
        self.assertEqual(sante._octets(0), "0o")
        self.assertEqual(sante._octets(512), "512o")
        self.assertEqual(sante._octets(1024), "1.0K")
        self.assertEqual(sante._octets(15 * 1024**3), "15G")


if __name__ == "__main__":
    unittest.main()
