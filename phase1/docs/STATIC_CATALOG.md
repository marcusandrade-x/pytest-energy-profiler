# Catálogo estático de fixtures

O catálogo usa AST para inspecionar definições de fixtures sem instalar ou
executar código dos repositórios. A entrada atual para gerar a amostra é
`phase1/data/catalog/fixture_catalog_batch.csv`, com SHA por projeto. O manifesto
`phase1/data/catalog/catalog_project_status.csv` registra projetos sem linhas
de fixture e completa a junção com os elegíveis.

## Gerar ou atualizar

Na raiz do repositório:

```bash
python3 scripts/phase1_catalog_batch.py \
  --input input_data/mined_projects.csv \
  --out-dir phase1/data/catalog
```

O script grava `fixture_catalog_batch.csv`. Revise o resultado e atualize o
manifesto de status para os projetos que não tenham definições. Não use uma
execução com `--limit` como catálogo final. O catálogo completo e o manifesto
devem representar o mesmo conjunto de commits.

`scripts/phase1_catalog.py` é uma implementação anterior de catalogação e
grava `fixture_catalog.csv`; o snapshot correspondente está preservado em
`phase1/data/catalog/archive/legacy/` e não deve substituir o catálogo atual.
O recorte em `archive/recovery/` também é parcial. A proveniência desses arquivos
está descrita em `phase1/data/catalog/archive/README.md`.

## Gerar e revisar a amostra

O gerador usa os caminhos padrão em `phase1/data/catalog/` e grava a seleção e a
auditoria em `phase1/data/pilot/`:

```bash
python3 scripts/generate_pilot.py --acknowledge-zero-fixture needle
```

Antes de congelar uma nova amostra:

1. Compare a contagem AST com `num_fixtures` da mineração e investigue diferenças
   relevantes; os métodos e snapshots não são uma verdade de referência única.
2. Confira se cada projeto tem SHA consistente entre catálogo, manifesto,
   amostra e auditoria.
3. Revise os escopos `module`/`session`, os desconhecidos/dinâmicos e a
   distribuição por quartis. A priorização dos escopos amplos torna o piloto
   intencional, não representativo da população.
4. Mantenha erros de clone, parse e projetos sem fixtures visíveis na auditoria;
   não os converta silenciosamente em projetos com catálogo vazio válido.
5. Versione amostra, auditoria, catálogo/manifesto usados e justificativa juntos
   antes de executar o piloto.

A rotina de catálogo não demonstra viabilidade de coleta, sucesso de fixtures,
isolamento de rede ou consumo de energia. Essas medidas dependem das etapas
dinâmicas posteriores.
