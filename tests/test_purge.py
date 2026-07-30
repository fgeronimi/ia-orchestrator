"""Purge des workspaces : sélection des branches et garde-fous.

Le risque de ce pipeline est la suppression de trop : une branche humaine, ou
une branche dont le travail n'est pas repris. Ces tests verrouillent le
périmètre, sur de **vrais dépôts git** (c'est git qu'il faut vérifier, pas un
mock de git) et **sans réseau** : le « remote » est un dépôt bare local, injecté
en remplaçant `workspace._url`.

Lancer : python3 -m unittest tests.test_purge -v
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pipelines import purge


class FauxGithub:
    """PR d'un repo, forme allégée de github.list_pulls."""

    def __init__(self, pulls=(), defaut="main"):
        self._pulls = list(pulls)
        self._defaut = defaut
        self.appels = []

    def list_pulls(self, repo, state="open"):
        self.appels.append((repo, state))
        return self._pulls

    def get_default_branch(self, repo):
        return self._defaut


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
        gh = FauxGithub()
        purge.branches_mergees("acme/toto", gh)
        self.assertEqual(gh.appels, [("acme/toto", "closed")])


class PurgerRepoTest(unittest.TestCase):
    """Dépôts git réels, remote bare local, aucun accès réseau.

    Topologie montée par _depot() :

        A ── B          main (remote et local), ai/1 est sur B
             └── C      ai/2
        l1-web reste sur A

    Donc : ai/1 et l1-web sont des ancêtres de main, ai/2 non.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._racine = Path(self._tmp.name)
        self._workspaces = self._racine / "workspaces"
        self._workspaces.mkdir()
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(purge, "WORKSPACES", self._workspaces)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _run(path, *args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True, check=True)

    def _depot(self, repo="acme/toto"):
        path = self._workspaces / repo.replace("/", "-")
        path.mkdir(parents=True)
        self._run(path, "init", "-q", "-b", "main")
        self._run(path, "config", "user.email", "t@t.local")
        self._run(path, "config", "user.name", "test")

        (path / "f.txt").write_text("A")
        self._run(path, "add", "-A")
        self._run(path, "commit", "-q", "-m", "A")
        self._run(path, "branch", "l1-web")          # humaine, reste sur A

        self._run(path, "checkout", "-q", "-b", "ai/1")
        (path / "f.txt").write_text("B")
        self._run(path, "commit", "-qam", "B")

        self._run(path, "checkout", "-q", "main")
        self._run(path, "merge", "-q", "--ff-only", "ai/1")  # main → B

        self._run(path, "checkout", "-q", "-b", "ai/2")
        (path / "f.txt").write_text("C")
        self._run(path, "commit", "-qam", "C")
        self._run(path, "checkout", "-q", "main")

        # « remote » : bare local contenant main à B
        bare = self._racine / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self._run(path, "push", "-q", str(bare), "main")

        faux_url = patch.object(purge.workspace, "_url",
                                lambda r, _b=str(bare): _b)
        faux_url.start()
        self.addCleanup(faux_url.stop)
        return path

    def _branches(self, path):
        r = self._run(path, "branch", "--format=%(refname:short)")
        return sorted(l.strip() for l in r.stdout.splitlines() if l.strip())

    # --- critère 2 : déjà contenue dans la base ---------------------------

    def test_orpheline_contenue_dans_main_est_purgee(self):
        """Le cas demandé : aucune PR, mais le travail est déjà dans main."""
        path = self._depot()
        gh = FauxGithub()  # aucune PR du tout

        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], ["ai/1"])
        self.assertEqual(r["raisons"]["ai/1"], "déjà dans main")
        # ai/2 porte un commit absent de main → conservée
        self.assertEqual(self._branches(path), ["ai/2", "l1-web", "main"])

    def test_branche_humaine_contenue_dans_main_survit(self):
        """l1-web est un ancêtre de main, mais n'est pas préfixée ai/."""
        path = self._depot()
        purge.purger_repo("acme/toto", FauxGithub())
        self.assertIn("l1-web", self._branches(path))

    def test_le_token_ne_fuit_jamais_dans_les_logs(self):
        """Toute sortie de _git est expurgée du token avant d'atteindre les logs.

        git redacte lui-même les identifiants dans *certains* messages, mais pas
        tous : on ne s'en remet pas à lui. `git checkout <ref-inexistante>`
        recrache son argument, ce qui force le cas de façon déterministe — si
        le token vaut cette ref, il doit ressortir masqué.
        """
        path = self._depot()
        faux_token = "ghp_TOKENSECRETQUINEDOITPASFUIR"
        with patch.dict(os.environ, {"GITHUB_TOKEN": faux_token}):
            code, sortie = purge._git(path, "checkout", faux_token)

        self.assertNotEqual(code, 0, "le checkout devait échouer")
        self.assertNotIn(faux_token, sortie)
        self.assertIn("***", sortie)

    def test_fetch_en_echec_ne_recrache_pas_le_token(self):
        """Le vrai chemin : un fetch avec URL authentifiée qui échoue."""
        path = self._depot()
        faux_token = "ghp_AUTRETOKENSECRET"
        url = f"https://x-access-token:{faux_token}@github.invalid/a/b.git"
        with patch.dict(os.environ, {"GITHUB_TOKEN": faux_token}):
            code, sortie = purge._git(path, "fetch", url, "main")

        self.assertNotEqual(code, 0, "le fetch devait échouer")
        self.assertNotIn(faux_token, sortie)

    def test_fetch_en_echec_neutralise_ce_critere(self):
        """Sans fetch, pas de verdict : on ne supprime rien de ce chef."""
        path = self._depot()
        with patch.object(purge.workspace, "_url",
                          return_value="/chemin/inexistant.git"):
            r = purge.purger_repo("acme/toto", FauxGithub())
        self.assertEqual(r["branches"], [])
        self.assertIn("ai/1", self._branches(path))

    # --- critère 1 : PR mergée -------------------------------------------

    def test_pr_mergee_hors_de_main_est_purgee(self):
        """Cas du merge squash : les commits ne sont pas dans main, la PR si."""
        path = self._depot()
        gh = FauxGithub([_pr(2, "ai/2")])  # ai/2 n'est pas ancêtre de main

        r = purge.purger_repo("acme/toto", gh)

        self.assertIn("ai/2", r["branches"])
        self.assertEqual(r["raisons"]["ai/2"], "PR mergée")
        self.assertNotIn("ai/2", self._branches(path))

    def test_pr_non_mergee_hors_de_main_est_conservee(self):
        path = self._depot()
        gh = FauxGithub([_pr(2, "ai/2", merged=False)])
        r = purge.purger_repo("acme/toto", gh)
        self.assertNotIn("ai/2", r["branches"])
        self.assertIn("ai/2", self._branches(path))

    def test_pr_mergee_sur_branche_humaine_ne_supprime_rien(self):
        path = self._depot()
        gh = FauxGithub([_pr(9, "l1-web")])
        r = purge.purger_repo("acme/toto", gh)
        self.assertNotIn("l1-web", r["branches"])
        self.assertIn("l1-web", self._branches(path))

    # --- garde-fous ------------------------------------------------------

    def test_branche_courante_est_supprimee_apres_detachement(self):
        """Cas réel : le workspace est resté sur une branche à purger."""
        path = self._depot()
        self._run(path, "checkout", "-q", "ai/1")
        gh = FauxGithub()

        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], ["ai/1"])
        self.assertNotIn("ai/1", self._branches(path))
        tete = self._run(path, "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(tete.stdout.strip(), "HEAD")

    def test_workspace_absent_ne_casse_rien(self):
        r = purge.purger_repo("acme/jamais-clone", FauxGithub([_pr(1, "ai/1")]))
        self.assertEqual(r, {"branches": [], "raisons": {}, "recupere": 0})

    def test_aucune_branche_ai_aucun_appel_api(self):
        """Économie : sans branche ai/* locale, inutile d'interroger GitHub."""
        path = self._workspaces / "acme-toto"
        path.mkdir(parents=True)
        self._run(path, "init", "-q", "-b", "main")
        self._run(path, "config", "user.email", "t@t.local")
        self._run(path, "config", "user.name", "test")
        (path / "f.txt").write_text("A")
        self._run(path, "add", "-A")
        self._run(path, "commit", "-q", "-m", "A")

        gh = FauxGithub()
        r = purge.purger_repo("acme/toto", gh)

        self.assertEqual(r["branches"], [])
        self.assertEqual(gh.appels, [])


class PurgerTest(unittest.IsolatedAsyncioTestCase):
    async def test_rien_a_purger_aucune_notif(self):
        with patch.object(purge, "purger_repo",
                          return_value={"branches": [], "raisons": {},
                                        "recupere": 0}), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock) as mock:
            resultat = await purge.purger(["acme/toto"], github=object())
        mock.assert_not_awaited()
        self.assertIn("rien à purger", resultat)

    async def test_la_notif_porte_la_raison(self):
        with patch.object(purge, "purger_repo",
                          return_value={"branches": ["ai/1", "ai/2"],
                                        "raisons": {"ai/1": "PR mergée",
                                                    "ai/2": "déjà dans main"},
                                        "recupere": 5 * 1024**2}), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock) as mock:
            resultat = await purge.purger(["acme/toto"], github=object())
        message = mock.await_args.args[0]
        self.assertIn("ai/1 (PR mergée)", message)
        self.assertIn("ai/2 (déjà dans main)", message)
        self.assertIn("2 branche(s)", resultat)

    async def test_un_repo_qui_casse_ne_bloque_pas_les_suivants(self):
        vus = []

        def faux(repo, github):
            vus.append(repo)
            if repo == "acme/casse":
                raise RuntimeError("dépôt corrompu")
            return {"branches": ["ai/1"], "raisons": {"ai/1": "PR mergée"},
                    "recupere": 1024}

        with patch.object(purge, "purger_repo", side_effect=faux), \
             patch("pipelines.purge.notify.notify", new_callable=AsyncMock):
            resultat = await purge.purger(["acme/casse", "acme/toto"],
                                          github=object())

        self.assertEqual(vus, ["acme/casse", "acme/toto"])
        self.assertIn("1 branche(s)", resultat)


if __name__ == "__main__":
    unittest.main()
