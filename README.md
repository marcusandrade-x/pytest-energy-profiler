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
│   ├── analyzer.py                  # Script de análise estática e extração AST
│   ├── repo_miner.py                # Minerador de repositórios do GitHub
│   └── energy_orchestrator.py       # Orquestrador de clones e execuções dinâmicas
├── tools/
│   └── fixture_energy_profiler.py   # Plugin profiler de energia para o pytest
├── input_data/
│   ├── mined_projects.csv           # Lista detalhada de repositórios pré-minerados da pesquisa
│   └── candidates.txt               # URLs brutas dos repositórios
└── workspace/                       # Diretório ignorado pelo Git (clones e medições)
```

## Tecnologias e Ferramentas
- Python 3.x
- [cloc](https://github.com/AlDanial/cloc) (Count Lines of Code)
- [CodeCarbon](https://codecarbon.io/) (Estimativa de emissões e energia)
- [pytest](https://docs.pytest.org/) (Framework de testes)
- Bibliotecas internas: `ast`, `subprocess`, `csv`, `requests`

## Como executar

O repositório já inclui o arquivo `input_data/mined_projects.csv` contendo os metadados dos projetos Python de código aberto selecionados para esta pesquisa. Você pode ir direto para a execução da medição de energia.

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
pip install requests pytest codecarbon pandas
```

### 3. Fluxo de Execução

**Campanha de Medição de Energia (Principal)**
Para reproduzir a medição de energia em larga escala sobre a base de projetos fornecida, execute o orquestrador. Ele clonará os repositórios, fará a filtragem de *baseline* e rodará as repetições de forma automatizada:
```bash
python scripts/energy_orchestrator.py rodar --workspace workspace --resultados input_data/mined_projects.csv --profiler tools/fixture_energy_profiler.py --reps 2 --jobs 2 --timeout-factor 8.0
```
*Nota: O parâmetro `--timeout-factor 8.0` garante uma margem de tempo segura para a injeção do profiler nos testes reais. Os resultados (arquivos `.csv`) serão gerados em `workspace/resultados/`.*

**(Opcional) Mineração de Novos Repositórios**
Caso deseje minerar novos repositórios para além do dataset atual, adicione as URLs no arquivo `input_data/candidates.txt` e execute o minerador estático:
```bash
python scripts/repo_miner.py
```