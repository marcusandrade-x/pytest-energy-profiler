# CustoEnergicoFixtures

Framework de mineração de repositórios e medição dinâmica de consumo energético para suítes de teste Python (`pytest`). O projeto automatiza a extração de métricas estáticas de qualidade de software e orquestra a medição empírica de energia em nível de *fixtures* e fases de teste utilizando o `CodeCarbon`.

## Funcionalidades
- **Mineração Estática:** Cálculo rigoroso de SLOC com `cloc` (separando código de produção e testes) e contagem estrutural de funções de teste e *fixtures* via AST, sem instanciar ambientes.
- **Orquestração Dinâmica:** Gerenciamento automático de clonagem em paralelo, criação de *venvs* isolados, instalação de dependências e filtragem de repositórios através de execuções de *baseline*.
- **Energy Profiler:** Plugin customizado para `pytest` que afere o consumo de energia segmentado (setup, test body, teardown) e amortizado por uso de *fixture*.
- **Isolamento de Dados:** Estrutura organizada para separar *scripts* de análise, dados de entrada e diretórios de execução pesada (ignorados no versionamento).

## Estrutura do Projeto
```text
CustoEnergicoFixtures/
├── scripts/
│   ├── analyzer.py                  # Métricas estáticas dos candidatos em candidates.txt
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
│   ├── mined_projects.csv           # Métricas estáticas consumidas pelo orquestrador
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

O repositório já inclui `input_data/mined_projects.csv`, com as métricas estáticas usadas pelo orquestrador. Os caminhos padrão dos scripts são ancorados na pasta do projeto, então não dependem do diretório atual do terminal.

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
Para reproduzir a medição de energia em larga escala sobre a base de projetos fornecida, execute o orquestrador. Ele clonará os repositórios, fará a filtragem de *baseline* e rodará as repetições de forma automatizada:
```bash
python scripts/energy_orchestrator.py rodar --workspace workspace --reps 2 --jobs 2 --timeout-factor 8.0
```
O CSV de entrada padrão é `input_data/mined_projects.csv` e o profiler padrão é `tools/fixture_energy_profiler.py`. Os resultados consolidados serão gerados em `workspace/resultados/`.

**(Opcional) Mineração de Novos Repositórios**
Para calcular as métricas estáticas de novos projetos, adicione uma URL GitHub por linha em `input_data/candidates.txt` e rode:
```bash
python scripts/analyzer.py
```
O minerador grava/atualiza `input_data/mined_projects.csv`, que é o formato consumido pelo orquestrador. A descoberta automática de candidatos no GitHub é uma etapa separada e opcional: `python scripts/repo_miner.py` grava `input_data/github_pytest_search.csv`, que contém evidências de uso do pytest, não as métricas estáticas. Copie as URLs escolhidas para `candidates.txt` antes de analisá-las.

**(Opcional) Calibração do profiler**
Na pasta do projeto, execute a matriz de versões do pytest com:
```bash
./calibration_suite/contract_matrix.sh
```
Ela instala versões isoladas do pytest via `uv` e executa a suíte de calibração com o profiler em `tools/`.
