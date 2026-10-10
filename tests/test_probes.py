import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentguard import probes  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = json.dumps({"server_alive": True, "extra": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/auth":
            self.send_response(401)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class TestTcp(unittest.TestCase):
    def test_open_and_closed_ports(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(probes.probe_tcp("127.0.0.1", port, 1)["ok"])
        self.assertFalse(probes.probe_tcp("127.0.0.1", port, 1)["ok"])
        self.assertFalse(probes.probe_tcp("127.0.0.1", None, 1)["ok"])


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_status_match(self):
        self.assertTrue(probes.probe_http("http://127.0.0.1:%d/" % self.port, 3, status=[200])["ok"])
        self.assertTrue(probes.probe_http("http://127.0.0.1:%d/auth" % self.port, 3, status=[200, 401])["ok"])
        self.assertFalse(probes.probe_http("http://127.0.0.1:%d/auth" % self.port, 3, status=[200])["ok"])

    def test_status_lt(self):
        self.assertTrue(probes.probe_http("http://127.0.0.1:%d/" % self.port, 3, status_lt=500)["ok"])
        self.assertFalse(probes.probe_http("http://127.0.0.1:%d/" % self.port, 3, status_lt=200)["ok"])

    def test_expect_json(self):
        result = probes.probe_http(
            "http://127.0.0.1:%d/json" % self.port, 3, status=[200], expect_json={"server_alive": True}
        )
        self.assertTrue(result["ok"])
        result = probes.probe_http(
            "http://127.0.0.1:%d/json" % self.port, 3, status=[200], expect_json={"server_alive": False}
        )
        self.assertFalse(result["ok"])

    def test_no_response(self):
        self.assertFalse(probes.probe_http("http://127.0.0.1:1/", 1, status=[200])["ok"])


class TestCmd(unittest.TestCase):
    def test_exit_codes(self):
        ok = probes.probe_cmd([sys.executable, "-c", "import sys; sys.exit(0)"], 20, [0])
        self.assertTrue(ok["ok"])
        bad = probes.probe_cmd([sys.executable, "-c", "import sys; sys.exit(3)"], 20, [0])
        self.assertFalse(bad["ok"])
        bad_as_ok = probes.probe_cmd([sys.executable, "-c", "import sys; sys.exit(3)"], 20, [0, 3])
        self.assertTrue(bad_as_ok["ok"])


class TestLog(unittest.TestCase):
    def write(self, tmp, name, text):
        path = os.path.join(tmp, name)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_last_marker_wins(self):
        from agentguard import util

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "app.log", "boot\nConnecting...\nDevice ready: x\nmore output\n")
            probe = {
                "type": "log",
                "path": util.expand(path),
                "markers": {"online": ["Device ready"], "connecting": ["Connecting"]},
                "healthy": ["online"],
            }
            self.assertTrue(probes.read_log_state(probe)["ok"])

    def test_since_offset_ignores_old_state(self):
        from agentguard import util

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "app.log", "Device ready: old\n")
            offset = os.path.getsize(path)
            self.write(tmp, "app.log", "Connecting...\n")
            probe = {
                "type": "log",
                "path": util.expand(path),
                "since": "last-start",
                "markers": {"online": ["Device ready"], "connecting": ["Connecting"]},
                "healthy": ["online"],
                "starting": ["connecting"],
            }
            result = probes.read_log_state(probe, since_offset=offset)
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "connecting")

    def test_missing_file(self):
        result = probes.read_log_state({"type": "log", "path": "C:\\nope\\nope.log", "markers": {"a": ["b"]}, "healthy": ["a"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "missing")

    def test_window_bytes(self):
        from agentguard import util

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "app.log", "Device ready: old\n" + ("x" * 5000) + "\n")
            probe = {
                "type": "log",
                "path": util.expand(path),
                "window_bytes": 100,
                "markers": {"online": ["Device ready"]},
                "healthy": ["online"],
            }
            self.assertFalse(probes.read_log_state(probe)["ok"])


class TestIdentity(unittest.TestCase):
    def test_match_identity(self):
        snapshot = [
            probes.ProcessInfo(1, "node.exe", 'node C:\\apps\\desktop-commander\\dist\\index.js remote'),
            probes.ProcessInfo(2, "node.exe", "node C:\\other\\server.js"),
            probes.ProcessInfo(3, "python.exe", "python desktop-commander helper"),
        ]
        identity = {"process_names": ["node.exe"], "cmdline_contains": ["desktop-commander"]}
        owned = probes.match_identity(identity, processes=snapshot)
        self.assertEqual([proc.pid for proc in owned], [1])

    def test_fail_closed_without_cmdline(self):
        snapshot = [probes.ProcessInfo(1, "node.exe", "")]
        identity = {"process_names": ["node.exe"], "cmdline_contains": ["desktop-commander"]}
        self.assertEqual(probes.match_identity(identity, processes=snapshot), [])

    def test_name_only_identity(self):
        snapshot = [probes.ProcessInfo(1, "pythonw.exe", "")]
        identity = {"process_names": ["pythonw.exe"]}
        self.assertEqual(len(probes.match_identity(identity, processes=snapshot)), 1)

    def test_established_without_pids(self):
        result = probes.probe_established([])
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
