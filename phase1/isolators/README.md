# Isolamento e execução dos controles

`phase1_runner.py` recebe um launcher como vetor JSON, sem avaliar uma string
como shell. O protocolo do launcher é:

```text
LAUNCHER -- COMANDO ARGUMENTOS...
```

O launcher de referência `sudo_unshare.sh` cria o namespace de rede, ativa
`lo`, limpa grupos suplementares e inicia o comando como o UID/GID dedicado.
Ele exige `sudo -n` habilitado para `unshare`; se algum pré-requisito falhar,
retorna erro e o runner não aceita o ambiente. O teste não usa `sudo` para
executar o código de projetos.

## Preparação fora do executor

Crie um usuário de estudo sem segredos e sem associação ao grupo `docker`.
Garanta que o workspace de Fase 1 e o ambiente virtual sejam graváveis por ele.
Instale o ambiente dos controles pela rede, em etapa separada, usando o launcher
`study_user.sh`:

```bash
export PHASE1_RUN_UID="$(id -u phase1runner)"
export PHASE1_RUN_GID="$(id -g phase1runner)"
mkdir -p workspace/phase1
sudo chown "$PHASE1_RUN_UID:$PHASE1_RUN_GID" workspace/phase1
export PHASE1_HOME="$PWD/workspace/phase1/home"
export PHASE1_TMPDIR="$PWD/workspace/phase1/tmp"
mkdir -p "$PHASE1_HOME" "$PHASE1_TMPDIR"
sudo chown "$PHASE1_RUN_UID:$PHASE1_RUN_GID" "$PHASE1_HOME" "$PHASE1_TMPDIR"
phase1/isolators/study_user.sh -- python3 -m venv workspace/phase1/control-venv
phase1/isolators/study_user.sh -- workspace/phase1/control-venv/bin/python -m pip install -r phase1/controls/requirements.txt
```

Scripts de build de pacotes podem executar durante a instalação; por isso ela
roda como o usuário dedicado, com `HOME`/`TMPDIR` isolados e ambiente mínimo,
sem tokens e sem grupo Docker. A instalação tem rede e não conta como teste de
rede isolada.

## Validar antes do piloto

```bash
export PHASE1_RUN_UID="$(id -u phase1runner)"
export PHASE1_RUN_GID="$(id -g phase1runner)"
python3 scripts/phase1_runner.py \
  --isolation-launcher-json '["/caminho/absoluto/phase1/isolators/sudo_unshare.sh"]' \
  --python "$PWD/workspace/phase1/control-venv/bin/python" \
  --run-uid "$PHASE1_RUN_UID" --run-gid "$PHASE1_RUN_GID" \
  --workspace "$PWD/workspace/phase1-validation"
```

Execute o runner sob o usuário dedicado (`sudo -u phase1runner ...`); ele se
recusa a iniciar quando UID/GID efetivos não correspondem aos argumentos. Assim,
workspace, HOME temporário, dependências e arquivos de controle pertencem ao
mesmo usuário sem segredos.

O runner executa os controles de UID/grupos, loopback, saída externa e socket
Docker; testa duas coletas, fixture positiva com `--setup-only`, servidor local,
fixture dependente de serviço externo e teste assíncrono. Os comandos pytest são
todos enviados pelo mesmo launcher. Qualquer controle reprovado resulta em
código de saída diferente de zero e impede a liberação do piloto. O relatório é
salvo em `workspace/phase1-validation/validation/isolation_validation.json`.

`sudo_unshare.sh` também cria namespaces PID e monta `/proc` dentro do namespace.
O processo permanece com o UID não privilegiado. O controle Docker verifica
conexão real ao socket; limpar grupos, por si só, não é considerado prova de
isolamento do socket.

## Smoke check do launcher dinâmico

`scripts/phase1_dynamic_launcher.py` no modo normal primeiro executa
`phase1_runner.py`; se qualquer controle falhar, não clona nem executa projeto.
Com os controles aprovados, clona apenas o primeiro projeto da amostra (ou o
`--project-id` indicado), fixa o SHA e tenta instalar dependências. Cada estratégia usa um
`.venv_attempt_N` novo e um cache `uv` separado, sob o usuário dedicado e com
timeout de 20 minutos. A ordem é `.[test]`, `.`, `requirements-test.txt`,
`requirements-dev.txt` e `requirements.txt`, considerando somente manifests
presentes. Após instalar o projeto, instala as ferramentas de coleta; a primeira
tentativa completa é renomeada para `.venv_ready`. Tentativas malsucedidas são
classificadas no JSON e seus ambientes descartados.

Com `.venv_ready`, executa um `--setup-plan` isolado com
`--continue-on-collection-errors`. A saída estruturada fica em
`dynamic_launcher_smoke.json` e o probe em `<project-id>-setup-plan.json`. O
checkout temporário é removido ao fim do processo.

Este smoke check instala dependências e faz o mapeamento inicial, mas ainda não
é a execução completa do piloto: não roda as duas execuções de fixtures, não
compara `--setup-only` com execução normal e não valida o corte K=3. Falhas de
instalação são registradas como `dependency_unresolvable`, separadas das falhas
de coleta e de isolamento.

Para testar somente a cascata de instalação de um projeto, use
`--install-smoke-only`. Esse modo mantém a instalação aberta à rede, exige o UID
dedicado sem grupo Docker, não executa os controles nem testes do projeto, e
preserva o clone e `.venv_ready` em `--artifacts/install-smoke/<project-id>/`.
O relatório `install_smoke_<project-id>.json` contém a estratégia vencedora, a
saída de `uv pip freeze`, a validação de que o ambiente promovido ainda importa
pytest e o resultado do teste de escrita no diretório. Use-o somente para um
projeto por chamada antes de liberar a fila completa.
