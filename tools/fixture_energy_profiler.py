#!/usr/bin/env python3
"""Profile pytest execution and fixture energy with CodeCarbon (v1).

Standalone pytest runner/plugin. Drop this file in the root directory of a
Python project and run it instead of invoking pytest directly. No changes to
conftest.py or to the project's tests are required.

Outputs (all inside --energy-output-dir):

1. fixture_energy.csv                  raw, exclusive measurements (one row per
                                       fixture phase / pytest phase)
2. fixture_energy_rq1_summary.csv      setup vs. test body vs. teardown (RQ1)
3. fixture_energy_rq2_fixtures.csv     per-fixture reuse/energy metrics (RQ2)
4. fixture_energy_rq2_scopes.csv       per-scope reuse/energy metrics (RQ2)
5. fixture_energy_run_metadata.json    environment, versions, test outcomes and
                                       measurement-validity diagnostics

What changed in v1 (relative to the original script)
----------------------------------------------------
* Profiler bookkeeping no longer leaks into the measurement. Rows are built and
  written only BETWEEN tests (pytest_runtest_logfinish), when no measurement is
  active. Previously, row construction and CSV flushes ran inside the outer
  (test-phase) measurement window and inflated its "residual" energy.
* Summaries are computed from streaming aggregates instead of keeping every row
  in memory and re-scanning it for each fixture. Memory and time are now
  roughly linear in the number of tests/fixture instances.
* Static fixture metadata (source file, line, scope, id) is computed once per
  fixture definition (precomputed after collection when possible) instead of
  twice per fixture setup.
* New raw column `test_outcome` (passed / failed / skipped / error / xfailed /
  xpassed). Summaries exclude skipped/error tests by default (see
  --energy-summary-include-all).
* New raw/summary column `experiment_labels` (from repeated --energy-label
  KEY=VALUE options), so runs can be concatenated and compared later.
* `is_project_fixture` no longer counts plugin fixtures that live inside a
  virtualenv created inside the project directory.
* Run metadata JSON with environment info and diagnostics that show how
  trustworthy the energy numbers are (share of zero-energy rows, share of rows
  shorter than the sampling interval, duration-vs-energy correlation).

Example:
    python fixture_energy_profiler.py --energy-project-only \
        --energy-country-iso-code BRA --energy-label rep=1 tests/ -q

Profiler-specific options use the --energy-* prefix. All unknown arguments are
forwarded unchanged to pytest.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import importlib.util
import inspect
import json
import math
import os
import platform
import subprocess
import sys
import time
import uuid
import warnings
from array import array
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:
    import pytest
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "pytest is not installed in this Python environment. "
        "Install it before running the profiler."
    ) from exc

try:
    import codecarbon
    from codecarbon import EmissionsTracker, OfflineEmissionsTracker
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "CodeCarbon is not installed. Install it with: pip install codecarbon"
    ) from exc


SCHEMA_VERSION = "v1"

# Tests with these final outcomes did not really execute the code under test,
# so their energy is left out of the derived summaries (raw CSV keeps them).
SUMMARY_EXCLUDED_OUTCOMES = frozenset({"skipped", "error"})

RAW_FIELDS = [
    "record_type",
    "test_name",
    "fixture_name",
    "fixture_definition_id",
    "fixture_instance_id",
    "fixture_scope",
    "fixture_source",
    "fixture_line",
    "is_project_fixture",
    "phase",
    "duration_ms",
    "energy_consumed_kwh",
    "codecarbon_duration_ms",
    "cpu_energy_kwh",
    "gpu_energy_kwh",
    "ram_energy_kwh",
    "emissions_kgco2eq",
    "emissions_rate_kgco2eq_per_s",
    "cpu_power_w",
    "gpu_power_w",
    "ram_power_w",
    "cpu_utilization_percent",
    "gpu_utilization_percent",
    "ram_utilization_percent",
    "ram_used_gb",
    "water_consumed_l",
    "country_name",
    "country_iso_code",
    "region",
    "tracking_mode",
    "measurement_chunks",
    "measurement_status",
    "measurement_error",
    "project_name",
    "run_id",
    "experiment_labels",
    "test_outcome",
    "timestamp_utc",
    "pytest_version",
    "codecarbon_version",
    "python_version",
]

RQ1_FIELDS = [
    "phase",
    "duration_ms",
    "energy_consumed_kwh",
    "percentage_of_execution_energy",
    "profiled_fixture_energy_kwh",
    "residual_energy_kwh",
    "profiled_fixture_percentage_of_phase_energy",
    "cpu_energy_kwh",
    "gpu_energy_kwh",
    "ram_energy_kwh",
    "project_name",
    "run_id",
    "experiment_labels",
]

RQ2_FIXTURE_FIELDS = [
    "fixture_definition_id",
    "fixture_name",
    "fixture_scope",
    "fixture_source",
    "fixture_line",
    "fixture_instance_count",
    "total_fixture_uses",
    "unique_tests_using_fixture",
    "mean_uses_per_instance",
    "median_uses_per_instance",
    "total_setup_duration_ms",
    "total_teardown_duration_ms",
    "total_lifecycle_duration_ms",
    "total_setup_energy_kwh",
    "total_teardown_energy_kwh",
    "total_lifecycle_energy_kwh",
    "mean_lifecycle_energy_kwh_per_instance",
    "median_lifecycle_energy_kwh_per_instance",
    "amortized_setup_energy_kwh_per_use",
    "amortized_teardown_energy_kwh_per_use",
    "amortized_lifecycle_energy_kwh_per_use",
    "project_name",
    "run_id",
    "experiment_labels",
]

RQ2_SCOPE_FIELDS = [
    "fixture_scope",
    "fixture_definition_count",
    "fixture_instance_count",
    "total_fixture_uses",
    "unique_tests_using_scope_fixtures",
    "mean_uses_per_instance",
    "median_uses_per_instance",
    "total_setup_energy_kwh",
    "total_teardown_energy_kwh",
    "total_lifecycle_energy_kwh",
    "mean_lifecycle_energy_kwh_per_instance",
    "amortized_setup_energy_kwh_per_use",
    "amortized_teardown_energy_kwh_per_use",
    "amortized_lifecycle_energy_kwh_per_use",
    "project_name",
    "run_id",
    "experiment_labels",
]


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MeasurementMetadata:
    """Metadata that identifies one measurement target."""

    record_type: str
    test_name: str
    fixture_name: str = ""
    fixture_definition_id: str = ""
    fixture_instance_id: str = ""
    fixture_scope: str = ""
    fixture_source: str = ""
    fixture_line: Optional[int] = None
    is_project_fixture: bool = False


@dataclass(frozen=True)
class FixtureStaticInfo:
    """Properties of a fixture definition that never change during a run."""

    fixture_name: str
    scope: str
    source: str
    line: Optional[int]
    is_project: bool
    definition_id: str


@dataclass
class LogicalMeasurement:
    """One logical measurement that may span multiple CodeCarbon task chunks."""

    metadata: MeasurementMetadata
    phase: str
    duration_ns: int = 0
    codecarbon_chunks: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    current_task_name: Optional[str] = None
    current_segment_start_ns: Optional[int] = None
    ended_at: float = 0.0  # wall-clock epoch seconds, set when the measurement ends


@dataclass
class TeardownState:
    """Aggregate the finalizers belonging to one fixture instance."""

    measurement: LogicalMeasurement
    wrapped_finalizer_count: int
    completed_finalizer_count: int = 0


@dataclass
class PhaseAgg:
    """Streaming aggregate used for the RQ1 summary."""

    duration_ms: float = 0.0
    energy: float = 0.0
    fixture_energy: float = 0.0
    residual_energy: float = 0.0
    cpu: float = 0.0
    gpu: float = 0.0
    ram: float = 0.0

    def add_row(self, row: dict[str, Any]) -> None:
        energy = _safe_number(row.get("energy_consumed_kwh"))
        self.duration_ms += _safe_number(row.get("duration_ms"))
        self.energy += energy
        if row.get("record_type") == "fixture":
            self.fixture_energy += energy
        else:
            self.residual_energy += energy
        self.cpu += _safe_number(row.get("cpu_energy_kwh"))
        self.gpu += _safe_number(row.get("gpu_energy_kwh"))
        self.ram += _safe_number(row.get("ram_energy_kwh"))

    def __add__(self, other: "PhaseAgg") -> "PhaseAgg":
        return PhaseAgg(
            *(
                getattr(self, f.name) + getattr(other, f.name)
                for f in dataclasses.fields(self)
            )
        )


@dataclass
class InstanceAgg:
    setup_energy: float = 0.0
    teardown_energy: float = 0.0
    setup_ms: float = 0.0
    teardown_ms: float = 0.0

    @property
    def lifecycle_energy(self) -> float:
        return self.setup_energy + self.teardown_energy


@dataclass
class DefinitionAgg:
    name: str
    scope: str
    source: str
    line: Any
    instances: dict[str, InstanceAgg] = field(default_factory=dict)


@dataclass
class DefinitionStats:
    """Per-definition numbers derived once and reused by both RQ2 summaries."""

    definition_id: str
    agg: DefinitionAgg
    instance_ids: list[str]
    uses_per_instance: list[int]
    tests: set[int]
    setup_energy: float
    teardown_energy: float
    setup_ms: float
    teardown_ms: float
    lifecycle_by_instance: list[float]

    @property
    def total_uses(self) -> int:
        return sum(self.uses_per_instance)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


@contextmanager
def _suppress_codecarbon_short_task_warnings() -> Iterator[None]:
    """Suppress only known warnings produced by very short CodeCarbon tasks.

    The filter is intentionally scoped to calls into CodeCarbon. RuntimeWarning
    instances raised by the project under test are not globally suppressed.
    """

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Mean of empty slice.*",
            category=RuntimeWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"invalid value encountered in scalar divide.*",
            category=RuntimeWarning,
        )
        yield


def _safe_number(value: Any, default: float = 0.0) -> float:
    """Convert a value to a finite float, mapping None/NaN/Inf to default."""

    if value is None:
        return default
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    return converted if math.isfinite(converted) else default


def _safe_mean(values: list[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _safe_median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pearson(xs: Any, ys: Any) -> Optional[float]:
    """Pearson correlation without depending on statistics.correlation (3.10+)."""

    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = math.fsum(xs) / n
    mean_y = math.fsum(ys) / n
    sxx = math.fsum((x - mean_x) ** 2 for x in xs)
    syy = math.fsum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = math.fsum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _ranks(values: Any) -> list[float]:
    """Average ranks (ties share the mean rank), as used by Spearman's rho."""

    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def _spearman(xs: Any, ys: Any) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _weighted_average(chunks: list[Any], attribute: str) -> float:
    """Compute a duration-weighted average of a CodeCarbon metric."""

    if not chunks:
        return 0.0

    weighted_sum = 0.0
    total_weight = 0.0
    values: list[float] = []

    for chunk in chunks:
        value = _safe_number(getattr(chunk, attribute, None))
        duration = _safe_number(getattr(chunk, "duration", None))
        values.append(value)
        if duration > 0:
            weighted_sum += value * duration
            total_weight += duration

    if total_weight > 0:
        return weighted_sum / total_weight
    return _safe_mean(values)


