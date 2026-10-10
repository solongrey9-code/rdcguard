import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentguard import scheduler  # noqa: E402


class TestTaskXml(unittest.TestCase):
    def test_xml_shape(self):
        xml = scheduler.build_task_xml(
            "AgentGuard-Watchdog",
            "Keep services online & healthy",
            "DOMAIN\\user",
            "C:\\Python\\pythonw.exe",
            '"C:\\AgentGuard\\guard.py" ensure',
            3,
            logon=True,
        )
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<Interval>PT3M</Interval>", xml)
        self.assertIn("pythonw.exe", xml)
        self.assertIn("guard.py", xml)
        self.assertIn("IgnoreNew", xml)
        self.assertIn("InteractiveToken", xml)

    def test_xml_escapes_entities(self):
        xml = scheduler.build_task_xml(
            "T",
            "a & b < c",
            "D\\u",
            "cmd",
            "x",
            1,
            logon=False,
        )
        self.assertIn("a &amp; b &lt; c", xml)
        self.assertNotIn("<LogonTrigger>", xml)

    def test_startup_vbs(self):
        vbs = scheduler.build_startup_vbs("C:\\AgentGuard\\guard.py", runner="C:\\Python\\pythonw.exe")
        self.assertIn("pythonw.exe", vbs)
        self.assertIn("guard.py", vbs)
        self.assertIn("ensure", vbs)
        self.assertIn("0, False", vbs)
        self.assertTrue(vbs.endswith("\r\n"))

    def test_startup_shim_path(self):
        path = scheduler.startup_shim_path("AgentGuard")
        self.assertTrue(path.endswith(os.path.join("Programs", "Startup", "AgentGuard.vbs")))


if __name__ == "__main__":
    unittest.main()
