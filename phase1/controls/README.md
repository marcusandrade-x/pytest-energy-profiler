# Controles do piloto

Este diretório contém apenas fixtures de controle criadas para validar a Fase 1:

- `test_local_server_round_trip`: servidor HTTP ligado a `127.0.0.1`; deve passar
  em execução normal mesmo sem rede externa.
- `test_external_dependency_is_blocked`: a fixture tenta abrir uma conexão HTTPS
  externa; em execução isolada deve falhar no setup antes do corpo do teste.
- `test_async_control`: controle assíncrono; o ambiente do controle precisa de
  `pytest-asyncio` instalado antes do isolamento.
- `test_parametrized_fixture`: duas instâncias controladas da mesma fixture.

`--setup-only` valida o setup e teardown do servidor e das fixtures, mas não
valida a requisição HTTP feita pelo corpo do teste. O critério de loopback exige
também uma execução normal do controle local, em namespace sem saída externa.
O controle externo é deliberadamente esperado como erro de setup.

Não executar este diretório fora de um runner que passe os três controles de
isolamento (loopback, saída externa e socket Docker) e registre o UID efetivo.
