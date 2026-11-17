# Pré-registro Fase 1 — versão 2

**Estado:** parâmetros reservados definidos em 16/11/2026, antes da varredura
completa e antes de qualquer medição de energia. A versão 1 permanece congelada
e intacta. Depois de versionada, esta versão não poderá ser alterada em função
dos resultados da varredura.

## Perguntas e escopo

- RQ1: custo de setup/teardown versus corpo do teste, na população filtrada pela
  viabilidade de fixtures.
- RQ2: associação observacional entre escopo de fixtures existentes e energia.
  Escopo não será alterado. Unidade independente para inferência aproxima-se do
  projeto, não do número de fixtures.
- RQ3 está removida. RQ4 é exploratória; `autouse`, `yield`, parametrização,
  dependências e tamanho do código serão catalogados como atributos/covariáveis.
- A Fase 1 não usa CodeCarbon. Fases 1, 2 e 3 bloqueiam rede externa e mantêm
  loopback, com as mesmas validações de isolamento.

## Regras congeladas

1. **Meta de projetos:** ao menos 250 projetos que cumpram o mínimo de fixtures
   utilizáveis definido na v2.
2. **Perda X:** calcular sobre os itens de teste coletados em `--setup-plan`.
   Cada item parametrizado conta separadamente; cada item perdido conta uma vez,
   mesmo que dependa de várias fixtures falhas. Os limiares candidatos são 20%,
   30% e 50%. Depois de toda a coleta de viabilidade e antes de qualquer dado de
   energia, escolher o limiar mais estrito que ainda alcance 250 projetos. Se
   50% não alcançar a meta, reportar inviabilidade e não elevar X. O piloto não
   pode ajustar essa regra nem escolher outro limiar.
3. **Perdas de coleta:** registrar módulos que falham na coleta como categoria
   própria e estimar os testes perdidos pela contagem AST do módulo. Não misturar
   essa estimativa ao denominador de X, que é baseado nos itens de `--setup-plan`.
4. **Fixtures utilizáveis:** uma fixture falha se setup ou teardown falhar em
   qualquer uma das duas execuções. Se falhar em somente uma, marcar também como
   intermitente. Erros de instalação não são falhas de fixture.
5. **Grafo:** usar o plano de fixtures por item e enriquecê-lo com as dependências
   observadas em execução, inclusive `request.getfixturevalue`. A junção do
   catálogo estático com execução é por `(arquivo, linha, nome)`.
6. **IDs e aleatoriedade:** coletar duas vezes antes da execução, exigir IDs
   estáveis ou explicar a diferença antes do congelamento; desligar plugins de
   ordenação aleatória. Versionar a lista de desseleção por projeto e commit.
7. **Erros:** a assinatura inclui classe, mensagem normalizada e frame mais
   interno fora do pytest. A taxonomia detalhada e a classificação de
   resolubilidade serão propostas após agrupamento dos dados e revisão humana.
8. **Coleta:** habilitar `--continue-on-collection-errors`. Até três versões de
   Python podem ser tentadas por projeto, em ordem determinística baseada na
   compatibilidade declarada. A primeira que instala e satisfaz a fração mínima
   de coleta definida na v2 será escolhida, sem seleção pela taxa de sucesso das
   fixtures. As duas execuções serão na mesma versão.
9. **Corte por repetição:** avaliar K=3 falhas idênticas da mesma fixture para
   interromper tentativas futuras de itens dependentes. Validar contra a
   execução completa no piloto. O cálculo de X permanece baseado no plano
   completo, sem early stop.
10. **Escopos:** um escopo entra na comparação principal RQ2 somente se houver
    pelo menos 20 projetos contribuindo. Escopos abaixo disso ficam descritivos;
    não serão agrupados automaticamente.
11. **Isolamento:** antes de cada execução, validar loopback funcional, saída
    externa bloqueada e socket Docker inacessível; registrar UID efetivo e grupos
    suplementares dentro do isolamento. Se qualquer controle falhar, não executar
    projetos. Instalação ocorre em etapa separada, como usuário dedicado, sem
    segredos e sem grupo Docker.
