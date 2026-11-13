#!/usr/bin/env python3
"""Fail-closed pilot readiness and isolated collection smoke check.

This script is deliberately not the full pilot runner. It validates the exact
isolation launcher with ``phase1_runner.py`` before cloning or executing any
project, then performs one bounded collection smoke check. Pilot measurements,
the setup-only/normal comparison and K=3 validation remain separate steps.
"""

from __future__ import annotations

import argparse
import csv
import grp
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "phase1_runner.py"
SAMPLE = ROOT / "phase1" / "pilot_sample.csv"
SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
URL_CREDENTIALS_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
URL_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|access_token|password|key)=)[^&\s]+")


def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run argv without a shell and retain bounded output and timeout status."""
    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return {
            "argv": [redact(str(arg)) for arg in argv],
            "returncode": proc.returncode,
            "timed_out": False,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": redact(stdout[-20000:]),
            "stderr": redact(stderr[-20000:]),
        }
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = ""
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
            except ProcessLookupError:
                stdout, stderr = proc.communicate()
        return {
            "argv": [redact(str(arg)) for arg in argv],
            "returncode": None,
            "timed_out": True,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": redact(stdout[-20000:]),
            "stderr": redact(stderr[-20000:]),
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


def installation_strategies(repo: Path) -> list[tuple[str, list[str]]]:
    """Return ordered, manifest-backed installation attempts."""
    strategies: list[tuple[str, list[str]]] = []
    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        strategies.extend([
            ("editable_test_extra", ["pip", "install", "--editable", ".[test]"]),
            ("editable_project", ["pip", "install", "--editable", "."]),
        ])
    for filename in ("requirements-test.txt", "requirements-dev.txt", "requirements.txt"):
        if (repo / filename).is_file():
            strategies.append((f"{filename}", ["pip", "install", "--requirement", filename]))
    return strategies


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


def install_project_dependencies(
    repo: Path, *, project_id: str, python: Path, uv: str,
    base_env: dict[str, str], timeout: int, cache_root: Path,
) -> dict[str, Any]:
    """Try each install recipe in a fresh venv and promote the first success."""
    strategies = installation_strategies(repo)
    result: dict[str, Any] = {"attempts": [], "ready": False}
    if not strategies:
        result.update({"status": "no_install_manifest", "category": "dependency_unresolvable"})
        return result

    ready = repo / ".venv_ready"
    for index, (strategy_name, install_args) in enumerate(strategies, start=1):
        attempt = repo / f".venv_attempt_{index}"
        attempt_python = attempt / "bin" / "python"
        attempt_record: dict[str, Any] = {"index": index, "strategy": strategy_name}
        remove_attempt_venv(repo, attempt)
        env = dict(base_env)
        cache = cache_root / f"{project_id}-{index}"
        cache.mkdir(parents=True, exist_ok=True)
        env["UV_CACHE_DIR"] = str(cache)

        create = run([uv, "venv", "--python", str(python), str(attempt)],
                     cwd=repo, env=env, timeout=120)
        attempt_record["venv_creation"] = create
        if create["returncode"] != 0 or create["timed_out"] or not attempt_python.is_file():
            attempt_record["category"] = "venv_creation_error" if not create["timed_out"] else "timeout"
            result["attempts"].append(attempt_record)
            remove_attempt_venv(repo, attempt)
            continue

        install = run([uv, *install_args[:2], "--python", str(attempt_python), *install_args[2:]],
                      cwd=repo, env=env, timeout=timeout)
        attempt_record["project_install"] = install
        if install["returncode"] == 0 and not install["timed_out"]:
            tools = run([
                uv, "pip", "install", "--python", str(attempt_python),
                "pytest", "pytest-timeout", "pytest-asyncio", "pytest-json-report",
            ], cwd=repo, env=env, timeout=timeout)
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
            attempt_record["category"] = "success"
            result["attempts"].append(attempt_record)
            result.update({
                "ready": True,
                "status": "installed",
                "successful_strategy": strategy_name,
                "python": str(ready / "bin" / "python"),
            })
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
        remove_attempt_venv(repo, attempt)

    result.update({"status": "all_install_attempts_failed", "category": "dependency_unresolvable"})
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
    python = Path(args.python).resolve()
    return {
        "PATH": f"{python.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(temp_root / "home"),
        "TMPDIR": str(temp_root / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PHASE1_RUN_UID": str(args.run_uid),
        "PHASE1_RUN_GID": str(args.run_gid),
    }


def validate_isolation(args: argparse.Namespace, temp_root: Path, env: dict[str, str]) -> dict[str, Any]:
    validation_dir = temp_root / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, str(RUNNER),
        "--isolation-launcher-json", args.isolation_launcher_json,
        "--python", str(Path(args.python).resolve()),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--project-id", help="ID de um projeto da amostra; padrão: primeiro")
    parser.add_argument("--isolation-launcher-json", required=True)
    parser.add_argument("--python", required=True, help="Python do ambiente dedicado com dependências de controle")
    parser.add_argument("--run-uid", type=int, required=True)
    parser.add_argument("--run-gid", type=int, required=True)
    parser.add_argument("--artifacts", type=Path, required=True, help="Diretório persistente gravável pelo usuário dedicado")
    parser.add_argument("--timeout-per-test", type=int, default=120)
    parser.add_argument("--clone-timeout", type=int, default=600)
    parser.add_argument("--install-timeout", type=int, default=1200,
                        help="limite por tentativa de dependências; padrão do pré-registro: 20 minutos")
    parser.add_argument("--uv", default="uv", help="executável uv ou caminho absoluto")
    parser.add_argument("--max-repo-bytes", type=int, default=2_000_000_000)
    args = parser.parse_args()

    if args.run_uid == 0 or os.geteuid() != args.run_uid or os.getegid() != args.run_gid:
        parser.error("execute como o usuário dedicado; UID/GID efetivos devem corresponder aos argumentos e UID não pode ser 0")
    try:
        docker_gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        docker_gid = None
    if docker_gid is not None and (docker_gid == os.getegid() or docker_gid in os.getgroups()):
        parser.error("o usuário dedicado não pode ter o grupo docker como grupo primário ou suplementar")
    try:
        launcher = json.loads(args.isolation_launcher_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--isolation-launcher-json inválido: {exc}")
    if not isinstance(launcher, list) or not launcher or not all(isinstance(x, str) for x in launcher):
        parser.error("launcher deve ser uma lista JSON não vazia de argumentos")
    executable = Path(launcher[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        parser.error("o primeiro argumento do launcher deve ser um executável absoluto")
    if args.timeout_per_test <= 0 or args.clone_timeout <= 0 or args.install_timeout <= 0 or args.max_repo_bytes <= 0:
        parser.error("timeouts e limite do clone devem ser positivos")

    try:
        projects = load_sample(args.sample.resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.project_id:
        projects = [p for p in projects if p["project_id"] == args.project_id]
        if not projects:
            parser.error(f"projeto não encontrado na amostra: {args.project_id}")
    else:
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
        preflight = validate_isolation(args, temp_root, env)
        record: dict[str, Any] = {
            "created_at_epoch": int(time.time()),
            "sample": str(args.sample.resolve()),
            "isolation_preflight": preflight,
            "projects": [],
        }
        if not preflight["passed"]:
            record["status"] = "blocked_isolation_controls_failed"
            (artifacts / "dynamic_launcher_smoke.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps(record, indent=2, ensure_ascii=False))
            return 2

        for project in projects:
            pid, sha, url = project["project_id"], project["commit_sha"], project["url"]
            repo = temp_root / f"repo-{pid}"
            project_record: dict[str, Any] = {
                "project_id": pid, "commit_sha": sha, "url": redact(url),
            }
            clone = run(["git", "clone", "--quiet", "--no-tags", url, str(repo)],
                        cwd=temp_root, env=env, timeout=args.clone_timeout)
            project_record["clone"] = clone
            if clone["returncode"] != 0 or clone["timed_out"]:
                project_record["status"] = "clone_failed"
                record["projects"].append(project_record)
                continue
            checkout = run(["git", "checkout", "--quiet", "--detach", sha],
                           cwd=repo, env=env, timeout=120)
            project_record["checkout"] = checkout
            if checkout["returncode"] != 0:
                project_record["status"] = "checkout_failed"
                record["projects"].append(project_record)
                continue
            size = sum(p.stat().st_size for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
            project_record["working_tree_bytes"] = size
            if size > args.max_repo_bytes:
                project_record["status"] = "repo_size_limit_exceeded"
                record["projects"].append(project_record)
                continue

            installation = install_project_dependencies(
                repo,
                project_id=pid,
                python=Path(args.python).resolve(),
                uv=uv,
                base_env=env,
                timeout=args.install_timeout,
                cache_root=temp_root / "uv-cache",
            )
            project_record["dependency_installation"] = installation
            if not installation["ready"]:
                project_record["status"] = "dependency_unresolvable"
                record["projects"].append(project_record)
                continue

            output = artifacts / f"{pid}-setup-plan.json"
            isolated_env = dict(env)
            isolated_env.update({
                "PHASE1_PROJECT_ID": pid,
                "PHASE1_PROJECT_ROOT": str(repo.resolve()),
                "PHASE1_RUN_ID": f"smoke-{pid}",
                "PHASE1_OUTPUT": str(output),
                "PYTHONPATH": str(ROOT / "scripts"),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            })
            pytest_args = [
                installation["python"], "-m", "pytest", "--continue-on-collection-errors",
                "--setup-plan", "-p", "phase1_probe", "-q",
            ]
            execution = run([*launcher, "--", *pytest_args], cwd=repo,
                            env=isolated_env, timeout=30 * 60)
            project_record["setup_plan"] = execution
            project_record["probe_artifact"] = str(output)
            try:
                metadata = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            project_record["collected_item_count"] = len(metadata.get("test_ids", []))
            project_record["collection_errors"] = metadata.get("collection_errors", [])
            project_record["status"] = "setup_plan_complete" if execution["returncode"] == 0 else "setup_plan_failed"
            record["projects"].append(project_record)

        record["status"] = "smoke_complete"
        report_path = artifacts / "dynamic_launcher_smoke.json"
        report_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(record, indent=2, ensure_ascii=False))
        print(f"Relatório salvo em: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
