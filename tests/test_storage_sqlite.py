import tempfile
import time
import unittest
from pathlib import Path

from app.storage import Storage


class SQLiteStorageTests(unittest.TestCase):
    def test_values_survive_storage_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first = Storage(sqlite_path=str(path))
            first.set_json("learning:provider:coding:ollama", {"successes": 4})
            first.close()

            restarted = Storage(sqlite_path=str(path))
            self.assertEqual(restarted.backend, "sqlite")
            self.assertEqual(
                restarted.get_json("learning:provider:coding:ollama"),
                {"successes": 4},
            )
            restarted.close()

    def test_ttl_and_prefix_keys_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(sqlite_path=str(Path(directory) / "state.db"))
            store.set_json("session:one", {"active": True})
            store.set_json("session:expired", {"active": False}, ttl=1)
            with store._lock:
                store._sqlite.execute(
                    "UPDATE kemy_store SET expires_at = ? WHERE key = ?",
                    (time.time() - 1, "session:expired"),
                )
                store._sqlite.commit()

            self.assertEqual(store.keys("session:"), ["session:one"])
            self.assertIsNone(store.get_json("session:expired"))
            self.assertEqual(store.status()["type"], "sqlite")
            store.close()


if __name__ == "__main__":
    unittest.main()
