#!/usr/bin/env python3
"""Build the independent, project-level dynamic inventory ``mined_projects_v2.csv``.

This analyzer reads project URLs and static metadata from ``mined_projects.csv``.
It clones each repository, records the checked-out commit, installs a test profile,
and runs two isolated-in-time ``pytest --collect-only`` commands. It does not read,
import, or require any Phase 1 data or scripts.

``num_tests`` is the exact count of distinct pytest node IDs when both clean
collections produce the same IDs. ``num_fixtures`` is the exact count of
project-owned fixture definitions registered by pytest when both clean
collections produce the same definition IDs. Partial observations remain in the
provenance report; the project CSV does not pretend they are complete counts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import csv
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DEFAULT = PROJECT_ROOT / "input_data" / "mined_projects.csv"
OUTPUT_DEFAULT = PROJECT_ROOT / "input_data" / "mined_projects_v2.csv"
WORKSPACE_DEFAULT = PROJECT_ROOT / "workspace" / "analyzer_v2"
CSV_FIELDS = [
    "project_name", "descricao", "url", "num_tests", "num_fixtures",
    "loc_tests", "arquivos_tests", "loc_producao", "arquivos_producao",
    "loc_projeto_total", "motivo_exclusao",
]
PROVENANCE_FIELDS = [
    "project_name", "url", "commit_sha", "status", "python_version",
    "install_strategy", "collection_1_status", "collection_1_tests",
    "collection_1_fixtures", "collection_1_errors", "collection_1_test_ids_sha256",
    "collection_1_fixture_ids_sha256", "collection_2_status", "collection_2_tests",
    "collection_2_fixtures", "collection_2_errors", "collection_2_test_ids_sha256",
    "collection_2_fixture_ids_sha256", "collection_ids_stable",
    "fixture_ids_stable", "num_tests_exact", "num_fixtures_exact",
    "original_num_tests_ast", "original_num_fixtures_ast", "note",
]

TEST_DEPENDENCY_TOKENS = {
    "test", "tests", "testing", "unittest", "integration", "qa", "ci",
    "dev", "develop", "development",
}
REQUIREMENT_FILES = (
    "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt",
    "test-requirements.txt", "test_requirements.txt", "dev-requirements.txt",
    "requirements-dev.txt", "requirements_dev.txt", "requirements/tests.txt",
    "requirements/testing.txt", "requirements/test.txt", "requirements/dev.txt",
    "requirements/ci.txt",
)


def sha256_ids(ids: list[str]) -> str:
    payload = "\n".join(sorted(set(ids))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def run_command(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    tails = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            tail = tails[name]
            tail.extend(chunk)
            if len(tail) > 12000:
                del tail[:-12000]

    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process)
            returncode = process.wait()
        if returncode in (-signal.SIGKILL, 128 + signal.SIGKILL):
            _terminate_process_group(process, term_only=True)
        for reader in readers:
            reader.join(timeout=3)
        if any(reader.is_alive() for reader in readers):
            _terminate_process_group(process, term_only=True)
            for reader in readers:
                reader.join(timeout=2)
        for reader, stream in zip(readers, (process.stdout, process.stderr)):
            if not reader.is_alive():
                stream.close()
        return {
            "argv": argv,
            "returncode": returncode,
            "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3),
            "stdout_tail": bytes(tails["stdout"]).decode("utf-8", "replace"),
            "stderr_tail": bytes(tails["stderr"]).decode("utf-8", "replace"),
        }
    except OSError as exc:
        return {
            "argv": argv,
            "returncode": 127,
            "timed_out": False,
            "seconds": round(time.monotonic() - started, 3),
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, term_only: bool = False,
) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        if not term_only:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
    except (ProcessLookupError, OSError):
        pass


def _manifest(repo: Path) -> dict[str, Any]:
    path = repo / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}


def _test_names(mapping: Any) -> list[str]:
    if not isinstance(mapping, dict):
        return []
    names = []
    for name in mapping:
        tokens = set(re.findall(r"[a-z0-9]+", str(name).casefold()))
        if tokens & TEST_DEPENDENCY_TOKENS:
            names.append(str(name))
    return sorted(names)


def _requirement_files(repo: Path) -> list[str]:
    found = [name for name in REQUIREMENT_FILES if (repo / name).is_file()]
    req_dir = repo / "requirements"
    if req_dir.is_dir():
        for path in sorted(req_dir.glob("*.txt")):
            if re.search(r"test|dev|ci|qa|integration", path.name, re.I):
                found.append(path.relative_to(repo).as_posix())
    return list(dict.fromkeys(found))


def installation_strategies(repo: Path) -> list[tuple[str, list[str]]]:
    """Manifest-led common test/development profiles, with narrower fallbacks."""
    data = _manifest(repo)
    project = data.get("project", {})
    project = project if isinstance(project, dict) else {}
    extras = _test_names(project.get("optional-dependencies", {}))
    groups = _test_names(data.get("dependency-groups", {}))

    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {})
    if isinstance(poetry, dict):
        extras = sorted(set(extras) | set(_test_names(poetry.get("extras", {}))))
        groups = sorted(set(groups) | set(_test_names(poetry.get("group", {}))))

    setup_cfg = repo / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
            if parser.has_section("options.extras_require"):
                extras = sorted(set(extras) | set(_test_names(dict(parser.items("options.extras_require")))))
        except (OSError, configparser.Error):
            pass

    reqs = _requirement_files(repo)
    editable = any((repo / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg"))
    strategies: list[tuple[str, list[str]]] = []

    if extras or groups:
        combined: list[str] = []
        if groups:
            combined.extend(["--project", str(repo)])
            for group in groups:
                combined.extend(["--group", group])
        for req in reqs:
            combined.extend(["--requirement", req])
        if editable:
            extra_spec = f".[{','.join(extras)}]" if extras else "."
            combined.extend(["--editable", extra_spec])
        strategies.append(("combined_test_profiles", combined))

        if extras:
            strategies.append(("combined_test_extras", ["--editable", f".[{','.join(extras)}]"]))
            for extra in extras:
                strategies.append((f"test_extra_{extra}", ["--editable", f".[{extra}]"]))
        for group in groups:
            group_args = ["--project", str(repo), "--group", group]
            if editable:
                group_args.extend(["--editable", "."])
            strategies.append((f"test_group_{group}", group_args))

    if reqs:
        all_reqs: list[str] = []
        for req in reqs:
            all_reqs.extend(["--requirement", req])
        if editable:
            all_reqs.extend(["--editable", "."])
        strategies.append(("test_requirements", all_reqs))
        for req in reqs:
            one = ["--requirement", req]
            if editable:
                one.extend(["--editable", "."])
            strategies.append((f"requirements_{Path(req).name}", one))

    if editable:
        strategies.append(("editable_project", ["--editable", "."]))
    elif (repo / "requirements.txt").is_file():
        strategies.append(("requirements_txt", ["--requirement", "requirements.txt"]))

    unique: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, ...]] = set()
    for name, args in strategies:
        key = tuple(args)
        if key not in seen:
            seen.add(key)
            unique.append((name, args))
    return unique or [("pytest_tooling_only", [])]


def _python_path(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def make_venv(
    uv: str | None, python_request: str, venv: Path, repo: Path,
    env: dict[str, str], timeout: int,
) -> tuple[Path | None, dict[str, Any]]:
    if uv:
        command = [uv, "venv", "--python", python_request, str(venv)]
    else:
        interpreter = shutil.which(python_request)
        if interpreter is None and python_request == f"{sys.version_info.major}.{sys.version_info.minor}":
            interpreter = sys.executable
        if interpreter is None:
            return None, {"returncode": 127, "stderr_tail": f"Python indisponível: {python_request}"}
        command = [interpreter, "-m", "venv", str(venv)]
    result = run_command(command, cwd=repo, env=env, timeout=timeout)
    python = _python_path(venv)
    return (python if result.get("returncode") == 0 and python.is_file() else None), result


def install_command(
    uv: str | None, python: Path, args: list[str],
) -> list[str]:
    if uv:
        return [uv, "pip", "install", "--python", str(python), *args]
    return [str(python), "-m", "pip", "install", *args]


def _run_profile(
    *, name: str, args: list[str], repo: Path, python: Path, uv: str | None,
    env: dict[str, str], timeout: int,
) -> tuple[dict[str, Any], bool]:
    attempts: list[dict[str, Any]] = []
    if args:
        result = run_command(install_command(uv, python, args), cwd=repo, env=env, timeout=timeout)
        attempts.append({"step": name, **result})
    else:
        result = {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": ""}
    if result.get("returncode") != 0 or result.get("timed_out"):
        return {"strategy": name, "commands": attempts}, False

    check = run_command(
        [str(python), "-m", "pytest", "--version"], cwd=repo,
        env=env, timeout=60,
    )
    if check.get("returncode") != 0:
        tooling = run_command(
            install_command(uv, python, ["pytest"]), cwd=repo,
            env=env, timeout=timeout,
        )
        attempts.append({"step": "pytest_tooling_fallback", **tooling})
        if tooling.get("returncode") != 0 or tooling.get("timed_out"):
            return {"strategy": name, "commands": attempts}, False
    return {"strategy": name, "commands": attempts}, True


def static_metrics(repo: Path, cloc: str) -> dict[str, int]:
    py_files = [
        path for path in repo.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(repo).parts)
        and not any(part in {"site-packages", "dist-packages", "venv", ".tox"}
                    or part.startswith(".venv") for part in path.relative_to(repo).parts)
    ]
    test_files = [
        path for path in py_files
        if path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py"
    ]
    test_set = set(test_files)
    prod_files = [path for path in py_files if path not in test_set]

    def count(files: list[Path]) -> tuple[int, int]:
        if not files:
            return 0, 0
        result = subprocess.run(
            [cloc, "--json", *map(str, files)], capture_output=True,
            text=True, errors="replace", check=False,
        )
        try:
            stats = json.loads(result.stdout).get("Python", {})
            return int(stats.get("nFiles", 0)), int(stats.get("code", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            return len(files), 0

    test_file_count, test_loc = count(test_files)
    prod_file_count, prod_loc = count(prod_files)
    return {
        "loc_tests": test_loc,
        "arquivos_tests": test_file_count,
        "loc_producao": prod_loc,
        "arquivos_producao": prod_file_count,
        "loc_projeto_total": test_loc + prod_loc,
    }


def clone_repository(
    url: str, repo: Path, env: dict[str, str], timeout: int,
) -> tuple[str | None, dict[str, Any]]:
    result = run_command(
        ["git", "clone", "--depth", "1", url, str(repo)],
        cwd=repo.parent, env=env, timeout=timeout,
    )
    if result.get("returncode") != 0 or result.get("timed_out"):
        return None, result
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=False,
    )
    if sha.returncode != 0:
        return None, {**result, "stderr_tail": (result.get("stderr_tail", "") + sha.stderr)[-12000:]}
    return sha.stdout.strip(), result


def collect_once(
    *, repo: Path, python: Path, run_file: Path, project_root: Path,
    env_base: dict[str, str], timeout: int,
) -> dict[str, Any]:
    env = dict(env_base)
    env["ANALYZER_V2_OUTPUT"] = str(run_file)
    env["ANALYZER_V2_PROJECT_ROOT"] = str(project_root)
    plugin_dir = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = plugin_dir + os.pathsep + env.get("PYTHONPATH", "")
    argv = [
        str(python), "-m", "pytest", "--collect-only", "--continue-on-collection-errors",
        "-q", "-p", "no:cacheprovider", "-p", "analyzer_v2_probe",
    ]
    command = run_command(argv, cwd=repo, env=env, timeout=timeout)
    probe: dict[str, Any] = {}
    try:
        probe = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    ids = probe.get("test_ids", []) if isinstance(probe, dict) else []
    fixtures = probe.get("project_fixture_ids", []) if isinstance(probe, dict) else []
    return {
        **command,
        "test_count": len(set(ids)) if isinstance(ids, list) else None,
        "fixture_count": len(set(fixtures)) if isinstance(fixtures, list) else None,
        "fixture_registry_available": bool(probe.get("fixture_registry_available")) if probe else False,
        "collection_error_count": probe.get("collection_error_count") if probe else None,
        "test_ids_sha256": probe.get("test_ids_sha256", "") if probe else "",
        "fixture_ids_sha256": probe.get("fixture_ids_sha256", "") if probe else "",
        "probe_written": bool(probe),
        "python_version": probe.get("python_version", "") if probe else "",
        "stderr_tail": command.get("stderr_tail", ""),
        "stdout_tail": command.get("stdout_tail", ""),
    }


def collection_exactness(runs: list[dict[str, Any]]) -> tuple[bool, bool]:
    if len(runs) != 2:
        return False, False
    completed = all(
        run.get("probe_written")
        and not run.get("timed_out")
        and run.get("returncode") in (0, 5)
        and run.get("collection_error_count") == 0
        for run in runs
    )
    tests_stable = (
        completed
        and all(isinstance(run.get("test_count"), int) for run in runs)
        and runs[0].get("test_count") == runs[1].get("test_count")
        and runs[0].get("test_ids_sha256") == runs[1].get("test_ids_sha256")
    )
    fixtures_stable = (
        completed
        and all(run.get("fixture_registry_available") for run in runs)
        and all(isinstance(run.get("fixture_count"), int) for run in runs)
        and runs[0].get("fixture_count") == runs[1].get("fixture_count")
        and runs[0].get("fixture_ids_sha256") == runs[1].get("fixture_ids_sha256")
    )
    return bool(tests_stable), bool(fixtures_stable)


def _observation_score(record: dict[str, Any]) -> tuple[int, int, int, int]:
    runs = record.get("collection_runs") or []
    collected = sum(int(run.get("test_count") or 0) for run in runs)
    errors = sum(int(run.get("collection_error_count") or 0) for run in runs)
    return (
        int(bool(record.get("collection_ids_stable"))),
        int(bool(record.get("fixture_ids_stable"))),
        collected,
        -errors,
    )


def analyze_project(
    source: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    url = str(source.get("url", "")).strip()
    name = str(source.get("project_name", "") or url.rstrip("/").split("/")[-1])
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    record_dir = args.workspace / "projects" / key
    record_path = record_dir / "project_result.json"
    if args.resume and record_path.is_file() and not args.retry_failures:
        try:
            saved = json.loads(record_path.read_text(encoding="utf-8"))
            if saved.get("url") == url and saved.get("analyzer_schema") == 1:
                return saved
        except (OSError, json.JSONDecodeError):
            pass

    started = time.time()
    print(f"[Analyzer V2] iniciando {name}: {url}", flush=True)
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(args.workspace / "uv-cache")
    uv = shutil.which(args.uv) if args.uv else None
    if args.uv and uv is None:
        return _failure_record(source, key, "uv_unavailable", f"uv não encontrado: {args.uv}")

    project_record: dict[str, Any] = {
        "analyzer_schema": 1,
        "project_name": name,
        "url": url,
        "status": "in_progress",
        "started_at_epoch": started,
        "original_num_tests_ast": source.get("num_tests", ""),
        "original_num_fixtures_ast": source.get("num_fixtures", ""),
    }
    record_dir.mkdir(parents=True, exist_ok=True)

    temp_parent = args.workspace / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temporary = None if args.keep_workspaces else tempfile.TemporaryDirectory(
        prefix=f"analyzer-v2-{key}-", dir=temp_parent
    )
    work = Path(temporary.name) if temporary else record_dir / f"workspace-{int(started)}"
    work.mkdir(parents=True, exist_ok=True)
    repo = work / "source"
    try:
        sha, clone = clone_repository(url, repo, env, args.clone_timeout)
        project_record["clone"] = clone
        if not sha:
            project_record.update(status="clone_failed", note="git clone não concluído")
            return _save_project_record(project_record, record_path, temporary)
        project_record["commit_sha"] = sha
        project_record.update(static_metrics(repo, args.cloc))

        install_attempts: list[dict[str, Any]] = []
        best_observation: dict[str, Any] | None = None
        strategies = installation_strategies(repo)
        for candidate_index, python_request in enumerate(args.python_candidates, start=1):
            attempts_for_python = 0
            for strategy_index, (strategy_name, profile_args) in enumerate(strategies, start=1):
                # A clean environment per strategy prevents failed installs from
                # contaminating a later attempt.
                venv = work / f"venv-{candidate_index}-{strategy_index}"
                python, venv_result = make_venv(
                    uv, python_request, venv, repo, env, args.install_timeout,
                )
                install_attempt: dict[str, Any] = {
                    "python_request": python_request,
                    "strategy": strategy_name,
                    "venv": venv_result,
                }
                if python is None:
                    install_attempt["status"] = "python_unavailable"
                    install_attempts.append(install_attempt)
                    break

                profile_attempt, ready = _run_profile(
                    name=strategy_name, args=profile_args,
                    repo=repo, python=python, uv=uv, env=env,
                    timeout=args.install_timeout,
                )
                install_attempt["commands"] = profile_attempt["commands"]
                install_attempt["status"] = "ready" if ready else "install_failed"
                install_attempts.append(install_attempt)
                attempts_for_python += 1
                if ready:
                    collect_runs = []
                    for collect_index in (1, 2):
                        result_path = record_dir / f"collect-{candidate_index}-{strategy_index}-{collect_index}.json"
                        result_path.unlink(missing_ok=True)
                        collect_runs.append(collect_once(
                            repo=repo, python=python, run_file=result_path,
                            project_root=repo.resolve(), env_base=env,
                            timeout=args.collect_timeout,
                        ))
                    tests_stable, fixtures_stable = collection_exactness(collect_runs)
                    observation = {
                        "python_version": _python_version(python, repo, env),
                        "python_request": python_request,
                        "install_strategy": strategy_name,
                        "collection_runs": collect_runs,
                        "collection_ids_stable": tests_stable,
                        "fixture_ids_stable": fixtures_stable,
                        "oom_suspected": any(
                            run.get("returncode") in (-signal.SIGKILL, 128 + signal.SIGKILL)
                            for run in collect_runs
                        ),
                        "num_tests": collect_runs[0].get("test_count") if tests_stable else "",
                        "num_fixtures": collect_runs[0].get("fixture_count") if fixtures_stable else "",
                    }
                    install_attempt["collection_runs"] = collect_runs
                    score = _observation_score(observation)
                    if best_observation is None or score > _observation_score(best_observation):
                        best_observation = observation
                    if tests_stable and fixtures_stable:
                        best_observation = observation
                        break
                    if observation["oom_suspected"]:
                        best_observation = observation
                        break
                if args.max_install_attempts_per_python and attempts_for_python >= args.max_install_attempts_per_python:
                    break
            if best_observation and best_observation["collection_ids_stable"] and best_observation["fixture_ids_stable"]:
                break
            if best_observation and best_observation.get("oom_suspected"):
                break

        project_record["installation_attempts"] = install_attempts
        if best_observation is None:
            project_record.update(status="dependency_install_failed", note="nenhum perfil de teste instalou com sucesso")
            return _save_project_record(project_record, record_path, temporary)
        project_record.update(best_observation)
        project_record.update(
            status=(
                "analyzed" if best_observation["collection_ids_stable"] and best_observation["fixture_ids_stable"]
                else "collection_oom_suspected" if best_observation.get("oom_suspected")
                else "collection_incomplete_or_unstable"
            ),
            note=(
                "" if best_observation["collection_ids_stable"] and best_observation["fixture_ids_stable"]
                else "contagens parciais/inestáveis; consulte collection_runs"
            ),
        )
        return _save_project_record(project_record, record_path, temporary)
    except Exception as exc:
        project_record.update(status="analyzer_error", note=f"{type(exc).__name__}: {exc}")
        return _save_project_record(project_record, record_path, temporary)


def _python_version(python: Path, cwd: Path, env: dict[str, str]) -> str:
    result = run_command([str(python), "--version"], cwd=cwd, env=env, timeout=30)
    return (result.get("stdout_tail") or result.get("stderr_tail") or "").strip()


def _failure_record(source: dict[str, str], key: str, status: str, note: str) -> dict[str, Any]:
    return {
        "analyzer_schema": 1,
        "project_name": source.get("project_name", ""),
        "url": source.get("url", ""),
        "status": status,
        "note": note,
        "original_num_tests_ast": source.get("num_tests", ""),
        "original_num_fixtures_ast": source.get("num_fixtures", ""),
    }


def _save_project_record(
    record: dict[str, Any], path: Path, temporary: tempfile.TemporaryDirectory[str] | None,
) -> dict[str, Any]:
    write_json_atomic(path, record)
    if temporary is not None:
        temporary.cleanup()
    return record


def output_rows(
    source_rows: list[dict[str, str]], records: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projects: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for source in source_rows:
        key = hashlib.sha256(str(source.get("url", "")).strip().encode("utf-8")).hexdigest()[:16]
        record = records.get(key, {})
        row = {field: source.get(field, "") for field in CSV_FIELDS}
        for field in (
            "loc_tests", "arquivos_tests", "loc_producao",
            "arquivos_producao", "loc_projeto_total",
        ):
            row[field] = record.get(field, row.get(field, ""))
        row["num_tests"] = record.get("num_tests", "")
        row["num_fixtures"] = record.get("num_fixtures", "")

        status = str(record.get("status") or "not_analyzed")
        reason = ""
        if status == "clone_failed":
            reason = "clone_failed"
        elif status == "dependency_install_failed":
            reason = "dependency_install_failed"
        elif status in {"analyzer_error", "uv_unavailable", "not_analyzed"}:
            reason = status
        elif status in {"collection_incomplete_or_unstable", "collection_oom_suspected"}:
            reason = status
        elif row["num_tests"] == 0:
            reason = "nenhum item pytest coletado"
        elif row["num_fixtures"] == 0:
            reason = "nenhuma fixture de projeto registrada pelo pytest"
        row["motivo_exclusao"] = reason
        projects.append(row)

        runs = record.get("collection_runs") or [{}, {}]
        runs = (runs + [{}, {}])[:2]
        provenance.append({
            "project_name": source.get("project_name", ""),
            "url": source.get("url", ""),
            "commit_sha": record.get("commit_sha", ""),
            "status": status,
            "python_version": record.get("python_version", ""),
            "install_strategy": record.get("install_strategy", ""),
            "collection_1_status": _collection_status(runs[0]),
            "collection_1_tests": runs[0].get("test_count", ""),
            "collection_1_fixtures": runs[0].get("fixture_count", ""),
            "collection_1_errors": runs[0].get("collection_error_count", ""),
            "collection_1_test_ids_sha256": runs[0].get("test_ids_sha256", ""),
            "collection_1_fixture_ids_sha256": runs[0].get("fixture_ids_sha256", ""),
            "collection_2_status": _collection_status(runs[1]),
            "collection_2_tests": runs[1].get("test_count", ""),
            "collection_2_fixtures": runs[1].get("fixture_count", ""),
            "collection_2_errors": runs[1].get("collection_error_count", ""),
            "collection_2_test_ids_sha256": runs[1].get("test_ids_sha256", ""),
            "collection_2_fixture_ids_sha256": runs[1].get("fixture_ids_sha256", ""),
            "collection_ids_stable": record.get("collection_ids_stable", False),
            "fixture_ids_stable": record.get("fixture_ids_stable", False),
            "num_tests_exact": bool(record.get("collection_ids_stable")),
            "num_fixtures_exact": bool(record.get("fixture_ids_stable")),
            "original_num_tests_ast": source.get("num_tests", ""),
            "original_num_fixtures_ast": source.get("num_fixtures", ""),
            "note": record.get("note", ""),
        })
    return projects, provenance


def _collection_status(run: dict[str, Any]) -> str:
    if not run:
        return "not_run"
    if run.get("timed_out"):
        return "timeout"
    if not run.get("probe_written"):
        return "probe_output_missing"
    if run.get("collection_error_count", 0):
        return "completed_with_collection_errors"
    if run.get("returncode") == 5:
        return "completed_no_tests"
    if run.get("returncode") == 0:
        return "completed_cleanly"
    return "pytest_error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT_DEFAULT,
                        help="CSV de origem com URLs e metadados estáticos")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT,
                        help="CSV v2 por projeto; não altera o CSV de origem")
    parser.add_argument("--provenance", type=Path,
                        help="CSV complementar de SHA, completude e hashes de IDs")
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_DEFAULT,
                        help="diretório de checkpoints/cache do Analyzer V2")
    parser.add_argument("--uv", default="uv", help="executável uv; use vazio para pip/venv local")
    parser.add_argument("--python-candidates", default="3.13,3.12,3.11",
                        help="versões tentadas, na ordem informada")
    parser.add_argument("--workers", type=int, default=1,
                        help="projetos simultâneos (padrão conservador: 1)")
    parser.add_argument("--max-install-attempts-per-python", type=int, default=4,
                        help="máximo de perfis tentados por versão Python; 0 = sem limite")
    parser.add_argument("--clone-timeout", type=int, default=900)
    parser.add_argument("--install-timeout", type=int, default=1800)
    parser.add_argument("--collect-timeout", type=int, default=1800)
    parser.add_argument("--cloc", default="cloc")
    parser.add_argument("--resume", action="store_true",
                        help="reutiliza checkpoints do mesmo URL quando já registrados")
    parser.add_argument("--retry-failures", action="store_true",
                        help="com --resume, repete projetos que não terminaram como analyzed")
    parser.add_argument("--keep-workspaces", action="store_true",
                        help="preserva clones e venvs para diagnóstico (consome bastante disco)")
    args = parser.parse_args(argv)
    args.python_candidates = [v.strip() for v in args.python_candidates.split(",") if v.strip()]
    args.workspace = args.workspace.resolve()
    args.workspace.mkdir(parents=True, exist_ok=True)

    with args.input.open(encoding="utf-8", newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    if not source_rows:
        parser.error(f"CSV de entrada vazio: {args.input}")
    missing = {"project_name", "url"} - set(source_rows[0])
    if missing:
        parser.error("colunas obrigatórias ausentes: " + ", ".join(sorted(missing)))

    deduplicated: dict[str, dict[str, str]] = {}
    for row in source_rows:
        normalized_url = str(row.get("url", "")).strip().rstrip("/").removesuffix(".git").casefold()
        if normalized_url:
            deduplicated.setdefault(normalized_url, row)
    source_rows = list(deduplicated.values())
    keys = [hashlib.sha256(str(row.get("url", "")).strip().encode("utf-8")).hexdigest()[:16]
            for row in source_rows]
    records: dict[str, dict[str, Any]] = {}
    if args.resume:
        for key in keys:
            path = args.workspace / "projects" / key / "project_result.json"
            if path.is_file():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if (saved.get("analyzer_schema") == 1
                            and (not args.retry_failures or saved.get("status") == "analyzed")):
                        records[key] = saved
                except (OSError, json.JSONDecodeError):
                    pass

    pending = [(key, row) for key, row in zip(keys, source_rows) if key not in records]
    print(f"Projetos na entrada: {len(source_rows)}; checkpoints reutilizados: {len(records)}")
    if args.workers < 1:
        parser.error("--workers precisa ser >= 1")
    if args.max_install_attempts_per_python < 0:
        parser.error("--max-install-attempts-per-python precisa ser >= 0")
    if not args.python_candidates:
        parser.error("--python-candidates precisa conter ao menos uma versão")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {
            pool.submit(analyze_project, row, args): key
            for key, row in pending
        }
        completed = len(records)
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                records[key] = future.result()
            except Exception as exc:
                records[key] = _failure_record(
                    next(row for candidate_key, row in pending if candidate_key == key),
                    key, "analyzer_error", f"{type(exc).__name__}: {exc}",
                )
            completed += 1
            project_rows, provenance_rows = output_rows(source_rows, records)
            write_csv_atomic(args.output, CSV_FIELDS, project_rows)
            provenance_path = args.provenance or args.output.with_name("mined_projects_v2_provenance.csv")
            write_csv_atomic(provenance_path, PROVENANCE_FIELDS, provenance_rows)
            write_json_atomic(args.workspace / "progress.json", {
                "status": "in_progress" if completed < len(source_rows) else "complete",
                "completed": completed,
                "total": len(source_rows),
                "last_project": records[key].get("project_name"),
                "last_status": records[key].get("status"),
            })
            print(f"[{completed}/{len(source_rows)}] {records[key].get('project_name')}: {records[key].get('status')}")

    project_rows, provenance_rows = output_rows(source_rows, records)
    write_csv_atomic(args.output, CSV_FIELDS, project_rows)
    provenance_path = args.provenance or args.output.with_name("mined_projects_v2_provenance.csv")
    write_csv_atomic(provenance_path, PROVENANCE_FIELDS, provenance_rows)
    summary: dict[str, int] = {}
    for record in records.values():
        state = str(record.get("status", "unknown"))
        summary[state] = summary.get(state, 0) + 1
    write_json_atomic(args.workspace / "progress.json", {
        "status": "complete", "completed": len(source_rows),
        "total": len(source_rows), "status_counts": summary,
    })
    print(f"CSV v2: {args.output}")
    print(f"Proveniência: {provenance_path}")
    print("Estados:", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
