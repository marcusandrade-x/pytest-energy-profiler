"""Static catalog and deterministic helpers for the offline fixture triage.

This module deliberately has no pytest, CodeCarbon, or network dependency so
its parsing and decision rules can be tested in a minimal Python installation.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


CATALOG_FIELDS = [
    "project_id", "commit_sha", "fixture_id", "file", "line", "name",
    "function_name", "scope", "autouse", "uses_yield", "parameterized", "parameter_count",
    "dependencies_static", "dependency_count_static", "code_lines",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_DIR = PROJECT_ROOT / "phase1" / "data" / "catalog"


def _literal_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _YieldFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found = False

    def visit_Yield(self, node: ast.Yield) -> None:  # noqa: N802
        self.found = True

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:  # noqa: N802
        self.found = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return


def _has_yield(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    finder = _YieldFinder()
    for statement in function.body:
        finder.visit(statement)
    return finder.found


def _fixture_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    pytest_modules = {"pytest"}
    fixture_names = {"fixture"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"pytest", "pytest_asyncio"}:
                    pytest_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "pytest", "pytest_asyncio"
        }:
            for alias in node.names:
                if alias.name == "fixture":
                    fixture_names.add(alias.asname or alias.name)
    return pytest_modules, fixture_names


def _is_fixture_decorator(
    decorator: ast.expr, pytest_modules: set[str], fixture_names: set[str]
) -> ast.Call | ast.Attribute | ast.Name | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name) and target.id in fixture_names:
        return decorator if isinstance(decorator, ast.Call) else target
    if (
        isinstance(target, ast.Attribute)
        and target.attr == "fixture"
        and isinstance(target.value, ast.Name)
        and target.value.id in pytest_modules
    ):
        return decorator if isinstance(decorator, ast.Call) else target
    return None


def _args(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = function.args
    ordered = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        ordered.append(args.vararg)
    if args.kwarg:
        ordered.append(args.kwarg)
    return [arg.arg for arg in ordered if arg.arg != "request"]


def _definition_id(project_id: str, relpath: str, line: int, name: str) -> str:
    key = f"{project_id}\0{relpath}\0{line}\0{name}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def catalog_python_file(path: Path, repo: Path, project_id: str) -> list[dict[str, Any]]:
    """Extract local fixture definitions from one Python file using AST."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return []

    modules, names = _fixture_aliases(tree)
    relpath = path.relative_to(repo).as_posix()
    result: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator_call = next(
            (
                found
                for decorator in node.decorator_list
                if (found := _is_fixture_decorator(decorator, modules, names)) is not None
            ),
            None,
        )
        if decorator_call is None:
            continue

        kwargs = {
            keyword.arg: keyword.value
            for keyword in decorator_call.keywords
            if keyword.arg is not None
        } if isinstance(decorator_call, ast.Call) else {}
        effective_name = _literal_str(kwargs.get("name")) or node.name
        definition_line = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        scope = _literal_str(kwargs.get("scope")) or (
            "function" if "scope" not in kwargs else "dynamic/unknown"
        )
        autouse = (
            _literal_bool(kwargs["autouse"]) if "autouse" in kwargs else False
        )
        params_node = kwargs.get("params")
        parameterized = "params" in kwargs
        parameter_count = (
            len(params_node.elts)
            if isinstance(params_node, (ast.List, ast.Tuple, ast.Set))
            else (None if parameterized else 0)
        )
        end_line = getattr(node, "end_lineno", node.lineno)
        dependencies = _args(node)
        result.append({
            "project_id": project_id,
            "commit_sha": "",
            "fixture_id": _definition_id(project_id, relpath, definition_line, effective_name),
            "file": relpath,
            "line": definition_line,
            "name": effective_name,
            "function_name": node.name,
            "scope": scope,
            "autouse": autouse,
            "uses_yield": _has_yield(node),
            "parameterized": parameterized,
            "parameter_count": parameter_count,
            "dependencies_static": ";".join(dependencies),
            "dependency_count_static": len(dependencies),
            "code_lines": max(1, end_line - node.lineno + 1),
        })
    return result


def catalog_project(repo: Path, project_id: str, commit_sha: str = "") -> list[dict[str, Any]]:
    """Catalog project-owned fixtures, excluding hidden and VCS directories."""
    rows: list[dict[str, Any]] = []
    ignored = {".git", ".hg", ".svn"}
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if any(part in ignored or part.startswith(".") for part in rel.parts):
            continue
        for row in catalog_python_file(path, repo, project_id):
            row["commit_sha"] = commit_sha
            rows.append(row)
    return rows


