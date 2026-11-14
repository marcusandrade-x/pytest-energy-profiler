#!/usr/bin/env python3
"""Build an auditable, AST-test-stratified pilot sample.

The script writes the selection audit even when it refuses to produce a sample.
It never silently treats an absent catalog row as a successfully scanned project.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from phase1_catalog_batch import select_stratified_pilot, write_csv
except ModuleNotFoundError:  # supports `python -m scripts.generate_pilot`
    from scripts.phase1_catalog_batch import select_stratified_pilot, write_csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "phase1" / "data" / "catalog" / "fixture_catalog_batch.csv"
DEFAULT_PROJECTS = PROJECT_ROOT / "input_data" / "mined_projects.csv"
DEFAULT_STATUS = PROJECT_ROOT / "phase1" / "data" / "catalog" / "catalog_project_status.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "phase1" / "data" / "pilot" / "pilot_sample.csv"
DEFAULT_AUDIT = PROJECT_ROOT / "phase1" / "data" / "pilot" / "pilot_sample_audit.csv"
def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_status(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    rows = _read_csv(path)
    required = {"project_id", "catalog_status", "commit_sha"}
    if rows and not required.issubset(rows[0]):
        raise ValueError(
            f"{path} deve conter as colunas: {', '.join(sorted(required))}"
        )
    status: dict[str, dict[str, str]] = {}
    for row in rows:
        project_id = row.get("project_id", "").strip()
        if not project_id:
            continue
        if project_id in status:
            raise ValueError(f"Status duplicado para {project_id} em {path}")
        status[project_id] = row
    return status


def _int_field(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valor inválido em {key}: {row.get(key)!r}") from exc


def _quartiles(projects: list[dict[str, Any]]) -> dict[str, int]:
    """Use the same rank-to-bucket boundaries as select_stratified_pilot."""
    ordered = sorted(
        projects,
        key=lambda row: (_int_field(row, "num_tests"), row.get("url", "")),
    )
    return {
        str(row["project_id"]): min(3, index * 4 // len(ordered)) + 1
        for index, row in enumerate(ordered)
    } if ordered else {}


def build_catalog_summary(
    fixtures: list[dict[str, str]], status_rows: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"commit_sha": "", "fixture_count": 0, "scope_counts": Counter(), "status": ""}
    )
    for fixture in fixtures:
        project_id = fixture.get("project_id", "").strip()
        if not project_id:
            raise ValueError("Há linha de fixture sem project_id")
        entry = summary[project_id]
        sha = fixture.get("commit_sha", "").strip()
        if sha and entry["commit_sha"] and entry["commit_sha"] != sha:
            raise ValueError(f"Mais de um commit SHA no catálogo para {project_id}")
        if sha:
            entry["commit_sha"] = sha
        entry["fixture_count"] += 1
        entry["scope_counts"][fixture.get("scope", "unknown")] += 1
        entry["status"] = "ok"

    for project_id, status in status_rows.items():
        entry = summary[project_id]
        stated_sha = status.get("commit_sha", "").strip()
        if stated_sha and entry["commit_sha"] and stated_sha != entry["commit_sha"]:
            raise ValueError(f"SHA do manifesto diverge do catálogo para {project_id}")
        if stated_sha:
            entry["commit_sha"] = stated_sha
        stated_status = status.get("catalog_status", "").strip().lower()
        if stated_status:
            if entry["status"] == "ok" and stated_status != "ok":
                raise ValueError(f"Manifesto marca {project_id} como {stated_status}, mas há fixtures")
            entry["status"] = stated_status
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument(
        "--catalog-status", type=Path, default=DEFAULT_STATUS,
        help="CSV opcional com project_id,catalog_status,commit_sha; necessário para projetos sem fixtures.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--acknowledge-zero-fixture", action="append", default=[], metavar="PROJECT_ID",
        help="Confirma revisão manual de divergência: mineração > 0, catálogo = 0. Repetível.",
    )
    args = parser.parse_args()

    for path in (args.catalog, args.projects):
        if not path.is_file():
            parser.error(f"Arquivo não encontrado: {path}")
    if args.size < 1:
        parser.error("--size deve ser positivo")

    fixture_rows = _read_csv(args.catalog)
    status_rows = _read_status(args.catalog_status)
    catalog = build_catalog_summary(fixture_rows, status_rows)
    source_rows = _read_csv(args.projects)
    eligible = [
        row for row in source_rows
        if not (row.get("motivo_exclusao") or "").strip()
    ]
    if len({row.get("project_name", "") for row in eligible}) != len(eligible):
        raise SystemExit("Há project_name duplicado entre os elegíveis; não é seguro fazer a junção.")

    acknowledged = set(args.acknowledge_zero_fixture)
    projects: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    failed: list[str] = []
    unacknowledged_zero: list[str] = []
    for source in eligible:
        project_id = source.get("project_name", "").strip()
        if not project_id:
            unresolved.append("<project_name vazio>")
            continue
        record = catalog.get(project_id)
        # A catalog row proves the scan found a fixture; a zero-row outcome needs
        # an explicit status record so failure and a valid empty scan differ.
        status = record["status"] if record else ""
        sha = record["commit_sha"] if record else ""
        fixture_count = record["fixture_count"] if record else 0
        scope_counts = record["scope_counts"] if record else Counter()
        sha_is_valid = bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha, re.IGNORECASE))
        if not status or not sha_is_valid:
            unresolved.append(f"{project_id} (status={status or 'ausente'}, SHA={sha or 'ausente'})")
        elif status != "ok":
            failed.append(f"{project_id} ({status})")

        source_count = _int_field(source, "num_fixtures")
        zero_divergence = source_count > 0 and fixture_count == 0
        if zero_divergence and project_id not in acknowledged:
            unacknowledged_zero.append(project_id)

        project = dict(source)
        project.update({
            "project_id": project_id,
            "catalog_status": status or "unresolved",
            "commit_sha": sha,
            "catalog_fixture_count": fixture_count,
            "has_module_session": bool(scope_counts.get("module", 0) or scope_counts.get("session", 0)),
            "catalog_function_fixtures": scope_counts.get("function", 0),
            "catalog_class_fixtures": scope_counts.get("class", 0),
            "catalog_module_fixtures": scope_counts.get("module", 0),
            "catalog_package_fixtures": scope_counts.get("package", 0),
            "catalog_session_fixtures": scope_counts.get("session", 0),
            "catalog_dynamic_unknown_fixtures": scope_counts.get("dynamic/unknown", 0),
            "fixture_count_difference": fixture_count - source_count,
            "zero_fixture_divergence_acknowledged": zero_divergence and project_id in acknowledged,
        })
        projects.append(project)
        audit_rows.append({
            "project_id": project_id,
            "url": source.get("url", ""),
            "catalog_status": status or "unresolved",
            "commit_sha": sha,
            "num_fixtures_source": source_count,
            "catalog_fixture_count": fixture_count,
            "difference_catalog_minus_source": fixture_count - source_count,
            "zero_fixture_divergence_acknowledged": zero_divergence and project_id in acknowledged,
            "has_module_session": project["has_module_session"],
        })

    args.audit.parent.mkdir(parents=True, exist_ok=True)
    audit_fields = list(audit_rows[0]) if audit_rows else [
        "project_id", "url", "catalog_status", "commit_sha", "num_fixtures_source",
        "catalog_fixture_count", "difference_catalog_minus_source",
        "zero_fixture_divergence_acknowledged", "has_module_session",
    ]
    write_csv(args.audit, audit_fields, audit_rows)

    extra_catalog_ids = sorted(set(catalog) - {row.get("project_name", "") for row in eligible})
    if extra_catalog_ids:
        print(f"Aviso: {len(extra_catalog_ids)} IDs do catálogo não estão entre os elegíveis.")
    print(f"Elegíveis: {len(eligible)}; IDs com fixtures: {len(catalog)}")
    print(f"Auditoria salva em: {args.audit}")
    if unresolved or failed or unacknowledged_zero:
        if unresolved:
            print("Projetos sem status de catálogo e/ou SHA Git válido: " + ", ".join(unresolved))
        if failed:
            print("Projetos com falha de catálogo: " + ", ".join(failed))
        if unacknowledged_zero:
            print("Divergência positiva->zero requer revisão explícita: " + ", ".join(unacknowledged_zero))
        print("Amostra não gerada. Resolva os itens acima e rode novamente.")
        if args.output.exists():
            print(f"Atenção: {args.output} já existe, foi preservado e não deve ser congelado com esta auditoria bloqueada.")
        return 2

    # Ensure the fixture-priority helper joins on the same identity as the
    # static catalog; the source CSV otherwise has no project_id column.
    selected = select_stratified_pilot(
        projects, n=args.size, seed=args.seed, fixture_catalog=fixture_rows,
    )
    if len(selected) != min(args.size, len(projects)):
        raise SystemExit(f"A seleção retornou {len(selected)} de {min(args.size, len(projects))} projetos.")

    quartile_by_id = _quartiles(projects)
    selected_ids = {row["project_id"] for row in selected}
    for row in selected:
        row["pilot_stratum"] = f"AST_TEST_Q{quartile_by_id[row['project_id']]}"
        row["selection_seed"] = args.seed

    sample_fields = [
        "pilot_stratum", "selection_seed", "project_id", "project_name", "url",
        "commit_sha", "num_tests", "num_fixtures", "catalog_fixture_count",
        "catalog_function_fixtures", "catalog_class_fixtures", "catalog_module_fixtures",
        "catalog_package_fixtures", "catalog_session_fixtures",
        "catalog_dynamic_unknown_fixtures", "has_module_session", "loc_projeto_total",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, sample_fields, selected)

    by_quartile = Counter(quartile_by_id[project_id] for project_id in selected_ids)
    broad_scope = sum(bool(row["has_module_session"]) for row in selected)
    print("Amostra gerada:")
    for quartile in range(1, 5):
        print(f"  AST_TEST_Q{quartile}: {by_quartile[quartile]} projetos")
    print(f"  Com fixture module/session: {broad_scope}/{len(selected)}")
    print(f"Amostra salva em: {args.output}")
    print("Revise e versione a amostra antes de qualquer execução do piloto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
