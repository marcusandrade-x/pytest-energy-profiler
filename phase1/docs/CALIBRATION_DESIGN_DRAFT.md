# Desenho em rascunho: elegibilidade e calibração energética

**Estado:** `K=3`, `p=0,90` e fração mínima de coleta `0,50` foram escolhidos
para constar na versão 2 do pré-registro. A v2 foi preparada, ainda precisa ser
versionada antes da varredura. Decisões da calibração energética continuam
abertas. O pré-registro v1 permanece intacto.

## 1. O que o piloto pode informar

O piloto fixo de 30 projetos pode informar a regra de elegibilidade pela
distribuição observada de fixtures: quantas definições cada projeto teve em uso,
quantas foram utilizáveis nas duas execuções e como esses números variam com o
tamanho da suíte. Como a amostra é intencional, esses dados servem para desenhar
e testar a regra; não estimam diretamente a taxa de elegibilidade dos 417
projetos.

O piloto não inclui medições CodeCarbon. Portanto, os parâmetros da regra devem
ser escolhidos sem resultados de energia. A triagem completa dos 417 projetos
estimará quantos passam pela regra já congelada.

## 2. Unidade e regra candidata de elegibilidade

Para cada projeto:

- `N`: número de definições distintas de fixture observadas durante as duas
  execuções normais. A identidade é `(arquivo, linha, nome)` no commit fixado.
  Usar a união das definições observadas nas duas execuções evita ocultar
  instabilidade.
- `U`: número dessas definições utilizáveis. Uma definição é utilizável se
  todas as suas instâncias observadas completarem setup e teardown sem erro nas
  duas execuções. Uma fase de teardown legitimamente ausente não é falha. Uma
  falha em apenas uma execução também marca a fixture como intermitente e não
  utilizável.
- `N = 0`: o projeto não satisfaz o requisito de fixtures utilizáveis.

Regra híbrida candidata:

```text
U >= min(N, max(K, ceil(p * N)))
```

`K` é o piso absoluto de definições utilizáveis; `p` é a fração mínima das
definições observadas. Para projetos pequenos, a regra exige todas as fixtures
observadas quando `N` é menor que o piso. Para projetos grandes, exige pelo
menos o piso ou a fração, o que for maior.

O piloto deve produzir uma tabela de sensibilidade para combinações candidatas
de `K` e `p`: projetos aprovados, distribuição por tamanho, escopos presentes,
projetos perdidos por falha/intermitência e cobertura de fixtures utilizáveis.
Os valores finais não devem ser escolhidos apenas para maximizar a retenção dos
30 projetos. A justificativa deve relacionar o piso à quantidade mínima de
observações que torna a sumarização por projeto interpretável e a fração à
cobertura das fixtures que o projeto realmente executa.

Grade exploratória pré-declarada para a primeira tabela: `K ∈ {1, 3, 5, 10}` e
`p ∈ {0.50, 0.75, 0.90, 1.00}`. `K=3` preserva como referência o piso usado
pelos helpers estáticos existentes; os outros pisos mostram regras mais
permissivas e mais exigentes. As quatro frações cobrem de metade até todas as
definições. Esta grade serve para comparar consequências, não escolhe
automaticamente a regra final.

O pré-registro v1 reservou o mínimo de definições utilizáveis para calibração
no piloto. A fração `p` é uma extensão da regra e deve ser explicitamente
justificada e congelada no v2, sem alterar silenciosamente o v1.

### Decisão de `K`/`p` e leitura do piloto

Decisão tomada em 16/11/2026: registrar `K=3`, `p=0,90` e a regra híbrida
acima na versão 2. A fórmula preserva suítes pequenas se todas as fixtures
observadas forem utilizáveis; para `N=0`, o projeto não passa. K é um piso
limitado a N, não uma exigência de pelo menos três fixtures para todo projeto.

Na amostra intencional de 30, 14 projetos tiveram duas execuções completas e
dados utilizáveis para N/U. Um tinha `N=0`; os outros 13 passaram com todos os
K testados e com `p=0,50`, `0,75` ou `0,90`. Com `p=1,00`, passaram 6. Os valores
de K não diferenciaram projetos porque o menor N positivo observado foi 12.
Esse resultado apoia rejeitar a exigência de cobertura perfeita, mas não estima
a elegibilidade dos 417 projetos. Os 16 projetos sem duas execuções completas
são perdas anteriores à regra N/U e não entram no denominador 13/14.

### Relatórios do piloto para escolher outros parâmetros

1. Calcular `N`, `U`, `U/N` e as falhas/intermitências por projeto.
2. Exibir a distribuição total e por estrato de tamanho da suíte; mostrar também
   function/module/class/session/package separadamente.
3. Aplicar uma grade de regras candidatas à mesma tabela e mostrar quantos
   projetos cada regra manteria. A grade e seus valores serão definidos antes
   de examinar os resultados do piloto.
4. Registrar a decisão `K=3`, `p=0,90` no v2 antes da varredura dos 417. A
   varredura confirma o número elegível; ela não serve para ajustar `K` ou `p`.

O número de definições utilizáveis por projeto é uma regra de elegibilidade.
O número de repetições necessário para estabilizar uma média energética é um
parâmetro distinto, calibrado na seção seguinte.

## 3. Objetivo da calibração de energia

Determinar empiricamente se a soma das medidas de eventos curtos repetidos
produz uma média por ciclo estável no backend e no intervalo de amostragem que
serão usados na campanha. Se `E_batch` for a energia atribuída ao lote e `R` o
número de ciclos completos, o estimador candidato é:

