"""Tests de lib.workspace.diff_filtre sur un dépôt git réel en tmpdir.

Pas de dépendance à GitHub : diff_filtre ne fait que du git local, donc les
stubs sont de vrais petits dépôts git jetables (pas de mock de subprocess).
Lancer : python3 -m unittest tests.test_workspace_diff_filtre -v
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from lib import workspace


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


class DiffFiltreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        _git(self.path, "init", "-q", "-b", "main")
        _git(self.path, "config", "user.email", "test@example.com")
        _git(self.path, "config", "user.name", "Test")

        (self.path / "app.py").write_text("print('base')\n")
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", "base")

        _git(self.path, "checkout", "-q", "-b", "feature")

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_feature(self):
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", "feature")

    def test_petit_fichier_conserve(self):
        (self.path / "app.py").write_text("print('base')\nprint('feature')\n")
        self._commit_feature()

        diff, exclus = workspace.diff_filtre(self.path, "main")

        self.assertEqual(exclus, [])
        self.assertIn("app.py", diff)
        self.assertIn("print('feature')", diff)

    def test_fichier_volumineux_exclu_et_liste(self):
        (self.path / "data.txt").write_text("x" * 30_000)
        self._commit_feature()

        diff, exclus = workspace.diff_filtre(self.path, "main")

        self.assertEqual(len(exclus), 1)
        self.assertTrue(exclus[0].startswith("data.txt ("))
        self.assertIn("Ko)", exclus[0])
        self.assertNotIn("data.txt", diff)
        self.assertNotIn("x" * 100, diff)

    def test_fichier_petit_mais_extension_donnee_exclu(self):
        (self.path / "carte.geojson").write_text('{"type": "FeatureCollection"}')
        self._commit_feature()

        diff, exclus = workspace.diff_filtre(self.path, "main")

        self.assertEqual(len(exclus), 1)
        self.assertTrue(exclus[0].startswith("carte.geojson ("))
        self.assertNotIn("FeatureCollection", diff)

    def test_melange_fichier_conserve_et_fichier_exclu(self):
        (self.path / "app.py").write_text("print('base')\nprint('feature')\n")
        (self.path / "data.geojson").write_text("y" * 25_000)
        self._commit_feature()

        diff, exclus = workspace.diff_filtre(self.path, "main")

        self.assertEqual(len(exclus), 1)
        self.assertTrue(exclus[0].startswith("data.geojson ("))
        self.assertIn("print('feature')", diff)
        self.assertNotIn("y" * 100, diff)

    def test_seuil_personnalise(self):
        (self.path / "moyen.txt").write_text("z" * 500)
        self._commit_feature()

        diff, exclus = workspace.diff_filtre(self.path, "main", seuil=100)

        self.assertEqual(len(exclus), 1)
        self.assertTrue(exclus[0].startswith("moyen.txt ("))


if __name__ == "__main__":
    unittest.main()