def _first_nonempty(chunks: list[Any], attribute: str, default: str = "") -> str:
    for chunk in chunks:
        value = getattr(chunk, attribute, None)
        if value not in (None, ""):
            return str(value)
    return default


def _definition_id(name: str, source: str, line: Optional[int], scope: str) -> str:
    """Create a stable, compact identifier for a fixture definition."""

    raw = f"{source}|{line or ''}|{name}|{scope}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _labels_to_string(labels: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in labels.items())


def _cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _git_output(root: Path, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def collect_environment(project_root: Path) -> dict[str, Any]:
    """Describe the machine/software so runs can be compared and reproduced."""

    environment: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": sys.version.split()[0],
        "cpu_model": _cpu_model(),
        "cpu_count_logical": os.cpu_count(),
        "pytest_version": pytest.__version__,
        "codecarbon_version": getattr(codecarbon, "__version__", "unknown"),
    }
    try:
        import psutil  # CodeCarbon depends on it, so it is normally available.

        environment["cpu_count_physical"] = psutil.cpu_count(logical=False)
        environment["ram_total_gb"] = round(
            psutil.virtual_memory().total / (1024 ** 3), 2
        )
    except Exception:
        pass

    commit = _git_output(project_root, "rev-parse", "HEAD")
    if commit:
        environment["project_git_commit"] = commit
        dirty = _git_output(project_root, "status", "--porcelain")
        environment["project_git_dirty"] = bool(dirty) if dirty is not None else None
    return environment


