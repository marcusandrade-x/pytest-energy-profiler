# Resumo para reunião e plano de trabalho

## Roteiro oral

O projeto investiga como o escopo das fixtures do pytest se relaciona com o custo de preparação e limpeza dos testes e, depois, com o consumo de energia. A RQ3 foi retirada; a RQ4 permanece exploratória. A etapa atual valida o método e o executor antes da coleta completa.

Partimos de 417 projetos elegíveis. O catálogo atual percorre o código Python por AST, sem instalar nem executar os projetos, para identificar fixtures e testes. Isso evita contar texto em comentários como fixture, mas não garante captura de todo padrão dinâmico ou arquivo incompatível com o parser. Falhas de parse e divergências foram registradas; o caso `needle` foi revisado manualmente, mas a causa não ficou reproduzível no CSV.

Selecionamos 30 projetos priorizando fixtures `module` e `session` (22/30, 73%). Os quartis de tamanho usam `num_tests` do inventário antigo como heurística ordinal: o valor não foi recalculado para os SHAs atuais e não é LOC nem contagem AST atual. O rótulo `AST_TEST_Q` da amostra é impreciso e precisa ser corrigido ou explicado antes de uma nova versão da amostra. A seleção é intencional para estressar o tooling e não permite estimar prevalências da população.

O launcher faz clone completo, fixa o checkout no SHA registrado e tenta instalar dependências em ambientes `.venv_attempt_N`; o ambiente bem-sucedido é promovido a `.venv_ready`. A instalação tem rede e ocorre como usuário dedicado, sem segredos nem grupo Docker. Os testes passam por namespace de rede com loopback, sem saída externa, sob UID/GID não-root. O preflight validou também a inacessibilidade ao socket Docker. O clone atual não é raso.

No piloto, o launcher coleta duas vezes para comparar IDs, executa `--setup-plan`, compara `--setup-only` com execução normal numa coorte de seis projetos, faz duas execuções normais e tenta K=3 após falhas idênticas. O piloto foi interrompido antes de concluir os 30 projetos. Seis projetos têm duas execuções utilizáveis; também houve projetos sem itens coletados, timeout, OOM, dependências de rede/serviços e uma falha interna do probe. K=3 não acionou nos dois projetos com comparação utilizável, portanto seu benefício ainda não foi validado. Não há medições de energia do piloto.

O profiler e o orquestrador já existem em `tools/fixture_energy_profiler.py` e `scripts/energy_orchestrator.py`; falta validar a integração operacional e a calibração antes de medir a população.

## O que foi feito e por quê

| Trabalho | Como | Por quê / estado |
|---|---|---|
| Mineração e catálogo | Inventário de 417 candidatos; catálogo AST em checkout identificado por SHA, com 86.647 definições de fixture | Estimar estrutura e escopos sem executar código; parser e construções dinâmicas continuam sendo limites |
| Amostra piloto | 30 projetos; quartis de `num_tests` antigo como proxy ordinal e prioridade a `module`/`session`; 22/30 têm esses escopos | Estressar casos variados; a amostra é intencional e o rótulo `AST_TEST_Q` não descreve a base usada |
| Isolamento | Instalação separada; execução em namespace de rede/PID, loopback ativo, saída externa bloqueada, socket Docker inacessível e identidade dedicada | Reduzir acesso à rede e privilégios durante testes de terceiros; a instalação continua online |
| Piloto e auditoria | Duas coletas, plano, comparação `setup-only`, duas execuções normais e K=3; 16 checkpoints parciais auditados | Medir estabilidade e descobrir perdas; status legado `complete` não basta para validar um resultado |
| Correções locais | Códigos de saída/timeout, saída limitada, checkpoints compactos/atômicos, parametrizações AST literais e probe com `PosixPath` | Evitar falso sucesso, crescimento excessivo dos artefatos e erro interno visto em `OpenSRE` |

O analisador original em `scripts/analyzer.py` também usava AST para contar fixtures por nome de decorator. O catálogo atual resolve aliases com mais contexto e registra cada definição. Portanto, não apresentar o avanço como “Regex substituído por AST”; comparar os dois métodos como heurísticas AST de alcance diferente. As regexes de `repo_miner.py` servem para evidências de pytest em metadados, não para provar fixture definitions.

