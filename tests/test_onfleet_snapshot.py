"""Focused tests for durable OnFleet snapshot fallback behavior."""
import unittest
from unittest.mock import patch

from migration import onfleet_snapshot as snap


class SnapshotTests(unittest.TestCase):
    def test_no_database_fetches_live(self):
        expected = {"tasks": [{"id": "t1"}]}
        self.assertEqual(snap.get_snapshot(None, lambda: expected), expected)

    def test_recent_snapshot_does_not_fetch(self):
        expected = {"tasks": [{"id": "t1"}]}
        with patch.object(snap, "_read", return_value=(expected, 30)):
            result = snap.get_snapshot(object(), lambda: self.fail("unneeded fetch"))
        self.assertEqual(result, expected)

    def test_expired_snapshot_fetches_live(self):
        expected = {"tasks": [{"id": "t2"}]}
        with patch.object(snap, "_read", return_value=({"tasks": []}, 1900)):
            with patch.object(snap, "_write") as write:
                self.assertEqual(snap.get_snapshot(object(), lambda: expected), expected)
                write.assert_called_once()

    def test_force_fetches_live(self):
        with patch.object(snap, "_read", return_value=({"tasks": []}, 2)):
            with patch.object(snap, "_write"):
                self.assertEqual(snap.get_snapshot(object(), lambda: {"tasks": [{"id": "new"}]}, force=True)["tasks"][0]["id"], "new")

    def test_database_read_failure_fetches_live(self):
        with patch.object(snap, "_read", side_effect=RuntimeError("database down")):
            self.assertEqual(snap.get_snapshot(object(), lambda: {"tasks": []}), {"tasks": []})


if __name__ == "__main__":
    unittest.main()
