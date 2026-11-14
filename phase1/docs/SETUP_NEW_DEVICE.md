# Preparar outra máquina Linux

É possível analisar os CSVs e relatórios compactos em outro computador. Rodar o
piloto exige preparação adicional do host: namespaces de rede, conta dedicada,
helper root-owned e validação dos controles. O `requirements.txt` instala
dependências Python; ele não configura o isolamento do sistema.

## 1. Requisitos do host

O executor foi preparado para Linux. Em Debian/Ubuntu, instale os utilitários
necessários:

```bash
sudo apt update
sudo apt install git python3 python3-venv iproute2 util-linux sudo
```

`cloc` é necessário para a análise SLOC antiga, não para executar o piloto:

```bash
sudo apt install cloc
```

O host precisa oferecer `git`, `ip`, `unshare`, `setpriv` e `sudo` nos caminhos
usados pelo helper. A fase de clone/instalação precisa de rede; os testes dos
projetos só começam depois de o launcher comprovar que a execução isolada não
alcança a rede externa.

## 2. Obter o código e instalar dependências Python

Clone o commit revisado do repositório. Use a mesma versão do tooling, da amostra
e do pré-registro indicada no relatório da execução. O `uv` está listado em
`requirements.txt`; o executável ficará no ambiente virtual abaixo. Ao executar,
passe o caminho absoluto com `--uv` para não depender do `PATH` do `sudo`.

Crie um ambiente de tooling/control pertencente ao usuário de estudo e instale
as dependências enquanto a rede estiver disponível:

```bash
sudo useradd --create-home --shell /usr/sbin/nologin phase1runner
sudo -u phase1runner python3 -m venv /home/phase1runner/phase1-venv
sudo -u phase1runner /home/phase1runner/phase1-venv/bin/pip install \
  -r requirements.txt -r phase1/controls/requirements.txt
```

Se a conta já existir, não a crie novamente. Registre os valores locais de UID e
GID com `id phase1runner`; eles variam de máquina para máquina e não devem ser
copiados dos comandos de outra máquina. O usuário não pode ser root nem membro
do grupo `docker`. O workspace, o repositório e os caminhos de artefatos precisam
ser legíveis/graváveis conforme a função de cada arquivo.

`requirements.txt` cobre o tooling e o profiler. O launcher instala as
dependências específicas de cada projeto em ambientes descartáveis separados;
elas não devem ser adicionadas ao ambiente de tooling.

## 3. Configurar a bolha de rede

Um administrador instala o helper como root e autoriza somente esse helper no
`sudoers`:

```bash
sudo install -o root -g root -m 0755 \
  phase1/isolators/phase1-netns-root \
  /usr/local/sbin/phase1-netns-root
sudo install -o root -g root -m 0440 \
  phase1/isolators/phase1runner.sudoers \
  /etc/sudoers.d/phase1runner-netns
sudo visudo -cf /etc/sudoers.d/phase1runner-netns
```

O arquivo de `sudoers` pressupõe a conta chamada `phase1runner`. A regra permite
somente executar `/usr/local/sbin/phase1-netns-root`; não conceda `sudo`
genérico. O helper valida UID/GID em tempo de execução, cria namespaces de rede
e PID, ativa loopback e remove os grupos suplementares antes de iniciar o
processo de teste. Mantenha o helper instalado como root e não gravável pelo
usuário de estudo.

## 4. Validar antes de executar projetos

Na raiz do clone, obtenha os IDs locais e rode o preflight como a conta dedicada:

```bash
RUN_UID="$(id -u phase1runner)"
RUN_GID="$(id -g phase1runner)"
sudo -u phase1runner /usr/bin/python3 scripts/phase1_runner.py \
  --isolation-launcher-json "[\"$PWD/phase1/isolators/sudo_unshare.sh\"]" \
  --python /home/phase1runner/phase1-venv/bin/python \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --workspace "/var/tmp/phase1-validation-$RUN_UID"
```

Só continue se o relatório `isolation_validation.json` aprovar os controles de
identidade, loopback, bloqueio de rede externa e inacessibilidade ao socket
Docker, além dos controles pytest. Repita a validação se mudar o host, o helper
ou a configuração de permissões.

## 5. Executar ou retomar o piloto

Reserve armazenamento persistente suficiente para clones, ambientes virtuais e
JSONs. O conjunto bruto atual já ocupa cerca de 3,91 GB, sem contar clones e
venvs. Não use `/tmp` se ele for `tmpfs`; por padrão, o executor usa
`/var/tmp/phase1-artifacts-<UID>` para artefatos, cache do uv e Pythons geridos.
Esses caminhos podem ser ajustados na CLI.

Exemplo de chamada, usando os IDs desta máquina e valores obtidos localmente:

```bash
RUN_UID="$(id -u phase1runner)"
RUN_GID="$(id -g phase1runner)"
sudo -u phase1runner /home/phase1runner/phase1-venv/bin/python \
  scripts/phase1_dynamic_launcher.py \
  --pilot \
  --isolation-launcher-json "[\"$PWD/phase1/isolators/sudo_unshare.sh\"]" \
  --python /home/phase1runner/phase1-venv/bin/python \
  --run-uid "$RUN_UID" --run-gid "$RUN_GID" \
  --artifacts "/var/tmp/phase1-artifacts-$RUN_UID" \
  --uv /home/phase1runner/phase1-venv/bin/uv
```

Rode primeiro um projeto de validação conforme [`EXECUTOR.md`](EXECUTOR.md),
revise o relatório e só então inicie a amostra. Ao mover checkpoints entre
máquinas, confira commit/SHA, fingerprint do tooling, Python e opções da
execução. O launcher pode rejeitar checkpoints incompatíveis; não reutilize
venvs de outro host. Para a Fase 3, diferenças de hardware e sensores também
precisam ser calibradas e registradas.
