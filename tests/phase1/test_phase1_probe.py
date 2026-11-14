import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from phase1_probe import Phase1Probe  # noqa: E402


class _PluginManager:
    @staticmethod
    def list_name_plugin():
        return []


class Phase1ProbeTests(unittest.TestCase):
    def test_fixture_path_mapping_is_stable_if_project_changes_os_name(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            fixture_path = repo / "conftest.py"
            namespace = {}
            exec(compile("def resource():\n    return 1\n", str(fixture_path), "exec"), namespace)
            config = SimpleNamespace(rootpath=repo, pluginmanager=_PluginManager())
            with patch.dict(os.environ, {"PHASE1_PROJECT_ROOT": str(repo), "PHASE1_PROJECT_ID": "p"}):
                probe = Phase1Probe(config)

            fixturedef = SimpleNamespace(
                func=namespace["resource"], argname="resource", scope="function", argnames=[]
            )
            with patch("os.name", "nt"):
                info = probe._fixture_info(fixturedef)

        self.assertEqual(info["file"], "conftest.py")
        self.assertTrue(info["is_project"])


if __name__ == "__main__":
    unittest.main()
