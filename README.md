# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio da Hill Valley Tech (reserva de salas). Dois processos separados que conversam por HTTP:

| Processo | Porta | Papel |
| --- | --- | --- |
| `servidor-mcp/servidor.py` | `7301` (`/mcp`) | Servidor MCP (revisão `2026-07-28`, Streamable HTTP, SDK oficial `mcp` 2.3.0). Dono das regras de sala, da política e do ciclo MRTR. |
| `agente/agente.py` | `7300` | Agente: **cliente/host MCP** por dentro e **servidor A2A v1.0 (JSON-RPC)** por fora. Só traduz protocolo, sem regra de negócio e sem LLM. |

```
cliente A2A ──SendMessage──▶ agente :7300 ──tools/call──▶ servidor MCP :7301
            ◀──INPUT_REQUIRED─            ◀─input_required─
            ──escolha=...───▶             ──inputResponses + requestState──▶
```

## Como rodar

Requisitos: Python 3.10 ou superior (testado em 3.10 e 3.12) e acesso ao PyPI para instalar o SDK. Só o servidor MCP tem dependência externa; o agente usa apenas a biblioteca padrão.

A partir de um clone limpo:

```bash
git clone https://github.com/michelleamesquita/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp

# 1. dependência pinada (mcp==2.3.0), em um venv local
python3 -m venv .venv
.venv/bin/pip install .
```

Terminal 1, servidor MCP. O segredo que protege o `requestState` vem do ambiente (mínimo de 32 bytes) e nunca é gravado no repositório. O servidor se recusa a subir sem ele:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/python servidor-mcp/servidor.py
```

Terminal 2, agente:

```bash
python3 agente/agente.py
```

Terminal 3, validador (os dois processos precisam estar no ar):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O validador imprime um `trace-id`; procure esse valor no stderr do servidor MCP (uma linha `request metodo=... id=... traceparent=...` por request) para conferir a propagação do `traceparent`.

Variáveis opcionais: `MCP_HOST`/`MCP_PORT` (servidor), `AGENT_HOST`/`AGENT_PORT`/`AGENT_PUBLIC_URL`/`MCP_URL` (agente).

O pedido A2A tem formato fixo, não é linguagem natural: `reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>` (qualquer outro texto termina a Task em `TASK_STATE_FAILED` com a explicação do formato). Exemplo manual do fluxo de ponta a ponta (sala ocupada, depois escolha):

```bash
curl -s localhost:7300/a2a -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]}}}'
# -> Task em TASK_STATE_INPUT_REQUIRED com a mensagem "alternativas: sala-fusca, sala-mirante"
# continuar: novo SendMessage com "taskId": "<id da Task>" e o texto "escolha=sala-mirante" (ou "escolha=recusar")
```

## Onde a ponte acontece

Tudo está em `agente/agente.py`. **Ida:** `pausar()` recebe o `resultType: "input_required"` do `tools/call` (chamado por `aplicar_resposta()`), guarda `inputRequests` (a chave atribuída pelo servidor), o `requestState` e a lista de alternativas **dentro da Task**, e leva a Task para `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: a, b`, na ordem do `enum` da elicitation. **Volta:** `retomar()` interpreta `escolha=<id>|recusar`, monta o `inputResponses` com a mesma chave (`accept` com a sala escolhida, ou `decline`) e devolve o `requestState` guardado, sem abrir nem alterar, num novo `tools/call` com **id JSON-RPC novo**; o resultado desse retry (`aplicar_resposta()` de novo) leva a Task para `COMPLETED` com o artifact `reserva`, para `CANCELED` (recusa) ou para `FAILED`. O `requestState` mora só em `tarefa["pausa"]` e a função `visao()` o exclui de toda resposta A2A (`SendMessage`, `GetTask`, histórico, artifacts).

No lado MCP, o ciclo equivalente está em `reservar_sala()` / `concluir_reserva()` (`servidor-mcp/servidor.py`): conflito devolve `InputRequiredResult` com uma `ElicitRequest` em form mode; o retry (`ctx.request_state` presente) lê a resposta e cria a reserva.

## Decisões técnicas

**Proteção do `requestState`.** Uso o mecanismo do próprio SDK 2.3.0 (`RequestStateSecurity`), em vez de inventar um. O estado é cifrado e autenticado com **AES-256-GCM** (AEAD), a partir de `REQUEST_STATE_SECRET` (lido do ambiente, mínimo de 32 bytes, nunca commitado). A chave é derivada do segredo com HKDF-SHA256. O envelope é **vinculado ao request**: método, nome da tool, hash dos argumentos e audiência (nome do servidor), além da expiração. Por isso um `requestState` adulterado, reaproveitado em outra tool ou trazido com argumentos diferentes é recusado com `-32602` (`Invalid or expired requestState`), e o validador confirma que argumentos adulterados no retry não têm efeito. O conteúdo do estado é só a lista de alternativas oferecidas; o servidor ainda confere que a sala escolhida está nela e **revalida o conflito** antes de gravar.

**Validade.** 15 minutos (`TTL_REQUEST_STATE`, dentro da janela de 5 a 30 minutos do enunciado). Depois disso o retry recebe `-32602`. Como o segredo vem do ambiente e não de memória, um estado emitido antes de um restart do servidor, **com o mesmo segredo**, continua válido (verificado: o agente concluiu a reserva depois de reiniciar o MCP), e com **outro** segredo é rejeitado.

**Estado das Tasks.** Em memória do agente, um dicionário `TAREFAS` por `id`, protegido por um `RLock` que nunca é segurado durante I/O de rede. Cada Task tem a sua própria pausa (`requestState`, chave, alternativas, pedido original), então duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva. Um lock não bloqueante por Task impede duas continuações simultâneas da mesma Task. Reiniciar o agente perde as Tasks; isso é aceito pelo enunciado (sem persistência). As reservas do servidor também ficam só em memória (partem de `dados/reservas.json`).

**Agente com HTTP cru, não com o cliente do SDK.** O cliente do SDK trata elicitation através de um callback que responde sozinho, o que esconderia o `input_required` do agente; a ponte precisa enxergar esse resultado para pausar a Task e devolver a decisão ao cliente A2A. O agente fala MCP direto sobre HTTP (stdlib), com `_meta` completo por request (`protocolVersion`, `clientCapabilities` com `elicitation.form`, `clientInfo`, `traceparent`), cabeçalhos `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`, e `tools/list` + leitura de `politica://uso` **antes** da primeira chamada de tool (a versão da política vai no artifact `reserva`). Sem sessão: nada é inferido de request anterior.