def measurement_to_row(
    measurement: LogicalMeasurement,
    *,
    project_name: str,
    run_id: str,
    tracking_mode: str,
    experiment_labels: str,
    test_outcome: str,
) -> dict[str, Any]:
    """Convert one logical measurement into a raw CSV row."""

    chunks = measurement.codecarbon_chunks
    codecarbon_duration_s = sum(
        _safe_number(getattr(chunk, "duration", None)) for chunk in chunks
    )
    energy_kwh = sum(
        _safe_number(getattr(chunk, "energy_consumed", None)) for chunk in chunks
    )
    cpu_energy_kwh = sum(
        _safe_number(getattr(chunk, "cpu_energy", None)) for chunk in chunks
    )
    gpu_energy_kwh = sum(
        _safe_number(getattr(chunk, "gpu_energy", None)) for chunk in chunks
    )
    ram_energy_kwh = sum(
        _safe_number(getattr(chunk, "ram_energy", None)) for chunk in chunks
    )
    emissions_kg = sum(
        _safe_number(getattr(chunk, "emissions", None)) for chunk in chunks
    )
    water_l = sum(
        _safe_number(getattr(chunk, "water_consumed", None)) for chunk in chunks
    )

    if measurement.errors and not chunks:
        status = "failed"
    elif measurement.errors:
        status = "partial"
    elif not chunks:
        status = "no_codecarbon_data"
    else:
        status = "ok"

    timestamp = (
        datetime.fromtimestamp(measurement.ended_at, timezone.utc)
        if measurement.ended_at
        else datetime.now(timezone.utc)
    )

    m = measurement.metadata
    return {
        "record_type": m.record_type,
        "test_name": m.test_name,
        "fixture_name": m.fixture_name,
        "fixture_definition_id": m.fixture_definition_id,
        "fixture_instance_id": m.fixture_instance_id,
        "fixture_scope": m.fixture_scope,
        "fixture_source": m.fixture_source,
        "fixture_line": m.fixture_line or "",
        "is_project_fixture": m.is_project_fixture,
        "phase": measurement.phase,
        "duration_ms": measurement.duration_ns / 1_000_000.0,
        "energy_consumed_kwh": energy_kwh,
        "codecarbon_duration_ms": codecarbon_duration_s * 1000.0,
        "cpu_energy_kwh": cpu_energy_kwh,
        "gpu_energy_kwh": gpu_energy_kwh,
        "ram_energy_kwh": ram_energy_kwh,
        "emissions_kgco2eq": emissions_kg,
        "emissions_rate_kgco2eq_per_s": (
            emissions_kg / codecarbon_duration_s
            if codecarbon_duration_s > 0
            else 0.0
        ),
        "cpu_power_w": _weighted_average(chunks, "cpu_power"),
        "gpu_power_w": _weighted_average(chunks, "gpu_power"),
        "ram_power_w": _weighted_average(chunks, "ram_power"),
        "cpu_utilization_percent": _weighted_average(
            chunks, "cpu_utilization_percent"
        ),
        "gpu_utilization_percent": _weighted_average(
            chunks, "gpu_utilization_percent"
        ),
        "ram_utilization_percent": _weighted_average(
            chunks, "ram_utilization_percent"
        ),
        "ram_used_gb": _weighted_average(chunks, "ram_used_gb"),
        "water_consumed_l": water_l,
        "country_name": _first_nonempty(chunks, "country_name"),
        "country_iso_code": _first_nonempty(chunks, "country_iso_code"),
        "region": _first_nonempty(chunks, "region"),
        "tracking_mode": tracking_mode,
        "measurement_chunks": len(chunks),
        "measurement_status": status,
        "measurement_error": " | ".join(measurement.errors),
        "project_name": project_name,
        "run_id": run_id,
        "experiment_labels": experiment_labels,
        "test_outcome": test_outcome,
        "timestamp_utc": timestamp.isoformat(),
        "pytest_version": pytest.__version__,
        "codecarbon_version": getattr(codecarbon, "__version__", "unknown"),
        "python_version": sys.version.split()[0],
    }


# --------------------------------------------------------------------------- #
# CodeCarbon task manager
# --------------------------------------------------------------------------- #


class CodeCarbonTaskManager:
    """Serialize task measurements and support nested profiler measurements."""

    def __init__(self, tracker: Any) -> None:
        self.tracker = tracker
        self.stack: list[LogicalMeasurement] = []
        self._task_counter = 0

    def _next_task_name(self, measurement: LogicalMeasurement) -> str:
        self._task_counter += 1
        target = measurement.metadata.fixture_name or "test"
        return f"energy_{self._task_counter}_{measurement.phase}_{target}"

    def _start_segment(self, measurement: LogicalMeasurement) -> None:
        task_name = self._next_task_name(measurement)
        try:
            with _suppress_codecarbon_short_task_warnings():
                self.tracker.start_task(task_name)
            measurement.current_task_name = task_name
        except Exception as exc:
            measurement.errors.append(f"start_task failed: {exc!r}")
            measurement.current_task_name = None

        # Exclude start_task() overhead from the wall-clock duration.
        measurement.current_segment_start_ns = time.perf_counter_ns()

    def _stop_segment(self, measurement: LogicalMeasurement) -> None:
        if measurement.current_segment_start_ns is None:
            return

        segment_end_ns = time.perf_counter_ns()
        measurement.duration_ns += segment_end_ns - measurement.current_segment_start_ns
        measurement.current_segment_start_ns = None

        if measurement.current_task_name is None:
            return

        measurement.current_task_name = None
        try:
            # Current CodeCarbon task API stops the active task without a name.
            with _suppress_codecarbon_short_task_warnings():
                data = self.tracker.stop_task()
            if data is not None:
                measurement.codecarbon_chunks.append(data)
        except Exception as exc:
            measurement.errors.append(f"stop_task failed: {exc!r}")

    def enter(self, measurement: LogicalMeasurement) -> None:
        # CodeCarbon supports one active task. Pause the outer measurement first.
        if self.stack:
            self._stop_segment(self.stack[-1])
        self.stack.append(measurement)
        self._start_segment(measurement)

    def begin(
        self, metadata: MeasurementMetadata, phase: str
    ) -> LogicalMeasurement:
        measurement = LogicalMeasurement(metadata=metadata, phase=phase)
        self.enter(measurement)
        return measurement

    def end(self, measurement: LogicalMeasurement) -> None:
        if not self.stack or self.stack[-1] is not measurement:
            measurement.errors.append("measurement stack became inconsistent")
            return

        self._stop_segment(measurement)
        measurement.ended_at = time.time()
        self.stack.pop()

        if self.stack:
            self._start_segment(self.stack[-1])

    def close_active_measurements(self) -> None:
        while self.stack:
            measurement = self.stack.pop()
            self._stop_segment(measurement)


# --------------------------------------------------------------------------- #
# The pytest plugin
# --------------------------------------------------------------------------- #


