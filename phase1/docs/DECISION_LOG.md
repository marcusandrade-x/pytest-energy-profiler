# Registro de decisões metodológicas e operacionais

Este arquivo reúne decisões tomadas ao longo do planejamento e sua localização
nos artefatos. Ele complementa o pré-registro; **não altera nem substitui** a
versão 1 congelada em `65693ac` / tag `phase1-prereg-v1`.

## Decisões formais já congeladas no pré-registro v1

- **Perguntas:** RQ3 foi removida. RQ4 permanece exploratória, com atributos
  como `autouse`, `yield`, parametrização, dependências e tamanho do código.
- **Meta e limiar:** buscar pelo menos 250 projetos que cumpram o mínimo de
  fixtures utilizáveis. Escolher o limiar mais estrito entre 20%, 30% e 50% que
  alcance essa meta; se 50% não alcançar, reportar inviabilidade e não elevar X.
  Calcular X a partir do plano completo de `--setup-plan`, antes de energia.
- **Falha e intermitência:** falha de setup ou teardown em qualquer das duas
  execuções torna a fixture falha; falha em apenas uma também é marcada como
  intermitente. Cada item de teste perdido conta uma vez, mesmo se depender de
  várias fixtures falhas.
- **Coleta e grafo:** habilitar `--continue-on-collection-errors` e manter
  módulos com erro de coleta em categoria própria, estimando testes perdidos por
  contagem AST. Dependências dinâmicas (`request.getfixturevalue`) vêm da
  execução. A junção estática/dinâmica é por `(arquivo, linha, nome)`.
- **Repetibilidade:** verificar IDs em duas coletas, desativar plugins de ordem
  aleatória e versionar a lista de desseleção por projeto e commit. Assinaturas
  de erro incluem o frame mais interno fora do pytest.
- **Python e K=3:** usar a primeira versão Python compatível que satisfizer a
  fração mínima de coleta reservada à v2, sem escolher pela taxa de sucesso de
  fixtures. Avaliar K=3 no piloto contra execução completa; X continua baseado
  no plano completo.
- **Isolamento:** bloquear rede externa nas Fases 1, 2 e 3, manter loopback e
  verificar UID/grupos e inacessibilidade ao socket Docker. Instalação ocorre
  separadamente, como usuário dedicado sem segredos nem grupo Docker. Não
  executar projetos se qualquer controle falhar.
- **RQ2 e validade:** escopo é fator da comparação; energia por instância,
  energia total de fixtures por suíte e energia amortizada por uso serão
  reportadas. Escopos com menos de 20 projetos ficam descritivos. Reportar viés
  de sobrevivência em favor de bibliotecas simples e com menos dependências
  externas.
- **Parâmetros da v2:** somente o mínimo de fixtures utilizáveis por projeto e a
  fração mínima de coleta podem ser calibrados após o piloto, sempre antes da
  varredura completa e sem observar energia.

Os detalhes completos e os critérios de aceite permanecem em
[`PREREGISTRATION.md`](PREREGISTRATION.md).

## Decisões específicas da amostra piloto

- Selecionar 30 projetos entre os 417 elegíveis, seed 42, usando `num_tests` do
  inventário inicial como heurística ordinal de tamanho para distribuir os
  quartis. O valor não é apresentado como contagem atual no SHA catalogado.
- Priorizar projetos com fixtures `module` ou `session`. A seleção resultou em
  22/30 (73,3%) com esses escopos. É amostragem intencional para testar o
  tooling e escopos de maior duração; não sustenta inferências de prevalência
  nem estimativas populacionais.
- As diferenças entre contagens antigas e atuais são registradas sem tratar uma
  implementação como verdade de referência. A mineração antiga também usa AST;
  evolução temporal é explicação plausível, não quantificada porque faltam SHAs
  do inventário antigo.
- `needle` mantém a decisão de aceitação da divergência 16→0 após revisão e
  `--acknowledge-zero-fixture needle`. A inspeção manual relatada classificou o
  caso como incompatibilidade de parser com código legado; o catálogo não
  guardou o erro de sintaxe, portanto essa causa não é reproduzível pelos
  artefatos. O SHA catalogado é
  `3656918e1eff1aa252cae2915d9a096d71742f40`.

Os dados, ressalvas e contagens dessa decisão estão em
[`PILOT_SAMPLE_RATIONALE.md`](PILOT_SAMPLE_RATIONALE.md). A amostra e sua
auditoria ficam em `../data/pilot/pilot_sample.csv` e
`../data/pilot/pilot_sample_audit.csv`.

## Estado do pipeline

- Catálogo estático: concluído para 417 projetos elegíveis; 86.647 definições
  de fixture registradas.
- Amostra: gerada e validada estruturalmente — 30 IDs distintos, SHAs válidos e
  consistentes com auditoria/catálogo, quartis 8/8/7/7.
- Isolamento: os três controles foram aprovados no preflight executado em
  13/11/2026. Revalidar após mudanças no host antes da próxima execução.
- Piloto: iniciado e interrompido antes de consolidar os 30 projetos. Os
  artefatos parciais estão em `/var/tmp/phase1-artifacts-997/`; a leitura
  preliminar está em [`PARTIAL_PILOT_ANALYSIS.md`](PARTIAL_PILOT_ANALYSIS.md).
- Validade do piloto: seis projetos têm duas execuções completas utilizáveis;
  `deepeval` expirou, `pytorch-image-models` sofreu OOM e `OpenSRE` teve falha
  do probe ao resolver caminhos. O status legado de completo não é aceito
  nesses três casos.
- Correções locais não commitadas: controle de códigos de saída e timeout,
  saída de subprocesso limitada, checkpoints compactos/atômicos, expansão
  AST de parametrizações literais e uso de `PosixPath` no probe.
- Cache do `uv`: limpo; verificação local mostrou 4 KB no diretório de cache e
  aproximadamente 25 GB livres em `/var/tmp`.
- Conferência de `SuperClaude_Framework`: preparada, mas não iniciada pelo
  executor nesta sessão porque `sudo -u phase1runner` requer autenticação em
  terminal interativo. Rodar manualmente no terminal do responsável.
- Pré-registro v2, congelamento final do tooling e conclusão do piloto:
  pendentes. Não iniciar a varredura completa nem as medições de energia antes
  desses marcos.
- RQ2: medições de energia ainda não realizadas.
