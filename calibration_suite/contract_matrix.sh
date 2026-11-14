#!/usr/bin/env bash
# Teste de contrato do profiler: roda a suíte de calibração em cada versão de pytest.
# Uso: PYTEST_VERSIONS="7.4.4 8.0.2 8.3.5 8.4.2" ./contract_matrix.sh
set -euo pipefail
VERSIONS="${PYTEST_VERSIONS:-7.4.4 8.0.2 8.3.5 8.4.2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/.." && pwd)"
PROFILER="$PROJECT_ROOT/tools/fixture_energy_profiler.py"
status=0
for v in $VERSIONS; do
  echo "=== pytest==$v ==="
  venv="$(mktemp -d)/venv"
  uv venv --seed "$venv" >/dev/null
  uv pip install --python "$venv/bin/python" "pytest==$v" codecarbon >/dev/null
  out="$(mktemp -d)"
  ( cd "$HERE" && \
    "$venv/bin/python" "$PROFILER" --energy-project-only \
      --energy-country-iso-code BRA --energy-output-dir "$out" -p no:cacheprovider -q ) || true
  if "$venv/bin/python" "$HERE/check_calibration.py" "$out"; then
    echo "pytest==$v: CONTRATO OK"
  else
    echo "pytest==$v: CONTRATO QUEBRADO"; status=1
  fi
done
exit $status
