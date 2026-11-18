"""Tiny pytest plugin used only by the independent Analyzer V2 collector."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any


def _definition_id(name: str, source: str, line: int | None, scope: str) -> str:
    raw = f"{source}|{line or ''}|{name}|{scope}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class AnalyzerV2Probe:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.root = Path(os.environ["ANALYZER_V2_PROJECT_ROOT"]).resolve()
        self.tests: set[str] = set()
        self.fixtures: set[str] = set()
        self.errors: list[str] = []
        self.fixture_registry_available = False

    def _is_project_source(self, filename: str) -> tuple[bool, str]:
        try:
            path = Path(filename).resolve()
            relative = path.relative_to(self.root)
        except (OSError, ValueError):
            return False, ""
        parts = relative.parts
        if any(part in {"site-packages", "dist-packages"} for part in parts):
            return False, ""
        if any(part == ".tox" or part == "venv" or part.startswith(".venv") for part in parts):
            return False, ""
        return True, relative.as_posix()

    def pytest_collection_finish(self, session: Any) -> None:
        self.tests.update(str(item.nodeid) for item in session.items)
        manager = getattr(session, "_fixturemanager", None)
        if manager is None:
            manager = self.config.pluginmanager.get_plugin("funcmanage")
        definitions = getattr(manager, "_arg2fixturedefs", None)
        if not isinstance(definitions, dict):
            return
        self.fixture_registry_available = True
        for fixturedefs in definitions.values():
            for fixturedef in fixturedefs or ():
                func = getattr(fixturedef, "func", None)
                code = getattr(func, "__code__", None)
                if code is None:
                    continue
                try:
                    unwrapped = inspect.unwrap(func)
                except Exception:
                    unwrapped = func
                try:
                    source_file = inspect.getsourcefile(unwrapped) or inspect.getfile(unwrapped)
                    _, line = inspect.getsourcelines(unwrapped)
                except (OSError, TypeError):
                    source_file, line = code.co_filename, code.co_firstlineno
                is_project, source = self._is_project_source(source_file)
                if not is_project:
                    continue
                name = str(getattr(fixturedef, "argname", getattr(func, "__name__", "unknown")))
                line = int(line)
                scope = str(getattr(fixturedef, "scope", "unknown"))
                self.fixtures.add(_definition_id(name, source, line, scope))

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.errors.append(str(report.nodeid))

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        output = os.environ.get("ANALYZER_V2_OUTPUT")
        if not output:
            return
        test_ids = sorted(self.tests)
        fixture_ids = sorted(self.fixtures)
        payload = {
            "schema": 1,
            "exitstatus": int(exitstatus),
            "python_version": sys.version.split()[0],
            "test_ids": test_ids,
            "test_count": len(test_ids),
            "test_ids_sha256": hashlib.sha256("\n".join(test_ids).encode()).hexdigest(),
            "project_fixture_ids": fixture_ids,
            "project_fixture_count": len(fixture_ids),
            "fixture_ids_sha256": hashlib.sha256("\n".join(fixture_ids).encode()).hexdigest(),
            "fixture_registry_available": self.fixture_registry_available,
            "collection_error_count": len(self.errors),
            "collection_error_nodeids": self.errors,
        }
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def pytest_configure(config: Any) -> None:
    config.pluginmanager.register(AnalyzerV2Probe(config), "analyzer_v2_probe_instance")
