"""Run the JS streaming-client unit tests (the exact comparator, delta
application, backoff bounds) through Node, so they ride the normal test run.
Skipped when Node is unavailable."""

import os
import shutil
import subprocess
import unittest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST = os.path.join(_HERE, "examples", "live-todo", "stream.test.cjs")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestStreamClient(unittest.TestCase):
    def test_pure_helpers(self):
        r = subprocess.run(["node", _TEST], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn("all assertions passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
