import tempfile
import unittest
from pathlib import Path

from scripts.phase1_catalog import (
    catalog_project,
    catalog_test_modules_details,
    choose_x_threshold,
    collection_is_viable,
    early_stop_after_k_identical_failures,
    error_signature,
    loss_by_test,
    normalize_error,
    pytest_probe_args,
    select_stratified_pilot,
    stable_test_ids,
    summarize_fixture_repetitions,
)


class Phase1CatalogTests(unittest.TestCase):
    def test_ast_estimate_expands_literal_stacked_parametrization(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            test_file = repo / "tests" / "test_params.py"
            test_file.parent.mkdir()
            test_file.write_text(
                "import pytest\n"
                "CASES = [1, 2, 3]\n"
                "@pytest.mark.parametrize('x', CASES)\n"
                "@pytest.mark.parametrize('y', ('a', 'b'))\n"
                "def test_cartesian(x, y): pass\n"
                "@pytest.mark.parametrize('n', range(2, 5))\n"
                "def test_range(n): pass\n"
                "def test_single(): pass\n",
                encoding="utf-8",
            )
            details = catalog_test_modules_details(repo)["tests/test_params.py"]

        self.assertEqual(details["ast_test_functions"], 3)
        self.assertEqual(details["estimated_test_items"], 10)
        self.assertEqual(details["unexpanded_parametrized_functions"], [])

    def test_ast_estimate_marks_dynamic_parametrization_as_lower_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            test_file = repo / "test_dynamic.py"
            test_file.write_text(
                "import pytest\n"
                "@pytest.mark.parametrize('value', load_cases())\n"
                "def test_dynamic(value): pass\n",
                encoding="utf-8",
            )
            details = catalog_test_modules_details(repo)["test_dynamic.py"]

        self.assertEqual(details["estimated_test_items"], 1)
        self.assertEqual(details["unexpanded_parametrized_functions"], [
            {"function": "test_dynamic", "line": 3}
        ])

    def test_catalog_extracts_attributes_and_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            test_file = repo / "tests" / "test_sample.py"
            test_file.parent.mkdir()
            test_file.write_text(
                "from pytest import fixture as fx\n"
                "import pytest as pt\n"
                "@fx(scope='module', autouse=True, params=[1, 2])\n"
                "def resource(base, request):\n"
                "    yield base\n"
                "@pt.fixture\n"
                "def base():\n"
                "    return 1\n"
                "@pt.fixture(name='public_resource')\n"
                "def internal_resource():\n"
                "    return 2\n"
                "def test_ok(resource):\n"
                "    assert resource\n",
                encoding="utf-8",
            )
            rows = catalog_project(repo, "sample", "abc123")

        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {"resource", "base", "public_resource"})
        self.assertEqual(by_name["resource"]["scope"], "module")
        self.assertIs(by_name["resource"]["autouse"], True)
        self.assertTrue(by_name["resource"]["uses_yield"])
        self.assertTrue(by_name["resource"]["parameterized"])
        self.assertEqual(by_name["resource"]["parameter_count"], 2)
        self.assertEqual(by_name["resource"]["dependencies_static"], "base")
        self.assertEqual(by_name["resource"]["commit_sha"], "abc123")
        self.assertEqual(by_name["base"]["scope"], "function")
        self.assertEqual(by_name["public_resource"]["function_name"], "internal_resource")

    def test_scope_expression_is_explicitly_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            file = repo / "conftest.py"
            file.write_text(
                "import pytest\nSCOPE = 'session'\n"
                "@pytest.fixture(scope=SCOPE)\ndef resource(): return 1\n",
                encoding="utf-8",
            )
            row = catalog_project(repo, "p")[0]
        self.assertEqual(row["scope"], "dynamic/unknown")

    def test_error_normalization_removes_unstable_values(self):
        message = "Connection failed at /tmp/project/test.py: line 42 port 54321 id abcdef1234567890"
        normalized = normalize_error(message)
        self.assertNotIn("/tmp/project", normalized)
        self.assertNotIn("54321", normalized)
        self.assertNotIn("abcdef1234567890", normalized)
        signature = error_signature("RuntimeError", message, "File /tmp/project/f.py, line 42")
        self.assertIn("RuntimeError|", signature)
        self.assertIn("<path>", signature)

    def test_lost_test_items_count_once_when_multiple_fixtures_fail(self):
        graph = {
            "test_a": {"fixture_x", "fixture_y"},
            "test_b": {"fixture_y"},
            "test_c": {"fixture_z"},
        }
        self.assertEqual(loss_by_test(graph, {"fixture_x", "fixture_y"}), {"test_a", "test_b"})

    def test_collection_threshold_and_id_stability(self):
        self.assertTrue(collection_is_viable(50, 100, 0.5))
        self.assertFalse(collection_is_viable(49, 100, 0.5))
        self.assertTrue(stable_test_ids(["a", "b"], ["b", "a"]))
        self.assertFalse(stable_test_ids(["a"], ["a", "b"]))

    def test_any_failed_repetition_fails_fixture_and_marks_intermittence(self):
        runs = [
            {"fixture_id": "f", "run_id": "r1", "instance_id": "i1", "phase": "setup", "status": "ok"},
            {"fixture_id": "f", "run_id": "r1", "instance_id": "i1", "phase": "teardown", "status": "ok"},
            {"fixture_id": "f", "run_id": "r2", "instance_id": "i2", "phase": "setup", "status": "failed", "signature": "E|x|f.py:2"},
        ]
        result = summarize_fixture_repetitions(runs)["f"]
        self.assertTrue(result["failed"])
        self.assertTrue(result["intermittent"])
        self.assertFalse(result["usable"])
        self.assertEqual(result["instances_ok"], 1)
        self.assertEqual(result["instances_failed"], 1)

    def test_x_chooses_strictest_threshold_meeting_target(self):
        rows = [
            {"collection_viable": True, "usable_fixtures": 3, "fixture_test_loss_pct": loss}
            for loss in ([10] * 250 + [25] * 20 + [45] * 20)
        ]
        selected, counts = choose_x_threshold(rows)
        self.assertEqual(selected, 20)
        self.assertEqual(counts, {20: 250, 30: 270, 50: 290})

    def test_k_identical_failure_stop_is_only_a_simulation_of_later_dependents(self):
        order = ["t1", "t2", "t3", "t4", "unrelated"]
        graph = {node: ({"f"} if node != "unrelated" else {"other"}) for node in order}
        failures = [
            {"fixture_id": "f", "signature": "same", "nodeid": node}
            for node in order[:3]
        ]
        self.assertEqual(early_stop_after_k_identical_failures(order, graph, failures), {"t4"})

    def test_pytest_probe_modes_share_collection_and_randomization_controls(self):
        plan = pytest_probe_args("plan")
        setup = pytest_probe_args("setup-only")
        normal = pytest_probe_args("normal")
        self.assertIn("--continue-on-collection-errors", plan)
        self.assertIn("--setup-plan", plan)
        self.assertIn("--setup-only", setup)
        self.assertNotIn("--setup-only", normal)
        for args in (plan, setup, normal):
            self.assertIn("-p", args)
            self.assertIn("no:randomly", args)
            self.assertIn("--timeout-method=signal", args)

    def test_pilot_sample_is_deterministic_by_tests_and_prioritizes_broad_scopes(self):
        rows = [
            {"project_id": f"p{i}", "url": f"https://example.test/{i}",
             "loc_projeto_total": 10000 - i, "ast_test_definitions": i + 1}
            for i in range(100)
        ]
        fixtures = [
            {"project_id": f"p{i}", "scope": "session" if i % 5 == 0 else "function"}
            for i in range(100)
        ]
        first = select_stratified_pilot(rows, 30, 11, fixtures)
        second = select_stratified_pilot(rows, 30, 11, fixtures)
        self.assertEqual([r["url"] for r in first], [r["url"] for r in second])
        self.assertEqual(len(first), 30)
        buckets = {min(3, (int(r["ast_test_definitions"]) - 1) * 4 // 100) for r in first}
        self.assertEqual(buckets, {0, 1, 2, 3})
        self.assertGreaterEqual(sum(int(r["project_id"][1:]) % 5 == 0 for r in first), 12)


if __name__ == "__main__":
    unittest.main()
