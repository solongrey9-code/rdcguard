import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentguard import config as config_mod  # noqa: E402


def minimal_guard(name="demo"):
    return {
        "name": name,
        "health": {"probes": [{"type": "tcp", "port": 12345}]},
        "start": {"command": ["cmd", "/c", "echo hi"]},
    }


def minimal_config():
    return {"guards": [minimal_guard()]}


class TestValidate(unittest.TestCase):
    def test_minimal_config_valid(self):
        self.assertEqual(config_mod.validate_config(minimal_config()), [])

    def test_guard_without_action_is_invalid(self):
        guard = {"name": "x", "health": {"probes": [{"type": "tcp", "port": 1}]}}
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any("start.command" in error or "observe_only" in error for error in errors))

    def test_observe_only_needs_no_action(self):
        guard = {"name": "x", "observe_only": True, "health": {"probes": [{"type": "tcp", "port": 1}]}}
        self.assertEqual(config_mod.validate_config({"guards": [guard]}), [])

    def test_delegate_requires_ensure_command(self):
        guard = {
            "name": "x",
            "health": {"probes": [{"type": "tcp", "port": 1}]},
            "delegate": {"stop_command": ["cmd", "/c", "echo"]},
        }
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any("ensure_command" in error for error in errors))

    def test_duplicate_names_rejected(self):
        cfg = {"guards": [minimal_guard("same"), minimal_guard("same")]}
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("duplicate" in error for error in errors))

    def test_bad_probe_type_rejected(self):
        guard = minimal_guard()
        guard["health"]["probes"] = [{"type": "telepathy"}]
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any("type must be one of" in error for error in errors))

    def test_bad_name_rejected(self):
        guard = minimal_guard("has space")
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any(".name must match" in error for error in errors))

    def test_young_grace_must_be_number(self):
        guard = minimal_guard()
        guard["health"]["young_grace_sec"] = "soon"
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any("young_grace_sec" in error for error in errors))

    def test_expect_json_must_be_object(self):
        guard = minimal_guard()
        guard["health"]["probes"] = [{"type": "http", "url": "http://127.0.0.1:1/", "expect_json": [1, 2]}]
        errors = config_mod.validate_config({"guards": [guard]})
        self.assertTrue(any("expect_json" in error for error in errors))

    def test_watchdog_interval_bounds(self):
        cfg = minimal_config()
        cfg["watchdog"] = {"interval_minutes": 0}
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("interval_minutes" in error for error in errors))


class TestLoad(unittest.TestCase):
    def test_load_expands_env_and_fills_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENTGUARD_TEST_DIR"] = tmp
            try:
                guard = minimal_guard()
                guard["health"]["probes"] = [{"type": "log", "path": "%AGENTGUARD_TEST_DIR%\\app.log", "markers": {"up": ["READY"]}, "healthy": ["up"]}]
                path = os.path.join(tmp, "agentguard.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"guards": [guard]}, fh)
                cfg = config_mod.load_config(path)
                expanded = cfg["guards"][0]["health"]["probes"][0]["path"]
                self.assertEqual(expanded, os.path.join(tmp, "app.log"))
                self.assertEqual(cfg["watchdog"]["task_name"], "AgentGuard-Watchdog")
                self.assertEqual(cfg["watchdog"]["interval_minutes"], 3)
                self.assertTrue(cfg["_state_dir"])
            finally:
                os.environ.pop("AGENTGUARD_TEST_DIR", None)

    def test_load_invalid_raises_with_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agentguard.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"guards": [{"name": "x"}]}, fh)
            with self.assertRaises(config_mod.ConfigError) as ctx:
                config_mod.load_config(path)
            self.assertTrue(ctx.exception.errors)

    def test_select_guards(self):
        cfg = {"guards": [minimal_guard("a"), minimal_guard("b")]}
        self.assertEqual(len(config_mod.select_guards(cfg, [])), 2)
        self.assertEqual(len(config_mod.select_guards(cfg, ["a"])), 1)
        self.assertEqual(len(config_mod.select_guards(cfg, ["all"])), 2)


if __name__ == "__main__":
    unittest.main()
