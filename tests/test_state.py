"""Backup/restore regression checks using real SQLite and local files."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tarfile
from tempfile import TemporaryDirectory
import unittest

from deploy.state import AREAS, backup, restore, safe_members


class StateTests(unittest.TestCase):
    def test_consistent_sqlite_and_index_roundtrip_without_overwrite(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            for area in AREAS:
                (state / area).mkdir(parents=True)
            with closing(sqlite3.connect(state / "storage" / "demo.sqlite3")) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE sample (text TEXT)")
                db.execute("INSERT INTO sample VALUES (?)", ("Проверка Windows",))
                db.commit()
                (state / "index" / "CURRENT").write_text("generation-one", encoding="utf-8")
                archive = root / "snapshot.tar.gz"
                backup(state, archive)
                with self.assertRaises(FileExistsError):
                    backup(state, archive)
            target = root / "restored"
            restore(target, archive)
            with closing(sqlite3.connect(target / "storage" / "demo.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT text FROM sample").fetchone()[0], "Проверка Windows")
            self.assertEqual((target / "index" / "CURRENT").read_text(), "generation-one")
            with self.assertRaises(ValueError):
                restore(target, archive)

    def test_windows_archive_paths_rejected(self):
        for name in ("storage/../escape", "storage/ads:secret", "storage/..\\escape", "C:/escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                list(safe_members([tarfile.TarInfo(name)]))


if __name__ == "__main__":
    unittest.main()
