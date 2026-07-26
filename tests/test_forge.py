"""Tests de pipelines.forge — API GitHub mockée (pas de réseau).

Lancer : python3 -m unittest tests.test_forge -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from lib import state
from pipelines import forge

CONDITIONS_V1 = {
    "version": 1,
    "labels": ["ai-ready"],
    "fichiers": ["CLAUDE.md"],
    "protection_main": True,
}

RULESET_INACTIF = [{"id": 1, "name": "main", "enforcement": "disabled"}]
RULESET_ACTIF_PR = [{"id": 1, "name": "main", "enforcement": "active"}]
DETAIL_RULESET_PR = {
    "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
    "rules": [{"type": "pull_request"}],
}
DETAIL_RULESET_SANS_PR = {
    "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
    "rules": [{"type": "deletion"}],
}


class ForgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        state.DB = tmp / "orchestrator.db"

        forge.REPOS_YAML = tmp / "repos.yaml"
        forge.REPOS_YAML.write_text(yaml.dump({"repos": ["acme/toto"]}))

        forge.FORGE_YAML = tmp / "forge.yaml"
        self._ecrire_conditions(CONDITIONS_V1)

        self._patches = [
            patch("lib.github.get_default_branch", return_value="main"),
            patch("lib.github.create_issue"),
            patch("lib.notify.notify", new_callable=AsyncMock),
        ]
        self.mock_default_branch, self.mock_create_issue, self.mock_notify = (
            p.start() for p in self._patches
        )

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _ecrire_conditions(self, conditions: dict) -> None:
        forge.FORGE_YAML.write_text(yaml.dump(conditions))

    def _conforme(self):
        """Patches faisant apparaître le repo comme entièrement conforme."""
        return [
            patch("lib.github.list_labels", return_value=["ai-ready"]),
            patch("lib.github.fichier_existe", return_value=True),
            patch("lib.github.list_rulesets", return_value=RULESET_ACTIF_PR),
            patch("lib.github.get_ruleset", return_value=DETAIL_RULESET_PR),
        ]

    async def test_conditions_remplies_aucun_ticket(self):
        patches = self._conforme()
        for p in patches:
            p.start()
        try:
            resultat = await forge.handle()
        finally:
            for p in patches:
                p.stop()

        self.mock_create_issue.assert_not_called()
        self.mock_notify.assert_not_called()
        self.assertIn("aucun écart", resultat)

    async def test_label_manquant_cree_un_ticket_sans_doublon(self):
        with patch("lib.github.list_labels", return_value=[]), \
             patch("lib.github.fichier_existe", return_value=True), \
             patch("lib.github.list_rulesets", return_value=RULESET_ACTIF_PR), \
             patch("lib.github.get_ruleset", return_value=DETAIL_RULESET_PR):
            resultat = await forge.handle()

            self.mock_create_issue.assert_called_once()
            args, kwargs = self.mock_create_issue.call_args
            self.assertEqual(args[0], "acme/toto")
            self.assertIn("forge:", kwargs["title"])
            self.assertIn("ai-ready", kwargs["title"])
            self.assertNotIn("labels", kwargs)  # jamais de label sur un ticket forge
            self.mock_notify.assert_awaited_once()
            self.assertIn("1 écart", resultat)

            # second passage, même version : pas de doublon.
            self.mock_create_issue.reset_mock()
            self.mock_notify.reset_mock()
            resultat2 = await forge.handle()

            self.mock_create_issue.assert_not_called()
            self.mock_notify.assert_not_called()
            self.assertIn("aucun écart", resultat2)

    async def test_protection_main_absente_signalee(self):
        with patch("lib.github.list_labels", return_value=["ai-ready"]), \
             patch("lib.github.fichier_existe", return_value=True), \
             patch("lib.github.list_rulesets", return_value=RULESET_INACTIF), \
             patch("lib.github.get_ruleset", return_value=DETAIL_RULESET_PR):
            resultat = await forge.handle()

        self.mock_create_issue.assert_called_once()
        _, kwargs = self.mock_create_issue.call_args
        self.assertIn("protection_main", kwargs["title"])
        self.assertIn("1 écart", resultat)

    async def test_version_incrementee_relance_le_signalement(self):
        with patch("lib.github.list_labels", return_value=[]), \
             patch("lib.github.fichier_existe", return_value=True), \
             patch("lib.github.list_rulesets", return_value=RULESET_ACTIF_PR), \
             patch("lib.github.get_ruleset", return_value=DETAIL_RULESET_PR):
            await forge.handle()
            self.mock_create_issue.assert_called_once()

            self.mock_create_issue.reset_mock()
            self.mock_notify.reset_mock()

            conditions_v2 = dict(CONDITIONS_V1, version=2)
            self._ecrire_conditions(conditions_v2)

            resultat = await forge.handle()

        self.mock_create_issue.assert_called_once()
        self.mock_notify.assert_awaited_once()
        self.assertIn("1 écart", resultat)


if __name__ == "__main__":
    unittest.main()
