# Resultados e artefatos do piloto

## Onde o executor grava os JSONs

O padrão do launcher é `/var/tmp/phase1-artifacts-<UID>`. Nesta máquina, com o
UID 997, o diretório é:

```text
/var/tmp/phase1-artifacts-997/
├── pilot_report.json
├── pilot_progress.json
├── setup_only_comparison_cohort.json
├── single_project_report.json
├── install_smoke_WhisperLiveKit.json
└── pilot/<projeto>/<SHA>/results/*.json
```

O caminho pode ser alterado com `--artifacts`. Em `pilot/<projeto>/<SHA>/` ficam
também o clone e o ambiente virtual; os JSONs incluem checkpoints e saídas por
fase, como coletas, `setup-plan`, execuções normais, `setup-only` e comparação
K=3.

Na inspeção local de 14/11/2026 havia 4.250 JSONs, totalizando cerca de 3,91 GB.
Os artefatos de `OpenSRE` são excepcionalmente grandes: um `project_result.json`
tem cerca de 1,56 GB. A quantidade e os tamanhos mudam conforme novas execuções
são feitas.

## Estado dos relatórios agregados atuais

Não trate o nome do arquivo ou o campo `status` isoladamente como prova de que o
piloto terminou. Na inspeção acima, `pilot_report.json` tinha
`status="pilot_complete"`, mas continha apenas um projeto
(`SuperClaude_Framework`). `pilot_progress.json` também listava somente esse
projeto. Esses relatórios não representam a conclusão dos 30 projetos.

`single_project_report.json` contém uma execução isolada do
`WhisperLiveKit`; `install_smoke_WhisperLiveKit.json` documenta a instalação e
não substitui a execução completa do piloto. A interpretação dos resultados
parciais está em [`PARTIAL_PILOT_ANALYSIS.md`](PARTIAL_PILOT_ANALYSIS.md).

## O que versionar e o que arquivar

Versione no Git os dados pequenos, revisados e necessários para entender a
análise: amostra, auditoria, pré-registro, código, relatório parcial e, quando
gerado a partir de resultados validados, um resumo compacto em CSV/JSON com
manifesto de SHAs e fingerprint do tooling. Registre também o `pip freeze`, a
versão do `uv`, a versão do Python, o commit do tooling e dados básicos do host;
guarde-os junto ao pacote de artefatos para comparar ou reproduzir em outra
máquina.

Não adicione os 4.250 JSONs brutos em um commit comum. Preserve-os integralmente
em armazenamento compartilhado apropriado ao projeto (por exemplo, drive
institucional ou armazenamento privado), ou em um arquivo externo com checksum.
Antes de compartilhar, revise saídas de testes para remover segredos, dados
pessoais e caminhos locais. Não apague a cópia original até validar a cópia e
seus checksums.

Para criar um manifesto dos JSONs sem copiá-los:

```bash
find /var/tmp/phase1-artifacts-997 -type f -name '*.json' -print0 \
  | sort -z \
  | xargs -0 sha256sum > /var/tmp/phase1-artifacts-997-jsons.sha256
```

O manifesto registra a integridade dos arquivos, não substitui o backup dos
artefatos. Para analisar em outra máquina, transfira o diretório de artefatos
completo ou o subconjunto documentado, junto com este manifesto e o commit do
tooling correspondente. Para retomar execuções, preserve também o checkpoint e
os resultados por projeto; ambientes virtuais não são portáveis entre sistemas
e podem precisar ser recriados.

O `.gitignore` ignora as áreas locais convencionadas (`workspace/`,
`phase1/results/`, `phase1/artifacts/` e `artifacts/`), mas não ignora `*.json`
globalmente: JSONs pequenos e aprovados para publicação podem ser versionados.
