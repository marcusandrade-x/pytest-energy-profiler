#!/usr/bin/env python3
"""Orquestrador de medição de energia de suítes pytest (clone -> venv -> baseline -> profiler).

Lê o CSV do minerador (`mine_projects.csv`), pega os candidatos elegíveis
(`motivo_exclusao` vazio) e, para cada um, automatiza:

    clone (commit fixado) -> venv + instalação -> baseline -> N medições com o profiler

Princípios de projeto (pensados para centenas de projetos)
----------------------------------------------------------
* PREPARAR em paralelo, MEDIR em série. Duas suítes rodando juntas contaminam a
  medição de energia uma da outra; já clonar/instalar/baseline podem rodar em
  paralelo. Nunca há preparação rodando durante uma medição.
* Os artefatos por execução são a fonte de verdade. Os CSVs consolidados são
  derivados e sempre reconstruíveis (`consolidar` é idempotente).
* Tudo é retomável: cada projeto tem um `status.json` (escrita atômica).
  Reexecutar o comando continua de onde parou; `--retentar-falhas` refaz as exclusões.
* Exclusões são classificadas por categoria (+ dica de causa provável) para triagem
  em `projects_status.csv` e para quantificar o VIÉS DE SELEÇÃO (`exclusion_funnel.csv`,
  `selection_comparison.csv`).
* Disco sob controle: trabalho em LOTES (`--lote`) e limpeza de repo+venv ao fim de
  cada lote. Logs, status, baseline e resultados permanecem. Com `uv` instalado a
  instalação fica muito mais rápida e barata em disco (cache com hardlinks).
* Segurança: testes e instaladores de terceiros rodam com ambiente MÍNIMO (sem
  GITHUB_TOKEN nem outros segredos) e com HOME/TMPDIR isolados por projeto.
  Ainda assim, é código arbitrário: use uma máquina/VM dedicada e nunca como root.

Validade das medições (v2)
--------------------------
Terminar o processo NÃO torna uma repetição válida. Cada repetição recebe três
classificações independentes (gravadas em status.json e runs.csv):

    status                 a execução terminou como esperado? (ok, run_timeout, ...)
    measurement_quality    good | degraded | unusable
    analysis_eligible      pode entrar na análise principal?

Uma repetição só é elegível se: (1) os resultados dos testes batem com o baseline
(mesmos nodeids, mesmos resultados, dentro de --max-outcome-diff-rate); (2) o
profiler não declarou funcionalidades degradadas (teardown/reuso não medidos);
(3) a fração de linhas de medição ruins é pequena; (4) a configuração do pytest
não distorce a medição (-x/maxfail, plugins de ordem aleatória, cobertura);
(5) commit e dependências são os mesmos das outras repetições do projeto.
Repetições inelegíveis são PRESERVADAS no disco e em runs.csv (com o motivo);
apenas ficam fora dos *_all.csv (use --incluir-inelegiveis para incluí-las).

Reprodutibilidade
-----------------
* O commit é fixado: coluna `commit_sha` do minerador (--coluna-commit) ou, na
  falta dela, o HEAD do primeiro clone; reclones posteriores usam o mesmo commit.
* Na primeira instalação as versões resolvidas são congeladas em `constraints.txt`;
  reinstalações (retry, limpeza de lote) respeitam esse arquivo.
* O baseline roda pelo MESMO lançador do profiler (`--energy-outcomes-only`):
  mesmo sys.path, mesma execução serial (-n 0), mesma definição de resultado.

Estrutura do workspace
----------------------
    <workspace>/
        manifest.csv                  seleção congelada de projetos
        tools/fixture_energy_profiler.py
        cache/                        cache de pip/uv compartilhado
        projects/<owner__repo>/
            status.json  pip_freeze.txt  constraints.txt  logs/  baseline/  runs/rep<k>/
            repo/ venv/               (removidos pela limpeza)
        resultados/
            projects_status.csv       1 linha por projeto (triagem)
            runs.csv                  1 linha por (projeto, repetição)
            exclusion_funnel.csv      funil de exclusões
            selection_comparison.csv  perfil (testes/fixtures/LOC) por grupo: viés de seleção
            rq1_all.csv  rq2_fixtures_all.csv  rq2_scopes_all.csv
            (o CSV bruto de cada execução fica em runs/rep<k>/*.csv.gz)

Comandos
--------
    iniciar     cria o manifesto (filtros/amostra) e copia o profiler
    preparar    clone + venv + instalação + baseline (paralelo, --jobs)
    medir       repetições com o profiler (serial, com retentativas)
    rodar       iniciar + [preparar -> medir -> consolidar -> limpar] por lote
    consolidar  reconstrói os CSVs consolidados
    status      resumo do andamento, exclusões e ESTIMATIVA de tempo restante

Exemplo
-------
    python energy_orchestrator.py rodar --resultados mine_projects.csv \\
        --lote 20 --jobs 3 --reps 3 --pais BRA
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
import statistics
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

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
REQ_GLOBS = ("*requirements*.txt", "requirements/*.txt", "reqs/*.txt", "ci/*.txt")
REQ_INCLUDE = re.compile(r"(test|dev|ci)", re.I)
REQ_EXCLUDE = re.compile(r"(doc|lint|typing|mypy|build|release|style|format|bench|audit|pre-?commit|publish)", re.I)
MAX_DISCOVERED_REQ_FILES = 5

MANIFEST_FIELDS = [
    "project_id", "url", "project_name", "num_tests", "num_fixtures", "loc_tests",
    "loc_producao", "loc_projeto_total", "arquivos_tests", "arquivos_producao",
]
OK_REP_STATUSES = ("ok", "ok_with_warnings")
MAX_REP_ATTEMPTS = 2
OUTCOMES_NAME = "fixture_energy_test_outcomes.csv"

PROBE_CODE = """
import sys, json, importlib.util as u
import pytest, codecarbon
mods = ("xdist", "pytest_cov", "pytest_randomly", "pytest_random_order")
print(json.dumps({
    "python": sys.version.split()[0],
    "pytest": pytest.__version__,
    "codecarbon": getattr(codecarbon, "__version__", "?"),
    "plugins": {m: u.find_spec(m) is not None for m in mods},
}))
"""

# Dicas de causa provável das falhas de baseline (para quantificar o viés de seleção).
HINT_PATTERNS = {
    "network_or_service": re.compile(
        r"ConnectionRefused|Connection refused|Name or service not known|"
        r"Temporary failure in name resolution|NewConnectionError|MaxRetryError|gaierror|"
        r"ConnectTimeout|could not connect to server|OperationalError|testcontainers|"
        r"docker\.errors|redis\.exceptions\.ConnectionError|Network is unreachable",
        re.I,
    ),
    "missing_dependency": re.compile(r"ModuleNotFoundError|ImportError|No module named", re.I),
    "missing_credentials": re.compile(r"\b(API_KEY|API[_ ]TOKEN|AUTH_TOKEN|SECRET_KEY|credentials)\b", re.I),
    "filesystem_or_permission": re.compile(r"PermissionError|Read-only file system", re.I),
}


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


def infer_hint(path: Path) -> Optional[str]:
    """Causa provável de uma falha de baseline, pelo conteúdo do log (heurística)."""

    try:
        data = path.read_bytes()[-300_000:].decode("utf-8", errors="replace")
    except OSError:
        return None
    counts = {name: len(rx.findall(data)) for name, rx in HINT_PATTERNS.items()}
    if not any(counts.values()):
        return None
    return max(counts, key=counts.get)  # empate: primeira da lista (rede/serviço)


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

    @property
    def constraints(self) -> Path:
        return self.dir / "constraints.txt"

    def load(self) -> dict[str, Any]:
        if self.status_path.exists():
            try:
                return json.loads(self.status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "project_id": self.id, "url": self.row["url"], "final": "pending",
            "stages": {}, "install": None, "baseline": None, "reps": {},
            "failure": None, "commit": None, "commit_source": None,
            "env_fingerprint": None, "completeness": None,
        }

    def save(self, st: dict[str, Any]) -> None:
        write_json_atomic(self.status_path, st)


def profiler_tool(cfg: argparse.Namespace) -> Path:
    return cfg.workspace / "tools" / "fixture_energy_profiler.py"


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


def exclude(
    p: Proj, st: dict[str, Any], stage: str, category: str, message: str = "",
    hint: Optional[str] = None,
) -> None:
    st["final"] = "excluded"
    st["failure"] = {
        "stage": stage, "category": category, "message": message[:800],
        "hint": hint, "at": now_iso(),
    }
    set_stage(st, stage, "failed", category=category)
    p.save(st)
    log.warning("[%s] excluído em %s: %s%s", p.id, stage, category, f" ({hint})" if hint else "")


def stage_done(p: Proj, st: dict[str, Any], name: str) -> bool:
    if st["stages"].get(name, {}).get("status") != "ok":
        return False
    if name == "clone":
        return p.repo.exists()
    if name == "install":
        return p.py.exists()
    return True


def rep_usable(info: Optional[dict[str, Any]]) -> bool:
    """Repetição que pode entrar na análise: execução ok E elegível."""

    if not info or info.get("status") not in OK_REP_STATUSES:
        return False
    return bool(info.get("analysis_eligible", True))


def rep_pending(info: Optional[dict[str, Any]]) -> bool:
    """Ainda deve ser (re)medida? Só se não é utilizável, há tentativas e vale retentar."""

    if info is None:
        return True
    if rep_usable(info):
        return False
    if info.get("attempts", 0) >= MAX_REP_ATTEMPTS:
        return False
    return bool(info.get("retriable", True))


def reset_failed(st: dict[str, Any]) -> dict[str, Any]:
    st["final"] = "pending"
    st["failure"] = None
    st["stages"] = {k: v for k, v in st["stages"].items() if v.get("status") == "ok"}
    st["reps"] = {k: v for k, v in st["reps"].items() if rep_usable(v)}
    st.pop("measurement_blocked", None)
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

    fields = list(MANIFEST_FIELDS)
    if rows and cfg.coluna_commit in rows[0] and cfg.coluna_commit not in fields:
        fields.append(cfg.coluna_commit)  # commit usado pelo minerador: o clone o reproduz
    elif rows and cfg.coluna_commit not in rows[0]:
        log.warning(
            "O CSV do minerador não tem a coluna '%s': o commit será fixado no primeiro clone "
            "e pode diferir do commit em que testes/fixtures/LOC foram contados.", cfg.coluna_commit)
    write_csv_atomic(cfg.workspace / "manifest.csv", fields, unique)
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
# Etapa 1: clone (commit fixado)
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


def clone_pinned(p: Proj, url: str, sha: str, env: dict[str, str], timeout: float) -> bool:
    """Busca exatamente `sha` (shallow). O GitHub permite fetch por SHA."""

    log_path = p.logs / "clone.log"
    steps = [
        ["git", "init", "--quiet", str(p.repo)],
        ["git", "-C", str(p.repo), "remote", "add", "origin", url],
        ["git", "-C", str(p.repo), "fetch", "--depth", "1", "--quiet", "origin", sha],
        ["git", "-C", str(p.repo), "checkout", "--quiet", "FETCH_HEAD"],
    ]
    for cmd in steps:
        res = run_cmd(cmd, cwd=p.dir, env=env, log_path=log_path, timeout=timeout)
        if res.returncode != 0:
            return False
    return True


def stage_clone(p: Proj, st: dict[str, Any], cfg: argparse.Namespace, ov: dict, installer: Installer) -> bool:
    env = build_env(cfg, p, git=True)
    miner_commit = (p.row.get(cfg.coluna_commit) or "").strip() or None
    pin = st.get("commit") or miner_commit
    timeout = cfg.timeout_clone_min * 60

    if p.repo.exists():
        head = git_head(p.repo, env)
        if head and (pin is None or head == pin):
            st["commit"] = head
            st["commit_source"] = st.get("commit_source") or "first_clone_head"
            set_stage(st, "clone", "ok", reused=True)
            p.save(st)
            return True
        shutil.rmtree(p.repo, ignore_errors=True)

    if pin:
        ok = clone_pinned(p, st["url"], pin, env, timeout)
        category = "commit_unavailable"
    else:
        res = run_cmd(
            ["git", "clone", "--depth", "1", "--quiet", st["url"], str(p.repo)],
            cwd=p.dir, env=env, log_path=p.logs / "clone.log", timeout=timeout,
        )
        ok, category = res.returncode == 0, "clone_failed"
    if not ok:
        shutil.rmtree(p.repo, ignore_errors=True)
        exclude(p, st, "clone", category, tail_log(p.logs / "clone.log"))
        return False

    head = git_head(p.repo, env)
    st["commit"] = head
    if not st.get("commit_source"):
        st["commit_source"] = "miner" if (miner_commit and head == miner_commit) else "first_clone_head"
    set_stage(st, "clone", "ok", pinned=bool(pin))
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


def _bump(nums: list[int], index: int) -> str:
    return ".".join(str(n) for n in [*nums[:index], nums[index] + 1])


def _poetry_version(spec: str) -> str:
    """Converte uma restrição de versão do Poetry (^, ~, curinga) para PEP 440."""

    parts = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item or item == "*":
            continue
        if item.startswith("^"):
            version = item[1:].strip()
            try:
                nums = [int(x) for x in version.split(".")]
            except ValueError:
                parts.append(f">={version}")
                continue
            index = next((i for i, n in enumerate(nums) if n != 0), len(nums) - 1)
            parts.append(f">={version},<{_bump(nums, index)}")
        elif item.startswith("~") and not item.startswith("~="):
            version = item[1:].strip()
            try:
                nums = [int(x) for x in version.split(".")]
            except ValueError:
                parts.append(f">={version}")
                continue
            parts.append(f">={version},<{_bump(nums, 0 if len(nums) == 1 else 1)}")
        elif item[0] in "<>=!~":
            parts.append(item)
        else:
            parts.append(f"=={item}")
    return ",".join(parts)


def _poetry_requirement(name: str, spec: Any) -> Optional[str]:
    if isinstance(spec, list):  # várias restrições condicionais: usa a primeira
        spec = spec[0] if spec else "*"
    extras: list[str] = []
    if isinstance(spec, dict):
        if any(k in spec for k in ("git", "path", "url")):
            return None  # dependência não instalável por nome
        extras = list(spec.get("extras") or [])
        version = str(spec.get("version", "*"))
    else:
        version = str(spec)
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"{name}{suffix}{_poetry_version(version)}"


def poetry_group_requirements(repo: Path) -> list[str]:
    """Dependências do grupo de teste/dev do Poetry, convertidas para requisitos pip."""

    poetry = (_read_pyproject(repo).get("tool") or {}).get("poetry") or {}
    groups = {
        name: (grp or {}).get("dependencies") or {}
        for name, grp in (poetry.get("group") or {}).items()
    }
    if poetry.get("dev-dependencies"):
        groups.setdefault("dev", poetry["dev-dependencies"])
    name = _pick_by_preference(groups.keys())
    if not name:
        return []
    out = []
    for dep, spec in groups[name].items():
        if dep.lower() == "python":
            continue
        requirement = _poetry_requirement(dep, spec)
        if requirement:
            out.append(requirement)
    return out


def discover_test_requirements(repo: Path) -> list[str]:
    """Arquivos de requisitos de teste: lista conhecida + descoberta por padrão de nome."""

    found = [f for f in TEST_REQ_FILES if (repo / f).exists()]
    for pattern in REQ_GLOBS:
        for path in sorted(repo.glob(pattern)):
            rel = path.relative_to(repo).as_posix()
            if rel in found or not path.is_file():
                continue
            if REQ_INCLUDE.search(path.name) and not REQ_EXCLUDE.search(path.name):
                found.append(rel)
    return found[:MAX_DISCOVERED_REQ_FILES + len(TEST_REQ_FILES)]


def plan_install(p: Proj, cfg: argparse.Namespace) -> list[tuple[str, list[str], Optional[list[str]]]]:
    """Plano automático. Cada passo: (rótulo, argumentos pip, alternativa se falhar)."""

    repo = p.repo
    steps: list[tuple[str, list[str], Optional[list[str]]]] = []
    has_pkg = any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg"))
    extra = _pick_by_preference(declared_extras(repo))
    test_files = discover_test_requirements(repo)

    if not has_pkg:  # sem pacote instalável: as dependências da aplicação vêm do requirements base
        for f in BASE_REQ_FILES:
            if (repo / f).exists() and f not in test_files:
                steps.append((f"req:{f}", ["-r", str(repo / f)], None))
                break

    if has_pkg:
        if extra:
            steps.append((f"projeto[{extra}]", ["-e", f".[{extra}]"], ["-e", "."]))
        else:
            steps.append(("projeto", ["-e", "."], None))

    group = dependency_group_requirements(repo) or poetry_group_requirements(repo)
    if group:
        group_file = p.logs / "dependency_group_requirements.txt"
        group_file.parent.mkdir(parents=True, exist_ok=True)
        group_file.write_text("\n".join(group) + "\n", encoding="utf-8")
        steps.append(("dependency-group", ["-r", str(group_file)], None))

    for f in test_files:
        steps.append((f"req:{f}", ["-r", str(repo / f)], None))
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
    # Reinstalações (retry, limpeza de lote) respeitam as versões da primeira instalação.
    constraint_args = ["-c", str(p.constraints)] if p.constraints.exists() else []
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
        res = run_cmd(installer.install_cmd(p, [*args, *constraint_args]), cwd=p.repo, env=venv_env,
                      log_path=log_path, timeout=timeout)
        if res.returncode != 0 and fallback is not None:
            log.info("[%s] %s falhou; tentando alternativa sem extras.", p.id, label)
            res = run_cmd(installer.install_cmd(p, [*fallback, *constraint_args]), cwd=p.repo, env=venv_env,
                          log_path=log_path, timeout=timeout)
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

    if not p.constraints.exists():  # congela as versões resolvidas na primeira instalação
        pins = subprocess.run([str(p.py), "-m", "pip", "freeze", "--exclude-editable"],
                              capture_output=True, text=True, env=venv_env, timeout=180)
        if pins.returncode == 0:
            lines = [ln for ln in pins.stdout.splitlines()
                     if ln.strip() and " @ " not in ln and not ln.startswith("-")]
            p.constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st["install"] = {
        "installer": installer.kind, "steps": executed, "freeze_sha1": freeze_sha1,
        "constrained": bool(constraint_args), **versions,
    }
    set_stage(st, "install", "ok", seconds=round(time.monotonic() - started, 1))
    p.save(st)
    return True


# --------------------------------------------------------------------------- #
# Argumentos do pytest e resultados por teste
# --------------------------------------------------------------------------- #


def pytest_args_for(cfg: argparse.Namespace, ov: dict) -> list[str]:
    return list(ov["pytest_args"]) if "pytest_args" in ov else shlex.split(cfg.pytest_args)


def common_pytest_args(st: dict[str, Any], cfg: argparse.Namespace, ov: dict) -> list[str]:
    """Argumentos IDÊNTICOS no baseline e nas medições (comparabilidade).

    Desliga plugins que alteram ordem/execução (`-p no:X` é inofensivo se X não está
    instalado) e a cobertura quando o pytest-cov existe. A execução serial (-n 0) é
    aplicada pelo próprio lançador (profiler), igual nos dois modos.
    """

    args = ["-p", "no:cacheprovider", "-p", "no:randomly", "-p", "no:random_order", "-q"]
    plugins = (st.get("install") or {}).get("plugins") or {}
    if plugins.get("pytest_cov"):
        args.append("--no-cov")
    return args + pytest_args_for(cfg, ov)


def normalize_outcome(raw: str) -> str:
    if "teardown_error" in raw:
        return "teardown_error"
    base = raw.split("+")[0]
    return {"passed": "pass", "xpassed": "pass", "failed": "fail", "error": "error",
            "skipped": "skip", "xfailed": "skip"}.get(base, base)


def read_outcomes(path: Path) -> Optional[dict[str, str]]:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return {row["nodeid"]: row["outcome"] for row in csv.DictReader(handle)}
    except (OSError, KeyError, csv.Error):
        return None


def compare_outcomes(base: dict[str, str], run: dict[str, str]) -> dict[str, Any]:
    """Diferenças entre o baseline e uma repetição (nodeids e resultado final de cada teste)."""

    missing = sorted(set(base) - set(run))
    extra = sorted(set(run) - set(base))
    changed = sorted(i for i in set(base) & set(run) if normalize_outcome(base[i]) != normalize_outcome(run[i]))
    count = len(missing) + len(extra) + len(changed)
    return {
        "count": count, "rate": count / max(len(base), 1),
        "missing": len(missing), "extra": len(extra), "changed": len(changed),
        "examples": (changed + missing + extra)[:5],
    }


# --------------------------------------------------------------------------- #
# Etapa 3: baseline (mesmo lançador do profiler, sem CodeCarbon)
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


def stage_baseline(p: Proj, st: dict[str, Any], cfg: argparse.Namespace, ov: dict, installer: Installer) -> bool:
    base_dir = p.dir / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    junit = base_dir / "junit.xml"
    outcomes_path = base_dir / OUTCOMES_NAME
    for stale in (junit, outcomes_path):
        stale.unlink(missing_ok=True)
    log_path = p.logs / "baseline.log"
    log_path.unlink(missing_ok=True)

    cmd = [
        str(p.py), str(profiler_tool(cfg)), "--energy-outcomes-only",
        "--energy-output-dir", str(base_dir), f"--junitxml={junit}",
        *common_pytest_args(st, cfg, ov),
    ]
    env = build_env(cfg, p, venv=True, pythonpath_repo=True, extra=ov.get("env"))
    res = run_cmd(cmd, cwd=p.repo, env=env, log_path=log_path, timeout=cfg.timeout_baseline_min * 60)

    info: dict[str, Any] = {"exit_code": res.returncode, "seconds": round(res.seconds, 1)}
    category: Optional[str] = None
    message = ""
    counts = parse_junit(junit)
    outcomes = read_outcomes(outcomes_path)
    if res.timed_out:
        category, message = "baseline_timeout", f"excedeu {cfg.timeout_baseline_min:g} min"
    elif res.returncode in (2, 3, 4):
        category = "baseline_interrupted"  # erro de coleta/uso: nenhum teste chegou a rodar
    elif counts is None or outcomes is None:
        category = "baseline_no_report"
    else:
        info.update(counts)
        tests = counts["tests"]
        bad = counts["failures"] + counts["errors"]
        miner_tests = safe_float(p.row.get("num_tests"))
        if miner_tests > 0:  # contagem estática do minerador vs. coletada pelo pytest
            info["collected_over_static_tests"] = round(tests / miner_tests, 3)
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
        hint = infer_hint(log_path) if category in (
            "baseline_high_failure_rate", "baseline_interrupted", "baseline_no_report",
            "baseline_too_many_skips") else None
        exclude(p, st, "baseline", category, message or tail_log(log_path), hint=hint)
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


def cpu_busy_fraction(sample_s: float = 2.0) -> Optional[float]:
    """Fração de CPU ocupada na máquina inteira (Linux, /proc/stat) durante `sample_s`."""

    def read() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as handle:
            values = [int(x) for x in handle.readline().split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    try:
        total1, idle1 = read()
        time.sleep(sample_s)
        total2, idle2 = read()
    except (OSError, ValueError, IndexError):
        return None
    delta = total2 - total1
    return max(0.0, 1.0 - (idle2 - idle1) / delta) if delta > 0 else None


def wait_quiet(max_busy: float, max_wait_s: float) -> tuple[Optional[float], bool]:
    """Espera a máquina ficar ociosa. Retorna (ocupação medida, estourou o tempo de espera)."""

    busy = cpu_busy_fraction()
    if busy is None or not max_busy or max_busy <= 0:
        return busy, False
    deadline = time.monotonic() + max_wait_s
    while busy > max_busy:
        if time.monotonic() >= deadline:
            return busy, True
        time.sleep(5)
        busy = cpu_busy_fraction()
        if busy is None:
            return None, False
    return busy, False


def compress_file(path: Path) -> None:
    if not path.exists():
        return
    with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def evaluate_quality(
    meta: dict[str, Any], outcome_check: Optional[dict[str, Any]], run_outcomes_ok: bool,
    quiet_timeout: bool, env_changed: bool, cfg: argparse.Namespace,
) -> dict[str, Any]:
    """Decide se a repetição pode entrar na análise.

    `reasons` reprovam a repetição; `flags` apenas a marcam como "degraded".
    `retriable` é True só se TODOS os motivos tendem a ser transitórios (flaky, ruído).
    """

    reasons: list[tuple[str, bool]] = []  # (motivo, transitório?)
    flags: list[str] = []

    for feature in (meta.get("degraded_features") or {}):
        reasons.append((f"degraded:{feature}", False))

    measurements = meta.get("measurements") or {}
    rows_total = measurements.get("rows_total") or 0
    status_counts = measurements.get("status_counts") or {}
    if rows_total == 0:
        reasons.append(("no_measurement_rows", False))
    else:
        bad = sum(v for k, v in status_counts.items() if k != "ok")
        if bad / rows_total > cfg.max_bad_measurement_share:
            reasons.append((f"measurement_status_bad:{bad}/{rows_total}", True))

    config = meta.get("pytest_config") or {}
    for flag in config.get("risk_flags") or []:
        reasons.append((f"config:{flag}", False))

    if not run_outcomes_ok:
        reasons.append(("outcomes_missing", True))
    elif outcome_check is not None and outcome_check["rate"] > cfg.max_outcome_diff_rate:
        reasons.append((f"outcome_mismatch:{outcome_check['count']}", True))

    if env_changed:
        reasons.append(("env_changed_between_reps", False))

    accounting = meta.get("energy_accounting") or {}
    share = accounting.get("unattributed_share")
    if share is not None:
        if cfg.max_unattributed_share and share > cfg.max_unattributed_share:
            reasons.append((f"energy_unattributed:{share:.0%}", False))
        elif share > cfg.flag_unattributed_share:
            flags.append(f"energy_unattributed:{share:.0%}")

    if quiet_timeout:
        if cfg.exigir_maquina_quieta:
            reasons.append(("noisy_machine", True))
        else:
            flags.append("noisy_machine")
    if meta.get("warnings"):
        flags.append("profiler_warnings")

    if reasons:
        quality = "unusable"
    elif flags:
        quality = "degraded"
    else:
        quality = "good"
    return {
        "measurement_quality": quality,
        "analysis_eligible": not reasons,
        "quality_reasons": [r for r, _ in reasons],
        "quality_flags": flags,
        "retriable": bool(reasons) and all(t for _, t in reasons),
    }


def measure_rep(p: Proj, st: dict[str, Any], rep: int, cfg: argparse.Namespace, ov: dict) -> bool:
    rundir = p.runs / f"rep{rep}"
    shutil.rmtree(rundir, ignore_errors=True)
    rundir.mkdir(parents=True, exist_ok=True)

    tools_profiler = profiler_tool(cfg)
    labels = [f"project={p.id}", f"rep={rep}", f"commit={(st.get('commit') or '')[:12]}", *cfg.label]
    cmd = [
        str(p.py), str(tools_profiler), "--energy-project-only",
        "--energy-country-iso-code", cfg.pais, "--energy-output-dir", str(rundir),
        "--energy-project-name", p.id, "--energy-measure-power-secs", str(cfg.measure_power_secs),
    ]
    for label in labels:
        cmd += ["--energy-label", label]
    cmd += common_pytest_args(st, cfg, ov)

    baseline_s = safe_float((st.get("baseline") or {}).get("seconds"), 60.0)
    timeout = min(max(baseline_s * cfg.timeout_factor, cfg.min_run_timeout_s), cfg.max_run_min * 60)
    busy, quiet_timeout = wait_quiet(cfg.max_cpu_busy, cfg.espera_quieta_max_s)
    load = os.getloadavg()[0]
    env = build_env(cfg, p, venv=True, pythonpath_repo=True, extra=ov.get("env"))

    attempt = (st["reps"].get(str(rep)) or {}).get("attempts", 0) + 1
    log.info("[%s] medindo rep %d (tentativa %d, timeout %.0fs, cpu ocupada %s)", p.id, rep, attempt, timeout,
             f"{busy:.0%}" if busy is not None else "?")
    res = run_cmd(cmd, cwd=p.repo, env=env, log_path=p.logs / f"rep{rep}.log", timeout=timeout)

    install = st.get("install") or {}
    meta_path = rundir / "fixture_energy_run_metadata.json"
    info: dict[str, Any] = {
        "attempts": attempt, "exit_code": res.returncode, "seconds": round(res.seconds, 1),
        "loadavg_1m_before": round(load, 2),
        "cpu_busy_before": round(busy, 3) if busy is not None else None,
        "quiet_timeout": quiet_timeout, "at": now_iso(),
        "profiler_sha1": hashlib.sha1(tools_profiler.read_bytes()).hexdigest()[:12],
        "commit": st.get("commit"), "freeze_sha1": install.get("freeze_sha1"),
        "analysis_eligible": False, "measurement_quality": "unusable", "quality_flags": [],
    }
    if res.timed_out:
        info.update(status="run_timeout", retriable=False, quality_reasons=["run_timeout"])
    elif not meta_path.exists():
        info.update(status="profiler_error", retriable=True, quality_reasons=["profiler_error"],
                    message=tail_log(p.logs / f"rep{rep}.log"))
    elif res.returncode not in (0, 1):
        info.update(status="run_failed", retriable=True, quality_reasons=["run_failed"],
                    message=tail_log(p.logs / f"rep{rep}.log"))
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        info["run_id"] = meta.get("run_id")
        info["status"] = "ok_with_warnings" if meta.get("warnings") else "ok"
        compress_file(rundir / "fixture_energy.csv")

        base_outcomes = read_outcomes(p.dir / "baseline" / OUTCOMES_NAME)
        run_outcomes = read_outcomes(rundir / OUTCOMES_NAME)
        outcome_check = None
        if base_outcomes is not None and run_outcomes is not None:
            outcome_check = compare_outcomes(base_outcomes, run_outcomes)
            info["outcome_diff_count"] = outcome_check["count"]
            info["outcome_diff_rate"] = round(outcome_check["rate"], 4)
            info["outcome_diff_examples"] = outcome_check["examples"]

        fingerprint = {"commit": st.get("commit"), "freeze_sha1": install.get("freeze_sha1")}
        reference = st.get("env_fingerprint")
        if reference is None:
            st["env_fingerprint"] = fingerprint
        env_changed = reference is not None and reference != fingerprint

        info.update(evaluate_quality(meta, outcome_check, run_outcomes is not None,
                                     quiet_timeout, env_changed, cfg))
        accounting = meta.get("energy_accounting") or {}
        info["unattributed_share"] = accounting.get("unattributed_share")
        info["cpu_mode"] = (meta.get("tracker") or {}).get("cpu_mode")
        if not info["analysis_eligible"]:
            log.warning("[%s] rep %d inelegível: %s", p.id, rep, ", ".join(info["quality_reasons"]))

    # Motivos sistemáticos (funcionalidade degradada, configuração, timeout) se repetiriam
    # nas demais repetições: não gasta mais horas de medição neste projeto.
    blocking = [r for r in info.get("quality_reasons") or []
                if str(r).startswith(("degraded:", "config:")) or r == "run_timeout"]
    if blocking:
        st["measurement_blocked"] = blocking[0]
    st["reps"][str(rep)] = info
    p.save(st)
    return rep_usable(info)


def finalize_measurement_state(p: Proj, st: dict[str, Any], cfg: argparse.Namespace) -> None:
    reps = st["reps"]
    usable = [k for k, v in reps.items() if rep_usable(v)]
    pending_any = not st.get("measurement_blocked") and any(
        rep_pending(reps.get(str(r))) for r in range(1, cfg.reps + 1))
    st["reps_planned"] = cfg.reps
    st["reps_usable"] = len(usable)
    if len(usable) >= cfg.reps:
        st["final"], st["completeness"] = "measured", "complete"
    elif not pending_any:
        if usable:
            st["final"], st["completeness"] = "measured", "partial"  # parcial explícito
        else:
            first = next(iter(reps.values()), {})
            reasons = first.get("quality_reasons") or [first.get("status", "measure_failed")]
            exclude(p, st, "measure", str(reasons[0]).split(":")[0], first.get("message", "")
                    or ", ".join(map(str, reasons)))
            return
    p.save(st)


def estimate_task_seconds(st: dict[str, Any], cfg: argparse.Namespace) -> float:
    """Custo estimado de UMA medição: suíte instrumentada + warm-up + pausa + espera."""

    baseline = safe_float((st.get("baseline") or {}).get("seconds"), 120.0)
    warmup = 2 * max(2.0, 2.0 * cfg.measure_power_secs)
    return baseline * cfg.overhead_estimado + warmup + 5 + cfg.pausa_seg


def measure_many(projects: list[Proj], cfg: argparse.Namespace, overrides: dict, installer: Installer) -> None:
    states = {p.id: p.load() for p in projects}
    base_order = list(projects)
    random.Random(cfg.seed).shuffle(base_order)

    def runnable(p: Proj) -> bool:
        return states[p.id]["final"] in ("ready", "measured")

    round_no = 0
    while True:  # rodadas: a 1ª mede o que falta; as seguintes retentam o que for retentável
        candidates = [p for p in base_order if runnable(p)]
        order = (
            [(rep, p) for rep in range(1, cfg.reps + 1) for p in candidates]
            if cfg.ordem == "rep"
            else [(rep, p) for p in candidates for rep in range(1, cfg.reps + 1)]
        )
        tasks = [(rep, p) for rep, p in order
                 if not states[p.id].get("measurement_blocked")
                 and rep_pending(states[p.id]["reps"].get(str(rep)))]
        if not tasks:
            if round_no == 0:
                log.info("Nenhuma medição pendente.")
            break
        round_no += 1
        eta_h = sum(estimate_task_seconds(states[p.id], cfg) for _, p in tasks) / 3600
        log.info("Rodada %d: %d medição(ões), em série (estimativa ~%.1f h).", round_no, len(tasks), eta_h)

        for index, (rep, p) in enumerate(tasks, start=1):
            st = states[p.id]
            if st.get("measurement_blocked"):  # bloqueado nesta mesma rodada: não gasta mais medições
                continue
            if not (p.py.exists() and p.repo.exists()):  # ambiente foi limpo: reprepara (mesmo commit/versões)
                st = prepare_project(p, cfg, overrides, installer, force=True)
                states[p.id] = st
                if st["final"] != "ready":
                    continue
            ov = overrides.get(p.id, {})
            measure_rep(p, st, rep, cfg, ov)
            finalize_measurement_state(p, st, cfg)
            if cfg.pausa_seg and index < len(tasks):
                time.sleep(cfg.pausa_seg)

    for p in base_order:  # estado final coerente mesmo sem tarefas novas
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
    "project_id", "url", "rep", "status", "measurement_quality", "analysis_eligible",
    "quality_reasons", "quality_flags", "attempts", "exit_code", "seconds_profiled",
    "seconds_baseline", "warmup_seconds", "overhead_factor", "overhead_factor_net",
    "loadavg_1m_before", "cpu_busy_before", "quiet_timeout", "run_id", "at",
    "outcome_diff_count", "outcome_diff_rate",
    "tests_total", "tests_passed", "tests_failed", "tests_error", "tests_skipped",
    "tests_teardown_error",
    "total_energy_kwh", "total_duration_ms", "setup_energy_kwh", "test_body_energy_kwh",
    "teardown_energy_kwh", "setup_teardown_pct", "profiled_fixture_pct_of_total",
    "global_energy_kwh", "attributed_energy_kwh", "unattributed_share", "cpu_mode",
    "rows_total", "rows_ok", "fixture_rows", "fixture_share_shorter_than_sampling",
    "fixture_share_zero_energy", "fixture_pearson", "fixture_spearman",
    "test_phase_pearson", "warnings_count", "degraded_features", "config_risk_flags",
    "cpu_model", "cpu_governor", "python_version",
    "pytest_version", "codecarbon_version", "project_git_commit", "freeze_sha1",
    "profiler_sha1", "message",
]

STATUS_FIELDS = [
    "project_id", "url", "final", "completeness", "failure_stage", "failure_category",
    "failure_hint", "failure_message", "commit", "commit_source", "num_tests", "num_fixtures",
    "loc_projeto_total", "baseline_tests", "baseline_failures", "baseline_errors",
    "baseline_skipped", "baseline_seconds", "collected_over_static_tests",
    "reps_planned", "reps_ok", "reps_usable", "reps_discarded",
    "python_version", "pytest_version", "codecarbon_version", "freeze_sha1",
]


def _read_rq1(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["phase"]: row for row in csv.DictReader(handle)}


def count_outcomes(counts: dict[str, int]) -> dict[str, int]:
    """Agrupa 'passed+teardown_error' etc. na categoria base + contador próprio."""

    totals: Counter[str] = Counter()
    for key, value in counts.items():
        if "teardown_error" in key:
            totals["teardown_error"] += value
            key = key.split("+")[0]
        totals[key] += value
    return dict(totals)


def build_run_row(p: Proj, st: dict[str, Any], rep: int) -> dict[str, Any]:
    info = st["reps"][str(rep)]
    install = st.get("install") or {}
    baseline_s = safe_float((st.get("baseline") or {}).get("seconds"))
    seconds = info.get("seconds")
    row: dict[str, Any] = {
        "project_id": p.id, "url": st["url"], "rep": rep, "status": info["status"],
        "measurement_quality": info.get("measurement_quality"),
        "analysis_eligible": rep_usable(info),
        "quality_reasons": ";".join(map(str, info.get("quality_reasons") or [])),
        "quality_flags": ";".join(map(str, info.get("quality_flags") or [])),
        "attempts": info.get("attempts"), "exit_code": info.get("exit_code"),
        "seconds_profiled": seconds, "seconds_baseline": baseline_s,
        "overhead_factor": round(seconds / baseline_s, 2) if baseline_s and seconds else "",
        "loadavg_1m_before": info.get("loadavg_1m_before"),
        "cpu_busy_before": info.get("cpu_busy_before"), "quiet_timeout": info.get("quiet_timeout"),
        "outcome_diff_count": info.get("outcome_diff_count"),
        "outcome_diff_rate": info.get("outcome_diff_rate"),
        "run_id": info.get("run_id"), "at": info.get("at"),
        "freeze_sha1": info.get("freeze_sha1") or install.get("freeze_sha1"),
        "profiler_sha1": info.get("profiler_sha1"), "message": (info.get("message") or "")[:300],
        "project_git_commit": info.get("commit"), "cpu_mode": info.get("cpu_mode"),
        "unattributed_share": info.get("unattributed_share"),
    }
    rundir = p.runs / f"rep{rep}"
    meta_path = rundir / "fixture_energy_run_metadata.json"
    if info["status"] not in OK_REP_STATUSES or not meta_path.exists():
        return row

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    counts = count_outcomes(meta.get("tests", {}).get("outcome_counts", {}))
    measurements = meta.get("measurements", {})
    diagnostics = meta.get("diagnostics", {})
    fixture_diag, phase_diag = diagnostics.get("fixture", {}), diagnostics.get("test_phase", {})
    env = meta.get("environment", {})
    accounting = meta.get("energy_accounting", {})
    warmup = safe_float((meta.get("run_info") or {}).get("warmup_seconds"))
    rq1 = _read_rq1(rundir / "fixture_energy_rq1_summary.csv")

    def energy(phase: str, key: str = "energy_consumed_kwh") -> Any:
        return rq1.get(phase, {}).get(key, "")

    row.update({
        "warmup_seconds": warmup,
        "overhead_factor_net": round(max(seconds - warmup, 0) / baseline_s, 2) if baseline_s and seconds else "",
        "tests_total": meta.get("tests", {}).get("total"),
        "tests_passed": counts.get("passed", 0) + counts.get("xpassed", 0),
        "tests_failed": counts.get("failed", 0),
        "tests_error": counts.get("error", 0),
        "tests_skipped": counts.get("skipped", 0) + counts.get("xfailed", 0),
        "tests_teardown_error": counts.get("teardown_error", 0),
        "total_energy_kwh": energy("total"), "total_duration_ms": energy("total", "duration_ms"),
        "setup_energy_kwh": energy("setup"), "test_body_energy_kwh": energy("test_body"),
        "teardown_energy_kwh": energy("teardown"),
        "setup_teardown_pct": energy("setup_teardown", "percentage_of_execution_energy"),
        "profiled_fixture_pct_of_total": energy("total", "profiled_fixture_percentage_of_phase_energy"),
        "global_energy_kwh": accounting.get("global_after_warmup_kwh"),
        "attributed_energy_kwh": accounting.get("attributed_kwh"),
        "rows_total": measurements.get("rows_total"),
        "rows_ok": measurements.get("status_counts", {}).get("ok", 0),
        "fixture_rows": fixture_diag.get("n"),
        "fixture_share_shorter_than_sampling": fixture_diag.get("share_shorter_than_sampling_interval"),
        "fixture_share_zero_energy": fixture_diag.get("share_zero_energy"),
        "fixture_pearson": fixture_diag.get("pearson_duration_vs_energy"),
        "fixture_spearman": fixture_diag.get("spearman_duration_vs_energy"),
        "test_phase_pearson": phase_diag.get("pearson_duration_vs_energy"),
        "warnings_count": len(meta.get("warnings", [])),
        "degraded_features": ";".join(sorted((meta.get("degraded_features") or {}).keys())),
        "config_risk_flags": ";".join((meta.get("pytest_config") or {}).get("risk_flags") or []),
        "cpu_model": env.get("cpu_model"), "cpu_governor": env.get("cpu_governor"),
        "python_version": env.get("python_version"),
        "pytest_version": env.get("pytest_version"), "codecarbon_version": env.get("codecarbon_version"),
    })
    return row


def build_status_row(p: Proj, st: dict[str, Any]) -> dict[str, Any]:
    failure = st.get("failure") or {}
    baseline = st.get("baseline") or {}
    install = st.get("install") or {}
    reps = st.get("reps", {})
    ok = sum(1 for v in reps.values() if v["status"] in OK_REP_STATUSES)
    usable = sum(1 for v in reps.values() if rep_usable(v))
    return {
        "project_id": p.id, "url": st["url"], "final": st["final"],
        "completeness": st.get("completeness"),
        "failure_stage": failure.get("stage"), "failure_category": failure.get("category"),
        "failure_hint": failure.get("hint"),
        "failure_message": (failure.get("message") or "").replace("\n", " | ")[:300],
        "commit": st.get("commit"), "commit_source": st.get("commit_source"),
        "num_tests": p.row.get("num_tests"),
        "num_fixtures": p.row.get("num_fixtures"), "loc_projeto_total": p.row.get("loc_projeto_total"),
        "baseline_tests": baseline.get("tests"), "baseline_failures": baseline.get("failures"),
        "baseline_errors": baseline.get("errors"), "baseline_skipped": baseline.get("skipped"),
        "baseline_seconds": baseline.get("seconds"),
        "collected_over_static_tests": baseline.get("collected_over_static_tests"),
        "reps_planned": st.get("reps_planned"), "reps_ok": ok, "reps_usable": usable,
        "reps_discarded": len(reps) - usable,
        "python_version": install.get("python"), "pytest_version": install.get("pytest"),
        "codecarbon_version": install.get("codecarbon"), "freeze_sha1": install.get("freeze_sha1"),
    }


def merge_summaries(
    sources: list[tuple[Path, dict[str, Any]]], dest: Path, extra_fields: list[str]
) -> int:
    """Concatena CSVs de resumo em streaming, acrescentando colunas de identificação."""

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


FUNNEL_METRICS = ("num_tests", "num_fixtures", "loc_tests", "loc_producao", "loc_projeto_total")


def _quartiles(values: list[float]) -> tuple[Any, Any, Any]:
    if not values:
        return "", "", ""
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3


def write_funnel(projects: list[Proj], out: Path) -> None:
    """Funil de exclusão e perfil dos grupos: quantifica o viés de seleção.

    Compare `incluido_*` com `excluido:*` (e com dicas como network_or_service):
    se suítes pesadas em infraestrutura caem no baseline, a amostra medida deixa
    de representar o universo minerado, e isso precisa constar nas ameaças à validade.
    """

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    funnel: Counter[tuple[str, str, str, str]] = Counter()
    for p in projects:
        st = p.load()
        final = st["final"]
        if final == "measured":
            group = f"incluido_{st.get('completeness') or 'completo'}"
            funnel[(group, "", "", "")] += 1
            groups[group].append(p.row)
        elif final == "excluded":
            failure = st.get("failure") or {}
            category, hint = failure.get("category") or "?", failure.get("hint") or ""
            group = f"excluido:{category}"
            funnel[(group, failure.get("stage") or "", category, hint)] += 1
            groups[group].append(p.row)
            if hint:
                groups[f"{group}[{hint}]"].append(p.row)
        else:
            group = "ready_nao_medido" if final == "ready" else "pendente"
            funnel[(group, "", "", "")] += 1
            groups[group].append(p.row)

    write_csv_atomic(
        out / "exclusion_funnel.csv", ["group", "stage", "category", "hint", "count"],
        [{"group": g, "stage": s, "category": c, "hint": h, "count": n}
         for (g, s, c, h), n in sorted(funnel.items())],
    )
    fields = ["group", "n"] + [f"{m}_{stat}" for m in FUNNEL_METRICS for stat in ("q1", "median", "q3")]
    rows = []
    for group, members in sorted(groups.items()):
        row: dict[str, Any] = {"group": group, "n": len(members)}
        for metric in FUNNEL_METRICS:
            values = [safe_float(m.get(metric)) for m in members if (m.get(metric) or "").strip() != ""]
            q1, median, q3 = _quartiles(values)
            row.update({f"{metric}_q1": q1, f"{metric}_median": median, f"{metric}_q3": q3})
        rows.append(row)
    write_csv_atomic(out / "selection_comparison.csv", fields, rows)


def consolidate(projects: list[Proj], cfg: argparse.Namespace) -> None:
    out = cfg.workspace / "resultados"
    out.mkdir(parents=True, exist_ok=True)
    include_ineligible = getattr(cfg, "incluir_inelegiveis", False)

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
            info = st["reps"][str(rep)]
            if info["status"] in OK_REP_STATUSES and (rep_usable(info) or include_ineligible):
                for key, filename in names.items():
                    sources[key].append((p.runs / f"rep{rep}" / filename,
                                         {"project_id": p.id, "rep": rep, "analysis_eligible": rep_usable(info)}))

    write_csv_atomic(out / "projects_status.csv", STATUS_FIELDS, status_rows)
    write_csv_atomic(out / "runs.csv", RUN_FIELDS, run_rows)
    sizes = {
        key: merge_summaries(items, out / f"{key}_all.csv", ["project_id", "rep", "analysis_eligible"])
        for key, items in sources.items()
    }
    write_funnel(projects, out)
    log.info("Consolidado em %s: %d projetos, %d execuções, linhas %s", out, len(status_rows), len(run_rows), sizes)


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def estimate_remaining_hours(projects: list[Proj], cfg: argparse.Namespace) -> tuple[float, int]:
    """Horas restantes de MEDIÇÃO (as preparações em paralelo não entram) e nº de projetos sem baseline."""

    reps = getattr(cfg, "reps", 3)
    states = [p.load() for p in projects]
    known = [safe_float((s.get("baseline") or {}).get("seconds")) for s in states if s.get("baseline")]
    fallback = statistics.median(known) if known else 120.0
    seconds, unknown = 0.0, 0
    for st in states:
        if st["final"] == "excluded":
            continue
        remaining = max(reps - sum(1 for v in st["reps"].values() if rep_usable(v)), 0)
        if st["final"] == "measured" and not remaining:
            continue
        if not st.get("baseline"):
            unknown += 1
            st = {**st, "baseline": {"seconds": fallback}}
        seconds += remaining * estimate_task_seconds(st, cfg)
    return seconds / 3600, unknown


def print_status(projects: list[Proj], cfg: argparse.Namespace) -> None:
    finals: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    completeness: Counter[str] = Counter()
    reps_ok = reps_usable = reps_total = 0
    for p in projects:
        st = p.load()
        finals[st["final"]] += 1
        if st["final"] == "excluded" and st.get("failure"):
            hint = st["failure"].get("hint")
            categories[st["failure"]["category"] + (f" [{hint}]" if hint else "")] += 1
        if st["final"] == "measured":
            completeness[st.get("completeness") or "?"] += 1
        for info in st["reps"].values():
            reps_total += 1
            reps_ok += info["status"] in OK_REP_STATUSES
            reps_usable += rep_usable(info)
    print(f"Projetos no manifesto: {len(projects)}")
    for key, value in sorted(finals.items()):
        print(f"  {key:10s} {value}")
    if completeness:
        print("Medidos: " + ", ".join(f"{k}={v}" for k, v in sorted(completeness.items())))
    if categories:
        print("Exclusões por categoria:")
        for key, value in categories.most_common():
            print(f"  {value:4d}  {key}")
    print(f"Repetições: {reps_usable} utilizáveis, {reps_ok} executadas ok, {reps_total} tentadas")
    hours, unknown = estimate_remaining_hours(projects, cfg)
    extra = f" ({unknown} projeto(s) sem baseline: usando a mediana)" if unknown else ""
    print(f"Medição restante estimada: ~{hours:.1f} h{extra}")


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
    common.add_argument("--coluna-commit", default="commit_sha",
                        help="Coluna do CSV do minerador com o SHA analisado (fixa o commit do clone).")
    common.add_argument("--profiler", type=Path, default=Path(__file__).with_name("fixture_energy_profiler.py"))
    common.add_argument("--overrides", type=Path, default=Path("overrides.json"),
                        help="JSON com ajustes por projeto (python, install, pytest_args, env, skip).")
    common.add_argument("--python", default=sys.executable, help="Python base para criar os venvs.")
    common.add_argument("--instalador", choices=("auto", "pip", "uv"), default="auto")
    common.add_argument("--seed", type=int, default=42)
    common.add_argument("--incluir-inelegiveis", action="store_true",
                        help="Inclui repetições inelegíveis nos *_all.csv (coluna analysis_eligible).")

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
    prep.add_argument("--max-baseline-min", type=float, default=30,
                      help="Suítes mais lentas são excluídas (baseline_too_slow). Com centenas de "
                           "projetos x reps, este limite domina o tempo total: veja `status`.")
    prep.add_argument("--retentar-falhas", action="store_true")

    med = argparse.ArgumentParser(add_help=False)
    med.add_argument("--reps", type=int, default=3)
    med.add_argument("--pais", default="BRA", help="Código ISO3 para o CodeCarbon offline.")
    med.add_argument("--measure-power-secs", type=int, default=1)
    med.add_argument("--pausa-seg", type=float, default=10)
    med.add_argument("--max-cpu-busy", type=float, default=0.10,
                     help="Espera a ocupação de CPU da máquina cair abaixo disto antes de medir (0 = só registra).")
    med.add_argument("--espera-quieta-max-s", type=float, default=300)
    med.add_argument("--exigir-maquina-quieta", action="store_true",
                     help="Repetições medidas com a máquina ocupada (timeout da espera) ficam inelegíveis.")
    med.add_argument("--timeout-factor", type=float, default=5.0)
    med.add_argument("--min-run-timeout-s", type=float, default=600)
    med.add_argument("--max-run-min", type=float, default=120)
    med.add_argument("--ordem", choices=("rep", "projeto"), default="rep",
                     help="rep: intercala projetos por repetição (reduz deriva); projeto: todas as reps seguidas.")
    med.add_argument("--label", action="append", default=[], metavar="K=V", help="Rótulos extras (repetível).")
    med.add_argument("--max-outcome-diff-rate", type=float, default=0.01,
                     help="Fração máxima de testes cujo resultado difere do baseline (inclui nodeids ausentes/novos).")
    med.add_argument("--max-bad-measurement-share", type=float, default=0.05,
                     help="Fração máxima de linhas de medição com status diferente de ok.")
    med.add_argument("--max-unattributed-share", type=float, default=0.0,
                     help="Reprova se a energia global não atribuída superar isto (0 = só registra; "
                          "calibre com o piloto).")
    med.add_argument("--flag-unattributed-share", type=float, default=0.5,
                     help="Marca a repetição como 'degraded' se a energia não atribuída superar isto.")
    med.add_argument("--overhead-estimado", type=float, default=1.3,
                     help="Fator tempo-profilado/baseline usado apenas nas estimativas de duração.")

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


# Valores usados quando o subcomando não define a opção (ex.: `status`, `consolidar`).
FALLBACK_DEFAULTS: dict[str, Any] = {
    "limite": None, "min_tests": None, "max_tests": None, "min_loc": None, "max_loc": None,
    "projeto": [], "refazer_manifesto": False, "retentar_falhas": False, "reps": 3,
    "measure_power_secs": 1, "pausa_seg": 10.0, "overhead_estimado": 1.3,
}


def main(argv: Optional[list[str]] = None) -> int:
    cfg = build_parser().parse_args(argv)
    cfg.workspace = cfg.workspace.resolve()
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    for name, default in FALLBACK_DEFAULTS.items():
        if not hasattr(cfg, name):
            setattr(cfg, name, default)
    if os.geteuid() == 0:
        log.warning("Rodando como root: NÃO recomendado (código de terceiros será executado).")
    logging.getLogger().addHandler(logging.FileHandler(cfg.workspace / "orquestrador.log", encoding="utf-8"))

    if cfg.comando == "status":
        print_status(load_projects(cfg), cfg)
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
    print_status(projects, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())