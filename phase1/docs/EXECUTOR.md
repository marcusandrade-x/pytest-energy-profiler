# Executor do piloto da Fase 1

O executor mantém a instalação com rede separada da coleta e das execuções. A
coleta, o `--setup-plan`, `--setup-only`, as duas execuções completas e o ensaio
K=3 só rodam depois que `phase1_runner.py` aprova os controles no ambiente
Python do projeto.

## Primeiro projeto já instalado

Use o clone e o `.venv_ready` do smoke test para validar o fluxo completo em um
único projeto antes de iniciar os 30:

```bash
RUN_UID="$(id -u phase1runner)"
RUN_GID="$(id -g phase1runner)"
ISOLATOR="$PWD/phase1/isolators/sudo_unshare.sh"
sudo -u phase1runner /home/phase1runner/phase1-venv/bin/python scripts/phase1_dynamic_launcher.py \
  --project-id WhisperLiveKit \
  --isolation-launcher-json "[\"$ISOLATOR\"]" \
  --python /home/phase1runner/phase1-venv/bin/python \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --artifacts "/var/tmp/phase1-artifacts-$RUN_UID" \
  --uv /home/phase1runner/phase1-venv/bin/uv
```

O `--reuse-source` exige um projeto específico e confere o SHA no checkout. O
usuário dedicado deve continuar sem grupo Docker. A validação de isolamento é
repetida antes de cada fase do pytest. Para projetos sem ambiente pronto, o
executor tenta, nesta ordem, Python 3.13, 3.12 e 3.11, usando duas coletas e o
limiar provisório de 50% para escolher a primeira versão viável. O limiar pode
ser informado por `--collection-min-fraction`; registre o valor piloto antes
da varredura completa na v2.

## Piloto completo

Depois de revisar o relatório do projeto único e a versão congelada do tooling,
execute `--pilot` para percorrer a amostra comprometida. Sem `--pilot`, o
launcher processa somente o primeiro projeto; `--project-id ID` seleciona um
projeto para depuração.

```bash
RUN_UID="$(id -u phase1runner)"
RUN_GID="$(id -g phase1runner)"
ISOLATOR="$PWD/phase1/isolators/sudo_unshare.sh"
sudo -u phase1runner /home/phase1runner/phase1-venv/bin/python scripts/phase1_dynamic_launcher.py \
  --pilot \
  --isolation-launcher-json "[\"$ISOLATOR\"]" \
  --python /home/phase1runner/phase1-venv/bin/python \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --artifacts "/var/tmp/phase1-artifacts-$RUN_UID" \
  --uv /home/phase1runner/phase1-venv/bin/uv
```

Cada projeto guarda clone, ambiente e artefatos em `artifacts/pilot/ID/SHA/`.
Resultados completos são retomados se o SHA e a impressão digital do tooling
forem iguais. O arquivo `pilot_progress.json` é atualizado após cada projeto.
O relatório final agrega o funil, erros, discordância de `--setup-only` e a
comparação K=3. O piloto não calcula X nem define o mínimo de fixtures; ambos
continuam reservados às regras já registradas.

## Aceite antes dos 30

- Depois de atualizar o código, instale também a versão atualizada do helper
  root-owned, pois ele precisa encaminhar ao processo isolado o limiar K e os
  caminhos dos mapas/inventário:

  ```bash
  sudo install -o root -g root -m 0755 phase1/isolators/phase1-netns-root /usr/local/sbin/phase1-netns-root
  ```

- Rodar novamente `phase1_runner.py` com o código atualizado e confirmar todos
  os controles, inclusive K=3, no usuário e launcher reais.
- Executar primeiro `WhisperLiveKit` com `--reuse-source` e verificar no JSON:
  duas coletas estáveis, `setup-plan`, duas execuções, comparação `setup-only`,
  comparação K=3 e todos os `isolation_preflight.passed`.
- Só então iniciar `--pilot`; parar após o relatório do piloto para revisão e
  registro da v2 do pré-registro.
