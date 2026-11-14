# Justificativa e limites da amostra do piloto

**Data do registro:** 2026-11-13
**Estado:** decisões documentadas antes da execução do piloto; amostra validada,
aguardando commit de congelamento.
**Amostra:** `../data/pilot/pilot_sample.csv`, 30 projetos, seed 42, selecionados
entre 417 projetos elegíveis. O pré-registro v1 permanece congelado e não é
alterado por esta nota.

## Diferenças entre a mineração original e o catálogo atual

O inventário original totaliza 67.273 fixtures e o catálogo AST atual totaliza
86.647. Em 239 dos 417 projetos as contagens diferem; em 232 desses casos o
catálogo atual encontrou mais definições. A diferença absoluta mediana é 3 e a
maior é 1.549 (`unsloth`).

A evolução dos repositórios entre os dois snapshots é uma explicação plausível,
especialmente para projetos de crescimento rápido. O inventário original não
registra o SHA de cada repositório, então não é possível atribuir ou quantificar
essa parcela da diferença. As contagens também vêm de implementações distintas
do minerador.

**Correção metodológica:** a mineração original em `scripts/analyzer.py` também
usa AST para contar funções decoradas como fixtures; portanto, a diferença não
deve ser explicada como “regex antigo versus AST novo”. O minerador original usa
uma heurística pelo nome do decorator, enquanto o catálogo atual resolve aliases
com mais contexto e registra definições individuais. Não se presume que uma
implementação seja uniformemente mais completa. As discrepâncias ficam
registradas em `../data/pilot/pilot_sample_audit.csv`; os valores antigos não serão tratados
como verdade de referência para validar o parser atual.

## Caso `needle`

O inventário original registra 16 fixtures para `needle`; o catálogo atual não
encontrou definições. Segundo a inspeção manual relatada pelo pesquisador, o caso
foi classificado como incompatibilidade do parser com código legado (por
exemplo, sintaxe Python 2). O projeto foi mantido na amostra após revisão
explícita e uso de `--acknowledge-zero-fixture needle`.

A rotina de leitura AST ignora arquivos que levantam `SyntaxError`, mas não
registra o arquivo nem o erro. Portanto, a classificação acima é a conclusão
da inspeção manual relatada pelo pesquisador, não um diagnóstico reproduzível a
partir dos artefatos atuais. A execução dinâmica poderá revelar falhas de
importação ou coleta, que serão registradas segundo o protocolo. O manifesto
registra `catalog_status=ok` e o SHA Git
`3656918e1eff1aa252cae2915d9a096d71742f40` para `needle`.

A primeira geração da amostra retornou sucesso com `unknown` como SHA de
`needle`, devido a validação insuficiente. Após a correção do SHA, a amostra e a
auditoria foram geradas novamente. A validação estrutural confirmou 417 projetos
na auditoria, 30 projetos distintos na amostra, SHAs válidos e coincidentes
entre amostra, auditoria e catálogo, seed 42 e distribuição 8/8/7/7 entre os
quartis. O gerador agora exige hashes Git completos de 40 ou 64 dígitos
hexadecimais. A amostra está pronta para congelamento em commit; nenhum projeto
do piloto foi executado.

## Base dos quartis de testes

Os quartis do piloto usam `num_tests` do inventário original como **heurística
ordinal de tamanho**. Seu objetivo é distribuir projetos de tamanhos distintos
no piloto, e não fornecer uma contagem absoluta de testes para o SHA atual. O
recálculo por commit foi dispensado nesta etapa; por isso, os rótulos de quartil
não devem ser interpretados como contagem atual de itens coletáveis. As
contagens exatas serão obtidas na coleta de viabilidade segundo o protocolo.

## Amostragem intencional e limite de inferência

Dos 30 projetos selecionados, 22 (73,3%) contêm fixtures `module` ou `session`.
Essa concentração é intencional: a amostra é enriquecida para exercitar
fixtures de escopo amplo e testar sua medição, inclusive em cenários com tempo
de vida maior. A seleção também serve à calibração do tooling do piloto.

Trata-se de **amostragem intencional (purposive sampling)**, não de amostragem
representativa da população. A composição por escopo não será usada para inferir
prevalência nem para estimar efeitos populacionais. As conclusões do piloto
ficam limitadas à validação operacional e à calibração previamente reservada.

## Congelamento

Antes de executar projetos, revisar e versionar em conjunto esta justificativa,
`../data/pilot/pilot_sample.csv`, `../data/pilot/pilot_sample_audit.csv`, o catálogo e o manifesto de status,
registrando os SHAs e a seed usados. A amostra não altera X, os limiares de X,
nem os critérios de viabilidade congelados no pré-registro v1.