**`requestState` opaco no agente.** O agente só guarda e ecoa; não parseia, não valida e não reconstrói. A regra de negócio (conflito, janela, duração, alternativas) é inteira do servidor.

**Rastreio.** O `traceparent` recebido do cliente A2A fica guardado na Task e é propagado a cada chamada MCP daquela Task (inclusive o retry após a pausa), mantendo o mesmo trace-id e com um span-id novo por request. O servidor registra uma linha por request no stderr (método, id e `traceparent`). Esse log é uma camada ASGI **por fora** do SDK (`RegistroDeRequests`), para que também apareçam requests rejeitados pelo transporte (por exemplo, sem `_meta`).

**Erros.** `-32602` para `_meta`/argumentos/estado inválidos, `-32021` (com `data.requiredCapabilities`) quando há conflito e o cliente não declarou elicitation, `-32020` para divergência entre cabeçalhos e corpo. Erros de domínio (sala inexistente, janela, duração, intervalo, sem alternativa) são `isError` com a mensagem exata da política e viram `TASK_STATE_FAILED` no agente, com o texto da tool.

**Versões.** `mcp==2.3.0` pinado em `pyproject.toml`. Não encontrei limitação do SDK que exigisse contornar o protocolo.

## Saída do validador

Última execução, a partir de um clone limpo com Python 3.10.19 (dois processos novos, segredo novo), código de saída `0`:

```
trace-id desta execucao: 2466d041c07793a14f99e505df1d79f9
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
