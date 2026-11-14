# Análise parcial do piloto

**Data do corte:** 13/11/2026
**Artefatos consultados:** `/var/tmp/phase1-artifacts-997/pilot/`
**Status:** preliminar; não substitui o relatório do piloto completo.

## Escopo e qualidade dos artefatos

Foram encontrados 16 arquivos `project_result.json`. Analisei 14 artefatos com até 100 MB. Nos dois maiores, extraí apenas campos e trechos específicos sem carregar os arquivos inteiros: `pytorch-image-models` (aprox. 120 MB) e `OpenSRE` (aprox. 1,56 GB). `pytorch-image-models` mostra duas execuções encerradas com código `-9`; não é uma medição completa. `OpenSRE` terminou seus processos, mas o plugin de instrumentação falhou repetidamente ao converter caminhos para `WindowsPath` em Linux (`UnsupportedOperation`). O status antigo `pilot_project_complete` não torna esse resultado válido.

## Funil observado

| Etapa nos 14 artefatos lidos | Projetos |
|---|---:|
| Duas coletas com IDs estáveis e pelo menos o limiar provisório de 50% | 7 |
| Duas execuções normais concluídas | 6 |
| Duas execuções concluídas sem falhas reportadas pelo pytest (código 0) | 2 |
| Duas execuções concluídas com falhas/erros de coleta reportados (código 1) | 4 |
| Execução normal interrompida por timeout | 1 (`deepeval`) |
| Coleta inviável, sem itens coletados | 7 |

Os seis projetos com duas execuções concluídas são `Douyin_TikTok_Download_API`, `SuperClaude_Framework`, `WhisperLiveKit`, `TradingAgents`, `OpenMontage` e `book-to-skill`. Código de saída 1 indica que o pytest terminou, mas reportou falhas ou erros; esses resultados são observações concluídas, não suítes aprovadas.

As sete coletas inviáveis foram `spleeter`, `uvicorn`, `evals`, `word_cloud`, `langflow`, `mvt` e `pytest`. Os códigos de saída foram 4 para cinco projetos, 1 para `evals` e 3 para `pytest`; nenhum item foi coletado. A estabilidade de dois conjuntos vazios não representa uma coleta bem-sucedida.

`deepeval` coletou 3.496 itens em duas coletas estáveis, mas as duas execuções normais atingiram o limite de 30 minutos. O executor marcou esse projeto como completo incorretamente. `pytorch-image-models` também recebeu indevidamente o status de completo após ambas as execuções serem encerradas pelo OOM killer. `OpenSRE` tem falhas do próprio probe; seus eventos não são confiáveis. Esses casos ficam fora das seis execuções completas utilizáveis.

## Execuções completas disponíveis

| Projeto | Itens coletados | Execução 1 (s) | Execução 2 (s) | Códigos pytest |
|---|---:|---:|---:|---|
| Douyin_TikTok_Download_API | 4.435 | 22,645 | 21,372 | 1 / 1 |
| SuperClaude_Framework | 139 | 0,598 | 0,613 | 0 / 0 |
| WhisperLiveKit | 373 | 105,355 | 103,118 | 1 / 1 |
| TradingAgents | 1.413 | 16,169 | 15,866 | 1 / 1 |
| OpenMontage | 2.517 | 140,752 | 138,020 | 1 / 1 |
| book-to-skill | 979 | 4,427 | 4,435 | 0 / 0 |

Os tempos são duração de execução observada pelo launcher, não energia. Eles não respondem à RQ2.

## Catálogo preliminar de falhas de fixtures

Entre os seis projetos completos, foram observadas nove assinaturas distintas de erro em três projetos. As ocorrências contam eventos de fixture somados nas duas execuções; não equivalem a testes únicos afetados.

- **Douyin_TikTok_Download_API:** PostgreSQL indisponível (1.326 eventos) e Redis indisponível (148 eventos, mais dois erros de conexão); também houve dois eventos por segredo obrigatório ausente. As falhas de conexão são compatíveis com os serviços locais não iniciados e com a rede externa bloqueada.
- **WhisperLiveKit:** quatro falhas da fixture que tenta baixar amostras de teste; o bloqueio de rede impede esse download.
- **OpenMontage:** dez eventos por `numpy` ausente, quatro por falta de renderer de diagramas, dois por arquivo de cenário dourado ausente e dois por vídeo de teste ausente.
- **SuperClaude_Framework**, **TradingAgents** e **book-to-skill:** nenhum evento de falha de fixture nos resultados consultados.

Esses casos mostram dependências de serviços locais, rede e recursos opcionais como categorias de perda distintas. Não devem ser tratados como falha do isolamento: os controles de rede passaram antes do piloto.

## Comparação `--setup-only` e K=3

Na coorte pré-fixada de seis projetos, apenas `WhisperLiveKit` e `SuperClaude_Framework` têm comparação completa utilizável nesta leitura. A discordância observada foi **0%** para IDs de testes e para fixtures configuradas nos dois projetos. O `--setup-only` não omitiu testes nem fixtures nesses casos.

O corte K=3 não abortou nenhum teste em nenhum dos dois projetos. A diferença de duração foi +2,204 s em `WhisperLiveKit` e -0,006 s em `SuperClaude_Framework`. Como não houve fixture que acionasse o corte, esses dados não validam o benefício do K=3; registram somente dois casos sem acionamento.

## Limitações que precisam ser resolvidas antes de usar o funil

1. Os dados do corte foram produzidos com a contagem antiga: definições AST de funções `test_*` no denominador e itens pytest no numerador. Parametrização pode fazer a razão passar de 1. O código agora expande parametrizações literais e sinaliza casos dinâmicos/`pytest_generate_tests`; os resultados antigos não foram recalculados e não entram como porcentagem interpretável.
2. `OpenSRE` não é válido por falhas no probe; `pytorch-image-models` foi encerrado pelo OOM killer. Não se deve abrir o JSON de 1,56 GB em editor ou carregá-lo inteiro na memória.
3. A amostra do piloto é intencional e prioriza escopos longos; há viés de sobrevivência em favor de projetos cujas dependências e testes sobrevivem à instalação, coleta e execução isolada.
4. Não há medições de energia nesses artefatos. A RQ2 depende das fases posteriores de profiling.

## Correções aplicadas ao executor

- Uma execução só é considerada concluída se não houver timeout e o processo terminar com código 0 ou 1. Timeout, sinal (incluindo OOM) e códigos 2 ou maiores são perdas explícitas; não recebem `pilot_project_complete`.
- A saída de subprocessos passa a ser drenada continuamente e limitada aos últimos 20 KB em memória, evitando acumular stdout/stderr sem limite.
- A contagem AST expande os valores literais de `pytest.mark.parametrize` e registra parametrização dinâmica e hooks `pytest_generate_tests` não expandidos.
- O probe usa `PosixPath` diretamente nos callbacks de fixture; isso evita a exceção de `WindowsPath` observada em `OpenSRE`.
- O arquivo de progresso passa a ser um índice compacto, gravado atomicamente, sem duplicar os resultados detalhados dos projetos.
- `project_result.json` e `project_checkpoint.json` passam a guardar resumos compactos. Os dados completos de cada fase continuam nos arquivos individuais gerados pelo plugin.
- A retomada lê checkpoints pequenos; para checkpoints antigos, procura apenas os campos de identificação/status linha a linha, sem carregar o JSON inteiro.

Os resultados existentes permanecem como análise preliminar. Como o fingerprint do tooling muda após essas correções, o executor não deve misturar silenciosamente checkpoints antigos e novos sob uma única versão de tooling.