```text
energia média por ciclo = E_batch / R
```

Esse valor representa a média do lote, não uma medição precisa de cada
execução individual. A repetição só é válida se cada ciclo executar o lifecycle
normal do pytest; não se deve chamar a função fixture diretamente.

## 4. Matriz experimental proposta

Usar os workloads já existentes na suíte como primeira base: setup/teardown de
50, 100, 200, 400, 500 e 1000 ms, corpo de teste de 300 ms e controles sem
trabalho ou sem teardown. Acrescentar workloads mais longos de CPU e um caso de
I/O/cache controlado. Registrar duração medida com `perf_counter_ns`, além da
duração nominal.

Executar dois estados de cache explicitamente rotulados:

- **Aquecido:** preparação e uma execução de warm-up fora da medição; repetir
  com os mesmos dados e caminhos.
- **Estado de aplicação renovado:** novo processo e diretório temporário por
  ciclo, para renovar estado Python/aplicação e evitar reutilização deliberada
  de arquivos. O cache de páginas do sistema operacional permanece
  não-controlado; não chamar esse estado de “cache frio”.

O workload principal deve ser CPU-bound para isolar a resolução temporal do
CodeCarbon. O workload de I/O serve para medir a sensibilidade ao cache em um
experimento separado. Não limpar caches globais do host; isso é privilegiado,
afeta outros processos e não é reprodutível em todas as máquinas.

| Bloco | Condição | Repetição por pytest | Leitura |
|---|---|---|---|
| Resolução temporal | controle sem trabalho; ciclos curtos, médios e longos de CPU | function fixture parametrizada; ciclo completo setup/corpo/teardown | energia por evento, soma/média por lote, duração e proporção zero |
| Cache da aplicação | estado renovado por ciclo versus cache previamente aquecido | fixture normal; cache limpo ou preparado fora da janela medida | diferença entre os dois estados, sem atribuí-la a cache frio do SO |
| Escopo | function, module e session | function: várias instâncias; module/session: um lifecycle por contexto pytest novo | separar energia de cada setup/teardown da energia amortizada por uso |
| Controle de instrumentação | fixture sem trabalho e corpo de teste vazio | mesma quantidade de itens e mesmo runner | custo de base e energia não atribuída |

O bloco principal usa CPU-bound para que I/O e cache não confundam a pergunta
de resolução temporal. O bloco de cache é análise separada e só é comparado
dentro do mesmo estado/host.

## 5. Repetições, lotes e critérios a avaliar

- Uma repetição conta somente quando setup, corpo e teardown esperados forem
  executados. Gravar o número real de ciclos concluídos, não apenas o número
  solicitado.
- Repetir itens de escopo function por parametrização normal do pytest. Para
  escopos module/session, uma repetição exige recriar o contexto pytest que
  executa o lifecycle; vários usos do mesmo objeto module/session não contam
  como vários setups.
- Comparar a atribuição atual por evento com a soma por fixture de lotes de
  eventos repetidos na mesma execução. Variar o tamanho do lote para observar
  se a média por ciclo converge e se a fração de linhas com energia zero cai.
- Usar `test_name`/nodeid parametrizado para reconstruir o lote e contar somente
  lifecycles completos. Para cada fixture e fase, calcular a soma da energia
  medida e dividir pelo número real de eventos completos; preservar também a
  distribuição dos eventos individuais para mostrar a quantização escondida
  pela média.
- Definir os tamanhos de lote pelo tempo total desejado em relação ao intervalo
  de amostragem configurado; guardar ambos no resultado. O protocolo final deve
  fixar previamente um critério de estabilidade entre lotes independentes e
  uma tolerância, antes de observar os valores.
- Ponto de partida para a triagem: lotes com duração de pelo menos dez
  intervalos de amostragem e cinco lotes por condição. Com `Δ` como intervalo
  configurado e `t` como duração mediana observada de um ciclo, usar
  `R = ceil(10 * Δ / t)` ciclos por lote. Comparar também tamanhos menores e
  maiores para testar a convergência; esses valores iniciais não são critério
  final de aceite.
- Executar vários lotes independentes por condição, em série, com a mesma
  versão de Python, pytest, CodeCarbon, backend e intervalo de amostragem da
  campanha. Registrar hardware, modo de tracking, energia global, energia
  atribuída, duração, contagem de ciclos e diagnóstico de zero/baixa resolução.

Se a média por ciclo variar com o tamanho do lote, permanecer em zero ou seguir
quase apenas a duração sem distinguir os workloads de controle, a repetição
não será tratada como solução suficiente. Nesse caso, avaliar um modo de
medição em lote dedicado ou um contador/medidor independente antes de medir os
projetos reais.

## 6. Decisões ainda abertas para a calibração energética

- Tamanhos de lote, quantidade de lotes independentes e tolerância de
  estabilidade.
- Se a calibração será feita somente no host de referência da campanha ou em
  cada host que venha a ser usado. Resultados de máquinas distintas não serão
  combinados sem calibração e identificação explícitas do host.
- Se o modo atual por evento é suficiente ou se a suíte requer um modo de
  medição em lote adicional.

## Limite operacional

Não executar a suíte de calibração no mesmo host enquanto o piloto dinâmico
estiver ativo. O desenho e as alterações de código podem prosseguir em paralelo;
a execução da matriz ocorre depois do piloto ou em uma máquina separada.
