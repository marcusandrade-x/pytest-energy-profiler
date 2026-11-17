#!/usr/bin/env python3
"""Run the fail-closed Phase 1 feasibility pilot.

Installation runs online as the dedicated user. Collection and pytest phases
run only after the isolation controls pass for that project's environment.
The executor records two collections, a setup plan, two full runs, and for a
preselected subset the setup-only comparison and K=3 circuit-breaker run.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import grp
import json
import os
import pwd
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

try:  # Supports both direct script execution and importing from unit tests.
    from phase1_catalog import (
        catalog_project, catalog_test_modules_details, collection_is_viable,
        stable_test_ids, summarize_fixture_repetitions,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by module imports
    from scripts.phase1_catalog import (
        catalog_project, catalog_test_modules_details, collection_is_viable,
        stable_test_ids, summarize_fixture_repetitions,
    )


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "phase1_runner.py"
SAMPLE = ROOT / "phase1" / "data" / "pilot" / "pilot_sample.csv"
SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
URL_CREDENTIALS_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
URL_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|access_token|password|key)=)[^&\s]+")
TEST_DEPENDENCY_TOKENS = {
    "test", "tests", "testing", "unittest", "integration", "qa", "ci",
    "dev", "develop", "development",
}
TEST_REQUIREMENT_FILES = (
    "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt",
    "test-requirements.txt", "test_requirements.txt", "dev-requirements.txt",
    "requirements-dev.txt", "requirements_dev.txt",
    "requirements/test.txt", "requirements/tests.txt", "requirements/testing.txt",
    "requirements/dev.txt", "requirements/ci.txt",
)
REQUIREMENT_INCLUDE_RE = re.compile(r"(test|dev|ci|qa|integration)", re.IGNORECASE)
REQUIREMENT_EXCLUDE_RE = re.compile(
    r"(doc|lint|typing|mypy|build|release|style|format|bench|audit|"
    r"pre-?commit|publish)", re.IGNORECASE,
)
COMPLETED_PROJECT_STATUSES = {
    "pilot_project_complete",
    "pilot_project_complete_with_collection_errors",
    "pilot_project_complete_with_test_failures",
}
TERMINAL_PILOT_STATUSES = COMPLETED_PROJECT_STATUSES | {
    "dependency_unresolvable",
    "no_compatible_python_candidate",
    "collection_unviable_for_all_python_candidates",
}


def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run argv without a shell and retain bounded output and timeout status."""
    started = time.monotonic()
    proc: subprocess.Popen[bytes] | None = None
    tails = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            tail = tails[name]
            tail.extend(chunk)
            if len(tail) > 20000:
                del tail[:-20000]

    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        readers = [
            threading.Thread(target=drain, args=(name, stream), daemon=True)
            for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
        ]
        for reader in readers:
            reader.start()
        proc.wait(timeout=timeout)
        for reader in readers:
            reader.join()
        proc.stdout.close()
        proc.stderr.close()
        return {
            "argv": [redact(str(arg)) for arg in argv],
            "returncode": proc.returncode,
            "timed_out": False,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": redact(bytes(tails["stdout"]).decode("utf-8", "replace")),
            "stderr": redact(bytes(tails["stderr"]).decode("utf-8", "replace")),
        }
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            for reader in locals().get("readers", []):
                reader.join()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        return {
            "argv": [redact(str(arg)) for arg in argv],
            "returncode": None,
            "timed_out": True,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": redact(bytes(tails["stdout"]).decode("utf-8", "replace")),
            "stderr": redact(bytes(tails["stderr"]).decode("utf-8", "replace")),
        }
    except OSError as exc:
        return {
            "argv": [redact(str(arg)) for arg in argv],
            "returncode": 127,
            "timed_out": False,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def redact(value: str) -> str:
    """Avoid persisting credentials if a project manifest embeds them in URLs."""
    value = URL_CREDENTIALS_RE.sub(r"\1<redacted>@", value)
    return URL_QUERY_SECRET_RE.sub(r"\1<redacted>", value)


def _preferred_test_dependency_names(names: Any) -> list[str]:
    """Select declared dependency profiles with test, CI, or development intent."""
    selected: list[str] = []
    for name in names:
        tokens = set(re.split(r"[-_.]+", str(name).lower()))
        if tokens & TEST_DEPENDENCY_TOKENS:
            selected.append(str(name))
    priority = {
        "test": 0, "tests": 1, "testing": 2, "unittest": 3,
        "integration": 4, "qa": 5, "ci": 6,
        "dev": 7, "develop": 8, "development": 9,
    }
    return sorted(dict.fromkeys(selected), key=lambda name: (
        min((priority.get(token, 20) for token in re.split(r"[-_.]+", name.lower())), default=20),
        name.lower(),
    ))


def _test_requirement_files(repo: Path) -> list[str]:
    """Find conventional and named test/development requirement files."""
    candidates = list(TEST_REQUIREMENT_FILES)
    for pattern in ("*requirements*.txt", "requirements/*.txt", "reqs/*.txt", "ci/*.txt"):
        candidates.extend(
            path.relative_to(repo).as_posix()
            for path in sorted(repo.glob(pattern)) if path.is_file()
        )
    selected: list[str] = []
    for filename in candidates:
        path = repo / filename
        if (filename in TEST_REQUIREMENT_FILES and path.is_file()) or (
            path.is_file() and REQUIREMENT_INCLUDE_RE.search(path.name)
            and not REQUIREMENT_EXCLUDE_RE.search(path.name)
        ):
            if filename not in selected:
                selected.append(filename)
    return selected[:8]


def installation_strategies(repo: Path) -> list[tuple[str, list[str]]]:
    """Return ordered, manifest-backed dependency profiles with fallbacks."""
    strategies: list[tuple[str, list[str]]] = []
    pyproject = repo / "pyproject.toml"
    editable_project = pyproject.is_file() or (repo / "setup.py").is_file() or (repo / "setup.cfg").is_file()
    requirement_files = _test_requirement_files(repo)
    if pyproject.is_file():
        try:
            manifest = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            manifest = {}

        project = manifest.get("project", {})
        if not isinstance(project, dict):
            project = {}
        extras = project.get("optional-dependencies", {})
        poetry = (manifest.get("tool", {}).get("poetry", {}) or {})
        if isinstance(poetry, dict):
            extras = {**(extras if isinstance(extras, dict) else {}),
                      **(poetry.get("extras", {}) or {})}
        setup_cfg = repo / "setup.cfg"
        if setup_cfg.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(setup_cfg, encoding="utf-8")
                if parser.has_section("options.extras_require"):
                    extras = {**(extras if isinstance(extras, dict) else {}),
                              **dict(parser.items("options.extras_require"))}
            except configparser.Error:
                pass
        if not isinstance(extras, dict):
            extras = {}
        extra_names = _preferred_test_dependency_names(extras)
        groups = manifest.get("dependency-groups", {})
        group_names = _preferred_test_dependency_names(groups) if isinstance(groups, dict) else []
        if extra_names or group_names:
            profile_args = ["pip", "install"]
            if group_names:
                profile_args.extend(["--project", str(repo)])
                for name in group_names:
                    profile_args.extend(["--group", name])
            for filename in requirement_files:
                profile_args.extend(["--requirement", filename])
            extra_spec = f".[{','.join(extra_names)}]" if extra_names else "."
            profile_args.extend(["--editable", extra_spec])
            strategies.append((
                "editable_test_profiles",
                profile_args,
            ))
        if extra_names:
            # If the combined profile conflicts, fresh-venv narrower attempts
            # below retain partial coverage.
            strategies.append((
                "editable_extras_" + "+".join(extra_names),
                ["pip", "install", "--editable", f".[{','.join(extra_names)}]"],
            ))
            for name in extra_names:
                strategies.append((
                    f"editable_extra_{name}",
                    ["pip", "install", "--editable", f".[{name}]"],
                ))

        if group_names:
            group_args = ["pip", "install", "--project", str(repo)]
            for name in group_names:
                group_args.extend(["--group", name])
            group_args.extend(["--editable", "."])
            strategies.append(("editable_groups_" + "+".join(group_names), group_args))
            for name in group_names:
                strategies.append((
                    f"editable_group_{name}",
                    ["pip", "install", "--project", str(repo), "--group", name,
                     "--editable", "."],
                ))

    if requirement_files:
        all_args = ["pip", "install"]
        for filename in requirement_files:
            all_args.extend(["--requirement", filename])
        if editable_project:
            all_args.extend(["--editable", "."])
        strategies.append(("requirements_" + "+".join(requirement_files), all_args))
        for filename in requirement_files:
            install_args = ["pip", "install", "--requirement", filename]
            if editable_project:
                install_args.extend(["--editable", "."])
            strategies.append((f"requirements_{filename}", install_args))

    if editable_project:
        strategies.append(("editable_project", ["pip", "install", "--editable", "."]))
    elif (repo / "requirements.txt").is_file():
        strategies.append(("requirements.txt", ["pip", "install", "--requirement", "requirements.txt"]))

    # Avoid retrying semantically identical commands when manifests overlap.
    unique: list[tuple[str, list[str]]] = []
    seen_commands: set[tuple[str, ...]] = set()
    for name, command in strategies:
        key = tuple(command)
        if key not in seen_commands:
            seen_commands.add(key)
            unique.append((name, command))
    return unique


def classify_install_failure(result: dict[str, Any]) -> str:
    if result.get("timed_out"):
        return "timeout"
    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".lower()
    if "failed to build wheel" in output or "building wheel" in output or "subprocess-exited-with-error" in output:
        return "build_wheel_error"
    if any(marker in output for marker in ("no solution found", "dependency conflict", "resolution impossible", "conflicting dependencies")):
        return "dependency_resolution_error"
    if "no matching distribution" in output or "could not find a version" in output:
        return "package_not_found"
    if result.get("returncode") == 127:
        return "installer_unavailable"
    return "install_error"


def remove_attempt_venv(repo: Path, attempt: Path) -> None:
    """Delete only the generated attempt directory directly under this clone."""
    repo_real = repo.resolve()
    if attempt.parent.resolve() != repo_real or not attempt.name.startswith(".venv_attempt_"):
        raise ValueError(f"recusa de exclusão fora do diretório de tentativa: {attempt}")
    if attempt.is_symlink():
        attempt.unlink()
    elif attempt.exists():
        shutil.rmtree(attempt)


def remove_ready_venv(repo: Path, ready: Path) -> None:
    """Delete only this clone's generated promoted environment."""
    if ready.parent.resolve() != repo.resolve() or ready.name != ".venv_ready":
        raise ValueError(f"recusa de exclusão de ambiente pronto fora do clone: {ready}")
    if ready.is_symlink():
        ready.unlink()
    elif ready.exists():
        shutil.rmtree(ready)


def discard_project_workspace(
    artifacts: Path, uv_cache_root: Path, project_id: str, commit_sha: str,
) -> None:
    """Remove only this project's generated checkout and cache after checkpointing."""
    project_root = artifacts.resolve() / "pilot" / project_id / commit_sha
    source = project_root / "source"
    if source.is_symlink():
        raise ValueError(f"recusa de remover checkout por symlink: {source}")
    if source.exists():
        if source.parent.resolve() != project_root.resolve() or source.name != "source":
            raise ValueError(f"recusa de remover checkout inesperado: {source}")
        shutil.rmtree(source)

    cache_root = uv_cache_root.resolve()
    if cache_root.exists():
        for cache in cache_root.iterdir():
            if not re.fullmatch(re.escape(project_id) + r"-\d+", cache.name):
                continue
            if cache.is_symlink() or cache.is_file():
                cache.unlink()
            elif cache.is_dir():
                shutil.rmtree(cache)


def repository_working_tree_bytes(repo: Path) -> int:
    """Count source checkout size without VCS metadata or generated virtualenvs."""
    total = 0
    for current, dirs, files in os.walk(repo, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git" and not name.startswith(".venv_") and name != ".venv_ready"]
        for name in files:
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def project_python_requirement(repo: Path) -> tuple[str | None, str | None]:
    """Read the project's declared Python constraint and where it came from."""
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            data = {}
        project = data.get("project", {})
        project = project if isinstance(project, dict) else {}
        requirement = project.get("requires-python")
        if isinstance(requirement, str) and requirement.strip():
            return requirement.strip(), "pyproject.toml:project.requires-python"
        tool = data.get("tool", {})
        tool = tool if isinstance(tool, dict) else {}
        poetry = tool.get("poetry", {})
        poetry = poetry if isinstance(poetry, dict) else {}
        poetry_dependencies = poetry.get("dependencies", {})
        poetry_dependencies = poetry_dependencies if isinstance(poetry_dependencies, dict) else {}
        poetry_python = poetry_dependencies.get("python")
        if isinstance(poetry_python, str) and poetry_python.strip():
            return poetry_python.strip(), "pyproject.toml:tool.poetry.dependencies.python"
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
            requirement = parser.get("options", "python_requires", fallback="").strip()
        except (OSError, configparser.Error):
            requirement = ""
        if requirement:
            return requirement, "setup.cfg:options.python_requires"
    version_file = repo / ".python-version"
    if version_file.is_file():
        try:
            request = version_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            request = ""
        if request:
            return request, ".python-version"
    return None, None


def python_request_satisfies(request: str, requirement: str) -> bool | None:
    """Check a requested Python version against a PEP 440 constraint.

    Return None when either value cannot be interpreted safely; unknown syntax
    must not cause a compatible candidate to be discarded.
    """
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", request)
    if not match:
        return None
    candidate = tuple(int(part or 0) for part in match.groups())
    bare_version = re.fullmatch(r"\s*(\d+(?:\.\d+){0,2})\s*", requirement)
    if bare_version:
        parts = tuple(int(part) for part in bare_version.group(1).split("."))
        return candidate[:len(parts)] == parts
    poetry_range = re.fullmatch(r"\s*([~^])(\d+(?:\.\d+){0,2})\s*", requirement)
    if poetry_range:
        operator, version_text = poetry_range.groups()
        parts = tuple(int(part) for part in version_text.split("."))
        lower = parts + (0,) * (3 - len(parts))
        if operator == "~":
            upper = (lower[0], lower[1] + 1, 0)
        elif lower[0] > 0:
            upper = (lower[0] + 1, 0, 0)
        elif lower[1] > 0:
            upper = (0, lower[1] + 1, 0)
        else:
            upper = (0, 0, lower[2] + 1)
        return candidate >= lower and candidate < upper
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        return Version(".".join(map(str, candidate))) in SpecifierSet(requirement)
    except ImportError:
        pass
    except Exception:
        return None

    # Small stdlib fallback for the common PEP 440 comparison operators.
    clauses = [item.strip() for item in requirement.split(",") if item.strip()]
    if not clauses:
        return None
    for clause in clauses:
        op_match = re.match(r"^(~=|===|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+){0,2})(\.\*)?$", clause)
        if not op_match:
            return None
        operator, version_text, wildcard = op_match.groups()
        target_parts = tuple(int(part) for part in version_text.split("."))
        target = target_parts + (0,) * (3 - len(target_parts))
        if wildcard:
            prefix_matches = candidate[:len(target_parts)] == target_parts
            passed = prefix_matches if operator == "==" else not prefix_matches if operator == "!=" else None
        elif operator == "~=":
            upper = ((target[0] + 1, 0, 0) if len(target_parts) == 1
                     else (target[0], target[1] + 1, 0) if len(target_parts) == 2
                     else (target[0], target[1] + 1, 0))
            passed = candidate >= target and candidate < upper
        else:
            passed = {
                "===": candidate == target,
                "==": candidate == target,
                "!=": candidate != target,
                "<=": candidate <= target,
                ">=": candidate >= target,
                "<": candidate < target,
                ">": candidate > target,
            }[operator]
        if passed is None:
            return None
        if not passed:
            return False
    return True


def project_python_request(repo: Path, fallback: str, override: str | None = None) -> str:
    """Use an explicit candidate first, then project metadata, then fallback."""
    if override:
        return override
    requirement, _source = project_python_requirement(repo)
    return requirement or fallback


def install_project_dependencies(
    repo: Path, *, project_id: str, python: str, uv: str,
    base_env: dict[str, str], timeout: int, cache_root: Path,
    python_request_override: str | None = None,
) -> dict[str, Any]:
    """Try each install recipe in a fresh venv and promote the first success."""
    strategies = installation_strategies(repo)
    result: dict[str, Any] = {"attempts": [], "ready": False}
    if not strategies:
        result.update({"status": "no_install_manifest", "category": "dependency_unresolvable"})
        return result

    ready = repo / ".venv_ready"
    python_request = project_python_request(repo, python, python_request_override)
    result["python_request"] = python_request

    def persist_attempts() -> None:
        # Keep install failures inspectable after the compact project checkpoint
        # drops the verbose candidate-attempt records.
        _write_json(repo.parent / "results" / "installation_attempts.json", result)

    for index, (strategy_name, install_args) in enumerate(strategies, start=1):
        attempt = repo / f".venv_attempt_{index}"
        attempt_python = attempt / "bin" / "python"
        attempt_record: dict[str, Any] = {"index": index, "strategy": strategy_name}
        remove_attempt_venv(repo, attempt)
        env = dict(base_env)
        cache = cache_root / f"{project_id}-{index}"
        cache.mkdir(parents=True, exist_ok=True)
        env["UV_CACHE_DIR"] = str(cache)
        # Keep peak RAM use predictable for large binary wheels and source builds.
        env["UV_CONCURRENT_DOWNLOADS"] = "1"
        env["UV_CONCURRENT_BUILDS"] = "1"
        env["UV_CONCURRENT_INSTALLS"] = "1"

        create = run([uv, "venv", "--python", python_request, str(attempt)],
                     cwd=repo, env=env, timeout=timeout)
        attempt_record["venv_creation"] = create
        if create["returncode"] != 0 or create["timed_out"] or not attempt_python.is_file():
            attempt_record["category"] = "venv_creation_error" if not create["timed_out"] else "timeout"
            result["attempts"].append(attempt_record)
            persist_attempts()
            remove_attempt_venv(repo, attempt)
            continue

        install = run([uv, *install_args[:2], "--python", str(attempt_python), *install_args[2:]],
                      cwd=repo, env=env, timeout=timeout)
        attempt_record["project_install"] = install
        if install["returncode"] == 0 and not install["timed_out"]:
            # Resolve profiler-owned test tools outside the project's checkout.
            # uv discovers project configuration from cwd; a project's private
            # package index must not hide standard pytest plugins such as
            # pytest-asyncio from this independent bootstrap step.
            tools = run([
                uv, "pip", "install", "--python", str(attempt_python),
                "pytest", "pytest-timeout", "pytest-asyncio", "pytest-json-report",
                "pytest-forked", "pytest-cov", "pytest-xdist",
            ], cwd=cache_root, env=env, timeout=timeout)
            attempt_record["test_tooling_install"] = tools
        else:
            tools = None

        if (install["returncode"] == 0 and not install["timed_out"]
                and tools is not None and tools["returncode"] == 0 and not tools["timed_out"]):
            snapshot = run([uv, "pip", "freeze", "--python", str(attempt_python)],
                           cwd=repo, env=env, timeout=60)
            attempt_record["resolved_packages"] = snapshot
        else:
            snapshot = None

        if (install["returncode"] == 0 and not install["timed_out"]
                and tools is not None and tools["returncode"] == 0 and not tools["timed_out"]
                and snapshot is not None and snapshot["returncode"] == 0
                and attempt_python.is_file()):
            if ready.exists() or ready.is_symlink():
                raise FileExistsError(f"ambiente pronto já existe: {ready}")
            attempt.rename(ready)
            ready_python = ready / "bin" / "python"
            promoted_check = run([
                str(ready_python), "-c",
                "import pytest, sys; print(sys.executable); print(pytest.__version__)",
            ], cwd=repo, env=env, timeout=30)
            attempt_record["post_promotion_check"] = promoted_check
            if promoted_check["returncode"] != 0:
                attempt_record["category"] = "promoted_environment_not_usable"
                result["attempts"].append(attempt_record)
                persist_attempts()
                remove_ready_venv(repo, ready)
                continue
            attempt_record["category"] = "success"
            result["attempts"].append(attempt_record)
            result.update({
                "ready": True,
                "status": "installed",
                "successful_strategy": strategy_name,
                "python": str(ready_python),
            })
            persist_attempts()
            return result

        if install["returncode"] != 0 or install["timed_out"]:
            attempt_record["category"] = classify_install_failure(install)
        elif tools is not None and (tools["returncode"] != 0 or tools["timed_out"]):
            attempt_record["category"] = classify_install_failure(tools)
        elif snapshot is not None and snapshot["returncode"] != 0:
            attempt_record["category"] = "environment_snapshot_error"
        else:
            attempt_record["category"] = "environment_setup_error"
        result["attempts"].append(attempt_record)
        persist_attempts()
        remove_attempt_venv(repo, attempt)

    result.update({"status": "all_install_attempts_failed", "category": "dependency_unresolvable"})
    persist_attempts()
    return result


def load_sample(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        projects = list(csv.DictReader(stream))
    if not projects:
        raise ValueError(f"amostra vazia: {path}")
    seen: set[str] = set()
    for row in projects:
        pid, sha, url = row.get("project_id", "").strip(), row.get("commit_sha", "").strip(), row.get("url", "").strip()
        if not PROJECT_ID_RE.fullmatch(pid):
            raise ValueError(f"project_id inválido na amostra: {pid!r}")
        if pid in seen:
            raise ValueError(f"project_id duplicado na amostra: {pid}")
        seen.add(pid)
        if not SHA_RE.fullmatch(sha):
            raise ValueError(f"SHA inválido para {pid}: {sha!r}")
        if not (url.startswith("https://") or url.startswith("ssh://") or re.fullmatch(r"git@[^:]+:.+", url)):
            raise ValueError(f"URL Git não permitida para {pid}: {url!r}")
    return projects


def clean_environment(args: argparse.Namespace, temp_root: Path) -> dict[str, str]:
    # Preserve a virtualenv's bin/python symlink; resolving it bypasses that venv.
    python = Path(os.path.abspath(args.python))
    return {
        "PATH": f"{python.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(temp_root / "home"),
        "TMPDIR": str(temp_root / "tmp"),
        "UV_PYTHON_INSTALL_DIR": str(args.python_install_dir.resolve()),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PHASE1_RUN_UID": str(args.run_uid),
        "PHASE1_RUN_GID": str(args.run_gid),
    }


def validate_isolation(args: argparse.Namespace, temp_root: Path, env: dict[str, str],
                       python: str | None = None) -> dict[str, Any]:
    validation_dir = temp_root / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, str(RUNNER),
        "--isolation-launcher-json", args.isolation_launcher_json,
        "--python", os.path.abspath(python or args.python),
        "--run-uid", str(args.run_uid), "--run-gid", str(args.run_gid),
        "--workspace", str(validation_dir),
        "--timeout-per-test", str(args.timeout_per_test),
    ]
    result = run(argv, cwd=ROOT, env=env, timeout=45 * 60)
    report_path = validation_dir / "validation" / "isolation_validation.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report = {}
    return {"run": result, "report_path": str(report_path), "report": report,
            "passed": result["returncode"] == 0 and report.get("passed") is True}


def _probe_environment(base: dict[str, str], *, project_id: str, repo: Path,
                       run_id: str, output: Path, prior_map: Path | None = None,
                       circuit_breaker_k: int = 0) -> dict[str, str]:
    env = dict(base)
    env.update({
        "PHASE1_PROJECT_ID": project_id,
        "PHASE1_PROJECT_ROOT": str(repo),
        "PHASE1_RUN_ID": run_id,
        "PHASE1_OUTPUT": str(output),
        "PHASE1_STATIC_INVENTORY": str(output.parent / "static_inventory.json"),
        "PYTHONPATH": str(ROOT / "scripts"),
    })
    if circuit_breaker_k:
        env["PHASE1_CIRCUIT_BREAKER_K"] = str(circuit_breaker_k)
    if prior_map:
        env["PHASE1_PRIOR_DEPENDENCY_MAP"] = str(prior_map)
    return env


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_project_checkpoint(project_dir: Path) -> dict[str, Any]:
    """Read the tiny checkpoint index, or scan headers of legacy large results."""
    index = _read_json(project_dir / "project_checkpoint.json")
    if index:
        return index
    legacy = project_dir / "project_result.json"
    if not legacy.is_file():
        return {}
    try:
        if legacy.stat().st_size > 100_000_000:
            return {}
    except OSError:
        return {}
    found: dict[str, Any] = {}
    wanted = {"project_id", "commit_sha", "tooling_fingerprint", "status"}
    try:
        with legacy.open("r", encoding="utf-8") as stream:
            for line in stream:
                for key in wanted - found.keys():
                    match = re.match(rf'^  "{key}"\s*:\s*("(?:[^"\\]|\\.)*"|[^,]+)', line)
                    if match:
                        try:
                            found[key] = json.loads(match.group(1))
                        except json.JSONDecodeError:
                            pass
                if wanted.issubset(found):
                    break
    except OSError:
        return {}
    return found


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _compact_project_record(project: dict[str, Any], checkpoint: Path | None = None) -> dict[str, Any]:
    """Keep the in-memory progress record small; detailed probe data stays in its checkpoint."""
    compact: dict[str, Any] = {
        key: project[key] for key in (
            "project_id", "commit_sha", "url", "status", "tooling_fingerprint",
            "collection_fraction", "collection_viable", "collection_ids_stable",
            "collected_item_count", "ast_test_definitions", "ast_test_functions",
            "ast_test_item_estimate", "unexpanded_parametrized_function_count",
            "dynamic_collection_hook_module_count", "collection_ratio_is_lower_bound",
            "collection_loss",
            "collection_minimum_fraction", "collection_error_count", "collection_status",
            "pytest_run_classification", "setup_only_comparison_selected",
            "fixture_repetition_summary", "incomplete_execution", "python_requirement",
            "clone", "checkout",
        ) if key in project
    }
    if project.get("setup_only_discordance"):
        discordance = project["setup_only_discordance"]
        compact["setup_only_discordance"] = {
            key: discordance.get(key) for key in (
                "test_id_discordance_rate", "fixture_setup_discordance_rate",
                "setup_only_teardown_failures", "normal_teardown_failures",
            )
        }
        compact["setup_only_discordance"]["test_id_symmetric_difference_count"] = len(
            discordance.get("test_id_symmetric_difference", [])
        )
        compact["setup_only_discordance"]["fixture_setup_symmetric_difference_count"] = len(
            discordance.get("fixture_setup_symmetric_difference", [])
        )
    if project.get("circuit_breaker_comparison"):
        breaker = project["circuit_breaker_comparison"]
        compact["circuit_breaker_comparison"] = {
            key: breaker.get(key) for key in (
                "k", "aborted_test_count", "full_run_elapsed_seconds",
                "circuit_breaker_elapsed_seconds", "elapsed_seconds_delta",
            )
        }
    install = project.get("dependency_installation", {})
    if install:
        compact["dependency_installation"] = {
            key: install.get(key) for key in ("ready", "status", "python", "successful_strategy")
        }
    compact["python_candidate_attempts"] = [
        {
            "candidate_order": item.get("candidate_order"),
            "python_request": item.get("python_request"),
            "attempted": item.get("attempted", True),
            "status": item.get("status"),
            "skip_reason": item.get("skip_reason"),
            "installation_status": (item.get("installation") or {}).get("status"),
            "installation_ready": (item.get("installation") or {}).get("ready"),
            "collection_fraction": item.get("collection_fraction"),
        }
        for item in project.get("python_candidate_attempts", [])
    ]
    compact["collection_runs"] = [
        {
            "returncode": row.get("run", {}).get("returncode"),
            "timed_out": row.get("run", {}).get("timed_out", False),
            "seconds": row.get("run", {}).get("seconds"),
            "test_count": len(row.get("metadata", {}).get("test_ids", [])),
            "collection_error_count": len(row.get("metadata", {}).get("collection_errors", [])),
        }
        for row in project.get("collection_runs", [])
    ]
    compact["collection_error_count"] = len(project.get("collection_errors", []))
    compact["setup_plan_summary"] = {
        "returncode": project.get("setup_plan", {}).get("run", {}).get("returncode"),
        "timed_out": project.get("setup_plan", {}).get("run", {}).get("timed_out", False),
        "test_count": len(project.get("setup_plan_test_ids", [])),
    }
    for phase_name in ("setup_only", "circuit_breaker_k3"):
        phase = project.get(phase_name)
        if phase:
            compact[f"{phase_name}_summary"] = {
                "returncode": phase.get("run", {}).get("returncode"),
                "timed_out": phase.get("run", {}).get("timed_out", False),
                "seconds": phase.get("run", {}).get("seconds"),
            }
    if project.get("pytest_run_classification"):
        compact["pytest_run_classification"] = project["pytest_run_classification"]
    compact["execution_runs"] = []
    fixture_errors: dict[str, dict[str, Any]] = {}
    for run_index, run_record in enumerate(project.get("execution_runs", []), start=1):
        run = run_record.get("run", {})
        metadata = run_record.get("metadata", {})
        events = metadata.get("fixture_events", [])
        run_summary = {
            "run_index": run_index,
            "returncode": run.get("returncode"), "timed_out": run.get("timed_out", False),
            "seconds": run.get("seconds"), "test_count": len(metadata.get("test_ids", [])),
            "fixture_event_count": len(events),
        }
        run_summary["completion"] = (
            "completed" if _pytest_execution_completed(run)
            else "timeout" if run.get("timed_out")
            else "terminated_or_aborted"
        )
        compact["execution_runs"].append(run_summary)
        for event in events:
            if event.get("status") != "failed":
                continue
            signature = str(event.get("signature") or f"{event.get('exception_class')}|{event.get('message')}")
            failure = fixture_errors.setdefault(signature, {
                "signature": signature, "exception_class": event.get("exception_class"),
                "message": event.get("message"), "innermost_frame": event.get("innermost_frame"),
                "occurrences": 0,
            })
            failure["occurrences"] += 1
    compact["fixture_errors"] = list(fixture_errors.values())
    if checkpoint is not None:
        compact["checkpoint"] = str(checkpoint)
    return compact


def _progress_manifest(record: dict[str, Any]) -> dict[str, Any]:
    """Progress is a small index, never a duplicate of full project artifacts."""
    return {
        "schema_version": 2,
        "created_at_epoch": record.get("created_at_epoch"),
        "status": record.get("status"),
        "tooling_fingerprint": record.get("tooling_fingerprint"),
        "sample": record.get("sample"),
        "projects": record.get("projects", []),
    }


def _pytest_execution_completed(run: dict[str, Any]) -> bool:
    """Pytest rc=1 means tests/collection failed but the run finished; negative/2+ did not."""
    code = run.get("returncode")
    return not run.get("timed_out", False) and isinstance(code, int) and 0 <= code <= 1


def _isolated_pytest(args: argparse.Namespace, *, temp_root: Path,
                     base_env: dict[str, str], launcher: list[str],
                     python: str, repo: Path, project_id: str, run_id: str,
                     artifact_dir: Path, mode: str, timeout: int,
                     prior_map: Path | None = None, circuit_breaker_k: int = 0,
                     deselect: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a single isolated pytest phase, validating the sandbox each time."""
    preflight = validate_isolation(args, temp_root, base_env, python=python)
    if not preflight["passed"]:
        return ({"returncode": 125, "timed_out": False, "seconds": 0,
                 "stdout": "", "stderr": "isolation controls failed",
                 "isolation_preflight": preflight}, {})
    output = artifact_dir / f"{run_id}.json"
    env = _probe_environment(
        base_env, project_id=project_id, repo=repo, run_id=run_id,
        output=output, prior_map=prior_map, circuit_breaker_k=circuit_breaker_k,
    )
    command = [python, "-m", "pytest", "--continue-on-collection-errors",
               "-p", "no:cacheprovider", "-p", "no:randomly",
               "-p", "no:random_order", "-p", "no:pytest_random_order",
               "--timeout", str(args.timeout_per_test), "--timeout-method=signal",
               "-p", "phase1_probe", "-q", "-n", "0",
               "-W",
               'ignore:The configuration option "asyncio_default_fixture_loop_scope" is unset:pytest.PytestDeprecationWarning']
    if mode == "collect-only":
        command += ["--collect-only"]
    elif mode == "setup-plan":
        command += ["--setup-plan"]
    elif mode == "setup-only":
        command += ["--setup-only"]
    elif mode != "normal":
        raise ValueError(f"modo pytest desconhecido: {mode}")
    command.extend(f"--deselect={nodeid}" for nodeid in (deselect or []))
    result = run([*launcher, "--", *command], cwd=repo,
                 env=env, timeout=timeout)
    result["output_log_files"] = _persist_phase_output(artifact_dir, run_id, result)
    metadata = _read_json(output)
    result["isolation_preflight"] = preflight
    result["probe_artifact"] = str(output)
    return result, metadata


def _persist_phase_output(artifact_dir: Path, run_id: str,
                          result: dict[str, Any]) -> dict[str, str]:
    """Keep the bounded, redacted subprocess output after compacting checkpoints."""
    log_dir = artifact_dir / "command-output"
    log_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for stream in ("stdout", "stderr"):
        path = log_dir / f"{run_id}.{stream}.txt"
        path.write_text(str(result.get(stream, "")), encoding="utf-8")
        paths[stream] = str(path)
    return paths


def _collection_loss_estimate(errors: list[dict[str, Any]], module_counts: dict[str, int]) -> dict[str, Any]:
    estimates: dict[str, int] = {}
    for error in errors:
        location = str(error.get("nodeid") or error.get("location") or "")
        module = location.split("::", 1)[0].replace("\\", "/")
        if module in module_counts:
            estimates[module] = module_counts[module]
    return {
        "failed_modules": sorted(estimates),
        "estimated_ast_test_items_lost": sum(estimates.values()),
        "estimated_ast_test_definitions_lost": sum(estimates.values()),
        "per_module_ast_test_definitions": estimates,
    }


def _process_pilot_project(args: argparse.Namespace, *, project: dict[str, str],
                           repo: Path, python: str, temp_root: Path,
                           base_env: dict[str, str], launcher: list[str],
                           project_dir: Path, comparison_ids: set[str],
                           run_prefix: str = "") -> dict[str, Any]:
    pid = project["project_id"]
    run_id = lambda label: f"{run_prefix}-{label}" if run_prefix else label
    project_dir.mkdir(parents=True, exist_ok=True)
    module_details = catalog_test_modules_details(repo)
    module_counts = {
        module: details["estimated_test_items"]
        for module, details in module_details.items()
    }
    ast_test_functions = sum(details["ast_test_functions"] for details in module_details.values())
    unexpanded_parametrized = [
        {"module": module, **function}
        for module, details in module_details.items()
        for function in details["unexpanded_parametrized_functions"]
    ]
    dynamic_collection_modules = [
        module for module, details in module_details.items()
        if details["has_pytest_generate_tests_hook"]
    ]
    fixtures = catalog_project(repo, pid, project.get("commit_sha", ""))
    _write_json(project_dir / "static_inventory.json", {
        "project_id": pid, "commit_sha": project.get("commit_sha"),
        "test_modules": module_counts,
        "test_module_details": module_details,
        "ast_test_functions": ast_test_functions,
        "estimated_ast_test_items": sum(module_counts.values()),
        "fixtures": fixtures,
    })
    result: dict[str, Any] = {
        "project_id": pid, "commit_sha": project.get("commit_sha"),
        "project_path": str(repo), "static_test_modules": len(module_counts),
        "ast_test_definitions": ast_test_functions,
        "ast_test_item_estimate": sum(module_counts.values()),
        "ast_test_functions": ast_test_functions,
        "unexpanded_parametrized_function_count": len(unexpanded_parametrized),
        "unexpanded_parametrized_functions": unexpanded_parametrized,
        "dynamic_collection_hook_module_count": len(dynamic_collection_modules),
        "collection_runs": [], "execution_runs": [],
    }

    # Two independent collections establish deterministic IDs before tests run.
    for index in (1, 2):
        collect_run_id = run_id(f"collect-{index}")
        phase, metadata = _isolated_pytest(
            args, temp_root=temp_root, base_env=base_env, launcher=launcher,
            python=python, repo=repo, project_id=pid, run_id=collect_run_id,
            artifact_dir=project_dir, mode="collect-only", timeout=30 * 60,
        )
        result["collection_runs"].append({"run": phase, "metadata": metadata})
        if not phase.get("isolation_preflight", {}).get("passed", False):
            result.update(status="blocked_isolation_controls_failed")
            return result
    first_ids = result["collection_runs"][0]["metadata"].get("test_ids", [])
    second_ids = result["collection_runs"][1]["metadata"].get("test_ids", [])
    ids_stable = stable_test_ids(first_ids, second_ids)
    result["collection_ids_stable"] = ids_stable
    result["collected_item_count"] = len(set(first_ids))
    result["collection_errors"] = result["collection_runs"][1]["metadata"].get("collection_errors", [])
    result["collection_loss"] = _collection_loss_estimate(result["collection_errors"], module_counts)
    result["collection_fraction"] = (
        result["collected_item_count"] / result["ast_test_item_estimate"]
        if result["ast_test_item_estimate"] else 0.0
    )
    result["collection_ratio_is_lower_bound"] = bool(
        unexpanded_parametrized or dynamic_collection_modules
    )
    result["collection_minimum_fraction"] = args.collection_min_fraction
    result["collection_viable"] = collection_is_viable(
        result["collected_item_count"], result["ast_test_item_estimate"], args.collection_min_fraction
    )
    if not ids_stable:
        result["status"] = "collection_ids_unstable"
        return result
    if not result["collection_viable"]:
        result["status"] = "collection_below_provisional_fraction_threshold"
        return result

    # Persist an explicit, SHA-bound deselection manifest before execution.
    manifest_path = project_dir / "deselection_manifest.json"
    manifest = _read_json(manifest_path)
    if manifest:
        if manifest.get("commit_sha") != project.get("commit_sha"):
            result["status"] = "deselection_manifest_sha_mismatch"
            return result
        ids_digest = __import__("hashlib").sha256(
            "\n".join(sorted(set(first_ids))).encode("utf-8")
        ).hexdigest()
        if manifest.get("collected_ids_sha256") != ids_digest:
            result["status"] = "deselection_manifest_collection_mismatch"
            return result
        deselected = manifest.get("deselected_ids", [])
        if not isinstance(deselected, list) or not set(deselected).issubset(set(first_ids)):
            result["status"] = "deselection_manifest_invalid"
            return result
    else:
        deselected = []
        manifest = {
            "schema_version": 1, "project_id": pid,
            "commit_sha": project.get("commit_sha"),
            "collected_ids_sha256": __import__("hashlib").sha256(
                "\n".join(sorted(set(first_ids))).encode("utf-8")
            ).hexdigest(),
            "deselected_ids": [],
        }
        _write_json(manifest_path, manifest)
    result["deselection_manifest"] = str(manifest_path)
    result["deselected_count"] = len(deselected)

    plan, plan_meta = _isolated_pytest(
        args, temp_root=temp_root, base_env=base_env, launcher=launcher,
        python=python, repo=repo, project_id=pid, run_id=run_id("setup-plan"),
        artifact_dir=project_dir, mode="setup-plan", timeout=30 * 60,
        deselect=deselected,
    )
    result["setup_plan"] = {"run": plan, "metadata": plan_meta}
    if not plan.get("isolation_preflight", {}).get("passed", False):
        result["status"] = "blocked_isolation_controls_failed"
        return result
    result["setup_plan_test_ids"] = plan_meta.get("test_ids", [])

    # A deterministic comparison cohort is fixed by the caller before execution.
    compare = pid in comparison_ids
    result["setup_only_comparison_selected"] = compare
    if compare:
        setup_only, setup_meta = _isolated_pytest(
            args, temp_root=temp_root, base_env=base_env, launcher=launcher,
            python=python, repo=repo, project_id=pid, run_id=run_id("setup-only"),
            artifact_dir=project_dir, mode="setup-only", timeout=30 * 60,
            deselect=deselected,
        )
        result["setup_only"] = {"run": setup_only, "metadata": setup_meta}
        if not setup_only.get("isolation_preflight", {}).get("passed", False):
            result["status"] = "blocked_isolation_controls_failed"
            return result
        if not _pytest_execution_completed(setup_only):
            result["status"] = "setup_only_execution_incomplete"
            return result

    for index in (1, 2):
        execution_run_id = run_id(f"normal-{index}")
        execution, metadata = _isolated_pytest(
            args, temp_root=temp_root, base_env=base_env, launcher=launcher,
            python=python, repo=repo, project_id=pid, run_id=execution_run_id,
            artifact_dir=project_dir, mode="normal", timeout=30 * 60,
            deselect=deselected,
        )
        result["execution_runs"].append({"run": execution, "metadata": metadata})
        if not execution.get("isolation_preflight", {}).get("passed", False):
            result["status"] = "blocked_isolation_controls_failed"
            return result
        if not _pytest_execution_completed(execution):
            result["status"] = "pytest_execution_incomplete"
            result["incomplete_execution"] = {
                "run_index": index,
                "returncode": execution.get("returncode"),
                "timed_out": execution.get("timed_out", False),
                "classification": (
                    "timeout" if execution.get("timed_out")
                    else "terminated_by_signal" if isinstance(execution.get("returncode"), int)
                    and execution["returncode"] < 0
                    else "pytest_aborted_or_internal_error"
                ),
            }
            return result
    if compare:
        normal_meta = result["execution_runs"][0]["metadata"]
        prior_map_path = project_dir / "runtime_dependency_map.json"
        _write_json(prior_map_path, normal_meta.get("test_to_fixtures", {}))
        breaker_run, breaker_meta = _isolated_pytest(
            args, temp_root=temp_root, base_env=base_env, launcher=launcher,
            python=python, repo=repo, project_id=pid, run_id=run_id("normal-k3"),
            artifact_dir=project_dir, mode="normal", timeout=30 * 60,
            prior_map=prior_map_path, circuit_breaker_k=3, deselect=deselected,
        )
        result["circuit_breaker_k3"] = {"run": breaker_run, "metadata": breaker_meta}
        if not breaker_run.get("isolation_preflight", {}).get("passed", False):
            result["status"] = "blocked_isolation_controls_failed"
            return result
        if not _pytest_execution_completed(breaker_run):
            result["status"] = "circuit_breaker_execution_incomplete"
            return result
        full_ids = set(normal_meta.get("test_ids", []))
        aborted_ids = {
            entry.get("nodeid")
            for entry in breaker_meta.get("circuit_breaker", {}).get("aborted_tests", [])
        }
        full_outcomes = normal_meta.get("test_phase_outcomes", {})
        full_seconds = float(result["execution_runs"][0]["run"].get("seconds", 0))
        breaker_seconds = float(breaker_run.get("seconds", 0))
        result["circuit_breaker_comparison"] = {
            "k": 3,
            "aborted_test_ids": sorted(aborted_ids),
            "aborted_ids_in_full_run": sorted(aborted_ids & full_ids),
            "aborted_test_count": len(aborted_ids),
            "full_run_elapsed_seconds": full_seconds,
            "circuit_breaker_elapsed_seconds": breaker_seconds,
            "elapsed_seconds_delta": round(full_seconds - breaker_seconds, 3),
            "full_run_outcomes_for_aborted": {
                nodeid: full_outcomes.get(nodeid, {}) for nodeid in sorted(aborted_ids & full_ids)
            },
        }
    if compare:
        setup_meta = result["setup_only"]["metadata"]
        normal_meta = result["execution_runs"][0]["metadata"]
        setup_events = setup_meta.get("fixture_events", [])
        normal_events = normal_meta.get("fixture_events", [])
        setup_fixtures = {e.get("fixture_id") for e in setup_events if e.get("phase") == "setup"}
        normal_fixtures = {e.get("fixture_id") for e in normal_events if e.get("phase") == "setup"}
        test_delta = set(setup_meta.get("test_ids", [])) ^ set(normal_meta.get("test_ids", []))
        fixture_delta = setup_fixtures ^ normal_fixtures
        result["setup_only_discordance"] = {
            "test_id_symmetric_difference": sorted(test_delta),
            "test_id_discordance_rate": len(test_delta) / max(
                1, len(set(setup_meta.get("test_ids", [])) | set(normal_meta.get("test_ids", [])))
            ),
            "fixture_setup_symmetric_difference": sorted(fixture_delta),
            "fixture_setup_discordance_rate": len(fixture_delta) / max(1, len(setup_fixtures | normal_fixtures)),
            "setup_only_teardown_failures": sum(
                e.get("phase") == "teardown" and e.get("status") == "failed" for e in setup_events
            ),
            "normal_teardown_failures": sum(
                e.get("phase") == "teardown" and e.get("status") == "failed" for e in normal_events
            ),
        }

    events = [
        event for run_record in result["execution_runs"]
        for event in run_record["metadata"].get("fixture_events", [])
    ]
    result["fixture_repetition_summary"] = summarize_fixture_repetitions(events)
    result["test_to_fixtures"] = result["execution_runs"][0]["metadata"].get("test_to_fixtures", {})
    collection_error_count = len(result.get("collection_errors", []))
    result["collection_error_count"] = collection_error_count
    result["collection_status"] = "collection_with_errors" if collection_error_count else "collection_clean"
    has_test_failures = any(
        run["run"].get("returncode") == 1 for run in result["execution_runs"]
    )
    if collection_error_count:
        result["pytest_run_classification"] = "completed_with_collection_errors"
        result["status"] = "pilot_project_complete_with_collection_errors"
    elif has_test_failures:
        result["pytest_run_classification"] = "completed_with_test_failures"
        result["status"] = "pilot_project_complete_with_test_failures"
    else:
        result["pytest_run_classification"] = "completed_without_pytest_failures"
        result["status"] = "pilot_project_complete"
    return result


def summarize_pilot(record: dict[str, Any]) -> dict[str, Any]:
    projects = record.get("projects", [])
    completed = [row for row in projects if row.get("status") in COMPLETED_PROJECT_STATUSES]
    collection_error_projects = [
        row for row in completed if row.get("status") == "pilot_project_complete_with_collection_errors"
    ]
    test_failure_projects = [
        row for row in completed if row.get("status") == "pilot_project_complete_with_test_failures"
    ]
    error_catalog: dict[str, dict[str, Any]] = {}
    setup_discordance = []
    breaker_comparisons = []
    for project in completed:
        events = [
            event for run_record in project.get("execution_runs", [])
            for event in run_record.get("metadata", {}).get("fixture_events", [])
        ]
        failures = project.get("fixture_errors", []) or [event for event in events if event.get("status") == "failed"]
        for event in failures:
            key = str(event.get("signature") or f"{event.get('exception_class')}|{event.get('message')}")
            entry = error_catalog.setdefault(key, {
                "signature": key, "exception_class": event.get("exception_class"),
                "message": event.get("message"), "innermost_frame": event.get("innermost_frame"),
                "project_ids": [], "occurrences": 0,
            })
            entry["occurrences"] += int(event.get("occurrences", 1))
            if project.get("project_id") not in entry["project_ids"]:
                entry["project_ids"].append(project.get("project_id"))
        if project.get("setup_only_discordance") is not None:
            setup_discordance.append(project["setup_only_discordance"])
        if project.get("circuit_breaker_comparison") is not None:
            breaker_comparisons.append(project["circuit_breaker_comparison"])
    statuses = [row.get("status") for row in projects]
    return {
        "projects_total": len(projects),
        "funnel": {
            "clone_succeeded": sum(bool(row.get("clone", {}).get("returncode") == 0) for row in projects),
            "dependencies_installed": sum(bool(row.get("dependency_installation", {}).get("ready")) for row in projects),
            "stable_collection": sum(row.get("collection_ids_stable") is True for row in projects),
            "collection_viable_at_provisional_fraction": sum(row.get("collection_viable") is True for row in projects),
            "setup_plan_available": sum(
                bool(row.get("setup_plan_test_ids") or row.get("setup_plan_summary", {}).get("test_count"))
                for row in projects
            ),
            "normal_executions_completed_twice": sum(
                len(row.get("execution_runs", [])) == 2
                and all(run.get("completion", "completed") == "completed"
                        for run in row.get("execution_runs", []))
                for row in projects
            ),
            "pilot_projects_complete": len(completed),
            "pilot_projects_clean": sum(row.get("status") == "pilot_project_complete" for row in completed),
            "pilot_projects_with_collection_errors": len(collection_error_projects),
            "pilot_projects_with_test_failures": len(test_failure_projects),
            "projects_with_python_fallback": sum(
                sum(item.get("attempted", True) for item in row.get("python_candidate_attempts", [])) > 1
                for row in projects
            ),
            "python_candidates_attempted": sum(
                sum(item.get("attempted", True) for item in row.get("python_candidate_attempts", []))
                for row in projects
            ),
        },
        "status_counts": {status: statuses.count(status) for status in sorted(set(statuses))},
        "setup_only_comparison": {
            "projects": len(setup_discordance),
            "test_id_discordance_rate_mean": (
                sum(item.get("test_id_discordance_rate", 0.0) for item in setup_discordance)
                / len(setup_discordance) if setup_discordance else None
            ),
            "fixture_setup_discordance_rate_mean": (
                sum(item.get("fixture_setup_discordance_rate", 0.0) for item in setup_discordance)
                / len(setup_discordance) if setup_discordance else None
            ),
            "details": setup_discordance,
        },
        "circuit_breaker_k3": {
            "projects": len(breaker_comparisons),
            "total_aborted_items": sum(item.get("aborted_test_count", 0) for item in breaker_comparisons),
            "total_elapsed_seconds_delta": round(
                sum(item.get("elapsed_seconds_delta", 0.0) for item in breaker_comparisons), 3
            ),
            "details": breaker_comparisons,
        },
        "error_catalog": sorted(error_catalog.values(), key=lambda item: (-item["occurrences"], item["signature"])),
        "x_threshold": "not_calculated_in_pilot",
        "minimum_usable_fixtures": "reserved_for_preregistration_v2",
    }


def tooling_fingerprint(configuration: dict[str, Any] | None = None) -> str:
    digest = __import__("hashlib").sha256()
    for path in (Path(__file__), ROOT / "scripts" / "phase1_probe.py",
                 ROOT / "scripts" / "phase1_catalog.py", RUNNER):
        digest.update(path.name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    digest.update(json.dumps(configuration or {}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--project-id", help="ID de um projeto da amostra; padrão: primeiro")
    parser.add_argument("--pilot", action="store_true",
                        help="executa todos os projetos da amostra (sem esta opção, executa só o primeiro)")
    parser.add_argument("--resume-existing-progress", action="store_true",
                        help=("reaproveita resultados terminais do progresso existente se o launcher mudou; "
                              "use somente com a mesma amostra e os mesmos parâmetros"))
    parser.add_argument("--comparison-projects", type=int, default=6,
                        help="quantidade de projetos iniciais da amostra para comparar setup-only/normal")
    parser.add_argument("--install-smoke-only", action="store_true",
                        help="instala somente o projeto selecionado e preserva clone/.venv_ready")
    parser.add_argument("--reuse-source", type=Path,
                        help="reutiliza um clone existente com .venv_ready; exige --project-id e SHA idêntico")
    parser.add_argument(
        "--discard-project-workspace-after-checkpoint", action="store_true",
        help=("após salvar cada projeto, remove seu clone, ambientes virtuais e cache uv; "
              "os resultados persistidos permanecem e a fase de energia deverá reinstalar os projetos selecionados"),
    )
    parser.add_argument("--isolation-launcher-json")
    parser.add_argument("--python", default=sys.executable,
                        help="Python base; padrão: interpretador que executa este launcher")
    parser.add_argument("--python-candidates", default="3.13,3.12,3.11",
                        help="ordem determinística de versões tentadas no piloto (máximo de três)")
    parser.add_argument("--run-uid", type=int, default=os.geteuid(),
                        help="UID dedicado; padrão: UID efetivo atual")
    parser.add_argument("--run-gid", type=int, default=os.getegid(),
                        help="GID dedicado; padrão: GID efetivo atual")
    parser.add_argument("--dedicated-user", default="phase1runner",
                        help="conta Unix dedicada esperada; padrão: phase1runner")
    parser.add_argument("--artifacts", type=Path,
                        default=Path("/var/tmp") / f"phase1-artifacts-{os.geteuid()}",
                        help="Diretório persistente gravável; padrão: diretório por UID em /var/tmp")
    parser.add_argument("--uv-cache-dir", type=Path,
                        default=Path("/var/tmp") / f"phase1-uv-cache-{os.geteuid()}",
                        help="Cache persistente do uv fora de tmpfs; padrão: diretório por UID em /var/tmp")
    parser.add_argument("--python-install-dir", type=Path,
                        default=Path("/var/tmp") / f"phase1-python-{os.geteuid()}",
                        help="Destino persistente para Pythons gerenciados pelo uv")
    parser.add_argument("--timeout-per-test", type=int, default=120)
    parser.add_argument("--collection-min-fraction", type=float, default=0.5,
                        help="limiar mínimo de coleta dinâmica definido no pré-registro v2")
    parser.add_argument("--clone-timeout", type=int, default=600)
    parser.add_argument("--install-timeout", type=int, default=1200,
                        help="limite por tentativa de dependências; padrão do pré-registro: 20 minutos")
    parser.add_argument("--uv", default="uv", help="executável uv ou caminho absoluto")
    parser.add_argument("--max-repo-bytes", type=int, default=2_000_000_000)
    args = parser.parse_args()

    if args.run_uid == 0 or os.geteuid() != args.run_uid or os.getegid() != args.run_gid:
        parser.error("execute como o usuário dedicado; UID/GID efetivos devem corresponder aos argumentos e UID não pode ser 0")
    try:
        effective_username = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        parser.error("UID efetivo não tem uma conta Unix identificável")
    if effective_username != args.dedicated_user:
        parser.error(
            f"usuário efetivo {effective_username!r} não corresponde à conta dedicada esperada {args.dedicated_user!r}"
        )
    try:
        docker_gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        docker_gid = None
    if docker_gid is not None and (docker_gid == os.getegid() or docker_gid in os.getgroups()):
        parser.error("o usuário dedicado não pode ter o grupo docker como grupo primário ou suplementar")
    launcher: list[str] = []
    if not args.install_smoke_only:
        if not args.isolation_launcher_json:
            parser.error("--isolation-launcher-json é obrigatório fora de --install-smoke-only")
        try:
            launcher = json.loads(args.isolation_launcher_json)
        except json.JSONDecodeError as exc:
            parser.error(f"--isolation-launcher-json inválido: {exc}")
        if not isinstance(launcher, list) or not launcher or not all(isinstance(x, str) for x in launcher):
            parser.error("launcher deve ser uma lista JSON não vazia de argumentos")
        executable = Path(launcher[0])
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            parser.error("o primeiro argumento do launcher deve ser um executável absoluto")
    if (args.timeout_per_test <= 0 or args.clone_timeout <= 0 or args.install_timeout <= 0
            or args.max_repo_bytes <= 0 or args.comparison_projects < 0
            or not 0 < args.collection_min_fraction <= 1):
        parser.error("timeouts e limite do clone devem ser positivos")
    if args.pilot and args.install_smoke_only:
        parser.error("--pilot e --install-smoke-only são modos distintos")
    if args.resume_existing_progress and not args.pilot:
        parser.error("--resume-existing-progress só pode ser usado com --pilot")
    if args.reuse_source and (not args.project_id or args.pilot or args.install_smoke_only):
        parser.error("--reuse-source exige --project-id e não combina com --pilot/--install-smoke-only")
    if args.discard_project_workspace_after_checkpoint and (
        not args.pilot or args.install_smoke_only or args.reuse_source
    ):
        parser.error(
            "--discard-project-workspace-after-checkpoint exige --pilot e não combina "
            "com --install-smoke-only/--reuse-source"
        )
    python_candidates = [value.strip() for value in args.python_candidates.split(",") if value.strip()]
    if not python_candidates or len(python_candidates) > 3 or len(set(python_candidates)) != len(python_candidates):
        parser.error("--python-candidates deve listar de uma a três versões únicas")

    try:
        projects = load_sample(args.sample.resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.project_id:
        projects = [p for p in projects if p["project_id"] == args.project_id]
        if not projects:
            parser.error(f"projeto não encontrado na amostra: {args.project_id}")
    elif not args.pilot:
        projects = projects[:1]

    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    # Temporary workspaces are unique and never recursively delete a caller path.
    with tempfile.TemporaryDirectory(prefix="phase1-smoke-") as temp_name:
        temp_root = Path(temp_name)
        for name in ("home", "tmp"):
            (temp_root / name).mkdir()
        env = clean_environment(args, temp_root)
        uv = shutil.which(args.uv, path=env["PATH"])
        if not uv:
            parser.error(f"uv não encontrado no PATH permitido: {env['PATH']}")
        preflight = (
            {"passed": None, "status": "not_run_network_enabled_install_only"}
            if args.install_smoke_only
            else {"passed": None, "status": "deferred_until_project_environment_is_ready"}
        )
        record: dict[str, Any] = {
            "created_at_epoch": int(time.time()),
            "sample": str(args.sample.resolve()),
            "uv_python_install_dir": env["UV_PYTHON_INSTALL_DIR"],
            "isolation_preflight": preflight,
            "mode": "install_smoke" if args.install_smoke_only else ("pilot" if args.pilot else "single_project"),
            "tooling_fingerprint": "",
            "projects": [],
        }
        # Freeze the comparison cohort before running any project tests.
        comparison_ids = (
            {args.project_id} if args.project_id and not args.install_smoke_only
            else {row["project_id"] for row in projects[:min(args.comparison_projects, len(projects))]}
        )
        comparison_record = {
            "schema_version": 1,
            "selection_rule": "first N projects in the committed sample order",
            "count": len(comparison_ids),
            "project_ids": [row["project_id"] for row in projects if row["project_id"] in comparison_ids],
            "created_at_epoch": int(time.time()),
        }
        sample_bytes = args.sample.resolve().read_bytes()
        record["tooling_fingerprint"] = tooling_fingerprint({
            "sample_sha256": __import__("hashlib").sha256(sample_bytes).hexdigest(),
            "python_candidates": python_candidates,
            "collection_min_fraction": args.collection_min_fraction,
            "discard_project_workspace_after_checkpoint": args.discard_project_workspace_after_checkpoint,
            "comparison_project_ids": comparison_record["project_ids"],
            "isolation_launcher": launcher,
            "timeout_per_test": args.timeout_per_test,
        })
        prior_progress = _read_json(artifacts / "pilot_progress.json") if args.pilot else {}
        prior_rows = prior_progress.get("projects", [])
        sample_prefix_matches = (
            isinstance(prior_rows, list)
            and len(prior_rows) <= len(projects)
            and all(
                isinstance(prior, dict)
                and prior.get("project_id") == current["project_id"]
                and prior.get("commit_sha") == current["commit_sha"]
                for prior, current in zip(prior_rows, projects)
            )
        )
        prior_fingerprint_matches = (
            prior_progress.get("tooling_fingerprint") == record["tooling_fingerprint"]
        )
        prior_sample_matches = prior_progress.get("sample") == str(args.sample.resolve())
        can_resume_progress = (
            bool(prior_rows)
            and sample_prefix_matches
            and prior_sample_matches
            and (prior_fingerprint_matches or args.resume_existing_progress)
        )
        prior_rows_by_id = {
            row.get("project_id"): row for row in prior_rows
            if isinstance(row, dict) and row.get("project_id")
        } if can_resume_progress else {}
        if can_resume_progress:
            record["resumed_from_progress"] = {
                "path": str(artifacts / "pilot_progress.json"),
                "prior_tooling_fingerprint": prior_progress.get("tooling_fingerprint"),
                "prior_fingerprint_matches": prior_fingerprint_matches,
                "explicit_fingerprint_override": bool(
                    args.resume_existing_progress and not prior_fingerprint_matches
                ),
            }

        def persist_progress() -> None:
            record["status"] = "pilot_in_progress"
            _write_json(artifacts / "pilot_progress.json", _progress_manifest(record))
            if args.discard_project_workspace_after_checkpoint and record["projects"]:
                completed = record["projects"][-1]
                project_id = completed.get("project_id")
                commit_sha = completed.get("commit_sha")
                if project_id and commit_sha:
                    discard_project_workspace(
                        artifacts, args.uv_cache_dir, project_id, commit_sha,
                    )

        if not args.install_smoke_only:
            _write_json(artifacts / "setup_only_comparison_cohort.json", comparison_record)
        for project in projects:
            pid, sha, url = project["project_id"], project["commit_sha"], project["url"]
            if args.install_smoke_only:
                run_stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
                persistent_run = artifacts / "install-smoke" / pid / run_stamp
                persistent_run.mkdir(parents=True, exist_ok=False)
                repo = persistent_run / "source"
                uv_cache_root = persistent_run / "uv-cache"
            else:
                project_root = artifacts / "pilot" / pid / sha
                project_root.mkdir(parents=True, exist_ok=True)
                repo = args.reuse_source.resolve() if args.reuse_source else project_root / "source"
                uv_cache_root = args.uv_cache_dir.resolve()
                project_dir = project_root / "results"
                prior_row = prior_rows_by_id.get(pid, {})
                if (prior_row.get("commit_sha") == sha
                        and prior_row.get("status") in TERMINAL_PILOT_STATUSES):
                    resumed_row = dict(prior_row)
                    resumed_row["resumed_from_checkpoint"] = True
                    if not prior_fingerprint_matches:
                        resumed_row["resumed_from_prior_tooling_fingerprint"] = prior_progress.get(
                            "tooling_fingerprint"
                        )
                    record["projects"].append(resumed_row)
                    persist_progress()
                    continue
                saved_project = _read_project_checkpoint(project_dir)
                if (saved_project.get("status") in TERMINAL_PILOT_STATUSES
                        and saved_project.get("commit_sha") == sha
                        and saved_project.get("tooling_fingerprint") == record["tooling_fingerprint"]):
                    saved_project["resumed_from_checkpoint"] = True
                    record["projects"].append(saved_project)
                    persist_progress()
                    continue
            project_record: dict[str, Any] = {
                "project_id": pid, "commit_sha": sha, "url": redact(url),
                "persistent_project_path": str(repo) if args.install_smoke_only else None,
                "install_only": args.install_smoke_only,
            }
            try:
                repo.parent.mkdir(parents=True, exist_ok=True)
                write_probe = repo.parent / ".phase1-write-check"
                write_probe.write_text("ok", encoding="utf-8")
                write_probe.unlink()
                project_record["artifact_directory_writable"] = True
            except OSError as exc:
                project_record["artifact_directory_writable"] = False
                project_record["artifact_write_error"] = f"{type(exc).__name__}: {exc}"
                project_record["status"] = "artifact_directory_not_writable"
                record["projects"].append(project_record)
                persist_progress()
                if args.install_smoke_only:
                    record["status"] = "installation_smoke_failed"
                    report_path = artifacts / f"install_smoke_{pid}.json"
                    report_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(json.dumps(record, indent=2, ensure_ascii=False))
                    return 2
                continue
            if not repo.exists():
                # Some projects derive their package version from Git tags
                # (e.g. setuptools-scm); omitting tags can create a false
                # version such as 0.1.dev and break their declared dev extras.
                clone = run(["git", "clone", "--quiet", url, str(repo)],
                            cwd=temp_root, env=env, timeout=args.clone_timeout)
                project_record["clone"] = clone
                if clone["returncode"] != 0 or clone["timed_out"]:
                    project_record["status"] = "clone_failed"
                    record["projects"].append(project_record)
                    persist_progress()
                    continue
            else:
                project_record["clone"] = {
                    "returncode": 0, "timed_out": False,
                    "status": "reused_existing_checkout",
                }
            checkout = run(["git", "checkout", "--quiet", "--detach", sha],
                           cwd=repo, env=env, timeout=120)
            project_record["checkout"] = checkout
            if checkout["returncode"] != 0:
                project_record["status"] = "checkout_failed"
                record["projects"].append(project_record)
                persist_progress()
                continue
            size = repository_working_tree_bytes(repo)
            project_record["working_tree_bytes"] = size
            if size > args.max_repo_bytes:
                project_record["status"] = "repo_size_limit_exceeded"
                record["projects"].append(project_record)
                persist_progress()
                continue

            python_requirement, python_requirement_source = project_python_requirement(repo)
            project_record["python_requirement"] = {
                "specifier": python_requirement,
                "source": python_requirement_source,
            }
            requested_candidates = [args.python] if args.install_smoke_only else python_candidates
            compatible_candidates: list[tuple[int, str]] = []
            skipped_candidates: list[dict[str, Any]] = []
            for candidate_order, candidate in enumerate(requested_candidates, start=1):
                compatible = (
                    python_request_satisfies(candidate, python_requirement)
                    if python_requirement and not args.install_smoke_only else True
                )
                if compatible is False:
                    skipped_candidates.append({
                        "candidate_order": candidate_order,
                        "python_request": candidate,
                        "attempted": False,
                        "status": "skipped_python_incompatible",
                        "skip_reason": f"does not satisfy {python_requirement} from {python_requirement_source}",
                    })
                else:
                    compatible_candidates.append((candidate_order, candidate))
            if not compatible_candidates:
                project_record["python_candidate_attempts"] = skipped_candidates
                project_record["status"] = "no_compatible_python_candidate"
                record["projects"].append(project_record)
                persist_progress()
                continue

            ready_python = repo / ".venv_ready" / "bin" / "python"
            candidate_attempts: list[dict[str, Any]] = list(skipped_candidates)
            if ready_python.is_file():
                check = run([str(ready_python), "-c", "import pytest; print(pytest.__version__)"],
                            cwd=repo, env=env, timeout=30)
                installation = {
                    "ready": check["returncode"] == 0,
                    "status": "reused" if check["returncode"] == 0 else "stale_ready_environment",
                    "python": str(ready_python), "post_promotion_check": check,
                }
                if check["returncode"] != 0:
                    project_record["status"] = "stale_ready_environment"
                    project_record["dependency_installation"] = installation
                    record["projects"].append(project_record)
                    persist_progress()
                    continue
                if args.install_smoke_only:
                    project_record["status"] = "installation_smoke_passed"
                    project_record["dependency_installation"] = installation
                    record["projects"].append(project_record)
                    persist_progress()
                    continue
                project_result = _process_pilot_project(
                    args, project=project, repo=repo, python=installation["python"],
                    temp_root=temp_root, base_env=env, launcher=launcher,
                    project_dir=project_dir, comparison_ids=comparison_ids,
                    run_prefix="reused",
                )
                project_record.update(installation=installation, **project_result)
                project_record["python_candidate_attempts"] = sorted(candidate_attempts + [{
                    "candidate_order": 0,
                    "python_request": "reused_ready_environment",
                    "attempted": True,
                    "status": project_result.get("status"),
                    "collection_fraction": project_result.get("collection_fraction"),
                }], key=lambda item: item["candidate_order"])
            else:
                project_result = None
                installation = {"ready": False, "status": "all_python_candidates_failed"}
                for candidate_index, candidate in compatible_candidates:
                    attempt = install_project_dependencies(
                        repo,
                        project_id=pid,
                        python=args.python,
                        python_request_override=(None if args.install_smoke_only else candidate),
                        uv=uv,
                        base_env=env,
                        timeout=args.install_timeout,
                        cache_root=(args.uv_cache_dir.resolve()
                                    if args.install_smoke_only else uv_cache_root),
                    )
                    candidate_record: dict[str, Any] = {
                        "candidate_order": candidate_index,
                        "python_request": candidate,
                        "attempted": True,
                        "installation": attempt,
                    }
                    candidate_attempts.append(candidate_record)
                    installation = attempt
                    if not attempt["ready"]:
                        continue
                    if args.install_smoke_only:
                        break
                    candidate_result = _process_pilot_project(
                        args, project=project, repo=repo, python=attempt["python"],
                        temp_root=temp_root, base_env=env, launcher=launcher,
                        project_dir=project_dir, comparison_ids=comparison_ids,
                        run_prefix=f"python-{re.sub(r'[^A-Za-z0-9_.-]', '_', candidate)}",
                    )
                    candidate_record["collection_fraction"] = candidate_result.get("collection_fraction")
                    candidate_record["status"] = candidate_result.get("status")
                    candidate_record["collection_runs"] = candidate_result.get("collection_runs", [])
                    candidate_record["collection_errors"] = candidate_result.get("collection_errors", [])
                    project_result = candidate_result
                    if candidate_result.get("status") == "collection_below_provisional_fraction_threshold":
                        remove_ready_venv(repo, repo / ".venv_ready")
                        attempt["ready"] = False
                        attempt["status"] = "installed_but_collection_unviable"
                        attempt["environment_removed"] = True
                        if candidate_order == compatible_candidates[-1][0]:
                            candidate_result["status"] = "collection_unviable_for_all_python_candidates"
                            project_result = candidate_result
                        continue
                    # Unstable collection or isolation failure is recorded; do not
                    # select a later interpreter based on fixture success.
                    break
                project_record["python_candidate_attempts"] = sorted(
                    candidate_attempts, key=lambda item: item["candidate_order"]
                )

            project_record["dependency_installation"] = installation
            if not installation.get("ready"):
                if project_result and project_result.get("status") in {
                    "collection_below_provisional_fraction_threshold",
                    "collection_unviable_for_all_python_candidates",
                }:
                    project_result["status"] = "collection_unviable_for_all_python_candidates"
                    project_record.update(project_result)
                    project_record["python_candidate_attempts"] = sorted(
                        candidate_attempts, key=lambda item: item["candidate_order"]
                    )
                    project_record["tooling_fingerprint"] = record["tooling_fingerprint"]
                    compact_project = _compact_project_record(
                        project_record, project_dir / "project_result.json"
                    )
                    record["projects"].append(compact_project)
                    _write_json(project_dir / "project_result.json", compact_project)
                    _write_json(project_dir / "project_checkpoint.json", compact_project)
                    persist_progress()
                    continue
                project_record["status"] = "dependency_unresolvable"
                record["projects"].append(project_record)
                persist_progress()
                continue
            if args.install_smoke_only:
                project_record["status"] = "installation_smoke_passed"
                record["projects"].append(project_record)
                persist_progress()
                continue
            if project_result is None:
                project_record["status"] = "collection_unviable_for_all_python_candidates"
                record["projects"].append(project_record)
                persist_progress()
                continue
            project_record.update(project_result)
            project_record["tooling_fingerprint"] = record["tooling_fingerprint"]
            compact_project = _compact_project_record(
                project_record, project_dir / "project_result.json"
            )
            record["projects"].append(compact_project)
            _write_json(project_dir / "project_result.json", compact_project)
            _write_json(project_dir / "project_checkpoint.json", compact_project)
            # Checkpoint after every project so an interrupted batch is resumable.
            persist_progress()

        if args.install_smoke_only:
            project_result = record["projects"][0] if record["projects"] else {}
            passed = project_result.get("status") == "installation_smoke_passed"
            record["status"] = "installation_smoke_passed" if passed else "installation_smoke_failed"
            report_path = artifacts / f"install_smoke_{projects[0]['project_id']}.json"
        else:
            statuses = {item.get("status") for item in record["projects"]}
            if statuses and statuses.issubset(TERMINAL_PILOT_STATUSES):
                if statuses - COMPLETED_PROJECT_STATUSES:
                    record["status"] = (
                        "pilot_complete_with_project_losses" if args.pilot
                        else "single_project_complete_with_project_losses"
                    )
                elif statuses == {"pilot_project_complete"}:
                    record["status"] = "pilot_complete" if args.pilot else "single_project_complete"
                elif any(item.get("status") == "pilot_project_complete_with_collection_errors"
                         for item in record["projects"]):
                    record["status"] = (
                        "pilot_complete_with_collection_errors" if args.pilot
                        else "single_project_complete_with_collection_errors"
                    )
                else:
                    record["status"] = (
                        "pilot_complete_with_test_failures" if args.pilot
                        else "single_project_complete_with_test_failures"
                    )
            else:
                record["status"] = "pilot_incomplete"
            record["setup_only_comparison_cohort"] = comparison_record
            record["pilot_summary"] = summarize_pilot(record)
            report_path = artifacts / ("pilot_report.json" if args.pilot else "single_project_report.json")
        _write_json(report_path, record)
        print(json.dumps({
            "status": record.get("status"),
            "projects_total": len(record.get("projects", [])),
            "pilot_summary": record.get("pilot_summary"),
            "report": str(report_path),
        }, indent=2, ensure_ascii=False))
        if args.install_smoke_only and passed:
            print(f"Ambiente persistido em: {project_result['dependency_installation']['python']}")
        print(f"Relatório salvo em: {report_path}")
    if args.install_smoke_only:
        return 0 if passed else 2
    return 0 if record.get("status") in {
        "pilot_complete", "single_project_complete",
        "pilot_complete_with_project_losses", "single_project_complete_with_project_losses",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
