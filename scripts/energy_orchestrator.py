#!/usr/bin/env python3
"""Orquestrador de medição de energia de suítes pytest (clone -> venv -> baseline -> profiler).

Lê o CSV do minerador (`mine_projects.csv`), pega os candidatos elegíveis
(`motivo_exclusao` vazio) e, para cada um, automatiza o que antes era manual:

    clone  ->  venv + instalação  ->  baseline (pytest puro)  ->  N medições com o profiler

Princípios de projeto (pensados para centenas de projetos)
----------------------------------------------------------
* PREPARAR em paralelo, MEDIR em série. Duas suítes rodando juntas contaminam a
  medição de energia uma da outra; já clonar/instalar/baseline podem rodar em
  paralelo. Nunca há preparação rodando durante uma medição.
* Os artefatos por execução são a fonte de verdade. Os CSVs consolidados são
  derivados e sempre reconstruíveis (`consolidar` é idempotente).
* Tudo é retomável: cada projeto tem um `status.json` (escrita atômica).
  Reexecutar o comando continua de onde parou; `--retentar-falhas` refaz as exclusões.
* Exclusões são classificadas por categoria (clone_failed, install_failed,
  baseline_high_failure_rate, ...) para triagem rápida em `projects_status.csv`.
* Disco sob controle: trabalho em LOTES (`--lote`) e limpeza de repo+venv ao fim de
  cada lote. Logs, status e resultados permanecem. Com `uv` instalado a instalação
  fica muito mais rápida e barata em disco (cache com hardlinks).
* Segurança: testes e instaladores de terceiros rodam com ambiente MÍNIMO (sem
  GITHUB_TOKEN nem outros segredos) e com HOME/TMPDIR isolados por projeto.
  Ainda assim, é código arbitrário: use uma máquina/VM dedicada e nunca como root.

Estrutura do workspace
----------------------
    <workspace>/
        manifest.csv                  seleção congelada de projetos
        tools/fixture_energy_profiler.py
        cache/                        cache de pip/uv compartilhado
        projects/<owner__repo>/
            status.json  pip_freeze.txt  logs/  baseline/  runs/rep<k>/
            repo/ venv/               (removidos pela limpeza)
        resultados/
            projects_status.csv       1 linha por projeto (triagem)
            runs.csv                  1 linha por (projeto, repetição)
            rq1_all.csv  rq2_fixtures_all.csv  rq2_scopes_all.csv
            (o CSV bruto de cada execução fica em runs/rep<k>/*.csv.gz)

Comandos
--------
    iniciar     cria o manifesto (filtros/amostra) e copia o profiler
    preparar    clone + venv + instalação + baseline (paralelo, --jobs)
    medir       repetições com o profiler (serial)
    rodar       iniciar + [preparar -> medir -> consolidar -> limpar] por lote
    consolidar  reconstrói os CSVs consolidados
    status      resumo do andamento e das exclusões

Exemplo
-------
    python energy_orchestrator.py rodar --resultados mine_projects.csv \\
        --limite 40 --lote 10 --jobs 3 --reps 3 --pais BRA
"""

from __future__ import annotations

import argparse
import configparser
import csv
import gzip
import hashlib
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: detecção de extras via pyproject fica desligada
    tomllib = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orquestrador")

# Variáveis herdadas pelos processos de terceiros. GITHUB_TOKEN e afins ficam FORA.
ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME", "SHELL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL",
)

TEST_EXTRA_NAMES = ("test", "tests", "testing", "dev", "develop", "development")
TEST_REQ_FILES = (
    "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt",
    "test-requirements.txt", "test_requirements.txt", "dev-requirements.txt",
    "requirements-dev.txt", "requirements_dev.txt",
    "requirements/test.txt", "requirements/tests.txt", "requirements/testing.txt",
    "requirements/dev.txt", "requirements/ci.txt",
)
BASE_REQ_FILES = ("requirements.txt", "requirements/base.txt", "requirements/main.txt")

MANIFEST_FIELDS = [
    "project_id", "url", "project_name", "num_tests", "num_fixtures", "loc_tests",
    "loc_producao", "loc_projeto_total", "arquivos_tests", "arquivos_producao",
]
OK_REP_STATUSES = ("ok", "ok_with_warnings")
MAX_REP_ATTEMPTS = 2

PROBE_CODE = (
    "import sys, json, pytest, codecarbon; "
    "print(json.dumps({'python': sys.version.split()[0], 'pytest': pytest.__version__, "
    "'codecarbon': getattr(codecarbon, '__version__', '?')}))"
)


# --------------------------------------------------------------------------- #
# Utilidades gerais
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_csv_atomic(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def project_id_from_url(url: str) -> str:
    parts = url.rstrip("/").removesuffix(".git").split("/")
    owner, name = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1])
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{owner}__{name}")


