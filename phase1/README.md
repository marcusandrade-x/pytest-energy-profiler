# Fase 1 — viabilidade e piloto

Este diretório reúne o protocolo, os dados estáticos revisados e os recursos de
execução isolada da Fase 1. Os scripts executáveis ficam em `scripts/`; seus
resultados de execução ficam fora desta árvore, em `workspace/` ou no caminho
passado em `--artifacts`.

## Mapa

```text
phase1/
├── README.md                 # Este índice e as convenções
├── docs/                     # Protocolo, decisões, análise e instruções
├── data/
│   ├── catalog/              # Catálogo AST e manifesto de status
│   │   └── archive/          # Snapshot antigo e recorte parcial preservados
│   └── pilot/                # Amostra e auditoria da seleção
├── controls/                 # Projetos de controle executados no preflight
└── isolators/                # Launchers e helper de isolamento Linux
```

Os testes unitários do código da Fase 1 ficam em `tests/phase1/`. Eles são
distintos de `phase1/controls/`: estes últimos são pequenos projetos pytest
executados dentro do isolamento para validar os controles experimentais.

## Documentos

- [`docs/PREREGISTRATION.md`](docs/PREREGISTRATION.md): protocolo e critérios
  definidos antes da varredura.
- [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md): decisões e estado conhecido.
- [`docs/STATIC_CATALOG.md`](docs/STATIC_CATALOG.md): catálogo AST e auditoria.
- [`docs/PILOT_SAMPLE_RATIONALE.md`](docs/PILOT_SAMPLE_RATIONALE.md): método e
  limites da amostra piloto.
- [`docs/EXECUTOR.md`](docs/EXECUTOR.md): comandos e critérios de aceite do
  executor.
- [`docs/PARTIAL_PILOT_ANALYSIS.md`](docs/PARTIAL_PILOT_ANALYSIS.md): análise
  parcial; não equivale ao relatório final do piloto.
- [`docs/RESULTS_AND_ARTIFACTS.md`](docs/RESULTS_AND_ARTIFACTS.md): localização,
  tamanho e política de preservação dos JSONs gerados.
- [`docs/SETUP_NEW_DEVICE.md`](docs/SETUP_NEW_DEVICE.md): preparação de outra
  máquina Linux, dependências e validação da bolha de rede.
- [`docs/MEETING_BRIEF.md`](docs/MEETING_BRIEF.md): resumo para apresentação e
  planejamento de atividades.
- [`isolators/README.md`](isolators/README.md): instalação e validação do
  isolamento de rede.

## Dados versionados

- `data/catalog/fixture_catalog_batch.csv`: catálogo AST com SHAs, usado como
  base atual para gerar a amostra.
- `data/catalog/catalog_project_status.csv`: status explícito de projetos sem
  linhas no catálogo; complementa o CSV acima.
- `data/pilot/pilot_sample.csv` e `pilot_sample_audit.csv`: seleção e trilha de
  auditoria correspondentes.
- `data/catalog/archive/legacy/fixture_catalog_unpinned.csv`: catálogo antigo
  sem SHAs, mantido apenas para referência histórica.
- `data/catalog/archive/recovery/fixture_catalog_batch_partial.csv`: recorte
  parcial de dois projetos; não combinar com o catálogo atual.

Os logs do catálogo foram movidos para `workspace/phase1/logs/`. Clones,
ambientes virtuais, checkpoints e JSONs de execução também pertencem ao
`workspace/` ou ao diretório externo configurado para os artefatos. Esses dados
locais não devem ser adicionados ao Git em lote.

## Fluxo de trabalho

1. Gerar ou atualizar o catálogo com `scripts/phase1_catalog_batch.py`; o
   diretório padrão é `phase1/data/catalog/`.
2. Revisar divergências e regenerar a seleção com
   `scripts/generate_pilot.py`; os caminhos padrão apontam para `data/catalog/`
   e `data/pilot/`.
3. Congelar amostra, protocolo e código antes de executar os projetos.
4. Validar os controles e executar o piloto com
   `scripts/phase1_runner.py` e `scripts/phase1_dynamic_launcher.py`. Os
   artefatos grandes devem ficar no caminho externo informado por
   `--artifacts`.
5. Analisar o piloto, registrar a v2 do pré-registro e só então preparar a
   varredura final.

Os caminhos padrão são ancorados na raiz do repositório. Assim, os comandos não
dependem de iniciar o terminal no diretório `phase1/`.
