"""Tests de la colonne `modele` de conso_claude (lib.state) : migration douce
et traçage du modèle utilisé par appel.

Lancer : python3 -m unittest tests.test_state_modele -v
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lib import state


class ConsoModeleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_originale = state.DB
        state.DB = Path(self._tmp.name) / "orchestrator.db"

    def tearDown(self):
        state.DB = self._db_originale
        self._tmp.cleanup()

    def test_migration_douce_ajoute_la_colonne_sans_perte(self):
        # Ancien schéma, sans la colonne modele.
        state.DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(state.DB)
        conn.execute(
            "CREATE TABLE conso_claude ("
            "  repo TEXT NOT NULL, numero INTEGER NOT NULL, etape TEXT NOT NULL,"
            "  tokens_entree INTEGER NOT NULL, tokens_cache INTEGER NOT NULL,"
            "  tokens_sortie INTEGER NOT NULL, cout_usd REAL NOT NULL,"
            "  le TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "INSERT INTO conso_claude (repo, numero, etape, tokens_entree, "
            "tokens_cache, tokens_sortie, cout_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("acme/toto", 1, "implementation", 100, 0, 50, 0.01),
        )
        conn.commit()
        conn.close()

        state.enregistrer_conso("acme/toto", 1, "auto-review", 10, 0, 5, 0.001, "haiku")

        with sqlite3.connect(state.DB) as conn:
            lignes = conn.execute(
                "SELECT numero, etape, modele FROM conso_claude ORDER BY rowid"
            ).fetchall()
        self.assertEqual(lignes, [(1, "implementation", None), (1, "auto-review", "haiku")])

    def test_enregistrer_conso_trace_le_modele(self):
        state.enregistrer_conso("acme/toto", 2, "implementation", 100, 0, 50, 0.01, "opus")
        with sqlite3.connect(state.DB) as conn:
            (modele,) = conn.execute(
                "SELECT modele FROM conso_claude WHERE numero = 2"
            ).fetchone()
        self.assertEqual(modele, "opus")

    def test_enregistrer_conso_sans_modele_reste_null(self):
        state.enregistrer_conso("acme/toto", 3, "implementation", 100, 0, 50, 0.01)
        with sqlite3.connect(state.DB) as conn:
            (modele,) = conn.execute(
                "SELECT modele FROM conso_claude WHERE numero = 3"
            ).fetchone()
        self.assertIsNone(modele)


if __name__ == "__main__":
    unittest.main()
