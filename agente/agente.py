#!/usr/bin/env python3
"""Agente da Central de Salas: host MCP por dentro, servidor A2A por fora.

So biblioteca padrao. O agente traduz protocolo e nao conhece regra de sala: conflito,
politica e alternativas sao decisao do servidor MCP. Nao ha LLM no caminho; o pedido
tem formato fixo e a decisao e uma tabela de estados.

Variaveis de ambiente (os padroes sao os do enunciado):
    AGENT_HOST / AGENT_PORT   127.0.0.1 / 7300
    AGENT_PUBLIC_URL          URL publicada no Agent Card (padrao http://127.0.0.1:<porta>)
    MCP_URL                   http://127.0.0.1:7301/mcp
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "7300"))
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

PROTOCOLO_MCP = "2026-07-28"
CAPACIDADES_MCP = {"elicitation": {"form": {}}}
CLIENTE_MCP = {"name": "agente-central-de-salas", "version": "1.0.0"}
TOOL_RESERVA = "reservar_sala"
TOOLS_CALL = "tools/call"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

FORMATO_DO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS = ("sala", "inicio", "fim", "responsavel")
PADRAO_PEDIDO = re.compile(r"(sala|inicio|fim|responsavel)=(.*?)(?=\s+(?:sala|inicio|fim|responsavel)=|$)", re.S)
PADRAO_ESCOLHA = re.compile(r"escolha=(\S+)")
PADRAO_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{PUBLIC_URL}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


class ErroMCP(Exception):
    """Falha ao falar com o servidor MCP; a mensagem pode ir ao cliente A2A."""


class ErroA2A(Exception):
    """Erro de JSON-RPC devolvido ao cliente A2A."""

    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


# ----------------------------------------------------------------- lado MCP (host)


def novo_traceparent(base: str | None) -> str | None:
    """Mesmo trace-id do cliente A2A, span-id novo a cada request MCP."""
    achado = PADRAO_TRACEPARENT.fullmatch(base or "")
    if not achado:
        return None
    return f"00-{achado.group(1)}-{secrets.token_hex(8)}-{achado.group(3)}"


def _corpo_json(bruto: bytes, tipo: str) -> dict[str, Any]:
    texto = bruto.decode("utf-8", "replace")
    if "text/event-stream" in tipo:  # o transporte pode responder em SSE; vale o ultimo evento com dados
        eventos = [linha[5:].strip() for linha in texto.splitlines() if linha.startswith("data:")]
        texto = eventos[-1] if eventos else ""
    try:
        corpo = json.loads(texto)
    except ValueError:
        raise ErroMCP("Resposta do servidor MCP nao e JSON") from None
    if not isinstance(corpo, dict):
        raise ErroMCP("Resposta do servidor MCP nao e um objeto JSON-RPC")
    return corpo


def chamar_mcp(metodo: str, params: dict[str, Any], nome: str | None = None, traceparent: str | None = None) -> dict[str, Any]:
    """Um request MCP completo: _meta e headers obrigatorios, id novo a cada chamada.

    Devolve a resposta JSON-RPC inteira (com `result` ou `error`). Nao usa o cliente do SDK
    de proposito: um cliente com callback de elicitation responderia a pergunta sozinho e a
    Task nunca pausaria. Aqui o `input_required` chega cru.
    """
    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOLO_MCP,
        "io.modelcontextprotocol/clientInfo": CLIENTE_MCP,
        "io.modelcontextprotocol/clientCapabilities": CAPACIDADES_MCP,
    }
    filho = novo_traceparent(traceparent)
    if filho:
        meta["traceparent"] = filho
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": metodo, "params": {**params, "_meta": meta}}
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO_MCP,
        "Mcp-Method": metodo,
    }
    if nome:
        cabecalhos["Mcp-Name"] = nome
    pedido = urllib.request.Request(MCP_URL, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:  # nosec B310 - URL vem do ambiente
            return _corpo_json(resposta.read(), resposta.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as erro:  # o servidor responde 400 com um erro JSON-RPC no corpo
        return _corpo_json(erro.read(), erro.headers.get("Content-Type", ""))
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        raise ErroMCP(f"Servidor MCP indisponivel: {erro}") from None


_descoberta: dict[str, Any] = {"ferramentas": None, "politica": None}
_trava_descoberta = threading.Lock()


def _resultado(resposta: dict[str, Any], o_que: str) -> dict[str, Any]:
    if "error" in resposta:
        raise ErroMCP(f"{o_que}: {(resposta['error'] or {}).get('message', 'erro do servidor MCP')}")
    return resposta.get("result") or {}


def descobrir(traceparent: str | None) -> tuple[set[str], str]:
    """tools/list antes da primeira chamada e leitura da versao da politica no resource."""
    with _trava_descoberta:
        if _descoberta["ferramentas"] is None:
            listagem = _resultado(chamar_mcp("tools/list", {}, traceparent=traceparent), "tools/list")
            ferramentas = {t["name"] for t in listagem.get("tools", []) if isinstance(t, dict) and "name" in t}
            recurso = _resultado(
                chamar_mcp("resources/read", {"uri": "politica://uso"}, nome="politica://uso", traceparent=traceparent),
                "resources/read politica://uso",
            )
            texto = ((recurso.get("contents") or [{}])[0]).get("text", "")
            versao = re.match(r"versao:\s*(\S+)", texto)
            if not ferramentas or not versao:
                raise ErroMCP("Servidor MCP sem ferramentas ou politica sem versao na primeira linha")
            _descoberta.update(ferramentas=ferramentas, politica=versao.group(1))
        return _descoberta["ferramentas"], _descoberta["politica"]


# --------------------------------------------------------------- lado A2A (tasks)

ESTADO = threading.RLock()  # protege mutacao e leitura do dicionario de tasks; nunca segura I/O de rede
TAREFAS: dict[str, dict[str, Any]] = {}


def nova_mensagem(papel: str, texto: str, tarefa: dict[str, Any] | None = None) -> dict[str, Any]:
    mensagem: dict[str, Any] = {"messageId": f"msg-{secrets.token_hex(6)}", "role": papel, "parts": [{"text": texto}]}
    if tarefa is not None:
        mensagem["taskId"], mensagem["contextId"] = tarefa["id"], tarefa["contextId"]
    return mensagem


def mudar_estado(tarefa: dict[str, Any], estado: str, texto: str | None = None) -> None:
    with ESTADO:
        tarefa["estado"] = estado
        tarefa["mensagem"] = nova_mensagem("ROLE_AGENT", texto, tarefa) if texto is not None else None
        if tarefa["mensagem"] is not None:
            tarefa["historico"].append(tarefa["mensagem"])


def visao(tarefa: dict[str, Any], limite_historico: int | None = None) -> dict[str, Any]:
    """O que o cliente A2A enxerga. A pausa (e o requestState dentro dela) nunca entra aqui."""
    with ESTADO:
        status: dict[str, Any] = {"state": tarefa["estado"]}
        if tarefa["mensagem"] is not None:
            status["message"] = tarefa["mensagem"]
        historico = list(tarefa["historico"])
        if limite_historico is not None:
            historico = historico[-limite_historico:] if limite_historico > 0 else []
        return {
            "id": tarefa["id"],
            "contextId": tarefa["contextId"],
            "status": status,
            "history": historico,
            "artifacts": list(tarefa["artefatos"]),
        }


def falhar(tarefa: dict[str, Any], texto: str) -> None:
    mudar_estado(tarefa, FAILED, texto)


def interpretar_pedido(texto: str) -> dict[str, str] | None:
    """Formato fixo: `reservar sala=... inicio=... fim=... responsavel=...`. Nao e linguagem natural."""
    if not texto.startswith("reservar"):
        return None
    achados = {chave: valor.strip() for chave, valor in PADRAO_PEDIDO.findall(texto)}
    return achados if all(achados.get(campo) for campo in CAMPOS) else None


def texto_do_resultado(resultado: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []) if isinstance(p, dict)).strip()


def pausar(tarefa: dict[str, Any], resultado: dict[str, Any]) -> None:
    """A ponte, ida: o `input_required` do MCP vira TASK_STATE_INPUT_REQUIRED.

    O requestState e guardado na Task, opaco: nunca e aberto, so guardado e ecoado depois.
    """
    pedidos = resultado.get("inputRequests") or {}
    estado = resultado.get("requestState")
    if len(pedidos) != 1 or not isinstance(estado, str):
        return falhar(tarefa, "Resposta input_required invalida do servidor MCP")
    chave, pedido = next(iter(pedidos.items()))
    params = (pedido or {}).get("params") or {}
    if (pedido or {}).get("method") != "elicitation/create" or params.get("mode") != "form":
        return falhar(tarefa, "O servidor MCP pediu um tipo de entrada que o agente nao suporta")
    campo = ((params.get("requestedSchema") or {}).get("properties") or {}).get("sala") or {}
    alternativas = campo.get("enum") or ([campo["const"]] if "const" in campo else [])
    if not alternativas:
        return falhar(tarefa, "A elicitation do servidor MCP nao oferece alternativas")
    with ESTADO:
        tarefa["pausa"] = {"chave": chave, "requestState": estado, "alternativas": list(alternativas)}
        mudar_estado(tarefa, INPUT_REQUIRED, "alternativas: " + ", ".join(alternativas))


def aplicar_resposta(tarefa: dict[str, Any], resposta: dict[str, Any], politica: str) -> None:
    """Traduz o resultado de um tools/call de reservar_sala no proximo estado da Task."""
    if "error" in resposta:
        return falhar(tarefa, (resposta["error"] or {}).get("message", "Erro do servidor MCP"))
    resultado = resposta.get("result") or {}
    if resultado.get("resultType") == "input_required":
        return pausar(tarefa, resultado)
    if resultado.get("isError"):
        return falhar(tarefa, texto_do_resultado(resultado))
    dados = resultado.get("structuredContent") or {}
    if dados.get("reservado") is False:
        return mudar_estado(tarefa, CANCELED, "Reserva nao realizada: " + str(dados.get("motivo") or "recusada"))
    reserva = {k: dados.get(k) for k in ("reserva", "sala", "inicio", "fim", "responsavel")}
    reserva["politica"] = politica  # versao lida do resource politica://uso
    with ESTADO:
        tarefa["artefatos"].append(
            {"artifactId": f"art-{secrets.token_hex(6)}", "name": "reserva", "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]}
        )
        mudar_estado(tarefa, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


def executar_pedido(tarefa: dict[str, Any], texto: str) -> None:
    pedido = interpretar_pedido(texto)
    if pedido is None:
        return falhar(tarefa, f"Pedido invalido: use o formato {FORMATO_DO_PEDIDO}")
    try:
        ferramentas, politica = descobrir(tarefa["traceparent"])
        if TOOL_RESERVA not in ferramentas:
            return falhar(tarefa, f"O servidor MCP nao anunciou a ferramenta {TOOL_RESERVA}")
        tarefa["pedido"] = pedido
        resposta = chamar_mcp(TOOLS_CALL, {"name": TOOL_RESERVA, "arguments": pedido}, TOOL_RESERVA, tarefa["traceparent"])
    except ErroMCP as erro:
        return falhar(tarefa, str(erro))
    aplicar_resposta(tarefa, resposta, politica)


def retomar(tarefa: dict[str, Any], texto: str) -> None:
    """A ponte, volta: a escolha do cliente A2A vira um retry MCP com id novo.

    O retry repete o tools/call original, leva `inputResponses` sob a mesma chave que veio em
    `inputRequests` e devolve o `requestState` guardado, sem modificacao.
    """
    pausa = tarefa["pausa"]
    escolhido = PADRAO_ESCOLHA.fullmatch(texto.strip())
    valor = escolhido.group(1) if escolhido else None
    if valor not in pausa["alternativas"] and valor != "recusar":
        return mudar_estado(tarefa, INPUT_REQUIRED, "alternativas: " + ", ".join(pausa["alternativas"]))
    recusa = valor == "recusar"
    resposta_usuario = {"action": "decline"} if recusa else {"action": "accept", "content": {"sala": valor}}
    mudar_estado(tarefa, WORKING)
    try:
        _, politica = descobrir(tarefa["traceparent"])
        resposta = chamar_mcp(
            TOOLS_CALL,
            {
                "name": TOOL_RESERVA,
                "arguments": tarefa["pedido"],
                "inputResponses": {pausa["chave"]: resposta_usuario},
                "requestState": pausa["requestState"],
            },
            TOOL_RESERVA,
            tarefa["traceparent"],
        )
    except ErroMCP as erro:
        if recusa:
            return mudar_estado(tarefa, CANCELED, "Reserva cancelada: alternativas recusadas.")
        return falhar(tarefa, str(erro))
    with ESTADO:
        tarefa["pausa"] = None
    if recusa:  # recusar cancela a Task qualquer que seja a resposta do servidor
        return mudar_estado(tarefa, CANCELED, "Reserva cancelada: alternativas recusadas.")
    aplicar_resposta(tarefa, resposta, politica)


def enviar_mensagem(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(-32602, "message ausente ou invalida")
    texto = " ".join(p["text"] for p in mensagem["parts"] if isinstance(p, dict) and isinstance(p.get("text"), str)).strip()
    if not texto:
        raise ErroA2A(-32602, "message sem parte de texto")
    registro = {"messageId": mensagem.get("messageId") or f"msg-{secrets.token_hex(6)}", "role": "ROLE_USER", "parts": [{"text": texto}]}
    traceparent = traceparent if PADRAO_TRACEPARENT.fullmatch(traceparent or "") else None

    if mensagem.get("taskId") is None:
        tarefa = {
            "id": f"task-{secrets.token_hex(8)}", "contextId": f"ctx-{secrets.token_hex(8)}", "estado": SUBMITTED,
            "mensagem": None, "historico": [registro], "artefatos": [], "pausa": None, "pedido": None,
            "traceparent": traceparent, "ocupada": threading.Lock(),
        }
        with ESTADO:
            TAREFAS[tarefa["id"]] = tarefa
        with tarefa["ocupada"]:
            mudar_estado(tarefa, WORKING)
            executar_pedido(tarefa, texto)
        return visao(tarefa)

    with ESTADO:
        tarefa = TAREFAS.get(mensagem["taskId"])
    if tarefa is None:
        raise ErroA2A(-32001, f"Task nao encontrada: {mensagem['taskId']}")
    with ESTADO:
        estado = tarefa["estado"]
    if estado in TERMINAIS:
        raise ErroA2A(-32602, f"Task {tarefa['id']} esta em {estado} e nao aceita novas mensagens")
    if not tarefa["ocupada"].acquire(blocking=False):
        raise ErroA2A(-32602, f"Task {tarefa['id']} esta em processamento")
    try:
        with ESTADO:
            if tarefa["estado"] != INPUT_REQUIRED or tarefa["pausa"] is None:
                raise ErroA2A(-32602, f"Task {tarefa['id']} nao esta aguardando entrada")
            registro["taskId"] = tarefa["id"]
            tarefa["historico"].append(registro)
            tarefa["traceparent"] = tarefa["traceparent"] or traceparent
        retomar(tarefa, texto)
    finally:
        tarefa["ocupada"].release()
    return visao(tarefa)


def obter_task(params: dict[str, Any]) -> dict[str, Any]:
    identificador = params.get("id")
    with ESTADO:
        tarefa = TAREFAS.get(identificador) if isinstance(identificador, str) else None
    if tarefa is None:
        raise ErroA2A(-32001, f"Task nao encontrada: {identificador}")
    limite = params.get("historyLength")
    return visao(tarefa, limite if isinstance(limite, int) and not isinstance(limite, bool) else None)


# ------------------------------------------------------------------- HTTP / JSON-RPC


def despachar(corpo: Any, traceparent: str | None) -> dict[str, Any]:
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request JSON-RPC invalido"}}
    identificador, metodo, params = corpo.get("id"), corpo["method"], corpo.get("params") or {}
    try:
        if not isinstance(params, dict):
            raise ErroA2A(-32602, "params deve ser um objeto")
        if metodo == "SendMessage":
            resultado = {"task": enviar_mensagem(params, traceparent)}
        elif metodo == "GetTask":
            resultado = {"task": obter_task(params)}
        else:
            raise ErroA2A(-32601, f"Metodo nao suportado: {metodo}")
    except ErroA2A as erro:
        return {"jsonrpc": "2.0", "id": identificador, "error": {"code": erro.codigo, "message": erro.mensagem}}
    return {"jsonrpc": "2.0", "id": identificador, "result": resultado}


class Handler(BaseHTTPRequestHandler):
    server_version = "central-de-salas-agente/1.0"
    LIMITE_CORPO = 1_000_000

    def log_message(self, formato: str, *args: Any) -> None:
        print(f"http {self.address_string()} {formato % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent-card.json":
            self.responder(200, CARD)
        else:
            self.responder(404, {"error": "nao encontrado"})

    def do_POST(self) -> None:
        if self.path != "/a2a":
            return self.responder(404, {"error": "nao encontrado"})
        try:
            tamanho = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            tamanho = -1
        if not 0 < tamanho <= self.LIMITE_CORPO:
            return self.responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Corpo ausente ou grande demais"}})
        try:
            corpo = json.loads(self.rfile.read(tamanho))
        except ValueError:
            return self.responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON invalido"}})
        self.responder(200, despachar(corpo, self.headers.get("traceparent")))

    def responder(self, status: int, corpo: dict[str, Any]) -> None:
        bruto = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(bruto)))
        self.end_headers()
        self.wfile.write(bruto)


if __name__ == "__main__":
    print(f"agente A2A em http://{HOST}:{PORT}/a2a (MCP: {MCP_URL})", file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
