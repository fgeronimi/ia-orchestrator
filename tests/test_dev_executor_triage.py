"""Tests de l'injection de l'analyse de triage dans le prompt d'exécution (#26)."""

import unittest
from unittest.mock import patch

from pipelines import dev_executor

REPO = "acme/toto"


class AnalyseTriageTest(unittest.TestCase):
    def test_derniere_analyse_triage_dans_le_bloc(self):
        commentaires = [
            {"id": 1, "body": "🤖 **Raffinement** — taille S\n\nPremière analyse.", "user": "acme"},
            {"id": 2, "body": "un vrai commentaire humain", "user": "acme"},
            {"id": 3, "body": "🤖 **Clarification** — prêt\n\nAnalyse à jour.", "user": "acme"},
        ]
        with patch("pipelines.dev_executor.github.list_comments", return_value=commentaires):
            bloc = dev_executor._analyse_triage(REPO, 1)

        self.assertIn("Analyse à jour.", bloc)          # la DERNIÈRE analyse
        self.assertNotIn("Première analyse.", bloc)
        self.assertIn("hypothèses", bloc)               # jamais présentée comme des faits
        self.assertIn("VÉRIFIER", bloc)

    def test_sans_analyse_chaine_vide(self):
        commentaires = [
            {"id": 1, "body": "🤖 PR ouverte : https://…", "user": "acme"},  # bot, pas une analyse
            {"id": 2, "body": "commentaire humain", "user": "acme"},
        ]
        with patch("pipelines.dev_executor.github.list_comments", return_value=commentaires):
            self.assertEqual(dev_executor._analyse_triage(REPO, 1), "")

    def test_echec_github_ne_bloque_jamais(self):
        with patch("pipelines.dev_executor.github.list_comments",
                   side_effect=RuntimeError("boom")):
            self.assertEqual(dev_executor._analyse_triage(REPO, 1), "")

    def test_prompt_impl_accepte_le_bloc(self):
        rendu = dev_executor.PROMPT_IMPL.format(
            n=1, titre="t", corps="c", analyse="\nAnalyse préalable…\n")
        self.assertIn("Analyse préalable…", rendu)
        rendu_sans = dev_executor.PROMPT_IMPL.format(n=1, titre="t", corps="c", analyse="")
        self.assertNotIn("Analyse préalable", rendu_sans)


if __name__ == "__main__":
    unittest.main()
