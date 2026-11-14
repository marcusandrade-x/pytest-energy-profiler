#!/usr/bin/env python3
"""Validate an isolation launcher and run Phase 1 controls through it.

The launcher is a JSON argv prefix (never shell-evaluated) that accepts
``-- COMMAND ARGS...``. Project executions must use the same validated prefix.
This command intentionally has no project-execution mode until controls pass.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from phase1_catalog import pytest_probe_args, stable_test_ids


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLS = PROJECT_ROOT / "phase1" / "controls"


def run_isolated(
    launcher: list[str], command: list[str], *, cwd: Path,
    env: dict[str, str], timeout: float,
) -> dict[str, Any]:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [*launcher, "--", *command], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return {
            "returncode": proc.returncode,
            "timed_out": False,
            "seconds": round(time.monotonic() - start, 3),
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "seconds": round(time.monotonic() - start, 3),
            "stdout": str(exc.stdout or "")[-12000:],
            "stderr": str(exc.stderr or "")[-12000:],
        }
    except OSError as exc:
        return {
            "returncode": 127,
            "timed_out": False,
            "seconds": round(time.monotonic() - start, 3),
            "stdout": "",
            "stderr": f"launcher error: {type(exc).__name__}: {exc}",
        }


def _probe_env(base: dict[str, str], output: Path, run_id: str) -> dict[str, str]:
    env = dict(base)
    env.update({
        "PHASE1_PROJECT_ID": "phase1-controls",
        "PHASE1_PROJECT_ROOT": str(CONTROLS.resolve()),
        "PHASE1_RUN_ID": run_id,
        "PHASE1_OUTPUT": str(output.resolve()),
        "PYTHONPATH": str(PROJECT_ROOT / "scripts"),
    })
    return env


def _pytest_command(python: str, target: Path, mode: str, timeout: int) -> list[str]:
    return [
        python, "-m", "pytest", str(target),
        *pytest_probe_args(mode, timeout),
        "-p", "phase1_probe", "-q",
    ]


def _load_probe(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_controls(launcher: list[str], python: str, output_dir: Path,
                 timeout: int, base_env: dict[str, str]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"controls": {}, "passed": False}

    def run_probe(name: str, mode: str, target: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata_path = output_dir / f"{name}.json"
        result = run_isolated(
            launcher,
            _pytest_command(python, target, mode, timeout),
            cwd=CONTROLS,
            env=_probe_env(base_env, metadata_path, name),
            timeout=30 * 60,
        )
        return result, _load_probe(metadata_path)

    net = run_isolated(
        launcher,
        [python, str(PROJECT_ROOT / "scripts" / "phase1_network_check.py")],
        cwd=CONTROLS,
        env=base_env,
        timeout=30,
    )
    try:
        net_data = json.loads(net["stdout"])
    except json.JSONDecodeError:
        net_data = {"passed": False, "parse_error": "network check did not emit JSON"}
    results["controls"]["isolation"] = {
        "passed": bool(net_data.get("passed")), **net, "checks": net_data
    }

    dependencies = run_isolated(
        launcher,
        [python, "-c", "import pytest, pytest_timeout, pytest_asyncio; print(pytest.__version__)"],
        cwd=CONTROLS,
        env=base_env,
        timeout=30,
    )
    results["controls"]["control_dependencies"] = {
        "passed": dependencies["returncode"] == 0,
        "run": dependencies,
    }
    if dependencies["returncode"] != 0:
        results["passed"] = False
        return results

    plan_1, plan_meta_1 = run_probe("setup-plan-1", "plan", CONTROLS)
    plan_2, plan_meta_2 = run_probe("setup-plan-2", "plan", CONTROLS)
    plan_stable = stable_test_ids(plan_meta_1.get("test_ids", []), plan_meta_2.get("test_ids", []))
    results["controls"]["collection_stability"] = {
        "passed": bool(plan_1["returncode"] == 0 and plan_2["returncode"] == 0
                        and plan_meta_1.get("test_ids") and plan_stable),
        "first_count": len(plan_meta_1.get("test_ids", [])),
        "second_count": len(plan_meta_2.get("test_ids", [])),
        "stable_ids": plan_stable,
        "first": plan_1,
        "second": plan_2,
    }

    positive, positive_meta = run_probe(
        "positive-setup-only", "setup-only", CONTROLS / "test_positive.py"
    )
    positive_events = positive_meta.get("fixture_events", [])
    positive_setup = [e for e in positive_events if e.get("phase") == "setup"]
    positive_teardown = [e for e in positive_events if e.get("phase") == "teardown"]
    positive_pass = (
        positive["returncode"] == 0
        and len(positive_meta.get("test_ids", [])) >= 2
        and len(positive_setup) >= 2 and len(positive_teardown) >= 2
        and all(e.get("status") == "ok" for e in positive_setup + positive_teardown)
    )
    results["controls"]["positive_setup_teardown"] = {
        "passed": positive_pass,
        "setup_instances": len(positive_setup),
        "teardown_instances": len(positive_teardown),
        "run": positive,
    }

    local, local_meta = run_probe(
        "local-server", "normal", CONTROLS / "test_controls.py::test_local_server_round_trip"
    )
    local_outcomes = local_meta.get("test_phase_outcomes", {})
    local_pass = local["returncode"] == 0 and any(
        phases.get("call") == "passed" for phases in local_outcomes.values()
    )
    results["controls"]["local_server"] = {
        "passed": local_pass,
        "outcomes": local_outcomes,
        "run": local,
    }

    external, external_meta = run_probe(
        "external-service", "setup-only", CONTROLS / "test_controls.py::test_external_dependency_is_blocked"
    )
    external_events = [
        event for event in external_meta.get("fixture_events", [])
        if event.get("name") == "external_service" and event.get("phase") == "setup"
    ]
    external_pass = (
        external["returncode"] != 0 and bool(external_events)
        and any(event.get("status") == "failed" for event in external_events)
    )
    results["controls"]["external_dependency"] = {
        "passed": external_pass,
        "events": external_events,
        "run": external,
    }

    async_run, async_meta = run_probe(
        "async", "normal", CONTROLS / "test_controls.py::test_async_control"
    )
    async_outcomes = async_meta.get("test_phase_outcomes", {})
    async_pass = async_run["returncode"] == 0 and any(
        phases.get("call") == "passed" for phases in async_outcomes.values()
    )
    results["controls"]["async_test"] = {
        "passed": async_pass,
        "outcomes": async_outcomes,
        "run": async_run,
    }

    k_path = output_dir / "circuit-breaker-k3.json"
    k_env = _probe_env(base_env, k_path, "circuit-breaker-k3")
    k_env["PHASE1_CIRCUIT_BREAKER_K"] = "3"
    k_run = run_isolated(
        launcher,
        _pytest_command(python, CONTROLS / "test_circuit_breaker.py", "normal", timeout),
        cwd=CONTROLS, env=k_env, timeout=30 * 60,
    )
    k_meta = _load_probe(k_path)
    k_failures = [
        event for event in k_meta.get("fixture_events", [])
        if event.get("name") == "repeatable_failure"
        and event.get("phase") == "setup" and event.get("status") == "failed"
    ]
    k_aborted = k_meta.get("circuit_breaker", {}).get("aborted_tests", [])
    k_outcomes = k_meta.get("test_phase_outcomes", {})
    k_pass = (
        k_run["returncode"] == 1
        and len(k_failures) == 3
        and len({event.get("signature") for event in k_failures}) == 1
        and len(k_aborted) == 1
        and k_aborted[0].get("nodeid", "").endswith("test_k_dependent_4")
        and any(
            nodeid.endswith("test_k_unrelated") and phases.get("call") == "passed"
            for nodeid, phases in k_outcomes.items()
        )
    )
    results["controls"]["circuit_breaker_k3"] = {
        "passed": k_pass,
        "identical_failures": len(k_failures),
        "aborted_tests": k_aborted,
        "run": k_run,
    }

    results["passed"] = all(
        result.get("passed", False) for result in results["controls"].values()
    ) and bool(net_data.get("passed"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolation-launcher-json", required=True,
                        help="JSON argv prefix; it receives `-- COMMAND ARGS...`.")
    parser.add_argument("--python", required=True,
                        help="Python from the dedicated control/project environment.")
    parser.add_argument("--run-uid", type=int, required=True,
                        help="Dedicated, non-root UID used inside isolation.")
    parser.add_argument("--run-gid", type=int, required=True)
    parser.add_argument("--workspace", type=Path, required=True,
                        help="Must be writable by --run-uid/--run-gid.")
    parser.add_argument("--timeout-per-test", type=int, default=120)
    args = parser.parse_args()

    try:
        launcher = json.loads(args.isolation_launcher_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--isolation-launcher-json inválido: {exc}")
    if not isinstance(launcher, list) or not launcher or not all(isinstance(x, str) for x in launcher):
        parser.error("launcher deve ser uma lista JSON não vazia de argumentos")
    if args.run_uid == 0:
        parser.error("execução como UID 0 proibida")
    if os.geteuid() != args.run_uid or os.getegid() != args.run_gid:
        parser.error("execute o runner como o usuário dedicado indicado por --run-uid/--run-gid")
    if not Path(launcher[0]).is_absolute():
        parser.error("o executável do launcher deve usar caminho absoluto")
    if not Path(launcher[0]).is_file() or not os.access(launcher[0], os.X_OK):
        parser.error("o launcher precisa existir e ser executável")

    workspace = args.workspace.resolve()
    output_dir = workspace / "validation"
    home = workspace / "home"
    tmp = workspace / "tmp"
    for path in (output_dir, home, tmp):
        path.mkdir(parents=True, exist_ok=True)
    safe_env = {
        # Keep the venv's bin/python path intact. Resolving the symlink to the
        # base interpreter makes Python ignore the venv's site-packages.
        "PATH": f"{Path(os.path.abspath(args.python)).parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PHASE1_RUN_UID": str(args.run_uid),
        "PHASE1_RUN_GID": str(args.run_gid),
    }
    result = run_controls(launcher, os.path.abspath(args.python), output_dir,
                          args.timeout_per_test, safe_env)
    result["launcher"] = launcher
    result["run_uid_requested"] = args.run_uid
    result["run_gid_requested"] = args.run_gid
    result["created_at_epoch"] = int(time.time())
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "isolation_validation.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
