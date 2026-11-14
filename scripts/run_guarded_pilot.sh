#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_UID="$(/usr/bin/id -u)"
RUN_GID="$(/usr/bin/id -g)"
RUN_USER="$(/usr/bin/id -un)"
ARTIFACTS="${PHASE1_ARTIFACTS:-/var/tmp/phase1-artifacts-${RUN_UID}}"
PYTHON="${PHASE1_PYTHON:-/home/phase1runner/phase1-venv/bin/python}"
UV="${UV_BIN:-/home/phase1runner/phase1-venv/bin/uv}"
ISOLATOR="${PROJECT_ROOT}/phase1/isolators/sudo_unshare.sh"
SAMPLE_REPORT="${ARTIFACTS}/single_project_report.json"

if [[ "$RUN_USER" != "phase1runner" || "$RUN_UID" == "0" ]]; then
  echo "Execute este script como a conta dedicada phase1runner (UID/GID locais)." >&2
  exit 64
fi
mkdir -p "$ARTIFACTS" || exit 64
cd "$PROJECT_ROOT" || exit 64

reuse_args=()
if [[ -n "${PHASE1_REUSE_SOURCE:-}" ]]; then
  reuse_args=(--reuse-source "$PHASE1_REUSE_SOURCE")
fi

echo "[1/2] Validando o fluxo completo do WhisperLiveKit..."
/usr/bin/env "$PYTHON" scripts/phase1_dynamic_launcher.py \
  --project-id WhisperLiveKit \
  "${reuse_args[@]}" \
  --isolation-launcher-json "[\"${ISOLATOR}\"]" \
  --python "$PYTHON" \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --artifacts "$ARTIFACTS" \
  --uv "$UV" \
  > "${ARTIFACTS}/whisper_single_project.log" 2>&1
single_rc=$?
if [[ "$single_rc" -ne 0 ]]; then
  echo "WhisperLiveKit não concluiu com sucesso (código ${single_rc}); piloto de 30 não iniciado." >&2
  tail -n 40 "${ARTIFACTS}/whisper_single_project.log" >&2 || true
  exit 2
fi

/usr/bin/env "$PYTHON" - "$SAMPLE_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("status") != "single_project_complete":
    raise SystemExit("relatório não indica single_project_complete")
projects = report.get("projects", [])
if len(projects) != 1:
    raise SystemExit("relatório não contém exatamente um projeto")
project = projects[0]
if project.get("project_id") != "WhisperLiveKit" or project.get("collection_ids_stable") is not True:
    raise SystemExit("identidade/estabilidade da coleta não passou")
if not project.get("setup_plan_test_ids") or len(project.get("execution_runs", [])) != 2:
    raise SystemExit("setup-plan ou duas execuções completas ausentes")
if not project.get("setup_only_discordance") or not project.get("circuit_breaker_comparison"):
    raise SystemExit("comparação setup-only ou K=3 ausente")
checks = []
checks.extend(run.get("run", {}).get("isolation_preflight", {}).get("passed") is True
              for run in project.get("collection_runs", []))
checks.append(project.get("setup_plan", {}).get("run", {}).get("isolation_preflight", {}).get("passed") is True)
checks.append(project.get("setup_only", {}).get("run", {}).get("isolation_preflight", {}).get("passed") is True)
checks.extend(run.get("run", {}).get("isolation_preflight", {}).get("passed") is True
              for run in project.get("execution_runs", []))
checks.append(project.get("circuit_breaker_k3", {}).get("run", {}).get("isolation_preflight", {}).get("passed") is True)
if not checks or not all(checks):
    raise SystemExit("ao menos um controle de isolamento falhou")
print("WhisperLiveKit aceito; controles e comparações presentes.")
PY
gate_rc=$?
if [[ "$gate_rc" -ne 0 ]]; then
  echo "Critérios do ensaio único não passaram; piloto de 30 não iniciado." >&2
  exit 3
fi

echo "[2/2] Ensaio único aceito; iniciando os 30 projetos..."
/usr/bin/env "$PYTHON" scripts/phase1_dynamic_launcher.py \
  --pilot \
  --isolation-launcher-json "[\"${ISOLATOR}\"]" \
  --python "$PYTHON" \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --artifacts "$ARTIFACTS" \
  --uv "$UV" \
  > "${ARTIFACTS}/pilot_launcher.log" 2>&1
pilot_rc=$?
echo "Piloto finalizado com código ${pilot_rc}; relatório: ${ARTIFACTS}/pilot_report.json"
exit "$pilot_rc"
