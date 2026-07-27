"""Tests de pipelines.dev_triage — API GitHub et Claude mockées (pas de réseau).

Lancer : python3 -m unittest tests.test_dev_triage -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lib import state
from lib.claude import ClaudeQuotaError, ResultatClaude
from pipelines import dev_triage

REPO = "acme/toto"

ISSUE = {"number": 1, "title": "Ajoute un bouton", "labels": [], "url": "u", "user": "acme"}

ANALYSE_CLAIRE = {
    "clair": True,
    "resume": "Ajouter un bouton dans la barre d'outils.",
    "complexite": "S",
    "modele_suggere": "haiku",
    "fichiers_probables": ["src/toolbar.py"],
    "questions": [],
}

ANALYSE_FLOUE = {
    "clair": False,
    "resume": "Le ticket ne précise pas où poser le bouton.",
    "complexite": "M",
    "modele_suggere": "sonnet",
    "fichiers_probables": [],
    "questions": ["Dans quel écran ajouter le bouton ?"],
}


def _resultat(texte: str) -> ResultatClaude:
    return ResultatClaude(texte=texte, tokens_entree=10, tokens_cache=0,
                          tokens_sortie=5, cout_usd=0.001)


class DevTriageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        state.DB = tmp / "orchestrator.db"
        dev_triage.MODELES_YAML = tmp / "modeles.yaml"  # absent = pas de défaut

        self._patches = [
            patch("pipelines.dev_triage.github.get_issue",
                 return_value={**ISSUE, "body": "Corps du ticket."}),
            patch("pipelines.dev_triage.github.add_labels"),
            patch("pipelines.dev_triage.github.comment_issue"),
            patch("pipelines.dev_triage.notify.notify", new_callable=AsyncMock),
        ]
        (self.mock_get_issue, self.mock_add_labels,
         self.mock_comment, self.mock_notify) = (p.start() for p in self._patches)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    # --- trier() : parse + effets -----------------------------------------

    async def test_ticket_clair_labels_size_et_model_plus_commentaire(self):
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_CLAIRE)))):
            await dev_triage.trier(REPO, ISSUE)

        self.mock_add_labels.assert_called_once_with(REPO, 1, ["size:S", "model:haiku"])
        corps = self.mock_comment.call_args.args[2]
        self.assertIn("Ajouter un bouton", corps)
        self.assertIn("src/toolbar.py", corps)
        self.mock_notify.assert_awaited_once()
        self.assertTrue(state.issue_deja_triee(REPO, 1))

    async def test_ticket_flou_ajoute_triage_questions_et_questions_en_commentaire(self):
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_FLOUE)))):
            await dev_triage.trier(REPO, ISSUE)

        self.mock_add_labels.assert_called_once_with(
            REPO, 1, ["size:M", "model:sonnet", "triage:questions"])
        corps = self.mock_comment.call_args.args[2]
        self.assertIn("Dans quel écran", corps)

    async def test_jamais_de_label_ai_ready_pose(self):
        for analyse in (ANALYSE_CLAIRE, ANALYSE_FLOUE):
            self.mock_add_labels.reset_mock()
            with patch("pipelines.dev_triage.run_claude",
                       new=AsyncMock(return_value=_resultat(json.dumps(analyse)))):
                await dev_triage.trier(REPO, ISSUE)
            labels_poses = self.mock_add_labels.call_args.args[2]
            self.assertNotIn("ai-ready", labels_poses)
            self.assertNotIn("ai-working", labels_poses)

    async def test_json_invalide_ignore_sans_commentaire_mais_marque_triee(self):
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat("n'importe quoi, pas du JSON"))):
            await dev_triage.trier(REPO, ISSUE)

        self.mock_add_labels.assert_not_called()
        self.mock_comment.assert_not_called()
        self.assertTrue(state.issue_deja_triee(REPO, 1))

    async def test_champ_manquant_traite_comme_invalide(self):
        incomplet = dict(ANALYSE_CLAIRE)
        del incomplet["fichiers_probables"]
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(incomplet)))):
            await dev_triage.trier(REPO, ISSUE)

        self.mock_add_labels.assert_not_called()

    async def test_trop_de_questions_traite_comme_invalide(self):
        trop = dict(ANALYSE_FLOUE, questions=["a", "b", "c", "d"])
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(trop)))):
            await dev_triage.trier(REPO, ISSUE)

        self.mock_add_labels.assert_not_called()

    async def test_quota_epuise_ne_marque_pas_triee(self):
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(side_effect=ClaudeQuotaError("quota épuisé"))):
            await dev_triage.trier(REPO, ISSUE)

        self.assertFalse(state.issue_deja_triee(REPO, 1))
        self.mock_add_labels.assert_not_called()
        self.mock_notify.assert_awaited_once()

    # --- trier_nouveaux() : sélection des candidats -------------------------

    async def test_owner_only_ignore_les_issues_de_tiers(self):
        issue_tiers = {**ISSUE, "user": "quelqu-un-d-autre"}
        with patch("pipelines.dev_triage.github.list_issues", return_value=[issue_tiers]), \
             patch("pipelines.dev_triage.trier", new=AsyncMock()) as mock_trier:
            await dev_triage.trier_nouveaux(REPO)
        mock_trier.assert_not_called()

    async def test_ignore_les_issues_de_la_forge(self):
        issue_forge = {**ISSUE, "title": "forge: label manquant : ai-ready"}
        with patch("pipelines.dev_triage.github.list_issues", return_value=[issue_forge]), \
             patch("pipelines.dev_triage.trier", new=AsyncMock()) as mock_trier:
            await dev_triage.trier_nouveaux(REPO)
        mock_trier.assert_not_called()

    async def test_ignore_les_issues_deja_ai_ready_ou_ai_working(self):
        issue_ready = {**ISSUE, "number": 2, "labels": ["ai-ready"]}
        issue_working = {**ISSUE, "number": 3, "labels": ["ai-working"]}
        with patch("pipelines.dev_triage.github.list_issues",
                   return_value=[issue_ready, issue_working]), \
             patch("pipelines.dev_triage.trier", new=AsyncMock()) as mock_trier:
            await dev_triage.trier_nouveaux(REPO)
        mock_trier.assert_not_called()

    async def test_ignore_les_issues_deja_triees(self):
        state.marquer_issue_triee(REPO, 1)
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE]), \
             patch("pipelines.dev_triage.trier", new=AsyncMock()) as mock_trier:
            await dev_triage.trier_nouveaux(REPO)
        mock_trier.assert_not_called()

    async def test_candidat_valide_est_triee(self):
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE]), \
             patch("pipelines.dev_triage.trier", new=AsyncMock()) as mock_trier:
            await dev_triage.trier_nouveaux(REPO)
        mock_trier.assert_awaited_once_with(REPO, ISSUE)

    async def test_quota_bloque_saute_le_triage_sans_lister_les_issues(self):
        state.bloquer_quota(9_999_999_999)
        with patch("pipelines.dev_triage.github.list_issues") as mock_list:
            await dev_triage.trier_nouveaux(REPO)
        mock_list.assert_not_called()


if __name__ == "__main__":
    unittest.main()
