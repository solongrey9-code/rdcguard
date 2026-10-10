import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentguard import engine, util  # noqa: E402


def make_script(tmp, name, body):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def log_guard(tmp, log_path, name="fake"):
    return {
        "name": name,
        "health": {
            "mode": "all",
            "young_grace_sec": 0,
            "probes": [
                {
                    "type": "log",
                    "path": log_path,
                    "window_bytes": 65536,
                    "since": "last-start",
                    "markers": {"online": ["ONLINE"], "failed": ["FATAL"]},
                    "healthy": ["online"],
                    "failed": ["failed"],
                    "empty_state": "boot",
                }
            ],
        },
        "start": {"command": ["cmd", "/c", "echo", "placeholder"], "hidden": True},
        "stop": {"kill": False},
        "heal": {"attempts": 2, "healthy_timeout_sec": 30, "backoff_sec": 1, "kill_stale": True},
    }


class TestManagedHeal(unittest.TestCase):
    def test_heal_with_start_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "heartbeat.log")
            open(log_path, "w").close()
            script = make_script(
                tmp,
                "start_ok.py",
                "import time\n"
                "time.sleep(1)\n"
                "open(%r, 'a', encoding='utf-8').write('ONLINE\\n')\n" % log_path,
            )
            guard = log_guard(tmp, log_path)
            guard["start"]["command"] = [sys.executable, script]
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))

            result = engine.ensure_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "recovered")
            self.assertTrue(result["healthy"])

            result = engine.ensure_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "healthy")

    def test_failed_heal_after_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "heartbeat.log")
            open(log_path, "w").close()
            script = make_script(tmp, "start_bad.py", "import time\ntime.sleep(0.1)\n")
            guard = log_guard(tmp, log_path)
            guard["start"]["command"] = [sys.executable, script]
            guard["heal"]["attempts"] = 1
            guard["heal"]["healthy_timeout_sec"] = 6
            guard["heal"]["backoff_sec"] = 0
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))

            result = engine.ensure_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["healthy"])

    def test_disarm_and_start_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "heartbeat.log")
            open(log_path, "w").close()
            script = make_script(
                tmp,
                "start_ok.py",
                "import time\n"
                "time.sleep(1)\n"
                "open(%r, 'a', encoding='utf-8').write('ONLINE\\n')\n" % log_path,
            )
            guard = log_guard(tmp, log_path)
            guard["start"]["command"] = [sys.executable, script]
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))

            engine.ensure_guard(cfg, guard, journal)
            engine.stop_guard(cfg, guard, journal)
            self.assertTrue(engine.is_disarmed(cfg, "fake"))
            result = engine.ensure_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "disarmed")

            result = engine.start_guard(cfg, guard, journal)
            self.assertFalse(engine.is_disarmed(cfg, "fake"))
            self.assertIn(result["status"], ("recovered", "healthy"))


class TestDelegated(unittest.TestCase):
    def test_delegate_ensure_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "svc.log")
            open(log_path, "w").close()
            marker = os.path.join(tmp, "ensure_ran.txt")
            ensure_script = make_script(
                tmp,
                "delegate_ensure.py",
                "import time\n"
                "open(%r, 'a', encoding='utf-8').write('ran\\n')\n"
                "time.sleep(0.2)\n"
                "open(%r, 'a', encoding='utf-8').write('ONLINE\\n')\n" % (marker, log_path),
            )
            stop_marker = os.path.join(tmp, "stop_ran.txt")
            stop_script = make_script(
                tmp,
                "delegate_stop.py",
                "open(%r, 'a', encoding='utf-8').write('stopped\\n')\n" % stop_marker,
            )
            guard = {
                "name": "delegated",
                "health": {
                    "mode": "all",
                    "probes": [
                        {
                            "type": "log",
                            "path": log_path,
                            "since": "last-start",
                            "markers": {"online": ["ONLINE"]},
                            "healthy": ["online"],
                        }
                    ],
                },
                "delegate": {
                    "ensure_command": [sys.executable, ensure_script],
                    "stop_command": [sys.executable, stop_script],
                    "healthy_timeout_sec": 30,
                },
                "heal": {"attempts": 1, "healthy_timeout_sec": 30, "backoff_sec": 0},
            }
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))

            result = engine.ensure_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "recovered")
            self.assertTrue(os.path.exists(marker))

            result = engine.stop_guard(cfg, guard, journal)
            self.assertEqual(result["status"], "stopped")
            self.assertTrue(os.path.exists(stop_marker))
            self.assertTrue(engine.is_disarmed(cfg, "delegated"))


class TestRotationAndStatus(unittest.TestCase):
    def test_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "big.log")
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("x" * 2000)
            guard = {
                "name": "rot",
                "health": {"probes": [{"type": "tcp", "port": 1}]},
                "start": {"command": ["cmd", "/c", "echo"]},
                "rotate": [{"path": log_path, "max_mb": 0.0001, "keep": 1}],
            }
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))
            engine.rotate_guard_logs(cfg, guard, journal)
            self.assertFalse(os.path.exists(log_path))
            self.assertTrue(os.path.exists(log_path + ".1"))

    def test_status_guard_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = {
                "name": "obs",
                "observe_only": True,
                "description": "observe",
                "health": {"probes": [{"type": "tcp", "port": 1}]},
            }
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard], "log_max_mb": 5}
            status = engine.status_guard(cfg, guard)
            self.assertEqual(status["name"], "obs")
            self.assertEqual(status["mode_kind"], "observe")
            self.assertFalse(status["healthy"])
            self.assertEqual(status["status"], "observe-only")

    def test_ensure_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            healthy_log = os.path.join(tmp, "ok.log")
            with open(healthy_log, "w", encoding="utf-8") as fh:
                fh.write("ONLINE\n")
            guard_a = log_guard(tmp, healthy_log, "a")
            guard_b = {"name": "b", "enabled": False, "health": {"probes": [{"type": "tcp", "port": 1}]}}
            cfg = {"_state_dir": tmp, "_path": "test", "guards": [guard_a, guard_b], "log_max_mb": 5}
            journal = util.Journal(os.path.join(tmp, "agentguard.log"))
            results = engine.ensure_all(cfg, [guard_a, guard_b], journal)
            by_name = {item["name"]: item for item in results}
            self.assertEqual(by_name["a"]["status"], "healthy")
            self.assertEqual(by_name["b"]["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
