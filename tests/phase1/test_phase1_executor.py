import tempfile
import unittest
from pathlib import Path

from scripts.phase1_dynamic_launcher import (
    _collection_loss_estimate,
    _compact_project_record,
    _pytest_execution_completed,
    repository_working_tree_bytes,
    summarize_pilot,
)


class Phase1ExecutorTests(unittest.TestCase):
    def test_interrupted_pytest_runs_are_not_complete_but_test_failures_are(self):
        self.assertTrue(_pytest_execution_completed({"returncode": 0, "timed_out": False}))
        self.assertTrue(_pytest_execution_completed({"returncode": 1, "timed_out": False}))
        self.assertFalse(_pytest_execution_completed({"returncode": -9, "timed_out": False}))
        self.assertFalse(_pytest_execution_completed({"returncode": None, "timed_out": True}))
        self.assertFalse(_pytest_execution_completed({"returncode": 2, "timed_out": False}))

    def test_compact_checkpoint_does_not_duplicate_fixture_event_payloads(self):
        event = {
            "status": "failed", "signature": "RuntimeError|x|f.py:1:f",
            "exception_class": "RuntimeError", "message": "x",
            "innermost_frame": "f.py:1:f",
        }
        compact = _compact_project_record({
            "project_id": "p", "status": "pilot_project_complete",
            "execution_runs": [{
                "run": {"returncode": 1, "timed_out": False, "seconds": 1.2},
                "metadata": {"test_ids": ["t"], "fixture_events": [event]},
            }],
        })
        self.assertEqual(compact["execution_runs"][0]["fixture_event_count"], 1)
        self.assertEqual(compact["fixture_errors"][0]["signature"], event["signature"])
        self.assertNotIn("fixture_events", compact["execution_runs"][0])

    def test_collection_loss_is_estimated_from_failed_module_ast_counts(self):
        result = _collection_loss_estimate(
            [{"nodeid": "tests/test_broken.py", "message": "ImportError"}],
            {"tests/test_broken.py": 7, "tests/test_ok.py": 3},
        )
        self.assertEqual(result["failed_modules"], ["tests/test_broken.py"])
        self.assertEqual(result["estimated_ast_test_definitions_lost"], 7)

    def test_repository_size_ignores_generated_virtualenvs_and_git(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "tests").mkdir()
            (repo / "tests" / "test_one.py").write_bytes(b"source")
            (repo / ".git" / "objects").mkdir(parents=True)
            (repo / ".git" / "objects" / "large").write_bytes(b"x" * 100)
            (repo / ".venv_ready" / "bin").mkdir(parents=True)
            (repo / ".venv_ready" / "bin" / "python").write_bytes(b"x" * 100)
            (repo / ".venv_attempt_1").mkdir()
            (repo / ".venv_attempt_1" / "junk").write_bytes(b"x" * 100)
            self.assertEqual(repository_working_tree_bytes(repo), 6)

    def test_pilot_summary_keeps_x_and_minimum_out_of_pilot(self):
        result = summarize_pilot({"projects": [{"status": "clone_failed"}]})
        self.assertEqual(result["x_threshold"], "not_calculated_in_pilot")
        self.assertEqual(result["minimum_usable_fixtures"], "reserved_for_preregistration_v2")
        self.assertEqual(result["status_counts"], {"clone_failed": 1})

    def test_isolation_wrappers_forward_runtime_probe_controls(self):
        root = Path(__file__).resolve().parents[2]
        wrapper = (root / "phase1/isolators/sudo_unshare.sh").read_text(encoding="utf-8")
        helper = (root / "phase1/isolators/phase1-netns-root").read_text(encoding="utf-8")
        for name in (
            "PHASE1_STATIC_INVENTORY",
            "PHASE1_PRIOR_DEPENDENCY_MAP",
            "PHASE1_CIRCUIT_BREAKER_K",
        ):
            self.assertIn(name, wrapper)
            self.assertIn(name, helper)


if __name__ == "__main__":
    unittest.main()
