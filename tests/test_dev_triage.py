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

# --- Fixtures de la boucle de clarification (phase 2) -----------------------

ISSUE_QUESTIONS = {
    "number": 1, "title": "Ajoute un bouton", "url": "u", "user": "acme",
    "body": "Corps du ticket.", "labels": ["triage:questions", "size:M", "model:sonnet"],
}

COMMENT_OWNER = {"id": 501, "body": "Dans le header.", "user": "acme"}
COMMENT_BOT = {"id": 502, "body": "🤖 **Raffinement** — précisions nécessaires", "user": "acme"}
COMMENT_SLASH = {"id": 503, "body": "/ticket autre chose", "user": "acme"}
COMMENT_TIERS = {"id": 504, "body": "je réponds à ta place", "user": "quelqu-un-d-autre"}

ANALYSE_CLARIFIEE = {
    "clair": True,
    "resume": "Le bouton va dans le header.",
    "complexite": "S",
    "modele_suggere": "haiku",
    "fichiers_probables": ["src/header.py"],
    "questions": [],
}

ANALYSE_TOUJOURS_FLOUE = {
    "clair": False,
    "resume": "Toujours pas clair où précisément.",
    "complexite": "M",
    "modele_suggere": "sonnet",
    "fichiers_probables": [],
    "questions": ["À quel endroit exact dans le header ?"],
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
            patch("pipelines.dev_triage.github.remove_label"),
            patch("pipelines.dev_triage.github.comment_issue"),
            patch("pipelines.dev_triage.github.list_comments", return_value=[]),
            patch("pipelines.dev_triage.notify.notify", new_callable=AsyncMock),
        ]
        (self.mock_get_issue, self.mock_add_labels, self.mock_remove_label,
         self.mock_comment, self.mock_list_comments, self.mock_notify) = (
            p.start() for p in self._patches)

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

    # --- clarifier() : re-triage suite à une réponse du propriétaire -------

    async def test_questions_levees_retire_le_label_et_maj_size_model(self):
        self.mock_get_issue.return_value = dict(ISSUE_QUESTIONS)
        commentaires = [{**COMMENT_OWNER, "cle": "issue-501"}]
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_CLARIFIEE)))):
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, commentaires)

        self.mock_remove_label.assert_any_call(REPO, 1, dev_triage.LABEL_QUESTIONS)
        self.mock_remove_label.assert_any_call(REPO, 1, "size:M")
        self.mock_remove_label.assert_any_call(REPO, 1, "model:sonnet")
        self.mock_add_labels.assert_any_call(REPO, 1, ["size:S"])
        self.mock_add_labels.assert_any_call(REPO, 1, ["model:haiku"])
        corps = self.mock_comment.call_args.args[2]
        self.assertIn("prêt pour", corps)
        self.assertIn("header", corps)
        self.mock_notify.assert_awaited_once()
        self.assertTrue(state.commentaire_deja_vu(REPO, "issue-501"))

    async def test_questions_levees_sans_changement_estimation_ne_touche_pas_les_labels(self):
        self.mock_get_issue.return_value = dict(ISSUE_QUESTIONS, labels=["triage:questions", "size:S", "model:haiku"])
        commentaires = [{**COMMENT_OWNER, "cle": "issue-501"}]
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_CLARIFIEE)))):
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, commentaires)

        self.mock_remove_label.assert_called_once_with(REPO, 1, dev_triage.LABEL_QUESTIONS)
        self.mock_add_labels.assert_not_called()

    async def test_encore_flou_repose_des_questions_sans_atteindre_le_plafond(self):
        self.mock_get_issue.return_value = dict(ISSUE_QUESTIONS)
        commentaires = [{**COMMENT_OWNER, "cle": "issue-501"}]
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_TOUJOURS_FLOUE)))):
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, commentaires)

        self.mock_remove_label.assert_not_called()
        corps = self.mock_comment.call_args.args[2]
        self.assertIn("À quel endroit exact", corps)
        self.assertFalse(state.triage_epuise(REPO, 1))
        self.assertTrue(state.commentaire_deja_vu(REPO, "issue-501"))

    async def test_plafond_de_deux_tours_marque_epuise_et_reste_silencieux(self):
        self.mock_get_issue.return_value = dict(ISSUE_QUESTIONS)

        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(ANALYSE_TOUJOURS_FLOUE)))):
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, [{**COMMENT_OWNER, "cle": "issue-501"}])
            self.assertFalse(state.triage_epuise(REPO, 1))

            self.mock_comment.reset_mock()
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, [{**COMMENT_OWNER, "id": 505, "cle": "issue-505"}])

        self.assertTrue(state.triage_epuise(REPO, 1))
        self.mock_comment.assert_not_called()  # silence : pas de 3e tour de questions

    async def test_issue_deja_epuisee_ignore_sans_appeler_claude(self):
        state.marquer_triage_epuise(REPO, 1)
        commentaires = [{**COMMENT_OWNER, "cle": "issue-501"}]
        with patch("pipelines.dev_triage.run_claude", new=AsyncMock()) as mock_run_claude:
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, commentaires)

        mock_run_claude.assert_not_called()
        self.assertTrue(state.commentaire_deja_vu(REPO, "issue-501"))

    async def test_quota_epuise_ne_marque_pas_les_commentaires_vus(self):
        commentaires = [{**COMMENT_OWNER, "cle": "issue-501"}]
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(side_effect=ClaudeQuotaError("quota épuisé"))):
            await dev_triage.clarifier(REPO, ISSUE_QUESTIONS, commentaires)

        self.assertFalse(state.commentaire_deja_vu(REPO, "issue-501"))
        self.mock_notify.assert_awaited_once()

    # --- clarifier_nouveaux() : sélection des candidats ---------------------

    async def test_clarifier_nouveaux_owner_only_et_ignore_bot_et_commandes(self):
        self.mock_list_comments.return_value = [COMMENT_BOT, COMMENT_SLASH, COMMENT_TIERS]
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE_QUESTIONS]), \
             patch("pipelines.dev_triage.clarifier", new=AsyncMock()) as mock_clarifier:
            await dev_triage.clarifier_nouveaux(REPO)
        mock_clarifier.assert_not_called()

    async def test_clarifier_nouveaux_detecte_un_commentaire_neuf_du_proprietaire(self):
        self.mock_list_comments.return_value = [COMMENT_BOT, COMMENT_OWNER]
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE_QUESTIONS]), \
             patch("pipelines.dev_triage.clarifier", new=AsyncMock()) as mock_clarifier:
            await dev_triage.clarifier_nouveaux(REPO)

        mock_clarifier.assert_awaited_once()
        args = mock_clarifier.call_args.args
        self.assertEqual(args[0], REPO)
        self.assertEqual(args[1], ISSUE_QUESTIONS)
        self.assertEqual([c["id"] for c in args[2]], [501])

    async def test_clarifier_nouveaux_dedup_commentaire_deja_vu(self):
        state.marquer_commentaire(REPO, "issue-501")
        self.mock_list_comments.return_value = [COMMENT_OWNER]
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE_QUESTIONS]), \
             patch("pipelines.dev_triage.clarifier", new=AsyncMock()) as mock_clarifier:
            await dev_triage.clarifier_nouveaux(REPO)
        mock_clarifier.assert_not_called()

    async def test_clarifier_nouveaux_saute_les_issues_deja_epuisees(self):
        state.marquer_triage_epuise(REPO, 1)
        self.mock_list_comments.return_value = [COMMENT_OWNER]
        with patch("pipelines.dev_triage.github.list_issues", return_value=[ISSUE_QUESTIONS]), \
             patch("pipelines.dev_triage.clarifier", new=AsyncMock()) as mock_clarifier:
            await dev_triage.clarifier_nouveaux(REPO)
        mock_clarifier.assert_not_called()

    async def test_clarifier_nouveaux_quota_bloque_ne_liste_pas_les_issues(self):
        state.bloquer_quota(9_999_999_999)
        with patch("pipelines.dev_triage.github.list_issues") as mock_list:
            await dev_triage.clarifier_nouveaux(REPO)
        mock_list.assert_not_called()


    async def test_clarification_marque_les_commentaires_vus(self):
        """Régression : sans marquage en chemin de succès, le poll suivant
        retraiterait la même réponse et brûlerait le plafond tout seul."""
        analyse = dict(ANALYSE_FLOUE)  # toujours flou → nouvelles questions
        with patch("pipelines.dev_triage.run_claude",
                   new=AsyncMock(return_value=_resultat(json.dumps(analyse)))):
            await dev_triage.clarifier(
                REPO, ISSUE, [{"id": 4242, "body": "ma réponse", "cle": "issue-4242"}]
            )
        self.assertTrue(state.commentaire_deja_vu(REPO, "issue-4242"))


if __name__ == "__main__":
    unittest.main()