class FixtureEnergyPlugin:
    """Standalone pytest plugin implementing RQ1 and RQ2 measurements."""

    def __init__(
        self,
        *,
        tracker: Any,
        raw_output_path: Path,
        rq1_output_path: Path,
        rq2_fixture_output_path: Path,
        rq2_scope_output_path: Path,
        metadata_output_path: Path,
        project_root: Path,
        project_name: str,
        tracking_mode: str,
        project_only: bool,
        measure_power_secs: int,
        labels: dict[str, str],
        include_all_outcomes: bool,
        run_info: dict[str, Any],
    ) -> None:
        self.raw_output_path = raw_output_path
        self.rq1_output_path = rq1_output_path
        self.rq2_fixture_output_path = rq2_fixture_output_path
        self.rq2_scope_output_path = rq2_scope_output_path
        self.metadata_output_path = metadata_output_path
        self.project_root = project_root.resolve()
        self.project_name = project_name
        self.tracking_mode = tracking_mode
        self.project_only = project_only
        self.sampling_interval_ms = measure_power_secs * 1000.0
        self.labels = dict(labels)
        self.labels_str = _labels_to_string(self.labels)
        self.include_all_outcomes = include_all_outcomes
        self.run_info = run_info
        self.run_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self.manager = CodeCarbonTaskManager(tracker)
        self.environment = collect_environment(self.project_root)

        self._venv_prefix: Optional[Path] = None
        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            self._venv_prefix = Path(sys.prefix).resolve()

        # Fixture bookkeeping.
        self._static_cache: dict[int, FixtureStaticInfo] = {}
        self.teardown_states: dict[int, TeardownState] = {}
        self.active_fixture_instances: dict[int, str] = {}
        self.instance_tests: dict[str, set[int]] = {}
        self._instance_counter = 0

        # Test bookkeeping.
        self.test_index: dict[str, int] = {}
        self._phase_outcomes: dict[str, dict[str, str]] = {}
        self.outcomes: dict[str, str] = {}
        self.outcome_counts: Counter[str] = Counter()

        # Measurements wait here until no measurement is active.
        self._pending: list[LogicalMeasurement] = []

        # Streaming aggregates (replace the old "keep every row" list).
        self.phase_agg: dict[str, PhaseAgg] = defaultdict(PhaseAgg)
        self.definitions: dict[str, DefinitionAgg] = {}
        self.status_counts: Counter[str] = Counter()
        self.rows_total = 0
        self.rows_excluded = 0
        self._diag_duration: dict[str, array] = defaultdict(lambda: array("d"))
        self._diag_energy: dict[str, array] = defaultdict(lambda: array("d"))
        self._zero_energy: Counter[str] = Counter()
        self._short: Counter[str] = Counter()

        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self._finalized = False

        # Create all derived files immediately. Even a collection error leaves
        # the user with the expected files and their headers.
        self._create_csv_with_header(self.rq1_output_path, RQ1_FIELDS)
        self._create_csv_with_header(
            self.rq2_fixture_output_path, RQ2_FIXTURE_FIELDS
        )
        self._create_csv_with_header(self.rq2_scope_output_path, RQ2_SCOPE_FIELDS)

        self.raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_file = self.raw_output_path.open("w", newline="", encoding="utf-8")
        self._raw_writer = csv.DictWriter(self._raw_file, fieldnames=RAW_FIELDS)
        self._raw_writer.writeheader()
        self._raw_file.flush()

    # ----- small utilities ------------------------------------------------- #

    @staticmethod
    def _create_csv_with_header(path: Path, fields: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(message)
        print(f"[test-energy] WARNING: {message}", file=sys.stderr)

    def _next_instance_id(self, definition_id: str) -> str:
        self._instance_counter += 1
        return f"{definition_id}:{self._instance_counter}"

    def _test_idx(self, nodeid: str) -> int:
        idx = self.test_index.get(nodeid)
        if idx is None:
            idx = len(self.test_index)
            self.test_index[nodeid] = idx
        return idx

    def _is_project_source(self, source_path: Path) -> bool:
        """True only for code that belongs to the project under test.

        Paths inside site-packages, or inside a virtualenv that happens to live
        in the project directory, are NOT project code (they are plugins or
        third-party libraries).
        """

        try:
            source_path.relative_to(self.project_root)
        except ValueError:
            return False
        if any(part in ("site-packages", "dist-packages") for part in source_path.parts):
            return False
        if self._venv_prefix is not None:
            try:
                source_path.relative_to(self._venv_prefix)
                return False
            except ValueError:
                pass
        return True

    def _static_info(self, fixturedef: Any) -> FixtureStaticInfo:
        """Compute (once) everything that depends only on the fixture definition."""

        key = id(fixturedef)
        cached = self._static_cache.get(key)
        if cached is not None:
            return cached

        fixture_name = str(getattr(fixturedef, "argname", "<unknown_fixture>"))
        fixture_scope = str(getattr(fixturedef, "scope", "unknown"))
        source = ""
        line: Optional[int] = None
        is_project = False
        func = getattr(fixturedef, "func", None)

        if func is not None:
            try:
                func = inspect.unwrap(func)
            except Exception:
                pass

            try:
                source_file = inspect.getsourcefile(func) or inspect.getfile(func)
                source_path = Path(source_file).resolve()
                is_project = self._is_project_source(source_path)
                try:
                    source = str(source_path.relative_to(self.project_root))
                except ValueError:
                    source = str(source_path)
            except (OSError, TypeError):
                source = ""

            try:
                _, line = inspect.getsourcelines(func)
            except (OSError, TypeError):
                line = None

        info = FixtureStaticInfo(
            fixture_name=fixture_name,
            scope=fixture_scope,
            source=source,
            line=line,
            is_project=is_project,
            definition_id=_definition_id(fixture_name, source, line, fixture_scope),
        )
        self._static_cache[key] = info
        return info

    @staticmethod
    def _test_name_from_request(request: Any) -> str:
        pyfuncitem = getattr(request, "_pyfuncitem", None)
        test_name = getattr(pyfuncitem, "nodeid", None)
        if not test_name:
            test_name = getattr(getattr(request, "node", None), "nodeid", "")
        return test_name or "<session>"

    @staticmethod
    def _test_phase_metadata(item: Any) -> MeasurementMetadata:
        return MeasurementMetadata(
            record_type="test_phase",
            test_name=str(getattr(item, "nodeid", "<unknown_test>")),
        )

    # ----- deferred writing: nothing below runs inside a measured window --- #

    def _defer(self, measurement: LogicalMeasurement) -> None:
        self._pending.append(measurement)

    def _flush_pending(self, force: bool = False) -> None:
        if not self._pending:
            return
        if self.manager.stack and not force:
            return

        pending, self._pending = self._pending, []
        for measurement in pending:
            row = measurement_to_row(
                measurement,
                project_name=self.project_name,
                run_id=self.run_id,
                tracking_mode=self.tracking_mode,
                experiment_labels=self.labels_str,
                test_outcome=self.outcomes.get(
                    measurement.metadata.test_name, "unknown"
                ),
            )
            self._raw_writer.writerow(row)
            self._observe(row)
        self._raw_file.flush()

    def _is_excluded_row(self, row: dict[str, Any]) -> bool:
        """Skipped/error tests are left out of the derived summaries.

        Long-lived fixtures (class/module/package/session) are kept even when the
        test that triggered them was skipped: they still serve other tests.
        """

        if row.get("test_outcome") not in SUMMARY_EXCLUDED_OUTCOMES:
            return False
        return (
            row.get("record_type") == "test_phase"
            or row.get("fixture_scope") == "function"
        )

    def _observe(self, row: dict[str, Any]) -> None:
        """Fold one finished row into the streaming aggregates."""

        self.rows_total += 1
        self.status_counts[row["measurement_status"]] += 1

        if not self.include_all_outcomes and self._is_excluded_row(row):
            self.rows_excluded += 1
            return

        record_type = row["record_type"]
        duration = _safe_number(row["duration_ms"])
        energy = _safe_number(row["energy_consumed_kwh"])

        self._diag_duration[record_type].append(duration)
        self._diag_energy[record_type].append(energy)
        if energy <= 0.0:
            self._zero_energy[record_type] += 1
        if duration < self.sampling_interval_ms:
            self._short[record_type] += 1

        self.phase_agg[row["phase"]].add_row(row)

        if record_type == "fixture":
            definition_id = row["fixture_definition_id"]
            definition = self.definitions.get(definition_id)
            if definition is None:
                definition = DefinitionAgg(
                    name=row["fixture_name"],
                    scope=row["fixture_scope"],
                    source=row["fixture_source"],
                    line=row["fixture_line"],
                )
                self.definitions[definition_id] = definition
            instance = definition.instances.get(row["fixture_instance_id"])
            if instance is None:
                instance = InstanceAgg()
                definition.instances[row["fixture_instance_id"]] = instance
            if row["phase"] == "setup":
                instance.setup_energy += energy
                instance.setup_ms += duration
            else:
                instance.teardown_energy += energy
                instance.teardown_ms += duration

    # ----- fixture finalizers ---------------------------------------------- #

    def _wrap_fixture_finalizers(
        self,
        fixturedef: Any,
        metadata: MeasurementMetadata,
        first_new_finalizer_index: int,
    ) -> None:
        """Wrap finalizers added by this fixture's setup execution."""

        finalizers = getattr(fixturedef, "_finalizers", None)
        if not isinstance(finalizers, list):
            self._warn_once(
                "finalizers",
                "fixturedef._finalizers is unavailable in this pytest version; "
                "fixture teardown energy will NOT be measured.",
            )
            return

        own_finalizers = finalizers[first_new_finalizer_index:]
        if not own_finalizers:
            return

        measurement = LogicalMeasurement(metadata=metadata, phase="teardown")
        state = TeardownState(
            measurement=measurement,
            wrapped_finalizer_count=len(own_finalizers),
        )
        self.teardown_states[id(fixturedef)] = state

        finalizers[first_new_finalizer_index:] = [
            self._make_finalizer_wrapper(finalizer, state)
            for finalizer in own_finalizers
        ]

    def _make_finalizer_wrapper(
        self,
        finalizer: Callable[[], Any],
        state: TeardownState,
    ) -> Callable[[], Any]:
        def wrapped_finalizer() -> Any:
            self.manager.enter(state.measurement)
            try:
                return finalizer()
            finally:
                self.manager.end(state.measurement)
                state.completed_finalizer_count += 1

        return wrapped_finalizer

    def _record_fixture_uses_for_item(self, item: Any) -> None:
        """Associate resolved fixture instances with the test that used them."""

        request = getattr(item, "_request", None)
        fixture_defs = getattr(request, "_fixture_defs", None)
        if not isinstance(fixture_defs, dict):
            self._warn_once(
                "fixture_defs",
                "request._fixture_defs is unavailable in this pytest version; "
                "fixture reuse (uses per instance) will NOT be recorded.",
            )
            return

        test_idx = self._test_idx(str(getattr(item, "nodeid", "<unknown_test>")))
        for fixturedef in fixture_defs.values():
            instance_id = self.active_fixture_instances.get(id(fixturedef))
            if instance_id is None:
                continue
            self.instance_tests.setdefault(instance_id, set()).add(test_idx)

    # ----- pytest hooks ----------------------------------------------------- #

    def pytest_configure(self, config: Any) -> None:
        # A single energy signal is not cleanly attributable across xdist workers.
        numprocesses = getattr(config.option, "numprocesses", None)
        if numprocesses not in (None, 0, "0"):
            print(
                "[test-energy] pytest-xdist detected; forcing serial execution (-n 0).",
                file=sys.stderr,
            )
            config.option.numprocesses = 0
            if hasattr(config.option, "dist"):
                config.option.dist = "no"

    def pytest_collection_finish(self, session: Any) -> None:
        """Precompute static fixture info so it never runs inside a measurement."""

        try:
            fixture_manager = session._fixturemanager
            for definitions in fixture_manager._arg2fixturedefs.values():
                for fixturedef in definitions:
                    self._static_info(fixturedef)
        except Exception as exc:
            self._warn_once(
                "precompute",
                f"could not precompute fixture metadata ({exc!r}); falling back "
                "to lazy computation (slightly less precise residual energy).",
            )

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item: Any):
        measurement = self.manager.begin(self._test_phase_metadata(item), "setup")
        try:
            yield
        finally:
            self.manager.end(measurement)
            self._defer(measurement)
            self._record_fixture_uses_for_item(item)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item: Any):
        measurement = self.manager.begin(
            self._test_phase_metadata(item), "test_body"
        )
        try:
            yield
        finally:
            self.manager.end(measurement)
            self._defer(measurement)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item: Any, nextitem: Any):
        measurement = self.manager.begin(
            self._test_phase_metadata(item), "teardown"
        )
        try:
            yield
        finally:
            self.manager.end(measurement)
            self._defer(measurement)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef: Any, request: Any):
        info = self._static_info(fixturedef)

        if self.project_only and not info.is_project:
            yield
            return

        # Every actual fixture initialization gets its own instance ID. A
        # session-scoped fixture therefore gets one, a function-scoped fixture
        # normally gets one per test.
        instance_id = self._next_instance_id(info.definition_id)
        metadata = MeasurementMetadata(
            record_type="fixture",
            test_name=self._test_name_from_request(request),
            fixture_name=info.fixture_name,
            fixture_definition_id=info.definition_id,
            fixture_instance_id=instance_id,
            fixture_scope=info.scope,
            fixture_source=info.source,
            fixture_line=info.line,
            is_project_fixture=info.is_project,
        )
        self.active_fixture_instances[id(fixturedef)] = instance_id

        finalizers = getattr(fixturedef, "_finalizers", None)
        first_new_finalizer_index = (
            len(finalizers) if isinstance(finalizers, list) else 0
        )

        measurement = self.manager.begin(metadata, "setup")
        try:
            yield
        finally:
            self.manager.end(measurement)
            self._defer(measurement)
            self._wrap_fixture_finalizers(
                fixturedef, metadata, first_new_finalizer_index
            )

    def pytest_fixture_post_finalizer(self, fixturedef: Any, request: Any) -> None:
        state = self.teardown_states.pop(id(fixturedef), None)
        if state is not None:
            if state.completed_finalizer_count != state.wrapped_finalizer_count:
                state.measurement.errors.append(
                    "not all wrapped finalizers completed "
                    f"({state.completed_finalizer_count}/"
                    f"{state.wrapped_finalizer_count})"
                )
            self._defer(state.measurement)

        self.active_fixture_instances.pop(id(fixturedef), None)

    @staticmethod
    def _classify_report(report: Any) -> str:
        if hasattr(report, "wasxfail"):
            return "xfailed" if getattr(report, "skipped", False) else "xpassed"
        return str(getattr(report, "outcome", "unknown"))

    @staticmethod
    def _final_outcome(phases: dict[str, str]) -> str:
        setup = phases.get("setup")
        if setup == "failed":
            return "error"
        if setup == "skipped":
            return "skipped"
        call = phases.get("call", "unknown")
        if phases.get("teardown") == "failed":
            return f"{call}+teardown_error"
        return call

    def pytest_runtest_logreport(self, report: Any) -> None:
        when = getattr(report, "when", None)
        if when not in ("setup", "call", "teardown"):
            return
        self._phase_outcomes.setdefault(report.nodeid, {})[when] = (
            self._classify_report(report)
        )

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        """Runs after all three phases of a test: no measurement is active here."""

        outcome = self._final_outcome(self._phase_outcomes.pop(nodeid, {}))
        self.outcomes[nodeid] = outcome
        self.outcome_counts[outcome] += 1
        self._test_idx(nodeid)
        self._flush_pending()

    # ----- summaries -------------------------------------------------------- #

    def _excluded_test_indexes(self) -> set[int]:
        if self.include_all_outcomes:
            return set()
        return {
            idx
            for nodeid, idx in self.test_index.items()
            if self.outcomes.get(nodeid) in SUMMARY_EXCLUDED_OUTCOMES
        }

    def _rq1_row(
        self, label: str, agg: PhaseAgg, total_energy: float
    ) -> dict[str, Any]:
        return {
            "phase": label,
            "duration_ms": agg.duration_ms,
            "energy_consumed_kwh": agg.energy,
            "percentage_of_execution_energy": (
                100.0 * agg.energy / total_energy if total_energy > 0 else 0.0
            ),
            "profiled_fixture_energy_kwh": agg.fixture_energy,
            "residual_energy_kwh": agg.residual_energy,
            "profiled_fixture_percentage_of_phase_energy": (
                100.0 * agg.fixture_energy / agg.energy if agg.energy > 0 else 0.0
            ),
            "cpu_energy_kwh": agg.cpu,
            "gpu_energy_kwh": agg.gpu,
            "ram_energy_kwh": agg.ram,
            "project_name": self.project_name,
            "run_id": self.run_id,
            "experiment_labels": self.labels_str,
        }

    def _write_rq1_summary(self) -> None:
        """Setup / test-body / teardown energy accounting for RQ1.

        Nested fixture tasks pause their outer pytest phase task, so every raw
        row is exclusive and summing them does not double-count.
        """

        phases = ("setup", "test_body", "teardown")
        aggs = {phase: self.phase_agg.get(phase, PhaseAgg()) for phase in phases}
        total = aggs["setup"] + aggs["test_body"] + aggs["teardown"]
        setup_teardown = aggs["setup"] + aggs["teardown"]

        with self.rq1_output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RQ1_FIELDS)
            writer.writeheader()
            for phase in phases:
                writer.writerow(self._rq1_row(phase, aggs[phase], total.energy))
            writer.writerow(self._rq1_row("setup_teardown", setup_teardown, total.energy))
            writer.writerow(self._rq1_row("total", total, total.energy))

    def _definition_stats(self, excluded_idx: set[int]) -> list[DefinitionStats]:
        stats: list[DefinitionStats] = []
        for definition_id in sorted(self.definitions):
            agg = self.definitions[definition_id]
            if not agg.instances:
                continue

            instance_ids = sorted(agg.instances)
            uses_per_instance: list[int] = []
            lifecycle_by_instance: list[float] = []
            tests: set[int] = set()
            setup_energy = teardown_energy = setup_ms = teardown_ms = 0.0

            for instance_id in instance_ids:
                instance = agg.instances[instance_id]
                used = self.instance_tests.get(instance_id, set())
                if excluded_idx:
                    used = used - excluded_idx
                uses_per_instance.append(len(used))
                tests |= used
                lifecycle_by_instance.append(instance.lifecycle_energy)
                setup_energy += instance.setup_energy
                teardown_energy += instance.teardown_energy
                setup_ms += instance.setup_ms
                teardown_ms += instance.teardown_ms

            stats.append(
                DefinitionStats(
                    definition_id=definition_id,
                    agg=agg,
                    instance_ids=instance_ids,
                    uses_per_instance=uses_per_instance,
                    tests=tests,
                    setup_energy=setup_energy,
                    teardown_energy=teardown_energy,
                    setup_ms=setup_ms,
                    teardown_ms=teardown_ms,
                    lifecycle_by_instance=lifecycle_by_instance,
                )
            )
        return stats

    @staticmethod
    def _per_use(value: float, uses: int) -> float:
        return value / uses if uses else 0.0

    def _write_rq2_summaries(self) -> None:
        stats = self._definition_stats(self._excluded_test_indexes())

        fixture_rows: list[dict[str, Any]] = []
        for s in stats:
            lifecycle_energy = s.setup_energy + s.teardown_energy
            total_uses = s.total_uses
            fixture_rows.append(
                {
                    "fixture_definition_id": s.definition_id,
                    "fixture_name": s.agg.name,
                    "fixture_scope": s.agg.scope,
                    "fixture_source": s.agg.source,
                    "fixture_line": s.agg.line,
                    "fixture_instance_count": len(s.instance_ids),
                    "total_fixture_uses": total_uses,
                    "unique_tests_using_fixture": len(s.tests),
                    "mean_uses_per_instance": _safe_mean(s.uses_per_instance),
                    "median_uses_per_instance": _safe_median(s.uses_per_instance),
                    "total_setup_duration_ms": s.setup_ms,
                    "total_teardown_duration_ms": s.teardown_ms,
                    "total_lifecycle_duration_ms": s.setup_ms + s.teardown_ms,
                    "total_setup_energy_kwh": s.setup_energy,
                    "total_teardown_energy_kwh": s.teardown_energy,
                    "total_lifecycle_energy_kwh": lifecycle_energy,
                    "mean_lifecycle_energy_kwh_per_instance": _safe_mean(
                        s.lifecycle_by_instance
                    ),
                    "median_lifecycle_energy_kwh_per_instance": _safe_median(
                        s.lifecycle_by_instance
                    ),
                    "amortized_setup_energy_kwh_per_use": self._per_use(
                        s.setup_energy, total_uses
                    ),
                    "amortized_teardown_energy_kwh_per_use": self._per_use(
                        s.teardown_energy, total_uses
                    ),
                    "amortized_lifecycle_energy_kwh_per_use": self._per_use(
                        lifecycle_energy, total_uses
                    ),
                    "project_name": self.project_name,
                    "run_id": self.run_id,
                    "experiment_labels": self.labels_str,
                }
            )

        with self.rq2_fixture_output_path.open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=RQ2_FIXTURE_FIELDS)
            writer.writeheader()
            writer.writerows(fixture_rows)

        by_scope: dict[str, list[DefinitionStats]] = defaultdict(list)
        for s in stats:
            by_scope[s.agg.scope].append(s)

        scope_rows: list[dict[str, Any]] = []
        for scope in sorted(by_scope):
            group = by_scope[scope]
            uses_per_instance = [u for s in group for u in s.uses_per_instance]
            lifecycle_by_instance = [
                e for s in group for e in s.lifecycle_by_instance
            ]
            tests: set[int] = set().union(*(s.tests for s in group))
            setup_energy = sum(s.setup_energy for s in group)
            teardown_energy = sum(s.teardown_energy for s in group)
            lifecycle_energy = setup_energy + teardown_energy
            total_uses = sum(uses_per_instance)

            scope_rows.append(
                {
                    "fixture_scope": scope,
                    "fixture_definition_count": len(group),
                    "fixture_instance_count": sum(len(s.instance_ids) for s in group),
                    "total_fixture_uses": total_uses,
                    "unique_tests_using_scope_fixtures": len(tests),
                    "mean_uses_per_instance": _safe_mean(uses_per_instance),
                    "median_uses_per_instance": _safe_median(uses_per_instance),
                    "total_setup_energy_kwh": setup_energy,
                    "total_teardown_energy_kwh": teardown_energy,
                    "total_lifecycle_energy_kwh": lifecycle_energy,
                    "mean_lifecycle_energy_kwh_per_instance": _safe_mean(
                        lifecycle_by_instance
                    ),
                    "amortized_setup_energy_kwh_per_use": self._per_use(
                        setup_energy, total_uses
                    ),
                    "amortized_teardown_energy_kwh_per_use": self._per_use(
                        teardown_energy, total_uses
                    ),
                    "amortized_lifecycle_energy_kwh_per_use": self._per_use(
                        lifecycle_energy, total_uses
                    ),
                    "project_name": self.project_name,
                    "run_id": self.run_id,
                    "experiment_labels": self.labels_str,
                }
            )

        with self.rq2_scope_output_path.open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=RQ2_SCOPE_FIELDS)
            writer.writeheader()
            writer.writerows(scope_rows)

    # ----- diagnostics and metadata ---------------------------------------- #

    def _diagnostics(self) -> dict[str, Any]:
        """How much can the energy numbers be trusted? (see Ponto 1 of the review)

        * share_shorter_than_sampling_interval: rows that lasted less than the
          CodeCarbon sampling interval. Their energy is mostly (duration x stale
          power sample), not a real measurement.
        * share_zero_energy: rows where CodeCarbon reported no energy at all.
        * duration-vs-energy correlation: close to 1 means energy carries almost
          no information beyond execution time.
        """

        diagnostics: dict[str, Any] = {
            "sampling_interval_ms": self.sampling_interval_ms
        }
        for record_type in ("fixture", "test_phase"):
            durations = self._diag_duration.get(record_type)
            energies = self._diag_energy.get(record_type)
            n = len(durations) if durations is not None else 0
            entry: dict[str, Any] = {"n": n}
            if n and energies is not None:
                entry["share_zero_energy"] = self._zero_energy[record_type] / n
                entry["share_shorter_than_sampling_interval"] = (
                    self._short[record_type] / n
                )
                entry["pearson_duration_vs_energy"] = _pearson(durations, energies)
                entry["spearman_duration_vs_energy"] = _spearman(durations, energies)
            diagnostics[record_type] = entry
        return diagnostics

    def _write_run_metadata(self, diagnostics: dict[str, Any]) -> None:
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "project_name": self.project_name,
            "experiment_labels": self.labels,
            "started_at_utc": self.started_at.isoformat(),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_info": self.run_info,
            "environment": self.environment,
            "tests": {
                "total": len(self.test_index),
                "outcome_counts": dict(self.outcome_counts),
            },
            "measurements": {
                "rows_total": self.rows_total,
                "rows_excluded_from_summaries": self.rows_excluded,
                "summaries_exclude_outcomes": (
                    [] if self.include_all_outcomes
                    else sorted(SUMMARY_EXCLUDED_OUTCOMES)
                ),
                "status_counts": dict(self.status_counts),
            },
            "diagnostics": diagnostics,
            "warnings": self.warnings,
        }
        with self.metadata_output_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, ensure_ascii=False, default=str)

    def finalize(self) -> None:
        """Close raw output and always materialize the derived files."""

        if self._finalized:
            return
        self._finalized = True

        self.manager.close_active_measurements()
        self._flush_pending(force=True)
        if not self._raw_file.closed:
            self._raw_file.flush()
            self._raw_file.close()

        self._write_rq1_summary()
        self._write_rq2_summaries()
        diagnostics = self._diagnostics()
        self._write_run_metadata(diagnostics)

        err = sys.stderr
        print(f"[test-energy] raw data: {self.raw_output_path.resolve()}", file=err)
        print(f"[test-energy] RQ1 summary: {self.rq1_output_path.resolve()}", file=err)
        print(
            f"[test-energy] RQ2 fixture summary: {self.rq2_fixture_output_path.resolve()}",
            file=err,
        )
        print(
            f"[test-energy] RQ2 scope summary: {self.rq2_scope_output_path.resolve()}",
            file=err,
        )
        print(f"[test-energy] run metadata: {self.metadata_output_path.resolve()}", file=err)
        print(
            f"[test-energy] tests: {dict(self.outcome_counts)}; "
            f"measurement status: {dict(self.status_counts)}",
            file=err,
        )
        fixture_diag = diagnostics.get("fixture", {})
        if fixture_diag.get("n"):
            print(
                "[test-energy] fixture rows: "
                f"{fixture_diag['share_shorter_than_sampling_interval']:.0%} shorter "
                f"than the {self.sampling_interval_ms:.0f} ms sampling interval, "
                f"{fixture_diag['share_zero_energy']:.0%} with zero energy.",
                file=err,
            )


