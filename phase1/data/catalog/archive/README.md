# Arquivo de catálogos

Os arquivos abaixo foram preservados para auditoria, mas não são a entrada
vigente do gerador de amostra:

- `legacy/fixture_catalog_unpinned.csv`: saída de uma execução anterior sem
  SHAs de commit confiáveis e com identificadores inconsistentes com a base
  atual.
- `recovery/fixture_catalog_batch_partial.csv`: recorte de recuperação com
  apenas dois projetos; não representa o catálogo completo.

Use `../fixture_catalog_batch.csv` com `../catalog_project_status.csv` para o
fluxo atual. Não concatene arquivos do arquivo ao catálogo atual: isso pode
duplicar definições ou misturar snapshots diferentes.