def catalog_test_modules(repo: Path) -> dict[str, int]:
    """Return AST estimates of collected items per test module.

    Literal ``pytest.mark.parametrize`` decorators are expanded. Expressions
    that cannot be evaluated statically contribute one base item; callers that
    need a defensible collection fraction should also inspect the uncertainty
    returned by :func:`catalog_test_modules_details`.
    """
    return {
        module: details["estimated_test_items"]
        for module, details in catalog_test_modules_details(repo).items()
    }


def _parametrize_value_count(node: ast.AST, constants: dict[str, Any]) -> int | None:
    if isinstance(node, ast.Name) and node.id in constants:
        value = constants[node.id]
    else:
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            value = None
    if isinstance(value, (list, tuple, set, dict, str, bytes)):
        return len(value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
        try:
            values = [ast.literal_eval(arg) for arg in node.args]
            if all(isinstance(arg, int) for arg in values):
                return len(range(*values))
        except (ValueError, TypeError, SyntaxError):
            pass
    return None


def _parametrize_decorator_count(
    decorator: ast.expr, constants: dict[str, Any]
) -> int | None | bool:
    if not isinstance(decorator, ast.Call):
        return False
    target = decorator.func
    if not (
        isinstance(target, ast.Attribute) and target.attr == "parametrize"
        or isinstance(target, ast.Name) and target.id == "parametrize"
    ):
        return False
    values_node = decorator.args[1] if len(decorator.args) > 1 else next(
        (keyword.value for keyword in decorator.keywords if keyword.arg == "argvalues"), None
    )
    if values_node is None:
        return None
    count = _parametrize_value_count(values_node, constants)
    return max(1, count) if count is not None else None


def catalog_test_modules_details(repo: Path) -> dict[str, dict[str, Any]]:
    """Return per-module AST test-item estimates and unresolved parametrizations."""
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if any(part.startswith(".") or part == ".git" for part in rel.parts):
            continue
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        constants: dict[str, Any] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                try:
                    value = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                try:
                    constants[statement.target.id] = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
        test_functions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        estimated_items = 0
        unresolved: list[dict[str, Any]] = []
        for function in test_functions:
            multiplier = 1
            unknown = False
            for decorator in function.decorator_list:
                count = _parametrize_decorator_count(decorator, constants)
                if count is False:
                    continue
                if count is None:
                    unknown = True
                else:
                    multiplier *= count
            estimated_items += multiplier
            if unknown:
                unresolved.append({"function": function.name, "line": function.lineno})
        result[rel.as_posix()] = {
            "ast_test_functions": len(test_functions),
            "estimated_test_items": estimated_items,
            "unexpanded_parametrized_functions": unresolved,
            "has_pytest_generate_tests_hook": any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "pytest_generate_tests"
                for node in ast.walk(tree)
            ),
        }
    return result