# --------------------------------------------------------------------------- #
# CodeCarbon tracker + CLI
# --------------------------------------------------------------------------- #


def build_tracker(
    *,
    project_name: str,
    tracking_mode: str,
    country_iso_code: Optional[str],
    measure_power_secs: int,
    force_cpu_load: bool,
) -> Any:
    """Create, start, and warm up a CodeCarbon tracker."""

    common_kwargs: dict[str, Any] = {
        "project_name": project_name,
        "tracking_mode": tracking_mode,
        "log_level": "error",
        "measure_power_secs": measure_power_secs,
        "force_mode_cpu_load": force_cpu_load,
    }

    try:
        common_kwargs["output_methods"] = []
        if country_iso_code:
            tracker = OfflineEmissionsTracker(
                country_iso_code=country_iso_code,
                **common_kwargs,
            )
        else:
            tracker = EmissionsTracker(**common_kwargs)
    except (TypeError, ValueError):
        # Compatibility fallback for older CodeCarbon versions.
        common_kwargs.pop("output_methods", None)
        common_kwargs.update({"save_to_file": False, "save_to_api": False})
        if country_iso_code:
            tracker = OfflineEmissionsTracker(
                country_iso_code=country_iso_code,
                **common_kwargs,
            )
        else:
            tracker = EmissionsTracker(**common_kwargs)

    if not hasattr(tracker, "start_task") or not hasattr(tracker, "stop_task"):
        raise SystemExit(
            "This profiler requires CodeCarbon start_task()/stop_task() support."
        )

    try:
        with _suppress_codecarbon_short_task_warnings():
            tracker.start()

            # Run two real warm-up tasks before pytest starts. This gives
            # CodeCarbon and the underlying power-measurement backend enough
            # time to initialize and collect samples. Warm-up measurements are
            # deliberately discarded and therefore never enter the CSV files.
            warmup_seconds = max(2.0, measure_power_secs * 2.0)
            for warmup_index in range(1, 3):
                tracker.start_task(
                    f"__test_energy_profiler_warmup_{warmup_index}__"
                )
                time.sleep(warmup_seconds)
                tracker.stop_task()
    except Exception as exc:
        try:
            with _suppress_codecarbon_short_task_warnings():
                tracker.stop()
        except Exception:
            pass
        raise SystemExit(
            "CodeCarbon could not initialize. If the machine has no Internet "
            "access, try --energy-country-iso-code with a 3-letter ISO code. "
            f"Original error: {exc!r}"
        ) from exc

    return tracker


