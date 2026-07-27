"""Tests de pipelines.dev_executor._modele : défauts data/modeles.yaml, et
priorité du label `model:<alias>` du ticket sur ces défauts.

Lancer : python3 -m unittest tests.test_dev_executor_modele -v
"""

import tempfile
import unittest
from pathlib import Path

import yaml

from pipelines import dev_executor


class ModeleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._yaml_originale = dev_executor.MODELES_YAML
        dev_executor.MODELES_YAML = Path(self._tmp.name) / "modeles.yaml"

    def tearDown(self):
        dev_executor.MODELES_YAML = self._yaml_originale
        self._tmp.cleanup()

    def test_yaml_absent_aucun_defaut(self):
        self.assertIsNone(dev_executor._modele("executer", []))

    def test_yaml_vide_pour_l_etape_aucun_defaut(self):
        dev_executor.MODELES_YAML.write_text(yaml.dump({"executer": None}))
        self.assertIsNone(dev_executor._modele("executer", []))

    def test_defaut_yaml_applique(self):
        dev_executor.MODELES_YAML.write_text(yaml.dump({"executer": "sonnet"}))
        self.assertEqual(dev_executor._modele("executer", []), "sonnet")

    def test_label_surclasse_le_defaut_yaml(self):
        dev_executor.MODELES_YAML.write_text(yaml.dump({"executer": "sonnet"}))
        self.assertEqual(dev_executor._modele("executer", ["model:haiku"]), "haiku")

    def test_label_invalide_retombe_sur_le_defaut_yaml(self):
        dev_executor.MODELES_YAML.write_text(yaml.dump({"executer": "sonnet"}))
        self.assertEqual(dev_executor._modele("executer", ["model:gpt4"]), "sonnet")


if __name__ == "__main__":
    unittest.main()