12. **Timeouts congelados:** 120 s por item de teste, 30 min por execução de
    projeto, 20 min por tentativa de instalação Python e 10 min para clone.
    Timeout permanece categoria separada de falha de fixture.
13. **Viés:** reportar como ameaça à validade que a amostra sobrevivente favorece
    bibliotecas simples, com menos dependências externas e menos requisitos de
    serviços locais.
14. **Energia RQ2:** reportar energia por instância e energia total de fixtures
    por suíte, além da energia amortizada por uso. Interpretar esta última com
    cautela, pois escopos amplos amortizam setup por mais usos por construção.

## Parâmetros definidos na versão 2

### Mínimo de fixtures utilizáveis

- Para cada projeto, `N` é a união das definições de fixtures próprias
  observadas em qualquer uma das duas execuções normais, identificadas por
  `(arquivo, linha, nome)`. Fixtures externas ao projeto são excluídas.
- `U` é a quantidade dessas definições que teve todas as instâncias observadas
  completarem setup e teardown sem falha nas duas execuções. Definições vistas
  em apenas uma execução entram em `N`, mas não em `U`.
- `N=0` não satisfaz o critério.
- O projeto passa quando
  `U >= min(N, max(3, ceil(0.90 * N)))`.
- Para `N<3`, todas as definições observadas precisam ser utilizáveis. A regra
  não exige três definições de fixture de projetos pequenos.

### Viabilidade da coleta dinâmica

- Uma versão Python é viável para a coleta quando a quantidade de itens
  coletados dividida pela estimativa de itens de teste por AST for pelo menos
  `0.50`.
- A coleta ainda deve ter IDs estáveis nas duas coletas. Erros de coleta são
  registrados separadamente e não são apagados por atingir a fração mínima.
- O valor `0.50` é um limiar de viabilidade e seleção de Python; não é o limiar
  X de perda de testes por falhas de fixture.

### Base empírica e limites

- Na amostra intencional de 30 projetos, 14 tiveram duas execuções completas e
  dados N/U; 13 passaram com `K=3`, `p=0.90`. Com `p=1.00`, passaram 6. O menor
  `N` positivo foi 12, então a amostra não distingue os pisos testados. Esses
  dados fundamentam a escolha, mas não estimam a taxa de elegibilidade dos 417.
- Os 16 projetos sem duas execuções completas são perdas anteriores à regra
  N/U; não foram contados como reprovados por ela.

Os dois parâmetros ficam congelados antes da varredura completa. Não podem ser
ajustados depois de examinar seus resultados nem após observar dados de energia.

## Desenho do piloto

- Cerca de 30 projetos; estratificar por quantidade AST de testes, não LOC,
  porque tempo de setup/execução cresce mais diretamente com os itens de teste.
- Dentro dos estratos, priorizar projetos com fixtures `module` ou `session`,
  mantendo projetos apenas `function` como parte da amostra.
- Incluir controle positivo conhecido, servidor local, dependência externa e
  testes assíncronos.
- Comparar `--setup-only` com execução normal em parte da amostra e reportar a
  discordância de IDs, fixtures executadas e falhas de teardown.
- Reportar a validação de K=3 contra a execução completa.
- O piloto não estima o funil final nem escolhe X; X só será calculado após a
  coleta de viabilidade completa, ainda antes de medições energéticas.

## Critérios de aceite antes da varredura completa

- Os três controles de isolamento passam; todos os controles de `phase1/controls`
  rodam dentro do mesmo launcher validado.
- IDs estáveis em duas coletas; causas de divergência documentadas.
- Erros, instâncias, fases, causas-raiz/consequências e perdas de coleta podem
  ser reconstruídos dos artefatos.
- Discordância `--setup-only`/normal e efeito de K=3 foram medidos no piloto.
- Tooling, dependências, amostra, v2 e este v1 estão versionados; a varredura
  completa usa os commits identificados.
