"""Le tour léger d'un repo en échec ne prive pas les autres de leur tour.

Lancer : python3 -m unittest tests.test_poll_isolation -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import poll
from lib import state


class PollIsolationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state.DB = Path(self._tmp.name) / "orchestrator.db"

    def tearDown(self):
        self._tmp.cleanup()

    async def test_un_repo_en_echec_ne_bloque_pas_les_suivants(self):
        vus = []

        async def tour(repo):
            vus.append(repo)
            if repo == "acme/casse":
                raise RuntimeError("hoquet GitHub")
            return []

        with patch("poll._tour_leger", side_effect=tour), \
             patch("poll.notify.notify", new_callable=AsyncMock) as mock_notify, \
             patch("poll.state.quota_bloque_jusqua", return_value=9_999_999_999):
            await poll.poll([{"repo": "acme/casse", "timeout": None},
                             {"repo": "acme/toto", "timeout": None}])

        # les deux repos ont eu leur tour malgré le crash du premier
        self.assertEqual(vus, ["acme/casse", "acme/toto"])
        # une info envoyée, nommant le repo sauté et le type d'erreur
        message = mock_notify.await_args.args[0]
        self.assertIn("acme/casse", message)
        self.assertIn("RuntimeError", message)

    async def test_sans_echec_aucune_notif_de_saut(self):
        with patch("poll._tour_leger", new=AsyncMock(return_value=[])), \
             patch("poll.notify.notify", new_callable=AsyncMock) as mock_notify, \
             patch("poll.state.quota_bloque_jusqua", return_value=9_999_999_999):
            await poll.poll([{"repo": "acme/toto", "timeout": None}])

        mock_notify.assert_not_awaited()

    async def test_hoquet_dans_la_chaine_de_priorite_ne_tue_pas_le_tour(self):
        """Régression du 2026-07-30 02:03 : un ReadTimeout GitHub dans
        `chercher_revision` faisait échouer le service entier."""
        vus = []

        def chercher_revision(repo):
            vus.append(repo)
            if repo == "acme/casse":
                raise TimeoutError("Read timed out")
            return None

        with patch("poll._tour_leger", new=AsyncMock(return_value=[])), \
             patch("poll.state.quota_bloque_jusqua", return_value=None), \
             patch("poll.notify.notify", new_callable=AsyncMock) as mock_notify, \
             patch("poll.dev_executor.chercher_revision", side_effect=chercher_revision), \
             patch("poll.dev_followup.chercher_review_demandee", return_value=None), \
             patch("poll.dev_followup.chercher_conflit", return_value=None), \
             patch("poll.dev_followup.chercher_ci_rouge", new=AsyncMock(return_value=None)):
            # ne doit pas lever : c'est tout l'objet du correctif
            await poll.poll([{"repo": "acme/casse", "timeout": None},
                             {"repo": "acme/toto", "timeout": None}])

        # le repo suivant a bien été interrogé malgré le timeout du premier
        self.assertEqual(vus, ["acme/casse", "acme/toto"])
        message = mock_notify.await_args.args[0]
        self.assertIn("acme/casse", message)
        self.assertIn("TimeoutError", message)

    async def test_chaine_de_priorite_trouve_et_deballe_la_revision(self):
        """La forme (entrée, trouvé) de _premier reste correctement déballée."""
        pr = {"number": 7}
        commentaires = [{"body": "revois ça"}]

        with patch("poll._tour_leger", new=AsyncMock(return_value=[])), \
             patch("poll.state.quota_bloque_jusqua", return_value=None), \
             patch("poll.notify.notify", new_callable=AsyncMock), \
             patch("poll.dev_executor.chercher_revision",
                   return_value=(pr, commentaires)), \
             patch("poll.dev_executor.reviser", new_callable=AsyncMock) as mock_reviser:
            await poll.poll([{"repo": "acme/toto", "timeout": 900}])

        mock_reviser.assert_awaited_once_with("acme/toto", pr, commentaires,
                                             timeout=900)


if __name__ == "__main__":
    unittest.main()
