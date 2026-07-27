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


if __name__ == "__main__":
    unittest.main()
