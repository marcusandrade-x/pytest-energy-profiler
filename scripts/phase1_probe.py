"""Pytest plugin used by the Phase 1 fixture execution probe.

Load with ``-p phase1_probe`` and set ``PHASE1_OUTPUT``.  It records collection
IDs, observed fixture dependencies, fixture setup attempts and collection
errors. It intentionally does not import CodeCarbon or change fixture scope.
"""

from __future__ import annotations

import json
import os
import traceback
from collections import defaultdict
from pathlib import PosixPath
from typing import Any

import pytest

from phase1_catalog import _definition_id, error_signature, normalize_error


class Phase1Probe:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.root = PosixPath(
            os.environ.get("PHASE1_PROJECT_ROOT", str(config.rootpath))
        ).resolve()
        self.project_id = os.environ.get("PHASE1_PROJECT_ID", "")
        self.run_id = os.environ.get("PHASE1_RUN_ID", "")
        self.current_nodeid = ""
        self.item_fixture_map: dict[str, set[str]] = defaultdict(set)
        self.fixture_defs: dict[str, dict[str, Any]] = {}
        self.static_fixture_keys: dict[tuple[str, str], dict[str, Any]] = {}
        static_inventory = os.environ.get("PHASE1_STATIC_INVENTORY")
        if static_inventory:
            try:
                inventory = json.loads(PosixPath(static_inventory).read_text(encoding="utf-8"))
                for fixture in inventory.get("fixtures", []):
                    key = (str(fixture.get("file", "")), str(fixture.get("function_name", "")))
                    self.static_fixture_keys[key] = fixture
            except (OSError, json.JSONDecodeError, AttributeError):
                self.static_fixture_keys = {}
        self.fixture_events: list[dict[str, Any]] = []
        self.active_fixture_instances: dict[int, str] = {}
        self.finalized_instances: set[str] = set()
        self.collection_errors: list[dict[str, Any]] = []
        self.test_ids: list[str] = []
        self.test_phase_outcomes: dict[str, dict[str, str]] = defaultdict(dict)
        try:
            self.circuit_breaker_k = max(0, int(os.environ.get("PHASE1_CIRCUIT_BREAKER_K", "0")))
        except ValueError:
            self.circuit_breaker_k = 0
        self.failure_streaks: dict[str, dict[str, Any]] = {}
        self.tripped_fixtures: dict[str, dict[str, Any]] = {}
        self.aborted_tests: list[dict[str, str]] = []
        prior_map_path = os.environ.get("PHASE1_PRIOR_DEPENDENCY_MAP")
        self.prior_dependency_map: dict[str, list[str]] = {}
        if prior_map_path:
            try:
                raw_map = json.loads(PosixPath(prior_map_path).read_text(encoding="utf-8"))
                self.prior_dependency_map = {
                    str(nodeid): [str(fixture_id) for fixture_id in fixture_ids]
                    for nodeid, fixture_ids in raw_map.items()
                }
            except (OSError, json.JSONDecodeError, AttributeError):
                self.prior_dependency_map = {}
        self.plugins = sorted(str(name) for name, _ in config.pluginmanager.list_name_plugin())
        self.sequence = 0

    def _fixture_info(self, fixturedef: Any) -> dict[str, Any]:
        func = fixturedef.func
        code = getattr(func, "__code__", None)
        source_path = PosixPath(code.co_filename).resolve() if code else None
        try:
            relpath = source_path.relative_to(self.root).as_posix() if source_path else ""
        except ValueError:
            relpath = ""
        line = int(code.co_firstlineno) if code else 0
        name = str(getattr(fixturedef, "argname", getattr(func, "__name__", "unknown")))
        is_project = bool(relpath)
        static = self.static_fixture_keys.get((relpath, str(getattr(func, "__name__", name))))
        if static:
            line = int(static.get("line", line))
            name = str(static.get("name", name))
            fixture_id = str(static.get("fixture_id") or _definition_id(self.project_id, relpath, line, name))
        else:
            fixture_id = (
                _definition_id(self.project_id, relpath, line, name)
                if is_project else f"external::{name}::{line}"
            )
        info = {
            "fixture_id": fixture_id,
            "name": name,
            "function_name": getattr(func, "__name__", name),
            "file": relpath,
            "line": line,
            "join_key": f"{relpath}:{line}:{name}",
            "scope": str(getattr(fixturedef, "scope", "unknown")),
            "is_project": is_project,
            "dependencies": [
                str(arg) for arg in getattr(fixturedef, "argnames", ())
                if str(arg) != "request"
            ],
        }
        self.fixture_defs[fixture_id] = info
        return info

    def _definition_for_name(self, item: Any, name: str) -> list[Any]:
        fixtureinfo = getattr(item, "_fixtureinfo", None)
        mapping = getattr(fixtureinfo, "name2fixturedefs", {}) or {}
        return list(mapping.get(name, ()) or ())

    def pytest_collection_finish(self, session: Any) -> None:
        for item in session.items:
            nodeid = str(item.nodeid)
            self.test_ids.append(nodeid)
            fixtureinfo = getattr(item, "_fixtureinfo", None)
            names = list(getattr(fixtureinfo, "names_closure", ()) or ())
            for name in names:
                definitions = self._definition_for_name(item, name)
                if definitions:
                    info = self._fixture_info(definitions[-1])
                    if info["is_project"]:
                        self.item_fixture_map[nodeid].add(info["fixture_id"])
        for nodeid, fixture_ids in self.prior_dependency_map.items():
            self.item_fixture_map[nodeid].update(fixture_ids)

    def pytest_collectreport(self, report: Any) -> None:
        if not report.failed:
            return
        longrepr = str(getattr(report, "longrepr", ""))
        self.collection_errors.append({
            "nodeid": str(getattr(report, "nodeid", "")),
            "location": str(getattr(report, "location", "")),
            "message": longrepr[-6000:],
        })

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: Any) -> None:
        self.current_nodeid = str(item.nodeid)
        if not self.circuit_breaker_k:
            return
        dependencies = self.item_fixture_map.get(self.current_nodeid, set())
        for fixture_id in sorted(dependencies):
            trip = self.tripped_fixtures.get(fixture_id)
            if trip:
                self.aborted_tests.append({
                    "nodeid": self.current_nodeid,
                    "fixture_id": fixture_id,
                    "signature": str(trip["signature"]),
                })
                pytest.skip(f"phase1-circuit-breaker:{fixture_id}")

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.test_phase_outcomes[str(report.nodeid)][str(report.when)] = str(report.outcome)

    def pytest_fixture_post_finalizer(self, fixturedef: Any, request: Any) -> None:
        info = self._fixture_info(fixturedef)
        instance_id = self.active_fixture_instances.get(id(fixturedef))
        if not instance_id or instance_id in self.finalized_instances:
            return
        self.finalized_instances.add(instance_id)
        self.sequence += 1
        self.fixture_events.append({
            **info,
            "event_id": f"{self.run_id}:{self.sequence}",
            "instance_id": instance_id,
            "run_id": self.run_id,
            "nodeid": self.current_nodeid,
            "phase": "teardown",
            "status": "ok",
            "exception_class": "",
            "message": "",
            "signature": "",
            "innermost_frame": "",
            "cause_root": False,
            "tentative": True,
        })

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef: Any, request: Any):
        info = self._fixture_info(fixturedef)
        self.sequence += 1
        instance_id = f"{self.run_id}:fixture:{self.sequence}"
        self.active_fixture_instances[id(fixturedef)] = instance_id
        event = {
            **info,
            "event_id": f"{self.run_id}:{self.sequence}",
            "instance_id": instance_id,
            "run_id": self.run_id,
            "nodeid": self.current_nodeid,
            "phase": "setup",
            "status": "ok",
            "exception_class": "",
            "message": "",
            "signature": "",
            "innermost_frame": "",
            "cause_root": False,
        }
        if self.current_nodeid and info["is_project"]:
            self.item_fixture_map[self.current_nodeid].add(info["fixture_id"])
        outcome = yield
        if outcome.excinfo is not None:
            exc_type, exc, tb = outcome.excinfo
            event["status"] = "failed"
            event["exception_class"] = getattr(exc_type, "__name__", str(exc_type))
            event["message"] = normalize_error(str(exc))
            frames = traceback.extract_tb(tb)
            external = [
                frame for frame in frames
                if "/_pytest/" not in frame.filename.replace("\\", "/")
                and "/pytest/" not in frame.filename.replace("\\", "/")
            ]
            frame = external[-1] if external else (frames[-1] if frames else None)
            if frame:
                event["innermost_frame"] = f"{frame.filename}:{frame.lineno}:{frame.name}"
            event["signature"] = error_signature(
                event["exception_class"], event["message"], event["innermost_frame"]
            )
            event["cause_root"] = True
        elif self.circuit_breaker_k:
            self.failure_streaks.pop(info["fixture_id"], None)
        self.fixture_events.append(event)

    def pytest_runtest_makereport(self, item: Any, call: Any):
        if call.when == "setup" and self.circuit_breaker_k:
            nodeid = str(item.nodeid)
            failed_events = [
                event for event in self.fixture_events
                if event.get("nodeid") == nodeid
                and event.get("phase") == "setup"
                and event.get("status") == "failed"
            ]
            for event in failed_events:
                fixture_id = str(event.get("fixture_id", ""))
                signature = str(event.get("signature", ""))
                if not fixture_id or not signature:
                    continue
                previous = self.failure_streaks.get(fixture_id, {})
                count = int(previous.get("count", 0)) + 1 if previous.get("signature") == signature else 1
                self.failure_streaks[fixture_id] = {"signature": signature, "count": count}
                if count >= self.circuit_breaker_k:
                    self.tripped_fixtures[fixture_id] = {
                        "signature": signature,
                        "failure_count": count,
                        "tripped_on_nodeid": nodeid,
                    }
        if call.excinfo is None or call.when != "teardown":
            return
        exc = call.excinfo.value
        frames = traceback.extract_tb(call.excinfo.tb)
        project_frames = []
        for frame in frames:
            path = PosixPath(frame.filename)
            try:
                relpath = path.resolve().relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            project_frames.append((relpath, frame))
        if not project_frames:
            return
        relpath, frame = project_frames[-1]
        matching = [
            info for info in self.fixture_defs.values()
            if info["file"] == relpath and info["function_name"] == frame.name
        ]
        if not matching:
            return
        info = max(matching, key=lambda candidate: candidate["line"])
        message_raw = str(exc)
        message = normalize_error(message_raw)
        exc_name = type(exc).__name__
        frame_text = f"{relpath}:{frame.lineno}:{frame.name}"
        for event in reversed(self.fixture_events):
            if (event.get("phase") == "teardown"
                    and event.get("fixture_id") == info["fixture_id"]
                    and event.get("nodeid") == str(item.nodeid)):
                event.update(status="failed", exception_class=exc_name,
                             message=message,
                             signature=error_signature(exc_name, message_raw, frame_text),
                             innermost_frame=frame_text, cause_root=True)
                break
        else:
            self.sequence += 1
            self.fixture_events.append({
                **info,
                "event_id": f"{self.run_id}:{self.sequence}",
                "run_id": self.run_id,
                "nodeid": str(item.nodeid),
                "phase": "teardown",
                "status": "failed",
                "exception_class": exc_name,
                "message": message,
                "signature": error_signature(exc_name, message_raw, frame_text),
                "innermost_frame": frame_text,
                "cause_root": True,
                "tentative": False,
            })

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        output = os.environ.get("PHASE1_OUTPUT")
        if not output:
            return
        path = PosixPath(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": "phase1-probe-v0",
            "project_id": self.project_id,
            "run_id": self.run_id,
            "root": str(self.root),
            "exitstatus": int(exitstatus),
            "effective_uid": os.geteuid(),
            "test_ids": list(dict.fromkeys(self.test_ids)),
            "test_to_fixtures": {
                nodeid: sorted(fixture_ids)
                for nodeid, fixture_ids in sorted(self.item_fixture_map.items())
            },
            "test_phase_outcomes": self.test_phase_outcomes,
            "item_fixture_map": {
                nodeid: sorted(fixture_ids)
                for nodeid, fixture_ids in sorted(self.item_fixture_map.items())
            },
            "fixture_defs": self.fixture_defs,
            "fixture_events": self.fixture_events,
            "collection_errors": self.collection_errors,
            "plugins": self.plugins,
            "circuit_breaker": {
                "k": self.circuit_breaker_k,
                "tripped_fixtures": self.tripped_fixtures,
                "aborted_tests": self.aborted_tests,
            },
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def pytest_configure(config: Any) -> None:
    config.pluginmanager.register(Phase1Probe(config), "phase1-probe-instance")
