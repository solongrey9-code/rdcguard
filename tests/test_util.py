import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentguard import util  # noqa: E402


class TestParseIso(unittest.TestCase):
    def test_plain(self):
        value = util.parse_iso("2026-10-01T07:26:53+08:00")
        self.assertIsNotNone(value)
        self.assertEqual(value.year, 2026)

    def test_dotnet_seven_digit_fraction(self):
        value = util.parse_iso("2026-10-10T13:39:22.1234567+08:00")
        self.assertIsNotNone(value)
        self.assertEqual(value.microsecond, 123456)

    def test_invalid(self):
        self.assertIsNone(util.parse_iso("not a date"))
        self.assertIsNone(util.parse_iso(""))


class TestFileLock(unittest.TestCase):
    def test_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            first = util.FileLock(path).acquire()
            try:
                with self.assertRaises(util.LockBusy):
                    util.FileLock(path).acquire()
            finally:
                first.release()
            second = util.FileLock(path).acquire()
            second.release()


class TestJournal(unittest.TestCase):
    def test_write_and_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "j.log")
            journal = util.Journal(path, max_mb=0.0001)
            journal.write("x" * 200, guard="g")
            journal.write("trigger rotation", guard="g")
            self.assertTrue(os.path.exists(path + ".1"))
            tail = journal.tail(10)
            self.assertTrue(any("trigger rotation" in line for line in tail))


class TestExpand(unittest.TestCase):
    def test_expand_tree(self):
        os.environ["AGENTGUARD_TEST_X"] = "VALUE"
        try:
            tree = util.expand_tree({"a": ["%AGENTGUARD_TEST_X%\\x", 1], "b": {"c": "plain"}})
            self.assertEqual(tree["a"][0], os.path.join("VALUE", "x"))
            self.assertEqual(tree["a"][1], 1)
            self.assertEqual(tree["b"]["c"], "plain")
        finally:
            os.environ.pop("AGENTGUARD_TEST_X", None)


if __name__ == "__main__":
    unittest.main()
