"""Tests de la détection des ai-working orphelins (#33) — GitHub mocké.

Lancer : python3 -m unittest tests.test_followup_orphelins -v
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lib import state
from pipelines import dev_followup

REPO = "acme/toto"


def _il_y_a(secondes: int) -> str:
    quand = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=secondes)
    return quand.strftime("%Y-%m-%dT%H:%M:%SZ")


class OrphelinsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state.DB = Path(self._tmp.name) / "orchestrator.db"

        self._patches = [
            patch("pipelines.dev_followup.github.list_issues",
                  return_value=[{"number": 7, "title": "t", "labels": ["ai-working"],
                                 "url": "u", "user": "acme"}]),
            patch("pipelines.dev_followup.github.find_open_pull", return_value=None),
            patch("pipelines.dev_followup.github.label_pose_le",
                  return_value=_il_y_a(2 * 3600)),
            patch("pipelines.dev_followup.github.add_labels"),
            patch("pipelines.dev_followup.github.remove_label"),
            patch("pipelines.dev_followup.github.comment_issue"),
            patch("pipelines.dev_followup.notify.notify", new_callable=AsyncMock),
        ]
        (self.mock_issues, self.mock_pull, self.mock_pose_le, self.mock_add,
         self.mock_remove, self.mock_comment, self.mock_notify) = (
            p.start() for p in self._patches)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    async def test_orphelin_remis_en_file(self):
        await dev_followup.remettre_orphelins_en_file(REPO)

        self.mock_remove.assert_called_once_with(REPO, 7, "ai-working")
        self.mock_add.assert_called_once_with(REPO, 7, ["ai-ready"])
        self.assertIn("remis en file", self.mock_comment.call_args.args[2])
        self.mock_notify.assert_awaited_once()

    async def test_avec_pr_ouverte_jamais_touche(self):
        self.mock_pull.return_value = {"number": 12}
        await dev_followup.remettre_orphelins_en_file(REPO)

        self.mock_remove.assert_not_called()
        self.mock_add.assert_not_called()

    async def test_plus_recent_que_le_seuil_ignore(self):
        self.mock_pose_le.return_value = _il_y_a(10 * 60)  # 10 min : exécution en cours
        await dev_followup.remettre_orphelins_en_file(REPO)

        self.mock_remove.assert_not_called()

    async def test_sans_horodatage_ne_rien_decider(self):
        self.mock_pose_le.return_value = None
        await dev_followup.remettre_orphelins_en_file(REPO)

        self.mock_remove.assert_not_called()

    async def test_dedup_meme_incident_signale_une_fois(self):
        await dev_followup.remettre_orphelins_en_file(REPO)
        self.mock_add.reset_mock()
        self.mock_remove.reset_mock()

        await dev_followup.remettre_orphelins_en_file(REPO)  # même horodatage

        self.mock_add.assert_not_called()
        self.mock_remove.assert_not_called()

    async def test_deuxieme_crash_pose_ai_failed(self):
        await dev_followup.remettre_orphelins_en_file(REPO)  # 1er incident
        self.mock_add.reset_mock()

        # nouveau crash : l'exécutant a repris le ticket (nouvelle pose du
        # label, plus vieille que le seuil) puis est mort à nouveau
        self.mock_pose_le.return_value = _il_y_a(3 * 3600 + 1)
        await dev_followup.remettre_orphelins_en_file(REPO)

        self.mock_add.assert_called_once_with(REPO, 7, ["ai-failed"])
        self.assertIn("seconde fois", self.mock_comment.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
