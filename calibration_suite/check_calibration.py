#!/usr/bin/env python3
"""Verifica os invariantes do profiler sobre a suíte `calibration_suite/`.

Uso (a partir da raiz da suíte de calibração):

    python fixture_energy_profiler.py --energy-project-only \
        --energy-country-iso-code BRA --energy-output-dir out -p no:cacheprovider -q
    python check_calibration.py out

Verifica ESTRUTURA e DURAÇÕES (determinísticas), não valores de energia: a energia
depende do backend do CodeCarbon e só é impressa (razão observada/esperada) para
inspeção humana. Rode uma vez por versão de pytest que pretende aceitar
(`run_contract_matrix.sh`).
"""
import csv
import json
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FALHA"), message)
    if not condition:
        failures.append(message)


def read(name: str) -> list[dict]:
    with (out / name).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


raw = read("fixture_energy.csv")
fixtures = {r["fixture_name"]: r for r in read("fixture_energy_rq2_fixtures.csv")}
scopes = {r["fixture_scope"]: r for r in read("fixture_energy_rq2_scopes.csv")}
outcomes = {r["nodeid"].split("::")[-1]: r["outcome"] for r in read("fixture_energy_test_outcomes.csv")}
meta = json.loads((out / "fixture_energy_run_metadata.json").read_text(encoding="utf-8"))


def rows(name: str, phase: str) -> list[dict]:
    return [r for r in raw if r["fixture_name"] == name and r["phase"] == phase]


def ms(row: dict) -> float:
    return float(row["duration_ms"])


# --- resultados dos testes -------------------------------------------------
check(outcomes.get("test_a1") == "passed" and outcomes.get("test_b2") == "passed", "testes normais: passed")
check(outcomes.get("test_b_skip") == "skipped", "test_b_skip: skipped")
check(outcomes.get("test_b_fail") == "failed", "test_b_fail: failed")
check(outcomes.get("test_g_error") == "error", "test_g_error: error (falha no setup)")
check(len(outcomes) == 8, f"8 testes registrados (obtido {len(outcomes)})")

# --- degradação declarada --------------------------------------------------
check(not meta.get("degraded_features"), f"sem funcionalidades degradadas: {meta.get('degraded_features')}")

# --- instâncias e usos (RQ2) -----------------------------------------------
check(int(fixtures["session_res"]["fixture_instance_count"]) == 1, "session_res: 1 instância")
check(int(fixtures["session_res"]["total_fixture_uses"]) == 5, "session_res: 5 usos elegíveis (skip excluído)")
check(int(fixtures["module_res"]["fixture_instance_count"]) == 2, "module_res: 2 instâncias (alpha, beta)")
check(int(fixtures["module_res"]["total_fixture_uses"]) == 4, "module_res: 4 usos (3 + 1)")
check(int(fixtures["func_res"]["fixture_instance_count"]) == 4, "func_res: 4 instâncias (a1, a2, b1, b_fail)")
check(int(fixtures["func_res"]["total_fixture_uses"]) == 4, "func_res: 4 usos")
check("cheap" not in {r["fixture_name"] for r in raw if r["phase"] == "teardown"}, "cheap: sem linha de teardown")
check("broken" not in fixtures, "broken: ausente do RQ2 (teste em error é excluído)")
check(scopes["session"]["definitions_without_eligible_uses"] == "0", "escopo session: nenhuma definição sem usos")

# --- durações conhecidas (tolerância generosa: +-35%) ----------------------
def near(value: float, expected: float, tol: float = 0.35) -> bool:
    return expected * (1 - tol) <= value <= expected * (1 + tol) + 60

check(near(ms(rows("session_res", "setup")[0]), 1000), "session_res setup ~1000 ms")
check(near(ms(rows("session_res", "teardown")[0]), 400), "session_res teardown ~400 ms")
check(len(rows("session_res", "teardown")) == 1, "session_res: teardown medido uma única vez")
check(all(near(ms(r), 500) for r in rows("module_res", "setup")), "module_res setup ~500 ms (x2)")
check(all(near(ms(r), 200) for r in rows("module_res", "teardown")), "module_res teardown ~200 ms (x2)")
check(all(near(ms(r), 100) for r in rows("func_res", "setup")), "func_res setup ~100 ms")
check(len(rows("addfin", "teardown")) == 1 and near(ms(rows("addfin", "teardown")[0]), 50),
      "addfin: teardown via request.addfinalizer ~50 ms")

# --- sem dupla contagem: o resíduo da fase de setup do teste é pequeno -----
a1_setup = [r for r in raw if r["record_type"] == "test_phase" and r["phase"] == "setup"
            and r["test_name"].endswith("test_a1")]
check(len(a1_setup) == 1 and ms(a1_setup[0]) < 150,
      f"resíduo do setup de test_a1 < 150 ms (fixtures medidas à parte): {ms(a1_setup[0]):.0f} ms")
a1_body = [r for r in raw if r["record_type"] == "test_phase" and r["phase"] == "test_body"
           and r["test_name"].endswith("test_a1")]
check(near(ms(a1_body[0]), 300), "corpo de test_a1 ~300 ms")

# --- energia: apenas informativa -------------------------------------------
acc = meta.get("energy_accounting", {})
print("\nINFO backend CodeCarbon:", (meta.get("tracker") or {}).get("cpu_mode"))
if acc.get("available"):
    print(f"INFO energia global {acc['global_after_warmup_kwh']:.3e} kWh; atribuída "
          f"{acc['attributed_kwh']:.3e} kWh; não atribuída {acc['unattributed_share']:.0%}")
for name, expected_ms in (("session_res", 1400), ("module_res", 700), ("func_res", 150)):
    row = fixtures[name]
    kwh = float(row["total_lifecycle_energy_kwh"])
    n = int(row["fixture_instance_count"])
    print(f"INFO {name}: energia do ciclo de vida {kwh:.3e} kWh em {n} instância(s) "
          f"(esperado ~{expected_ms} ms por instância; compare a razão entre fixtures)")

print()
if failures:
    print(f"{len(failures)} verificação(ões) falharam.")
    sys.exit(1)
print("Todas as verificações passaram.")