def tail_log(path: Path, lines: int = 12, limit: int = 700) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])[-limit:]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class CmdResult:
    returncode: int
    seconds: float
    timed_out: bool


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:  # derruba também filhos que tenham sobrado no grupo
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_cmd(
    cmd: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: float
) -> CmdResult:
    """Roda um comando em novo grupo de processos, com timeout que mata o grupo inteiro."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log_path.open("ab") as handle:
        handle.write(f"\n$ {shlex.join(cmd)}\n".encode())
        handle.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            handle.write(f"[orquestrador] falha ao iniciar: {exc}\n".encode())
            return CmdResult(127, 0.0, False)
        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            returncode, timed_out = -9, True
            handle.write(f"\n[orquestrador] TIMEOUT apos {timeout:.0f}s; grupo encerrado\n".encode())
        except BaseException:
            _kill_group(proc)
            raise
    return CmdResult(returncode, time.monotonic() - start, timed_out)


# --------------------------------------------------------------------------- #
# Configuração, ambiente e caminhos
# --------------------------------------------------------------------------- #


class Proj:
    """Caminhos e estado persistente de um projeto."""

    def __init__(self, workspace: Path, row: dict[str, str]) -> None:
        self.id = row["project_id"]
        self.row = row
        self.dir = workspace / "projects" / self.id
        self.repo = self.dir / "repo"
        self.venv = self.dir / "venv"
        self.logs = self.dir / "logs"
        self.runs = self.dir / "runs"
        self.status_path = self.dir / "status.json"

    @property
    def py(self) -> Path:
        return self.venv / "bin" / "python"

    def load(self) -> dict[str, Any]:
        if self.status_path.exists():
            try:
                return json.loads(self.status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "project_id": self.id, "url": self.row["url"], "final": "pending",
            "stages": {}, "install": None, "baseline": None, "reps": {},
            "failure": None, "commit": None,
        }

    def save(self, st: dict[str, Any]) -> None:
        write_json_atomic(self.status_path, st)


def build_env(
    cfg: argparse.Namespace, p: Optional[Proj] = None, *, venv: bool = False,
    pythonpath_repo: bool = False, extra: Optional[dict[str, str]] = None, git: bool = False,
) -> dict[str, str]:
    """Ambiente mínimo para processos de terceiros (sem segredos, HOME isolado)."""

    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    cache = cfg.workspace / "cache"
    env.update({
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(cache / "pip"),
        "UV_CACHE_DIR": str(cache / "uv"),
        "PYTHONUNBUFFERED": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if git:
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    if p is not None:
        for name, key in (("home", "HOME"), ("tmp", "TMPDIR")):
            (p.dir / name).mkdir(parents=True, exist_ok=True)
            env[key] = str(p.dir / name)
        if venv:
            env["VIRTUAL_ENV"] = str(p.venv)
            env["PATH"] = f"{p.venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        if pythonpath_repo:
            env["PYTHONPATH"] = str(p.repo)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


class Installer:
    """Abstrai pip puro versus uv (mais rápido e mais econômico em disco)."""

    def __init__(self, kind: str) -> None:
        uv_path = shutil.which("uv")
        if kind == "uv" and not uv_path:
            raise SystemExit("--instalador uv pedido, mas 'uv' não está no PATH.")
        self.uv = uv_path if kind in ("auto", "uv") else None
        self.kind = "uv" if self.uv else "pip"

    def create_venv_cmd(self, p: Proj, python: str) -> list[str]:
        if self.uv:
            return [self.uv, "venv", "--seed", "--python", python, str(p.venv)]
        return [python, "-m", "venv", str(p.venv)]

    def install_cmd(self, p: Proj, args: list[str]) -> list[str]:
        if self.uv:
            return [self.uv, "pip", "install", "--python", str(p.py), *args]
        return [str(p.py), "-m", "pip", "install", *args]


# --------------------------------------------------------------------------- #
# Estado e exclusões
# --------------------------------------------------------------------------- #


def set_stage(st: dict[str, Any], name: str, status: str, **info: Any) -> None:
    st["stages"][name] = {"status": status, "at": now_iso(), **info}


def exclude(p: Proj, st: dict[str, Any], stage: str, category: str, message: str = "") -> None:
    st["final"] = "excluded"
    st["failure"] = {"stage": stage, "category": category, "message": message[:800], "at": now_iso()}
    set_stage(st, stage, "failed", category=category)
    p.save(st)
    log.warning("[%s] excluído em %s: %s", p.id, stage, category)


def stage_done(p: Proj, st: dict[str, Any], name: str) -> bool:
    if st["stages"].get(name, {}).get("status") != "ok":
        return False
    if name == "clone":
        return p.repo.exists()
    if name == "install":
        return p.py.exists()
    return True


def reset_failed(st: dict[str, Any]) -> dict[str, Any]:
    st["final"] = "pending"
    st["failure"] = None
    st["stages"] = {k: v for k, v in st["stages"].items() if v.get("status") == "ok"}
    return st


# --------------------------------------------------------------------------- #
# Manifesto (seleção congelada)
# --------------------------------------------------------------------------- #


def create_manifest(cfg: argparse.Namespace) -> list[dict[str, str]]:
    if not cfg.resultados.exists():
        raise SystemExit(f"CSV do minerador não encontrado: {cfg.resultados}")

    with cfg.resultados.open(encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if not (r.get("motivo_exclusao") or "").strip()]

    def num(row: dict[str, str], key: str) -> float:
        return safe_float(row.get(key), 0.0)

    selected = []
    for row in rows:
        row["project_id"] = project_id_from_url(row["url"])
        if cfg.min_tests is not None and num(row, "num_tests") < cfg.min_tests:
            continue
        if cfg.max_tests is not None and num(row, "num_tests") > cfg.max_tests:
            continue
        if cfg.min_loc is not None and num(row, "loc_projeto_total") < cfg.min_loc:
            continue
        if cfg.max_loc is not None and num(row, "loc_projeto_total") > cfg.max_loc:
            continue
        if cfg.projeto and not any(n in (row["project_id"], row.get("project_name")) for n in cfg.projeto):
            continue
        selected.append(row)

    seen: set[str] = set()
    unique = []
    for row in selected:
        if row["project_id"] not in seen:
            seen.add(row["project_id"])
            unique.append(row)

    if cfg.limite and len(unique) > cfg.limite:
        unique = random.Random(cfg.seed).sample(unique, cfg.limite)

    write_csv_atomic(cfg.workspace / "manifest.csv", MANIFEST_FIELDS, unique)
    log.info("Manifesto criado: %d projetos elegíveis selecionados (de %d elegíveis).", len(unique), len(rows))
    return unique


def load_projects(cfg: argparse.Namespace) -> list[Proj]:
    manifest = cfg.workspace / "manifest.csv"
    if cfg.refazer_manifesto or not manifest.exists():
        create_manifest(cfg)
    with manifest.open(encoding="utf-8") as handle:
        return [Proj(cfg.workspace, row) for row in csv.DictReader(handle)]


def load_overrides(cfg: argparse.Namespace) -> dict[str, dict[str, Any]]:
    path = cfg.overrides
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def prepare_tools(cfg: argparse.Namespace) -> None:
    tools = cfg.workspace / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    if not cfg.profiler.exists():
        raise SystemExit(f"Profiler não encontrado: {cfg.profiler}")
    shutil.copy2(cfg.profiler, tools / "fixture_energy_profiler.py")


# --------------------------------------------------------------------------- #
# Etapa 1: clone
# --------------------------------------------------------------------------- #


def git_head(repo: Path, env: dict[str, str]) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
            env=env, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def stage_clone(p: Proj, st: dict[str, Any], cfg: argparse.Namespace, ov: dict, installer: Installer) -> bool:
    env = build_env(cfg, p, git=True)
    if p.repo.exists():
        commit = git_head(p.repo, env)
        if commit:
            st["commit"] = commit
            set_stage(st, "clone", "ok", reused=True)
            p.save(st)
            return True
        shutil.rmtree(p.repo, ignore_errors=True)

    res = run_cmd(
        ["git", "clone", "--depth", "1", "--quiet", st["url"], str(p.repo)],
        cwd=p.dir, env=env, log_path=p.logs / "clone.log", timeout=cfg.timeout_clone_min * 60,
    )
    if res.returncode != 0:
        shutil.rmtree(p.repo, ignore_errors=True)
        exclude(p, st, "clone", "clone_failed", tail_log(p.logs / "clone.log"))
        return False
    st["commit"] = git_head(p.repo, env)
    set_stage(st, "clone", "ok", seconds=round(res.seconds, 1))
    p.save(st)
    return True


# --------------------------------------------------------------------------- #
# Etapa 2: venv + instalação
# --------------------------------------------------------------------------- #


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pick_by_preference(names: Iterable[str]) -> Optional[str]:
    normalized = {_norm(n): n for n in names}
    for preferred in TEST_EXTRA_NAMES:
        if preferred in normalized:
            return normalized[preferred]
    return None


def _read_pyproject(repo: Path) -> dict[str, Any]:
    path = repo / "pyproject.toml"
    if tomllib is None or not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def declared_extras(repo: Path) -> list[str]:
    data = _read_pyproject(repo)
    names = list((data.get("project", {}).get("optional-dependencies") or {}).keys())
    names += list(((data.get("tool", {}).get("poetry", {}) or {}).get("extras") or {}).keys())
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
            if parser.has_section("options.extras_require"):
                names += list(parser["options.extras_require"].keys())
        except configparser.Error:
            pass
    return names


def _flatten_group(groups: dict[str, Any], name: str, seen: Optional[set[str]] = None) -> list[str]:
    seen = seen or set()
    if name in seen:
        return []
    seen.add(name)
    out: list[str] = []
    for item in groups.get(name, []):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "include-group" in item:
            out += _flatten_group(groups, item["include-group"], seen)
    return out


def dependency_group_requirements(repo: Path) -> list[str]:
    groups = _read_pyproject(repo).get("dependency-groups") or {}
    name = _pick_by_preference(groups.keys())
    return _flatten_group(groups, name) if name else []


def plan_install(p: Proj, cfg: argparse.Namespace) -> list[tuple[str, list[str], Optional[list[str]]]]:
    """Plano automático. Cada passo: (rótulo, argumentos pip, alternativa se falhar)."""

    repo = p.repo
    steps: list[tuple[str, list[str], Optional[list[str]]]] = []
    has_pkg = any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg"))
    extra = _pick_by_preference(declared_extras(repo))
    if has_pkg:
        if extra:
            steps.append((f"projeto[{extra}]", ["-e", f".[{extra}]"], ["-e", "."]))
        else:
            steps.append(("projeto", ["-e", "."], None))

    group = dependency_group_requirements(repo)
    if group:
        group_file = p.logs / "dependency_group_requirements.txt"
        group_file.parent.mkdir(parents=True, exist_ok=True)
        group_file.write_text("\n".join(group) + "\n", encoding="utf-8")
        steps.append(("dependency-group", ["-r", str(group_file)], None))

    test_files = [f for f in TEST_REQ_FILES if (repo / f).exists()]
    for f in test_files:
        steps.append((f"req:{f}", ["-r", str(repo / f)], None))

    if not has_pkg and not test_files:
        for f in BASE_REQ_FILES:
            if (repo / f).exists():
                steps.append((f"req:{f}", ["-r", str(repo / f)], None))
                break
    return steps


def stage_install(p: Proj, st: dict[str, Any], cfg: argparse.Namespace, ov: dict, installer: Installer) -> bool:
    if p.venv.exists():
        shutil.rmtree(p.venv, ignore_errors=True)
    log_path = p.logs / "install.log"
    log_path.unlink(missing_ok=True)
    timeout = cfg.timeout_install_min * 60
    started = time.monotonic()

    python = ov.get("python") or cfg.python
    res = run_cmd(installer.create_venv_cmd(p, python), cwd=p.dir, env=build_env(cfg, p),
                  log_path=log_path, timeout=timeout)
    if res.returncode != 0:
        exclude(p, st, "install", "venv_failed", tail_log(log_path))
        return False

    venv_env = build_env(cfg, p, venv=True, extra=ov.get("env"))
    steps: list[tuple[str, list[str], Optional[list[str]]]] = []
    if cfg.profiler_requirements:
        steps.append(("profiler_deps", shlex.split(cfg.profiler_requirements), None))

    def expand(template: str) -> list[str]:
        return shlex.split(
            template.replace("{venv}", str(p.venv)).replace("{python}", str(p.py)).replace("{repo}", str(p.repo))
        )

    if ov.get("install") is not None:  # instalação TOTALMENTE manual (substitui o plano automático)
        custom_cmds = [expand(c) for c in ov["install"]]
    else:  # plano automático + comandos opcionais depois dele
        steps += plan_install(p, cfg)
        custom_cmds = [expand(c) for c in ov.get("install_extra", [])]

    executed: list[str] = []
    for label, args, fallback in steps:
        res = run_cmd(installer.install_cmd(p, args), cwd=p.repo, env=venv_env, log_path=log_path, timeout=timeout)
        if res.returncode != 0 and fallback is not None:
            log.info("[%s] %s falhou; tentando alternativa sem extras.", p.id, label)
            res = run_cmd(installer.install_cmd(p, fallback), cwd=p.repo, env=venv_env, log_path=log_path, timeout=timeout)
        executed.append(label)
        if res.returncode != 0:
            category = "profiler_deps_failed" if label == "profiler_deps" else "install_failed"
            exclude(p, st, "install", category, f"passo '{label}': " + tail_log(log_path))
            return False
    for cmd in custom_cmds:
        res = run_cmd(cmd, cwd=p.repo, env=venv_env, log_path=log_path, timeout=timeout)
        executed.append("custom:" + (cmd[0] if cmd else "?"))
        if res.returncode != 0:
            exclude(p, st, "install", "install_failed", "comando customizado: " + tail_log(log_path))
            return False

    probe = subprocess.run([str(p.py), "-c", PROBE_CODE], capture_output=True, text=True, env=venv_env, timeout=120)
    if probe.returncode != 0:
        exclude(p, st, "install", "profiler_deps_missing", probe.stderr[-600:])
        return False
    versions = json.loads(probe.stdout.strip().splitlines()[-1])

    freeze = subprocess.run([str(p.py), "-m", "pip", "freeze"], capture_output=True, text=True, env=venv_env, timeout=180)
    freeze_sha1 = ""
    if freeze.returncode == 0:
        (p.dir / "pip_freeze.txt").write_text(freeze.stdout, encoding="utf-8")
        freeze_sha1 = hashlib.sha1(freeze.stdout.encode()).hexdigest()[:12]

    st["install"] = {"installer": installer.kind, "steps": executed, "freeze_sha1": freeze_sha1, **versions}
    set_stage(st, "install", "ok", seconds=round(time.monotonic() - started, 1))
    p.save(st)
    return True


# --------------------------------------------------------------------------- #
# Etapa 3: baseline (pytest puro)
# --------------------------------------------------------------------------- #


def parse_junit(path: Path) -> Optional[dict[str, float]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "junit_seconds": 0.0}
    for suite in root.iter("testsuite"):
        totals["tests"] += int(suite.get("tests", 0))
        totals["failures"] += int(suite.get("failures", 0))
        totals["errors"] += int(suite.get("errors", 0))
        totals["skipped"] += int(suite.get("skipped", 0))
        totals["junit_seconds"] += safe_float(suite.get("time"))
    return totals


def pytest_args_for(cfg: argparse.Namespace, ov: dict) -> list[str]:
    return list(ov["pytest_args"]) if "pytest_args" in ov else shlex.split(cfg.pytest_args)


def stage_baseline(p: Proj, st: dict[str, Any], cfg: argparse.Namespace, ov: dict, installer: Installer) -> bool:
    junit = p.dir / "baseline" / "junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    log_path = p.logs / "baseline.log"
    log_path.unlink(missing_ok=True)

    cmd = [str(p.py), "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={junit}", *pytest_args_for(cfg, ov)]
    env = build_env(cfg, p, venv=True, pythonpath_repo=True, extra=ov.get("env"))
    res = run_cmd(cmd, cwd=p.repo, env=env, log_path=log_path, timeout=cfg.timeout_baseline_min * 60)

    info: dict[str, Any] = {"exit_code": res.returncode, "seconds": round(res.seconds, 1)}
    category: Optional[str] = None
    message = ""
    counts = parse_junit(junit)
    if res.timed_out:
        category, message = "baseline_timeout", f"excedeu {cfg.timeout_baseline_min:g} min"
    elif res.returncode in (2, 3, 4):
        category = "baseline_interrupted"  # erro de coleta/uso: nenhum teste chegou a rodar
    elif counts is None:
        category = "baseline_no_report"
    else:
        info.update(counts)
        tests = counts["tests"]
        bad = counts["failures"] + counts["errors"]
        if res.returncode == 5 or tests == 0:
            category = "baseline_no_tests"
        elif tests < cfg.min_tests_baseline:
            category = "baseline_too_few_tests"
            message = f"{tests} teste(s); mínimo {cfg.min_tests_baseline}"
        elif bad / tests > cfg.max_fail_rate:
            category = "baseline_high_failure_rate"
            message = f"{counts['failures']} falhas + {counts['errors']} erros em {tests} testes (limite {cfg.max_fail_rate:.0%})"
        elif counts["skipped"] / tests > cfg.max_skip_rate:
            category = "baseline_too_many_skips"
            message = f"{counts['skipped']} pulados em {tests} testes (limite {cfg.max_skip_rate:.0%})"
        elif res.seconds > cfg.max_baseline_min * 60:
            category = "baseline_too_slow"
            message = f"{res.seconds / 60:.1f} min (limite {cfg.max_baseline_min:g} min)"

    st["baseline"] = info
    if category:
        exclude(p, st, "baseline", category, message or tail_log(log_path))
        return False
    set_stage(st, "baseline", "ok", seconds=info["seconds"])
    p.save(st)
    return True


# --------------------------------------------------------------------------- #
# Preparação (clone + install + baseline) de um projeto
# --------------------------------------------------------------------------- #


def prepare_project(
    p: Proj, cfg: argparse.Namespace, overrides: dict[str, dict], installer: Installer, force: bool = False
) -> dict[str, Any]:
    ov = overrides.get(p.id, {})
    st = p.load()
    if force:
        st["final"], st["stages"] = "pending", {}
    if st["final"] in ("ready", "measured"):
        return st
    if st["final"] == "excluded":
        if not cfg.retentar_falhas:
            return st
        st = reset_failed(st)
    if ov.get("skip"):
        exclude(p, st, "clone", "manual_skip", str(ov["skip"]))
        return st

    try:
        for name, fn in (("clone", stage_clone), ("install", stage_install), ("baseline", stage_baseline)):
            if stage_done(p, st, name):
                continue
            if not fn(p, st, cfg, ov, installer):
                return st
        st["final"], st["failure"] = "ready", None
        p.save(st)
        log.info("[%s] pronto para medir (baseline %.0fs, %s testes)", p.id,
                 st["baseline"]["seconds"], st["baseline"].get("tests"))
    except KeyboardInterrupt:
        raise
    except Exception:
        exclude(p, st, st["stages"] and list(st["stages"])[-1] or "clone", "internal_error", traceback.format_exc()[-700:])
    return st


def prepare_many(projects: list[Proj], cfg: argparse.Namespace, overrides: dict, installer: Installer) -> None:
    skip = ("measured",) if cfg.retentar_falhas else ("measured", "excluded")
    todo = [p for p in projects if p.load()["final"] not in skip]
    if not todo:
        return
    log.info("Preparando %d projeto(s) com %d worker(s)...", len(todo), cfg.jobs)
    with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
        list(pool.map(lambda proj: prepare_project(proj, cfg, overrides, installer), todo))


# --------------------------------------------------------------------------- #
# Medição (serial)
# --------------------------------------------------------------------------- #


def wait_quiet(max_load: float, max_wait_s: float = 300.0) -> float:
    if max_load and max_load > 0:
        deadline = time.monotonic() + max_wait_s
        while os.getloadavg()[0] > max_load and time.monotonic() < deadline:
            time.sleep(5)
    return os.getloadavg()[0]


def compress_file(path: Path) -> None:
    if not path.exists():
        return
    with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def measure_rep(p: Proj, st: dict[str, Any], rep: int, cfg: argparse.Namespace, ov: dict) -> bool:
    rundir = p.runs / f"rep{rep}"
    shutil.rmtree(rundir, ignore_errors=True)
    rundir.mkdir(parents=True, exist_ok=True)

    labels = [f"project={p.id}", f"rep={rep}", *cfg.label]
    tools_profiler = cfg.workspace / "tools" / "fixture_energy_profiler.py"
    cmd = [
        str(p.py), str(tools_profiler), "--energy-project-only",
        "--energy-country-iso-code", cfg.pais, "--energy-output-dir", str(rundir),
        "--energy-project-name", p.id, "--energy-measure-power-secs", str(cfg.measure_power_secs),
    ]
    for label in labels:
        cmd += ["--energy-label", label]
    cmd += ["-p", "no:cacheprovider", "-q", *pytest_args_for(cfg, ov)]

    baseline_s = safe_float((st.get("baseline") or {}).get("seconds"), 60.0)
    timeout = min(max(baseline_s * cfg.timeout_factor, cfg.min_run_timeout_s), cfg.max_run_min * 60)
    load = wait_quiet(cfg.max_load)
    env = build_env(cfg, p, venv=True, pythonpath_repo=True, extra=ov.get("env"))

    attempt = (st["reps"].get(str(rep)) or {}).get("attempts", 0) + 1
    log.info("[%s] medindo rep %d (tentativa %d, timeout %.0fs, load %.2f)", p.id, rep, attempt, timeout, load)
    res = run_cmd(cmd, cwd=p.repo, env=env, log_path=p.logs / f"rep{rep}.log", timeout=timeout)

    meta_path = rundir / "fixture_energy_run_metadata.json"
    info: dict[str, Any] = {
        "attempts": attempt, "exit_code": res.returncode, "seconds": round(res.seconds, 1),
        "loadavg_1m_before": round(load, 2), "at": now_iso(),
        "profiler_sha1": hashlib.sha1(tools_profiler.read_bytes()).hexdigest()[:12],
    }
    if res.timed_out:
        info["status"] = "run_timeout"
    elif not meta_path.exists():
        info["status"] = "profiler_error"
        info["message"] = tail_log(p.logs / f"rep{rep}.log")
    elif res.returncode not in (0, 1):
        info["status"] = "run_failed"
        info["message"] = tail_log(p.logs / f"rep{rep}.log")
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        info["run_id"] = meta.get("run_id")
        info["status"] = "ok_with_warnings" if meta.get("warnings") else "ok"
        compress_file(rundir / "fixture_energy.csv")

    st["reps"][str(rep)] = info
    p.save(st)
    return info["status"] in OK_REP_STATUSES


def finalize_measurement_state(p: Proj, st: dict[str, Any], cfg: argparse.Namespace) -> None:
    reps = st["reps"]
    ok = [k for k, v in reps.items() if v["status"] in OK_REP_STATUSES]
    exhausted = all(
        reps.get(str(r), {}).get("status") in OK_REP_STATUSES or reps.get(str(r), {}).get("attempts", 0) >= MAX_REP_ATTEMPTS
        for r in range(1, cfg.reps + 1)
    )
    if len(ok) >= cfg.reps:
        st["final"] = "measured"
    elif exhausted:
        if ok:
            st["final"] = "measured"  # parcial: o runs.csv mostra quais repetições falharam
        else:
            failed = next((v for v in reps.values() if v["status"] not in OK_REP_STATUSES), {})
            exclude(p, st, "measure", failed.get("status", "measure_failed"), failed.get("message", ""))
            return
    p.save(st)


def measure_many(projects: list[Proj], cfg: argparse.Namespace, overrides: dict, installer: Installer) -> None:
    states = {p.id: p.load() for p in projects}
    candidates = [p for p in projects if states[p.id]["final"] in ("ready", "measured")]
    random.Random(cfg.seed).shuffle(candidates)

    def pending(p: Proj, rep: int) -> bool:
        info = states[p.id]["reps"].get(str(rep))
        return not info or (info["status"] not in OK_REP_STATUSES and info["attempts"] < MAX_REP_ATTEMPTS)

    order = (
        [(rep, p) for rep in range(1, cfg.reps + 1) for p in candidates]
        if cfg.ordem == "rep"
        else [(rep, p) for p in candidates for rep in range(1, cfg.reps + 1)]
    )
    tasks = [(rep, p) for rep, p in order if pending(p, rep)]
    if not tasks:
        log.info("Nenhuma medição pendente.")
    log.info("%d medição(ões) pendente(s), em série.", len(tasks))

    for index, (rep, p) in enumerate(tasks, start=1):
        st = states[p.id]
        if not (p.py.exists() and p.repo.exists()):  # ambiente foi limpo: reprepara
            st = prepare_project(p, cfg, overrides, installer, force=True)
            states[p.id] = st
            if st["final"] != "ready":
                continue
        ov = overrides.get(p.id, {})
        measure_rep(p, st, rep, cfg, ov)
        finalize_measurement_state(p, st, cfg)
        if cfg.pausa_seg and index < len(tasks):
            time.sleep(cfg.pausa_seg)

    for p in candidates:  # estado final coerente mesmo sem tarefas novas
        st = states[p.id]
        if st["final"] == "ready":
            finalize_measurement_state(p, st, cfg)


def cleanup_projects(projects: list[Proj]) -> None:
    freed = 0
    for p in projects:
        for path in (p.repo, p.venv, p.dir / "home", p.dir / "tmp"):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                freed += 1
    log.info("Limpeza: %d diretório(s) removido(s) (repo/venv/home/tmp).", freed)


# --------------------------------------------------------------------------- #
# Consolidação
# --------------------------------------------------------------------------- #

RUN_FIELDS = [
    "project_id", "url", "rep", "status", "attempts", "exit_code", "seconds_profiled",
    "seconds_baseline", "overhead_factor", "loadavg_1m_before", "run_id", "at",
    "tests_total", "tests_passed", "tests_failed", "tests_error", "tests_skipped",
    "total_energy_kwh", "total_duration_ms", "setup_energy_kwh", "test_body_energy_kwh",
    "teardown_energy_kwh", "setup_teardown_pct", "profiled_fixture_pct_of_total",
    "rows_total", "rows_ok", "fixture_rows", "fixture_share_shorter_than_sampling",
    "fixture_share_zero_energy", "fixture_pearson", "fixture_spearman",
    "test_phase_pearson", "warnings_count", "cpu_model", "python_version",
    "pytest_version", "codecarbon_version", "project_git_commit", "freeze_sha1",
    "profiler_sha1", "message",
]

STATUS_FIELDS = [
    "project_id", "url", "final", "failure_stage", "failure_category", "failure_message",
    "commit", "num_tests", "num_fixtures", "loc_projeto_total", "baseline_tests",
    "baseline_failures", "baseline_errors", "baseline_skipped", "baseline_seconds",
    "reps_ok", "reps_failed", "python_version", "pytest_version", "codecarbon_version",
    "freeze_sha1",
]


def _read_rq1(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["phase"]: row for row in csv.DictReader(handle)}


def build_run_row(p: Proj, st: dict[str, Any], rep: int) -> dict[str, Any]:
    info = st["reps"][str(rep)]
    install = st.get("install") or {}
    baseline_s = safe_float((st.get("baseline") or {}).get("seconds"))
    row: dict[str, Any] = {
        "project_id": p.id, "url": st["url"], "rep": rep, "status": info["status"],
        "attempts": info.get("attempts"), "exit_code": info.get("exit_code"),
        "seconds_profiled": info.get("seconds"), "seconds_baseline": baseline_s,
        "overhead_factor": round(info["seconds"] / baseline_s, 2) if baseline_s and info.get("seconds") else "",
        "loadavg_1m_before": info.get("loadavg_1m_before"), "run_id": info.get("run_id"),
        "at": info.get("at"), "freeze_sha1": install.get("freeze_sha1"),
        "profiler_sha1": info.get("profiler_sha1"), "message": (info.get("message") or "")[:300],
    }
    rundir = p.runs / f"rep{rep}"
    meta_path = rundir / "fixture_energy_run_metadata.json"
    if info["status"] not in OK_REP_STATUSES or not meta_path.exists():
        return row

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    counts = meta.get("tests", {}).get("outcome_counts", {})
    measurements = meta.get("measurements", {})
    diagnostics = meta.get("diagnostics", {})
    fixture_diag, phase_diag = diagnostics.get("fixture", {}), diagnostics.get("test_phase", {})
    env = meta.get("environment", {})
    rq1 = _read_rq1(rundir / "fixture_energy_rq1_summary.csv")

    def energy(phase: str, key: str = "energy_consumed_kwh") -> Any:
        return rq1.get(phase, {}).get(key, "")

    row.update({
        "tests_total": meta.get("tests", {}).get("total"),
        "tests_passed": counts.get("passed", 0) + counts.get("xpassed", 0),
        "tests_failed": counts.get("failed", 0),
        "tests_error": counts.get("error", 0),
        "tests_skipped": counts.get("skipped", 0) + counts.get("xfailed", 0),
        "total_energy_kwh": energy("total"), "total_duration_ms": energy("total", "duration_ms"),
        "setup_energy_kwh": energy("setup"), "test_body_energy_kwh": energy("test_body"),
        "teardown_energy_kwh": energy("teardown"),
        "setup_teardown_pct": energy("setup_teardown", "percentage_of_execution_energy"),
        "profiled_fixture_pct_of_total": energy("total", "profiled_fixture_percentage_of_phase_energy"),
        "rows_total": measurements.get("rows_total"),
        "rows_ok": measurements.get("status_counts", {}).get("ok", 0),
        "fixture_rows": fixture_diag.get("n"),
        "fixture_share_shorter_than_sampling": fixture_diag.get("share_shorter_than_sampling_interval"),
        "fixture_share_zero_energy": fixture_diag.get("share_zero_energy"),
        "fixture_pearson": fixture_diag.get("pearson_duration_vs_energy"),
        "fixture_spearman": fixture_diag.get("spearman_duration_vs_energy"),
        "test_phase_pearson": phase_diag.get("pearson_duration_vs_energy"),
        "warnings_count": len(meta.get("warnings", [])),
        "cpu_model": env.get("cpu_model"), "python_version": env.get("python_version"),
        "pytest_version": env.get("pytest_version"), "codecarbon_version": env.get("codecarbon_version"),
        "project_git_commit": env.get("project_git_commit"),
    })
    return row


def build_status_row(p: Proj, st: dict[str, Any]) -> dict[str, Any]:
    failure = st.get("failure") or {}
    baseline = st.get("baseline") or {}
    install = st.get("install") or {}
    reps = st.get("reps", {})
    return {
        "project_id": p.id, "url": st["url"], "final": st["final"],
        "failure_stage": failure.get("stage"), "failure_category": failure.get("category"),
        "failure_message": (failure.get("message") or "").replace("\n", " | ")[:300],
        "commit": st.get("commit"), "num_tests": p.row.get("num_tests"),
        "num_fixtures": p.row.get("num_fixtures"), "loc_projeto_total": p.row.get("loc_projeto_total"),
        "baseline_tests": baseline.get("tests"), "baseline_failures": baseline.get("failures"),
        "baseline_errors": baseline.get("errors"), "baseline_skipped": baseline.get("skipped"),
        "baseline_seconds": baseline.get("seconds"),
        "reps_ok": sum(1 for v in reps.values() if v["status"] in OK_REP_STATUSES),
        "reps_failed": sum(1 for v in reps.values() if v["status"] not in OK_REP_STATUSES),
        "python_version": install.get("python"), "pytest_version": install.get("pytest"),
        "codecarbon_version": install.get("codecarbon"), "freeze_sha1": install.get("freeze_sha1"),
    }


def merge_summaries(
    sources: list[tuple[Path, dict[str, Any]]], dest: Path, extra_fields: list[str]
) -> int:
    """Concatena CSVs de resumo em streaming, acrescentando project_id e rep."""

    existing = [(path, extras) for path, extras in sources if path.exists()]
    if not existing:
        write_csv_atomic(dest, extra_fields, [])
        return 0
    with existing[0][0].open(encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    fields = extra_fields + [h for h in header if h not in extra_fields]

    tmp = dest.with_name(dest.name + ".tmp")
    count = 0
    with tmp.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for path, extras in existing:
            with path.open(encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    writer.writerow({**row, **extras})
                    count += 1
    os.replace(tmp, dest)
    return count


def consolidate(projects: list[Proj], cfg: argparse.Namespace) -> None:
    out = cfg.workspace / "resultados"
    out.mkdir(parents=True, exist_ok=True)

    status_rows, run_rows = [], []
    sources: dict[str, list[tuple[Path, dict[str, Any]]]] = {"rq1": [], "rq2_fixtures": [], "rq2_scopes": []}
    names = {
        "rq1": "fixture_energy_rq1_summary.csv",
        "rq2_fixtures": "fixture_energy_rq2_fixtures.csv",
        "rq2_scopes": "fixture_energy_rq2_scopes.csv",
    }
    for p in projects:
        st = p.load()
        status_rows.append(build_status_row(p, st))
        for rep in sorted(int(k) for k in st["reps"]):
            run_rows.append(build_run_row(p, st, rep))
            if st["reps"][str(rep)]["status"] in OK_REP_STATUSES:
                for key, filename in names.items():
                    sources[key].append((p.runs / f"rep{rep}" / filename, {"project_id": p.id, "rep": rep}))

    write_csv_atomic(out / "projects_status.csv", STATUS_FIELDS, status_rows)
    write_csv_atomic(out / "runs.csv", RUN_FIELDS, run_rows)
    sizes = {
        key: merge_summaries(items, out / f"{key}_all.csv", ["project_id", "rep"])
        for key, items in sources.items()
    }
    log.info("Consolidado em %s: %d projetos, %d execuções, linhas %s", out, len(status_rows), len(run_rows), sizes)


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def print_status(projects: list[Proj]) -> None:
    from collections import Counter

    finals: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    reps_ok = reps_total = 0
    for p in projects:
        st = p.load()
        finals[st["final"]] += 1
        if st["final"] == "excluded" and st.get("failure"):
            categories[st["failure"]["category"]] += 1
        for info in st["reps"].values():
            reps_total += 1
            reps_ok += info["status"] in OK_REP_STATUSES
    print(f"Projetos no manifesto: {len(projects)}")
    for key, value in sorted(finals.items()):
        print(f"  {key:10s} {value}")
    if categories:
        print("Exclusões por categoria:")
        for key, value in categories.most_common():
            print(f"  {value:4d}  {key}")
    print(f"Repetições medidas: {reps_ok} ok de {reps_total} tentadas")


def cmd_rodar(cfg, projects, overrides, installer) -> None:
    todo = [p for p in projects if p.load()["final"] not in ("excluded", "measured")]
    if cfg.retentar_falhas:
        todo = [p for p in projects if p.load()["final"] != "measured"]
    log.info("%d projeto(s) a processar em lotes de %d.", len(todo), cfg.lote)
    for start in range(0, len(todo), cfg.lote):
        batch = todo[start:start + cfg.lote]
        log.info("=== Lote %d/%d (%d projetos) ===", start // cfg.lote + 1, -(-len(todo) // cfg.lote), len(batch))
        prepare_many(batch, cfg, overrides, installer)
        measure_many(batch, cfg, overrides, installer)
        consolidate(projects, cfg)
        if not cfg.manter_ambientes:
            cleanup_projects(batch)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=Path("workspace"))
    common.add_argument("--resultados", type=Path, default=Path("mine_projects.csv"),
                        help="CSV gerado pelo minerador (usa linhas com motivo_exclusao vazio).")
    common.add_argument("--profiler", type=Path, default=Path(__file__).with_name("fixture_energy_profiler.py"))
    common.add_argument("--overrides", type=Path, default=Path("overrides.json"),
                        help="JSON com ajustes por projeto (python, install, pytest_args, env, skip).")
    common.add_argument("--python", default=sys.executable, help="Python base para criar os venvs.")
    common.add_argument("--instalador", choices=("auto", "pip", "uv"), default="auto")
    common.add_argument("--seed", type=int, default=42)

    sel = argparse.ArgumentParser(add_help=False)
    sel.add_argument("--limite", type=int, help="Amostra aleatória (com --seed) de N projetos.")
    sel.add_argument("--min-tests", type=int)
    sel.add_argument("--max-tests", type=int)
    sel.add_argument("--min-loc", type=int)
    sel.add_argument("--max-loc", type=int)
    sel.add_argument("--projeto", action="append", default=[], help="Filtra por id/nome (repetível).")
    sel.add_argument("--refazer-manifesto", action="store_true")

    prep = argparse.ArgumentParser(add_help=False)
    prep.add_argument("--jobs", type=int, default=2, help="Preparações em paralelo.")
    prep.add_argument("--profiler-requirements", default="pytest codecarbon",
                      help="Pacotes do profiler instalados em cada venv ('' = já provisionado).")
    prep.add_argument("--pytest-args", default="", help="Argumentos extras do pytest para todos (ex.: 'tests/').")
    prep.add_argument("--timeout-clone-min", type=float, default=10)
    prep.add_argument("--timeout-install-min", type=float, default=20)
    prep.add_argument("--timeout-baseline-min", type=float, default=30)
    prep.add_argument("--min-tests-baseline", type=int, default=10)
    prep.add_argument("--max-fail-rate", type=float, default=0.05)
    prep.add_argument("--max-skip-rate", type=float, default=0.20)
    prep.add_argument("--max-baseline-min", type=float, default=30)
    prep.add_argument("--retentar-falhas", action="store_true")

    med = argparse.ArgumentParser(add_help=False)
    med.add_argument("--reps", type=int, default=3)
    med.add_argument("--pais", default="BRA", help="Código ISO3 para o CodeCarbon offline.")
    med.add_argument("--measure-power-secs", type=int, default=1)
    med.add_argument("--pausa-seg", type=float, default=10)
    med.add_argument("--max-load", type=float, default=0.0, help="Espera o load 1m cair abaixo disso (0 = desligado).")
    med.add_argument("--timeout-factor", type=float, default=5.0)
    med.add_argument("--min-run-timeout-s", type=float, default=600)
    med.add_argument("--max-run-min", type=float, default=120)
    med.add_argument("--ordem", choices=("rep", "projeto"), default="rep",
                     help="rep: intercala projetos por repetição (reduz deriva); projeto: todas as reps seguidas.")
    med.add_argument("--label", action="append", default=[], metavar="K=V", help="Rótulos extras (repetível).")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("iniciar", parents=[common, sel])
    sub.add_parser("preparar", parents=[common, sel, prep])
    p_med = sub.add_parser("medir", parents=[common, sel, prep, med])
    p_med.add_argument("--limpar", action="store_true", help="Remove repo/venv ao final.")
    p_run = sub.add_parser("rodar", parents=[common, sel, prep, med])
    p_run.add_argument("--lote", type=int, default=20)
    p_run.add_argument("--manter-ambientes", action="store_true", help="Não remove repo/venv ao fim de cada lote.")
    sub.add_parser("consolidar", parents=[common])
    sub.add_parser("status", parents=[common])
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    cfg = build_parser().parse_args(argv)
    cfg.workspace = cfg.workspace.resolve()
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    for name in ("limite", "min_tests", "max_tests", "min_loc", "max_loc", "projeto", "refazer_manifesto"):
        if not hasattr(cfg, name):
            setattr(cfg, name, None if name != "projeto" else [])
    for name, default in (("retentar_falhas", False),):
        if not hasattr(cfg, name):
            setattr(cfg, name, default)
    if os.geteuid() == 0:
        log.warning("Rodando como root: NÃO recomendado (código de terceiros será executado).")
    logging.getLogger().addHandler(logging.FileHandler(cfg.workspace / "orquestrador.log", encoding="utf-8"))

    if cfg.comando == "status":
        print_status(load_projects(cfg))
        return 0

    projects = load_projects(cfg)
    if cfg.comando == "iniciar":
        prepare_tools(cfg)
        return 0
    if cfg.comando == "consolidar":
        consolidate(projects, cfg)
        return 0

    prepare_tools(cfg)
    overrides = load_overrides(cfg)
    installer = Installer(cfg.instalador)
    log.info("Instalador: %s | projetos: %d | workspace: %s", installer.kind, len(projects), cfg.workspace)
    try:
        if cfg.comando == "preparar":
            prepare_many(projects, cfg, overrides, installer)
        elif cfg.comando == "medir":
            measure_many(projects, cfg, overrides, installer)
            if cfg.limpar:
                cleanup_projects(projects)
        elif cfg.comando == "rodar":
            cmd_rodar(cfg, projects, overrides, installer)
        consolidate(projects, cfg)
    except KeyboardInterrupt:
        log.warning("Interrompido. O estado foi salvo; rode o mesmo comando para continuar.")
        consolidate(projects, cfg)
        return 130
    print_status(projects)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