Os artefatos por fase são JSONs nomeados pelo `run_id`, além de `static_inventory.json`, `project_checkpoint.json` e `project_result.json`. Não há um `collect_report.json` padrão nem arquivos `.txt` dedicados para cada `--setup-plan`; a saída textual do plano fica no JSON da execução. O relatório parcial está em [`PARTIAL_PILOT_ANALYSIS.md`](PARTIAL_PILOT_ANALYSIS.md).

## Divisão para três pessoas

| Responsável | Atividades | Entregável / aceite |
|---|---|---|
| **Você — líder e infraestrutura** | Publicar código e dados pequenos revisados; operar sua máquina serialmente; revisar helper root-owned e controles; coordenar fingerprints, congelamentos e v2; validar profiler/calibração existentes e planejar medições RAPL | Versão do código/amostra/pré-registro identificada; piloto corrigido concluído; calibração aprovada antes da coleta de energia |
| **Pessoa 2 — viabilidade e dados** | Criar parser dos JSONs por `run_id` e checkpoints compactos; consolidar coleta, erros de importação, timeout, OOM e dependências de serviço; comparar `setup-only` e normal sem atribuir causalidade sem evidência | Tabela por projeto/SHA com coletas, itens estimados/coletados, status e categoria de perda; contagens reproduzíveis |
| **Pessoa 3 — topologia e K=3** | Reconstituir closures do `--setup-plan` e mapa runtime; validar K=3 em controle positivo contra execução completa; verificar testes dependentes e não relacionados | Após três falhas idênticas, dependentes posteriores são abortados e testes não relacionados seguem; comparação completa/K=3 reproduzível, sem alegar benefício populacional a partir de dois casos |

As análises podem ocorrer em paralelo; a execução de projetos deve permanecer serializada na máquina do líder. Antes de compartilhar, não usar `git add .`: manter fora do Git caches, clones, venvs, progresso de 2 GB, resultados brutos volumosos e caminhos locais. Publicar primeiro código, testes, amostra, pré-registro e relatórios revisados. Armazenar JSONs brutos em local privado e versionado após revisar tamanho e conteúdo.

## Sequência e portões de aceite

1. **Compartilhar de forma controlada:** commitar arquivos revisados do projeto, amostra/auditoria, pré-registro, testes e documentação. Revisar permissões, caminhos locais e dados volumosos antes do push.
2. **Conferir o tooling:** Pessoa 2 revisa e roda os testes; Pessoa 3 audita a amostra e o denominador AST. Você revalida os três controles no launcher real e executa `SuperClaude_Framework` em primeiro plano. Aceite: duas coletas estáveis, plano, comparação normal/`setup-only`, duas execuções encerradas, K=3, checkpoints compactos e nenhum erro do probe.
3. **Congelar tooling do piloto:** atualizar o helper root-owned se o código de isolamento mudar; registrar commit e fingerprint. Tratar resultados antigos como preliminares. Reexecutar os 30 projetos serialmente, monitorando memória e disco.
4. **Fechar piloto e pré-registro v2:** reportar funil, discordância, catálogo de erros e teste positivo de K=3. Definir somente os parâmetros reservados (mínimo de fixtures utilizáveis e fração mínima de coleta); commitar/taguear v2 antes da varredura final.
5. **Varredura de viabilidade:** analisar candidatos elegíveis com SHA fixo, versão Python determinada pela regra congelada e isolamento. Separar módulos que falham na coleta. Depois, escolher X entre 20/30/50% para atingir 250 projetos; se nem 50% alcançar a meta, reportar inviabilidade. Calcular X antes de consultar energia.
6. **Energia e análise:** validar a calibração existente e a resolução temporal no host; então executar profiler/orquestrador com isolamento. Reportar RQ1, RQ2 (energia por instância e total por suíte) e RQ4 exploratória, com perdas, viés de sobrevivência e escopos sem amostra suficiente.

## Ação operacional pendente

O cache já foi limpo. A conferência ainda precisa ser iniciada no terminal interativo do responsável, pois esta sessão não consegue autenticar `sudo`. O comando atual faz clone completo, fixa o SHA da amostra e reutiliza `.venv_ready` se estiver válido. Rode em primeiro plano:

```bash
sudo -u phase1runner /usr/bin/python3 scripts/phase1_dynamic_launcher.py \
  --pilot --project-id SuperClaude_Framework \
  --isolation-launcher-json "[\"$PWD/phase1/isolators/sudo_unshare.sh\"]" \
  --python /usr/bin/python3 --run-uid 997 --run-gid 973 \
  --artifacts "/var/tmp/phase1-artifacts-$(id -u phase1runner)" \
  --uv /usr/local/bin/uv
```