def _derived_output_paths(raw_output_path: Path) -> tuple[Path, Path, Path, Path]:
    stem = raw_output_path.stem
    suffix = raw_output_path.suffix or ".csv"
    parent = raw_output_path.parent
    return (
        parent / f"{stem}_rq1_summary{suffix}",
        parent / f"{stem}_rq2_fixtures{suffix}",
        parent / f"{stem}_rq2_scopes{suffix}",
        parent / f"{stem}_run_metadata.json",
    )


def _parse_labels(raw_labels: list[str], parser: argparse.ArgumentParser) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in raw_labels:
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or ";" in item or "=" in key:
            parser.error(
                f"invalid --energy-label {item!r}: use KEY=VALUE (no ';' allowed)."
            )
        labels[key] = value
    return labels


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Run pytest while measuring setup, test-body, teardown, and fixture "
            "energy with CodeCarbon. Unknown options are forwarded to pytest."
        )
    )
    parser.add_argument(
        "--energy-output-dir",
        default="fixture_energy_results",
        help=(
            "Directory where all profiler files are written "
            "(default: fixture_energy_results)."
        ),
    )
    parser.add_argument(
        "--energy-output",
        default="fixture_energy.csv",
        help=(
            "Filename for the raw CSV inside --energy-output-dir "
            "(default: fixture_energy.csv)."
        ),
    )
    parser.add_argument(
        "--energy-tracking-mode",
        choices=("process", "machine"),
        default="process",
        help="CodeCarbon tracking mode (default: process).",
    )
    parser.add_argument(
        "--energy-country-iso-code",
        default=None,
        metavar="ISO3",
        help="Use CodeCarbon offline mode with a 3-letter country code (e.g. BRA).",
    )
    parser.add_argument(
        "--energy-project-only",
        action="store_true",
        help="Profile fixture rows only for fixtures defined inside the project.",
    )
    parser.add_argument(
        "--energy-project-name",
        default=Path.cwd().name or "pytest-energy",
        help="CodeCarbon/project label used in the CSV outputs.",
    )
    parser.add_argument(
        "--energy-measure-power-secs",
        type=int,
        default=1,
        metavar="SECONDS",
        help="CodeCarbon power sampling interval (default: 1 second).",
    )
    parser.add_argument(
        "--energy-label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Experiment label written to every row/summary (repeatable). "
            "Example: --energy-label rep=1 --energy-label variant=baseline"
        ),
    )
    parser.add_argument(
        "--energy-cpu-load-mode",
        choices=("on", "off"),
        default="on",
        help=(
            "Value passed to CodeCarbon's force_mode_cpu_load (default: on, as in "
            "the original script). Recorded in the run metadata."
        ),
    )
    parser.add_argument(
        "--energy-summary-include-all",
        action="store_true",
        help=(
            "Include skipped/error tests in the derived summaries "
            "(default: they are excluded; the raw CSV always keeps them)."
        ),
    )

    args, pytest_args = parser.parse_known_args(argv)
    if args.energy_measure_power_secs < 1:
        parser.error("--energy-measure-power-secs must be >= 1")
    args.energy_labels = _parse_labels(args.energy_label, parser)
    return args, pytest_args