def normalize_error(message: str) -> str:
    """Normalize unstable path, port, numeric, and hash portions of an error."""
    text = message.replace("\\", "/").strip().lower()
    text = re.sub(r"(?<!\w)(?:[a-z]:)?/[^\s:'\"]+", "<path>", text)
    text = re.sub(r"\b(?:0x)?[0-9a-f]{12,}\b", "<hash>", text)
    text = re.sub(r"(?<=[: ])\d{2,5}(?=\b)", "<num>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text)
    return text[:1200]


def error_signature(exception_class: str, message: str, innermost_frame: str) -> str:
    frame = normalize_error(innermost_frame)
    return f"{exception_class.strip()}|{normalize_error(message)}|{frame}"


def loss_by_test(
    test_to_fixtures: dict[str, set[str]], failed_fixture_ids: set[str]
) -> set[str]:
    """Test items lost because their --setup-plan closure contains a failed fixture."""
    return {
        nodeid for nodeid, fixture_ids in test_to_fixtures.items()
        if fixture_ids & failed_fixture_ids
    }


def collection_is_viable(collected_items: int, ast_test_definitions: int, fraction: float = 0.5) -> bool:
    if ast_test_definitions <= 0:
        return False
    return collected_items / ast_test_definitions >= fraction


def stable_test_ids(first: Iterable[str], second: Iterable[str]) -> bool:
    return set(first) == set(second)


def summarize_fixture_repetitions(
    runs: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fail closed across two runs and retain whether the result was intermittent."""
    by_fixture: dict[str, dict[str, dict[str, dict[str, list[dict[str, Any]]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    for event in runs:
        if event.get("phase") not in {"setup", "teardown"}:
            continue
        fixture_id = str(event.get("fixture_id", ""))
        run_id = str(event.get("run_id", ""))
        instance_id = str(event.get("instance_id", ""))
        if fixture_id and run_id and instance_id:
            by_fixture[fixture_id][run_id][instance_id][str(event["phase"])].append(event)

    summary: dict[str, dict[str, Any]] = {}
    for fixture_id, attempts in by_fixture.items():
        failed_instances = 0
        ok_instances = 0
        failed_runs: list[str] = []
        missing_teardown: list[str] = []
        signatures = sorted({
            str(event.get("signature", ""))
            for instances in attempts.values()
            for phases in instances.values()
            for phase in phases.values()
            for event in phase
            if event.get("status") == "failed" and event.get("signature")
        })
        run_failed: dict[str, bool] = {}
        for run, instances in attempts.items():
            run_failed[run] = False
            for phases in instances.values():
                setups = phases.get("setup", [])
                teardowns = phases.get("teardown", [])
                failed = (
                    not setups or not teardowns
                    or any(event.get("status") == "failed" for event in setups + teardowns)
                )
                if failed:
                    failed_instances += 1
                    run_failed[run] = True
                    if setups and not teardowns:
                        missing_teardown.append(run)
                else:
                    ok_instances += 1
        failed_runs = [run for run, failed in run_failed.items() if failed]
        summary[fixture_id] = {
            "runs_observed": len(attempts),
            "instances_ok": ok_instances,
            "instances_failed": failed_instances,
            "failed_runs": len(failed_runs),
            "missing_teardown_runs": missing_teardown,
            "usable": len(failed_runs) == 0 and len(attempts) >= 2,
            "failed": bool(failed_runs),
            "intermittent": len(failed_runs) == 1 and len(attempts) >= 2,
            "error_signatures": signatures,
        }
    return summary


def choose_x_threshold(
    projects: Iterable[dict[str, Any]],
    *,
    min_usable_fixtures: int = 3,
    target_projects: int = 250,
    thresholds: tuple[int, ...] = (20, 30, 50),
) -> tuple[int | None, dict[int, int]]:
    """Return the strictest pre-registered X that reaches the project target."""
    rows = list(projects)
    counts = {
        threshold: sum(
            1 for row in rows
            if row.get("collection_viable") is True
            and int(row.get("usable_fixtures", 0)) >= min_usable_fixtures
            and float(row.get("fixture_test_loss_pct", 100.0)) <= threshold
        )
        for threshold in thresholds
    }
    selected = next((threshold for threshold in sorted(thresholds) if counts[threshold] >= target_projects), None)
    return selected, counts


def early_stop_after_k_identical_failures(
    test_order: list[str],
    test_to_fixtures: dict[str, set[str]],
    failures: Iterable[dict[str, str]],
    k: int = 3,
) -> set[str]:
    """Simulate tests skipped after a fixture reaches K identical failures."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    stop_at: dict[str, int] = {}
    positions = {nodeid: index for index, nodeid in enumerate(test_order)}
    for failure in failures:
        fixture_id = failure.get("fixture_id", "")
        signature = failure.get("signature", "")
        nodeid = failure.get("nodeid", "")
        if not fixture_id or not signature or nodeid not in positions:
            continue
        key = (fixture_id, signature)
        counts[key] += 1
        if counts[key] == k:
            stop_at[fixture_id] = positions[nodeid]
    return {
        nodeid for nodeid in test_order
        if any(
            fixture_id in test_to_fixtures.get(nodeid, set())
            and positions[nodeid] > stop_at.get(fixture_id, len(test_order))
            for fixture_id in stop_at
        )
    }


def pytest_probe_args(mode: str, timeout_seconds: int = 120) -> list[str]:
    if mode not in {"plan", "setup-only", "normal"}:
        raise ValueError(f"unknown probe mode: {mode}")
    args = [
        "--continue-on-collection-errors",
        "-p", "no:cacheprovider",
        "-p", "no:randomly",
        "-p", "no:random_order",
        "-p", "no:pytest_random_order",
        "--timeout", str(timeout_seconds),
        "--timeout-method=signal",
    ]
    if mode == "plan":
        args.append("--setup-plan")
    elif mode == "setup-only":
        args.append("--setup-only")
    return args


def select_stratified_pilot(
    rows: Iterable[dict[str, Any]], n: int = 30, seed: int = 42,
    fixture_catalog: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Select by AST test-count quartile, prioritizing module/session fixtures."""
    fixtures_by_project: dict[str, set[str]] = defaultdict(set)
    for fixture in fixture_catalog:
        fixtures_by_project[str(fixture.get("project_id", ""))].add(
            str(fixture.get("scope", ""))
        )

    def test_count(row: dict[str, Any]) -> int:
        return int(row.get("ast_test_definitions") or row.get("num_tests") or 0)

    projects = sorted(
        list(rows),
        key=lambda row: (test_count(row), str(row.get("url", ""))),
    )
    if n <= 0 or not projects:
        return []
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(min(4, n, len(projects)))]
    for index, row in enumerate(projects):
        bucket = min(len(buckets) - 1, index * len(buckets) // len(projects))
        buckets[bucket].append(row)

    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    quota, remainder = divmod(min(n, len(projects)), len(buckets))
    for index, bucket in enumerate(buckets):
        take = quota + (1 if index < remainder else 0)
        preferred = [
            row for row in bucket
            if fixtures_by_project.get(str(row.get("project_id", "")), set())
               & {"module", "session"}
        ]
        ordinary = [row for row in bucket if row not in preferred]
        preferred_take = min(len(preferred), (take + 1) // 2)
        selected.extend(rng.sample(preferred, preferred_take))
        remaining = take - preferred_take
        pool = ordinary + [row for row in preferred if row not in selected]
        selected.extend(rng.sample(pool, min(remaining, len(pool))))
    return selected


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    import argparse
    import subprocess
    import tempfile
    import sys
    import os

    parser = argparse.ArgumentParser(description="Extrai catálogo estático de fixtures via AST.")
    parser.add_argument("--input", type=Path, required=True, help="CSV de entrada (ex: input_data/mined_projects.csv)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_CATALOG_DIR, help="Diretório para salvar os resultados")
    parser.add_argument("--limit", type=int, default=None, help="Processar apenas N projetos (útil para teste)")

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Erro: Arquivo '{args.input}' não encontrado.")
        sys.exit(1)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Lendo a base de candidatos
    projects = []
    with args.input.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            projects.append(row)

    if args.limit:
        projects = projects[:args.limit]

    all_fixtures = []
    print(f"Iniciando catalogação de {len(projects)} projetos...")

    # Variáveis de ambiente para o git clone não travar pedindo senha em repositórios privados/apagados
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    for i, proj in enumerate(projects, 1):
        # Acomodando os possíveis nomes de coluna do seu mined_projects.csv
        url = proj.get("url") or proj.get("repository") or proj.get("clone_url")
        project_id = proj.get("project_id") or proj.get("name") or proj.get("repo_name") or str(i)

        if not url:
            print(f"[{i}/{len(projects)}] {project_id}: URL não encontrada, ignorando.")
            continue

        print(f"[{i}/{len(projects)}] Analisando: {project_id}")

        # Cria uma pasta temporária que será magicamente apagada ao final do bloco 'with'
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "repo"
            try:
                # Faz o clone "raso" (depth 1) para baixar só os arquivos atuais (poupa 90% do disco e tempo)
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--single-branch", url, str(repo_path)],
                    env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )

                # Roda o motor AST que já estava no arquivo
                fixtures = catalog_project(repo_path, project_id)
                all_fixtures.extend(fixtures)

            except subprocess.CalledProcessError:
                print(f"  -> Erro ao clonar {url} (pode ter sido apagado do GitHub).")
            except Exception as e:
                print(f"  -> Erro interno ao analisar {project_id}: {e}")

    # Salva o resultado final
    out_file = args.out_dir / "fixture_catalog.csv"
    write_csv(out_file, CATALOG_FIELDS, all_fixtures)
    print(f"\nFinalizado! {len(all_fixtures)} fixtures estáticas catalogadas.")
    print(f"Catálogo salvo em: {out_file}")
