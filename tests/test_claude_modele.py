"""Tests de lib.claude : flag --model transmis au CLI, liste blanche des
labels `model:<alias>`.

Lancer : python3 -m unittest tests.test_claude_modele -v
"""

import json
import unittest
from unittest.mock import AsyncMock, patch

from lib import claude


def _proc(resultat: dict):
    proc = AsyncMock()
    proc.communicate.return_value = (json.dumps(resultat).encode(), b"")
    proc.returncode = 0
    return proc


class RunClaudeModeleTest(unittest.IsolatedAsyncioTestCase):
    async def test_sans_modele_pas_de_flag(self):
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}), \
             patch("lib.claude.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = _proc({"result": "ok", "usage": {}})
            await claude.run_claude("prompt")

        cmd = mock_exec.call_args.args
        self.assertNotIn("--model", cmd)

    async def test_avec_modele_passe_le_flag(self):
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}), \
             patch("lib.claude.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = _proc({"result": "ok", "usage": {}})
            await claude.run_claude("prompt", model="haiku")

        cmd = mock_exec.call_args.args
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")


class ModeleDepuisLabelTest(unittest.TestCase):
    def test_alias_valide(self):
        self.assertEqual(claude.modele_depuis_label(["ai-ready", "model:haiku"]), "haiku")

    def test_alias_hors_liste_blanche_ignore(self):
        self.assertIsNone(claude.modele_depuis_label(["model:gpt4"]))

    def test_aucun_label_modele(self):
        self.assertIsNone(claude.modele_depuis_label(["ai-ready"]))

    def test_alias_normalise_en_minuscules(self):
        self.assertEqual(claude.modele_depuis_label(["model:OPUS"]), "opus")


if __name__ == "__main__":
    unittest.main()