def main(argv: Optional[list[str]] = None) -> int:
    args, pytest_args = parse_args(list(sys.argv[1:] if argv is None else argv))

    project_root = Path.cwd()

    # Keep every file generated by the profiler in one dedicated directory.
    # Relative directories are resolved from the project root; absolute paths
    # are also supported for batch experiments that store results elsewhere.
    output_dir = Path(args.energy_output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # --energy-output is intentionally treated as a filename. This guarantees
    # that the raw CSV and every derived file live in the same directory.
    raw_output_path = output_dir / Path(args.energy_output).name
    rq1_path, rq2_fixture_path, rq2_scope_path, metadata_path = _derived_output_paths(
        raw_output_path
    )

    tracker = build_tracker(
        project_name=args.energy_project_name,
        tracking_mode=args.energy_tracking_mode,
        country_iso_code=args.energy_country_iso_code,
        measure_power_secs=args.energy_measure_power_secs,
        force_cpu_load=(args.energy_cpu_load_mode == "on"),
    )

    # Force xdist to run serially even if addopts enables it in project config.
    if importlib.util.find_spec("xdist") is not None:
        pytest_args.extend(["-n", "0"])

    run_info = {
        "energy_options": {
            key: value
            for key, value in vars(args).items()
            if key.startswith("energy_") and key != "energy_label"
        },
        "pytest_args": list(pytest_args),
    }

    plugin = FixtureEnergyPlugin(
        tracker=tracker,
        raw_output_path=raw_output_path,
        rq1_output_path=rq1_path,
        rq2_fixture_output_path=rq2_fixture_path,
        rq2_scope_output_path=rq2_scope_path,
        metadata_output_path=metadata_path,
        project_root=project_root,
        project_name=args.energy_project_name,
        tracking_mode=args.energy_tracking_mode,
        project_only=args.energy_project_only,
        measure_power_secs=args.energy_measure_power_secs,
        labels=args.energy_labels,
        include_all_outcomes=args.energy_summary_include_all,
        run_info=run_info,
    )

    exit_code = 1
    try:
        exit_code = int(pytest.main(pytest_args, plugins=[plugin]))
    finally:
        try:
            plugin.finalize()
        finally:
            try:
                with _suppress_codecarbon_short_task_warnings():
                    tracker.stop()
            except Exception:
                pass

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())