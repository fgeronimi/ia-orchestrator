"""Purge des workspaces : sélection des branches et garde-fous.

Le risque de ce pipeline est la suppression de trop : une branche humaine, ou
une branche dont le travail n'est pas repris. Ces tests verrouillent le
périmètre.

Lancer : python3 -m unittest tests.test_purge -v
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pipelines import purge


class FauxGithub:
    """PR d'un repo, forme allégée de github.list_pulls."""

    def __init__(self, pulls):
        self._pulls = pulls
        self.appels = []

    def list_pulls(self, repo, state="open"):
        self.appels.append((repo, state))
        return self._pulls


def _pr(numero, head, repo="acme/toto", merged=True):
    return {"number": numero, "head": head, "head_repo": repo,
            "merged_at": "2026-07-28T10:00:00Z" if merged else None}


class BranchesMergeesTest(unittest.TestCase):
    def test_ne_retient_que_les_ai_mergees(self):
        gh = FauxGithub([
            _pr(1, "ai/1"),                      # mergée → retenue
            _pr(2, "ai/2", merged=False),        # fermée sans merge → écartée
            _pr(3, "feat/humain"),               # pas une branche d'agent
            _pr(4, "ai/4", repo="fork/toto"),    # PR de fork → écartée
        ])
        self.assertEqual(purge.branches_mergees("acme/toto", gh), {"ai/1"})

    def test_interroge_bien_les_pr_fermees(self):
        gh = FauxGithub([])
        purge.branches_mergees("acme/toto", gh)
        self.assertEqual(gh.appels, [("acme/toto", "closed")])


class PurgerRepoTest(unittest.TestCase):
    """Tests sur de vrais dépôts git locaux : c'est git qui doit être vérifié."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        racine = Path(self._tmp.name)
        self._workspaces = racine / "workspaces"
        self._workspaces.mkdir()
        # purge.WORKSPACES est un constant de module : on le redirige.
        self._patch = patch.object(purge, "WORKSPACES", self._workspaces)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _depot(self, repo="acme/toto", branches=("ai/1", "ai/2", "l1-web")):
        """Crée un dépôt git avec un commit et les branches demandées."""
        path = self._workspaces / repo.replace("/", "-")
        path.mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(path), *a],
                                        capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t.local")
        run("config", "user.name", "test")
        (path / "f.txt").write_text("bonjour")
        run("add", "-A")
        run("commit", "-q", "-m", "initial")
        for b in branches:
            run("branch", b)
        return path

    def _branches(self, path):
        r = subprocess.run(["git", "-C", str(path), "branch", "--format=%(refname:short)"],
                           capture_output=True, text=True, check=True)
        return sorted(l.strip() for l in r.stdout.splitlines() if l.strip())

    def test_supprime_les_ai_mergees_et_garde_le_reste(self):
        path = self._depot()
        gh = FauxGithub([_pr(1, "ai/1"), _pr(2, "ai/2", merged=False)])

        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], ["ai/1"])
        # ai/2 (PR non mergée) et l1-web (humaine) intactes
        self.assertEqual(self._branches(path), ["ai/2", "l1-web", "main"])

    def test_ne_touche_jamais_une_branche_humaine_meme_mergee(self):
        """Une PR mergée sur une branche non-ai ne doit rien supprimer."""
        path = self._depot(branches=("l1-web",))
        gh = FauxGithub([_pr(9, "l1-web")])

        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], [])
        self.assertIn("l1-web", self._branches(path))

    def test_branche_courante_mergee_est_supprimee_apres_detachement(self):
        """Cas réel : le workspace est resté sur ai/27 après le merge."""
        path = self._depot(branches=("ai/27",))
        subprocess.run(["git", "-C", str(path), "checkout", "-q", "ai/27"], check=True)
        gh = FauxGithub([_pr(27, "ai/27")])

        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], ["ai/27"])
        self.assertNotIn("ai/27", self._branches(path))
        # HEAD détaché : plus sur aucune branche
        tete = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, check=True)
        self.assertEqual(tete.stdout.strip(), "HEAD")

    def test_workspace_absent_ne_casse_rien(self):
        gh = FauxGithub([_pr(1, "ai/1")])
        r = purge.purger_repo("acme/jamais-clone", gh)
        self.assertEqual(r, {"branches": [], "recupere": 0})

    def test_aucune_branche_ai_aucun_appel_api(self):
        """Économie : sans branche ai/* locale, inutile d'interroger GitHub."""
        self._depot(branches=("l1-web",))
        gh = FauxGithub([])
        r = purge.purger_repo("acme/toto", gh)
        self.assertEqual(r["branches"], [])
        self.assertEqual(gh.appels, [])


class PurgerTest(unittest.IsolatedAsyncioTestCase):
    async def test_rien_a_purger_aucune_notif(self):
        with patch.object(purge, "purger_repo",
                          return_value={"branches": [], "recupere": 0}), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock) as mock:
            resultat = await purge.purger(["acme/toto"], github=object())
        mock.assert_not_awaited()
        self.assertIn("rien à purger", resultat)

    async def test_resume_et_notif_quand_ca_purge(self):
        with patch.object(purge, "purger_repo",
                          return_value={"branches": ["ai/1", "ai/2"],
                                        "recupere": 5 * 1024**2}), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock) as mock:
            resultat = await purge.purger(["acme/toto"], github=object())
        mock.assert_awaited_once()
        message = mock.await_args.args[0]
        self.assertIn("2 branche(s)", message)
        self.assertIn("ai/1", message)
        self.assertIn("2 branche(s)", resultat)

    async def test_un_repo_qui_casse_ne_bloque_pas_les_suivants(self):
        vus = []

        def faux(repo, github):
            vus.append(repo)
            if repo == "acme/casse":
                raise RuntimeError("dépôt corrompu")
            return {"branches": ["ai/1"], "recupere": 1024}

        with patch.object(purge, "purger_repo", side_effect=faux), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock):
            resultat = await purge.purger(["acme/casse", "acme/toto"],
                                          github=object())

        self.assertEqual(vus, ["acme/casse", "acme/toto"])
        self.assertIn("1 branche(s)", resultat)


if __name__ == "__main__":
    unittest.main()
