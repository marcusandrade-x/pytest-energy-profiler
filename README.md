# CustoEnergicoFixtures

Framework de mineração de repositórios e medição dinâmica de consumo energético para suítes de teste Python (`pytest`). O projeto mantém métricas estáticas e inventário dinâmico de testes/fixtures por projeto, além de orquestrar medições empíricas de energia com o `CodeCarbon`.

## Funcionalidades
- **Mineração Estática:** SLOC com `cloc` e estimativas AST das funções de teste e declarações de *fixtures*.
- **Analyzer V2 independente:** coleta IDs reais de testes e fixtures registradas pelo pytest em duas coletas limpas, antes da Phase 1 e sem depender dela.
- **Orquestração Dinâmica:** Gerenciamento automático de clonagem em paralelo, criação de *venvs* isolados, instalação de dependências e filtragem de repositórios através de execuções de *baseline*.
- **Energy Profiler:** Plugin customizado para `pytest` que afere o consumo de energia segmentado (setup, test body, teardown) e amortizado por uso de *fixture*.
- **Isolamento de Dados:** Estrutura organizada para separar *scripts* de análise, dados de entrada e diretórios de execução pesada (ignorados no versionamento).

## Estrutura do Projeto
```text
CustoEnergicoFixtures/
├── scripts/
│   ├── analyzer.py                  # Métricas estáticas dos candidatos em candidates.txt
│   ├── analyzer_v2.py               # Inventário dinâmico independente por projeto
│   ├── analyzer_v2_probe.py         # Plugin de coleta do Analyzer V2
│   ├── repo_miner.py                # Descoberta opcional de candidatos no GitHub
│   ├── energy_orchestrator.py       # Orquestrador de clones e execuções dinâmicas
│   ├── phase1_catalog*.py            # Catálogo AST e preparação da amostra
│   ├── phase1_dynamic_launcher.py    # Executor do piloto de viabilidade
│   └── phase1_runner.py              # Preflight e execução sob isolamento
├── tools/
│   └── fixture_energy_profiler.py   # Plugin profiler de energia para o pytest
├── calibration_suite/                # Contratos e calibração do profiler
├── tests/phase1/                     # Testes unitários do tooling da Fase 1
├── requirements.txt                   # Dependências Python do tooling/profiler
├── docs/
│   └── projeto_ic_original.pdf       # Proposta que originou o estudo
├── phase1/
│   ├── README.md                     # Índice e convenções da Fase 1
│   ├── docs/                         # Pré-registro, decisões, análises e guias
│   ├── data/
│   │   ├── catalog/                  # Inventário AST e status dos projetos
│   │   ├── pilot/                    # Amostra e auditoria
│   │   └── catalog/archive/          # Snapshots antigos e parciais preservados
│   ├── controls/                     # Projetos de controle do piloto
│   └── isolators/                    # Launchers e helper de rede
├── input_data/
│   ├── mined_projects.csv           # Métricas estáticas e estimativas AST
│   ├── mined_projects_v2.csv        # Contagens dinâmicas para a medição
│   ├── candidates.txt               # URLs para analyzer.py
│   ├── mini_lote.csv                # Lote reduzido para validação manual do catálogo
│   └── github_pytest_search.csv     # Saída opcional de repo_miner.py
└── workspace/                       # Clones, logs e saídas locais; ignorado pelo Git
```

O mapa detalhado da Fase 1, incluindo os caminhos dos arquivos vigentes e o
fluxo de execução, está em [`phase1/README.md`](phase1/README.md). Saídas grandes
de execução ficam em `workspace/` ou no diretório de artefatos configurado para
a máquina, não junto ao código versionado. A proposta original está em
[`docs/projeto_ic_original.pdf`](docs/projeto_ic_original.pdf); os critérios
experimentais atuais estão no pré-registro da Fase 1.

