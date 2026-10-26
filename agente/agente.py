#!/usr/bin/env python3
"""Agente A2A deterministico que hospeda um cliente MCP HTTP."""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = int(os.environ.get("AGENT_PORT", "7300"))
MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
PROTOCOL = "2026-07-28"
TEXT_MODE = "text/plain"
TOOLS_CALL = "tools/call"
tasks: dict[str, dict[str, Any]] = {}
discovered = False
policy_version = ""

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"http://127.0.0.1:{PORT}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": [TEXT_MODE], "defaultOutputModes": [TEXT_MODE],
    "skills": [{"id": "reservar-sala", "name": "Reservar sala", "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.", "tags": ["salas", "agenda"], "inputModes": [TEXT_MODE], "outputModes": [TEXT_MODE], "examples": ["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"]}],
}


def call_mcp(method: str, params: dict, name: str | None, traceparent: str | None) -> dict:
    meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL, "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}}
    if traceparent:
        meta["traceparent"] = traceparent
    params = {**params, "_meta": meta}
    body = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": params}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def ensure_mcp(traceparent: str | None) -> None:
    global discovered, policy_version
    if discovered:
        return
    call_mcp("tools/list", {}, None, traceparent)
    resource = call_mcp("resources/read", {"uri": "politica://uso"}, "politica://uso", traceparent)
    text = ((resource.get("result") or {}).get("contents") or [{}])[0].get("text", "")
    if text.startswith("versao:"):
        policy_version = text.splitlines()[0].split(":", 1)[1].strip()
    discovered = True


def parse_request(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, value = token.split("=", 1)
            values[key] = value
    return values


def message(text: str) -> dict:
    return {"role": "ROLE_AGENT", "parts": [{"text": text}]}


def task_view(task: dict) -> dict:
    result = {"id": task["id"], "contextId": task["contextId"], "status": {"state": task["state"]}}
    if task.get("status_message") is not None:
        result["status"]["message"] = message(task["status_message"])
    if task.get("artifacts"):
        result["artifacts"] = task["artifacts"]
    return result


def run_mcp(task: dict, args: dict, response: dict | None = None) -> None:
    result = (response or call_mcp(TOOLS_CALL, {"name": "reservar_sala", "arguments": args}, "reservar_sala", task.get("traceparent"))).get("result") or {}
    if result.get("resultType") == "input_required":
        request_key, request = next(iter(result["inputRequests"].items()))
        schema = request["params"]["requestedSchema"]
        choices = schema["properties"]["sala"].get("enum") or [schema["properties"]["sala"]["const"]]
        task.update({"state": "TASK_STATE_INPUT_REQUIRED", "status_message": "alternativas: " + ", ".join(choices), "request_state": result["requestState"], "request_key": request_key, "choices": choices, "args": args})
        return
    if result.get("isError"):
        task.update({"state": "TASK_STATE_FAILED", "status_message": " ".join(p.get("text", "") for p in result.get("content", []))})
        return
    data = result.get("structuredContent") or {}
    task.update({"state": "TASK_STATE_COMPLETED", "artifacts": [{"name": "reserva", "parts": [{"text": json.dumps({k: data[k] for k in ("reserva", "sala", "inicio", "fim", "responsavel", "politica")}, ensure_ascii=False, separators=(",", ":"))}]}], "status_message": "Reserva concluida."})


def process_send(params: dict, traceparent: str | None) -> dict:
    incoming = params.get("message") or {}
    text = " ".join(p.get("text", "") for p in incoming.get("parts", []) if "text" in p)
    task_id = incoming.get("taskId")
    if task_id:
        task = tasks.get(task_id)
        if not task:
            return {"error": {"code": -32001, "message": "Task nao encontrada"}}
        if task["state"] in {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}:
            return {"error": {"code": -32002, "message": "Task terminal"}}
        choice = text.removeprefix("escolha=")
        if choice not in task["choices"] and choice != "recusar":
            return {"result": {"task": task_view(task)}}
        if choice == "recusar":
            mcp_response = call_mcp(TOOLS_CALL, {"name": "reservar_sala", "arguments": task["args"], "inputResponses": {task["request_key"]: {"action": "decline"}}, "requestState": task["request_state"]}, "reservar_sala", task.get("traceparent"))
            data = (mcp_response.get("result") or {}).get("structuredContent") or {}
            task.update({"state": "TASK_STATE_CANCELED", "status_message": data.get("motivo", "Reserva recusada.")})
        else:
            mcp_response = call_mcp(TOOLS_CALL, {"name": "reservar_sala", "arguments": task["args"], "inputResponses": {task["request_key"]: {"action": "accept", "content": {"sala": choice}}}, "requestState": task["request_state"]}, "reservar_sala", task.get("traceparent"))
            run_mcp(task, task["args"], mcp_response)
        return {"result": {"task": task_view(task)}}
    ensure_mcp(traceparent)
    task = {"id": "task-" + secrets.token_hex(8), "contextId": "ctx-" + secrets.token_hex(8), "state": "TASK_STATE_WORKING", "traceparent": traceparent}
    tasks[task["id"]] = task
    args = parse_request(text)
    required = {k: args.get(k) for k in ("sala", "inicio", "fim", "responsavel")}
    run_mcp(task, required)
    return {"result": {"task": task_view(task)}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # O servidor HTTP não deve duplicar logs; as respostas são auditáveis no wire.
        pass

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent-card.json":
            self.send_json(CARD)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            method, req_id = body.get("method"), body.get("id")
            trace = self.headers.get("traceparent")
            if method == "SendMessage":
                result = process_send(body.get("params") or {}, trace)
            elif method == "GetTask":
                task = tasks.get((body.get("params") or {}).get("id"))
                result = {"result": {"task": task_view(task)}} if task else {"error": {"code": -32001, "message": "Task nao encontrada"}}
            else:
                result = {"error": {"code": -32601, "message": "Metodo nao encontrado"}}
            if "error" in result:
                payload = {"jsonrpc": "2.0", "id": req_id, "error": result["error"]}
            else:
                payload = {"jsonrpc": "2.0", "id": req_id, "result": result["result"]}
            self.send_json(payload)
        except Exception as exc:
            self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}, 400)

    def send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