## Tecnologias e Ferramentas
- Python 3.x
- [cloc](https://github.com/AlDanial/cloc) (Count Lines of Code)
- [CodeCarbon](https://codecarbon.io/) (Estimativa de emissões e energia)
- [pytest](https://docs.pytest.org/) (Framework de testes)
- Bibliotecas internas: `ast`, `subprocess`, `csv`, `requests`

## Como executar

O repositório inclui `input_data/mined_projects.csv`, o inventário estático legado. O Analyzer V2 gera um CSV separado com contagens dinâmicas e um sidecar de proveniência; ele não lê artefatos da Phase 1.

### 1. Preparação do Sistema
Instale o utilitário `cloc` no seu sistema operacional (necessário para a análise estática):
```bash
sudo apt install cloc  # Debian/Ubuntu/Mint
sudo dnf install cloc  # Fedora
```

### 2. Ambiente Virtual e Dependências
Crie o ambiente virtual e instale as bibliotecas necessárias:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Para preparar outra máquina Linux para a execução do piloto isolado, siga
[`phase1/docs/SETUP_NEW_DEVICE.md`](phase1/docs/SETUP_NEW_DEVICE.md). O arquivo
`requirements.txt` instala dependências Python; ele não instala os binários do
sistema, o usuário dedicado nem a configuração de namespaces/rede.

### 3. Fluxo de Execução

**Campanha de Medição de Energia (Principal)**
Gere primeiro o inventário dinâmico independente e depois execute o orquestrador:
```bash
python scripts/energy_orchestrator.py rodar --resultados input_data/mined_projects_v2.csv --workspace workspace --reps 2 --jobs 2 --timeout-factor 8.0
```
O CSV v2 mantém o schema de projeto existente. O orquestrador lê o SHA do sidecar para medir o mesmo commit que foi analisado.

**Geração do inventário dinâmico independente**
O Analyzer V2 é uma etapa anterior à Phase 1 e à medição de energia. Ele clona cada projeto, tenta perfis comuns de dependências de teste/desenvolvimento e executa duas coletas `pytest --collect-only`; não executa os testes nem mede energia:
```bash
python scripts/analyzer_v2.py \
  --input input_data/mined_projects.csv \
  --output input_data/mined_projects_v2.csv \
  --workspace workspace/analyzer_v2 \
  --workers 1
```
`num_tests` é preenchido quando duas coletas limpas retornam os mesmos IDs. `num_fixtures` conta definições do projeto registradas pelo pytest nas mesmas condições. Valores parciais ou instáveis ficam vazios no CSV principal e são explicados em `mined_projects_v2_provenance.csv`, junto com o SHA analisado. O Analyzer salva checkpoints por projeto e retoma com `--resume`. Requer `uv` no `PATH` e `cloc` instalado.

**(Opcional) Mineração de Novos Repositórios**
Para calcular as métricas estáticas de novos projetos, adicione uma URL GitHub por linha em `input_data/candidates.txt` e rode:
```bash
python scripts/analyzer.py
```
O `analyzer.py` grava/atualiza `input_data/mined_projects.csv` com as métricas estáticas/AST. O Analyzer V2 usa esse CSV e grava em arquivos separados. A descoberta automática de candidatos no GitHub é uma etapa opcional: `python scripts/repo_miner.py` grava `input_data/github_pytest_search.csv`; copie as URLs escolhidas para `candidates.txt` antes de analisá-las.

Durante as medições, o profiler registra quantas fixtures do projeto foram registradas pelo pytest, quantas chegaram a ser ativadas e quantas tiveram ao menos uma linha de medição com status `ok`. O orquestrador compara esses totais com `num_fixtures` do Analyzer V2 em `projects_status.csv` e `runs.csv`; assim é possível quantificar a cobertura dinâmica do conjunto que o Analyzer inventariou.

**(Opcional) Calibração do profiler**
Na pasta do projeto, execute a matriz de versões do pytest com:
```bash
./calibration_suite/contract_matrix.sh
```
Ela instala versões isoladas do pytest via `uv` e executa a suíte de calibração com o profiler em `tools/`.
