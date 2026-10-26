#!/usr/bin/env python3
"""Servidor MCP Streamable HTTP da Central de Salas."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("MCP_PORT", "7301"))
PROTOCOL = "2026-07-28"
SECRET_TEXT = os.environ.get("REQUEST_STATE_SECRET", "")
SECRET = SECRET_TEXT.encode()
if len(SECRET) < 32:
    raise SystemExit("REQUEST_STATE_SECRET deve ter no minimo 32 bytes")

with (ROOT / "dados/salas.json").open(encoding="utf-8") as f:
    SALAS = json.load(f)
with (ROOT / "dados/reservas.json").open(encoding="utf-8") as f:
    RESERVAS: list[dict[str, Any]] = json.load(f)
with (ROOT / "dados/politica-de-uso.md").open(encoding="utf-8") as f:
    POLITICA_TEXTO = f.read()
POLITICA = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()
SALA_POR_ID = {s["id"]: s for s in SALAS}
RESERVA_NUMERO = max(int(r["id"].split("-")[-1]) for r in RESERVAS) + 1


def resposta_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def texto_resultado(data: dict, is_error: bool = False) -> dict:
    result = {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "resultType": "complete",
        "structuredContent": data,
    }
    if is_error:
        result["isError"] = True
    return result


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def validar_intervalo(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime] | str:
    if sala not in SALA_POR_ID:
        return f"Sala inexistente: {sala}"
    try:
        a, b = parse_dt(inicio), parse_dt(fim)
    except (TypeError, ValueError):
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if b <= a:
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if a.timetz().replace(tzinfo=None) < dtime(8) or b.timetz().replace(tzinfo=None) > dtime(20):
        return "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    if b - a > timedelta(hours=2):
        return "Duracao acima do limite: a politica permite no maximo 2 horas"
    return a, b


def conflitos(sala: str, inicio: datetime, fim: datetime) -> list[dict]:
    found = []
    for reserva in RESERVAS:
        if reserva["sala"] != sala:
            continue
        a, b = parse_dt(reserva["inicio"]), parse_dt(reserva["fim"])
        if a < fim and b > inicio:
            found.append({k: reserva[k] for k in ("id", "inicio", "fim", "responsavel")})
    return found


def alternativas(sala: str, inicio: datetime, fim: datetime) -> list[str]:
    capacidade = SALA_POR_ID[sala]["capacidade"]
    candidatas = [
        s for s in SALAS
        if s["capacidade"] >= capacidade and not conflitos(s["id"], inicio, fim)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:3]]


def seal(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    mac = hmac.new(SECRET, encoded.encode(), hashlib.sha256).digest()
    return "v1." + encoded + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def unseal(value: str) -> dict:
    try:
        version, encoded, signature = value.split(".", 2)
        if version != "v1":
            raise ValueError
        expected = hmac.new(SECRET, encoded.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        if payload["exp"] < int(time.time()):
            raise ValueError
        return payload
    except (ValueError, KeyError, TypeError):
        raise ValueError("requestState invalido ou expirado")


def tool_definitions() -> list[dict]:
    common = {"type": "object"}
    return [
        {"name": "listar_salas", "description": "Lista todas as salas com capacidade e recursos.",
         "inputSchema": {**common, "properties": {}},
         "outputSchema": {"type": "object", "properties": {"salas": {"type": "array"}}, "required": ["salas"]}},
        {"name": "consultar_disponibilidade", "description": "Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
         "inputSchema": {"type": "object", "properties": {"sala": {"type": "string"}, "inicio": {"type": "string"}, "fim": {"type": "string"}}, "required": ["sala", "inicio", "fim"]},
         "outputSchema": {"type": "object", "properties": {"sala": {"type": "string"}, "livre": {"type": "boolean"}, "conflitos": {"type": "array"}}, "required": ["sala", "livre", "conflitos"]}},
        {"name": "reservar_sala", "description": "Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
         "inputSchema": {"type": "object", "properties": {"sala": {"type": "string"}, "inicio": {"type": "string"}, "fim": {"type": "string"}, "responsavel": {"type": "string"}}, "required": ["sala", "inicio", "fim", "responsavel"]},
         "outputSchema": {"type": "object", "properties": {"reserva": {"type": ["string", "null"]}, "reservado": {"type": "boolean"}, "sala": {"type": ["string", "null"]}, "inicio": {"type": ["string", "null"]}, "fim": {"type": ["string", "null"]}, "responsavel": {"type": ["string", "null"]}, "politica": {"type": ["string", "null"]}}}},
    ]


def call_tool(name: str, args: dict, params: dict) -> dict:
    global RESERVA_NUMERO
    if name == "listar_salas":
        return texto_resultado({"salas": SALAS})
    if name not in {"consultar_disponibilidade", "reservar_sala"}:
        raise LookupError("Tool inexistente")
    required = ("sala", "inicio", "fim")
    if any(k not in args for k in required):
        return texto_resultado({"motivo": "argumentos invalidos"}, True)
    valid = validar_intervalo(args["sala"], args["inicio"], args["fim"])
    if isinstance(valid, str):
        return texto_resultado({"motivo": valid}, True)
    inicio, fim = valid
    found = conflitos(args["sala"], inicio, fim)
    if name == "consultar_disponibilidade":
        return texto_resultado({"sala": args["sala"], "livre": not found, "conflitos": found})
    if "requestState" in params:
        try:
            sealed = unseal(params["requestState"])
        except ValueError as exc:
            raise ValueError(str(exc))
        original = sealed["arguments"]
        if original != args:
            args = original
            valid = validar_intervalo(args["sala"], args["inicio"], args["fim"])
            if isinstance(valid, str):
                return texto_resultado({"motivo": valid}, True)
            inicio, fim = valid
        responses = params.get("inputResponses") or {}
        response = next(iter(responses.values()), {})
        action = response.get("action")
        if action in {"decline", "cancel"}:
            return texto_resultado({"reserva": None, "reservado": False, "sala": args["sala"], "inicio": args["inicio"], "fim": args["fim"], "responsavel": args["responsavel"], "politica": POLITICA, "motivo": "recusado"})
        chosen = ((response.get("content") or {}).get("sala"))
        if action != "accept" or chosen not in sealed["alternatives"]:
            raise ValueError("escolha de sala invalida")
        args = {**args, "sala": chosen}
        found = conflitos(chosen, inicio, fim)
    if found:
        caps = (params.get("_meta") or {}).get("io.modelcontextprotocol/clientCapabilities") or {}
        if not isinstance(caps.get("elicitation"), dict) or not isinstance(caps["elicitation"].get("form"), dict):
            raise PermissionError("cliente nao declarou elicitation em form mode")
        choices = alternativas(args["sala"], inicio, fim)
        if not choices:
            return texto_resultado({"motivo": "Sem alternativas disponiveis no intervalo"}, True)
        key = "servidor:escolha_de_sala"
        state = seal({"exp": int(time.time()) + 900, "arguments": args, "alternatives": choices})
        return {"resultType": "input_required", "inputRequests": {key: {"method": "elicitation/create", "params": {"mode": "form", "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", "requestedSchema": {"type": "object", "properties": {"sala": {"type": "string", "enum": choices}}, "required": ["sala"]}}}}, "requestState": state}
    reserva = {"reserva": f"res-{RESERVA_NUMERO:04d}", "reservado": True, "sala": args["sala"], "inicio": args["inicio"], "fim": args["fim"], "responsavel": args["responsavel"], "politica": POLITICA, "motivo": None}
    RESERVA_NUMERO += 1
    RESERVAS.append({"id": reserva["reserva"], **{k: reserva[k] for k in ("sala", "inicio", "fim", "responsavel")}})
    return texto_resultado(reserva)


class Handler(BaseHTTPRequestHandler):
    server_version = "central-de-salas/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # O contrato exige que o log operacional vá para stderr em formato próprio.
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length))
            req_id = body.get("id")
            params = body.get("params") or {}
            method = body.get("method")
            meta = params.get("_meta") or {}
            trace = meta.get("traceparent", "-")
            print(f"method={method} id={req_id} traceparent={trace}", file=sys.stderr, flush=True)
            if self.path != "/mcp":
                raise LookupError("endpoint inexistente")
            capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
            if meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL or not isinstance(capabilities, dict):
                self.finish_json(resposta_error(req_id, -32602, "metadados MCP obrigatorios ausentes"), 400)
                return
            if self.headers.get("MCP-Protocol-Version") != meta["io.modelcontextprotocol/protocolVersion"] or self.headers.get("Mcp-Method") != method:
                self.finish_json(resposta_error(req_id, -32020, "headers MCP nao correspondem ao corpo"), 400)
                return
            if method in {"tools/call", "resources/read"} and self.headers.get("Mcp-Name") != (params.get("name") or params.get("uri")):
                self.finish_json(resposta_error(req_id, -32020, "Mcp-Name nao corresponde ao corpo"), 400)
                return
            if method == "tools/list":
                result = {"resultType": "complete", "tools": tool_definitions()}
            elif method == "resources/read":
                if params.get("uri") != "politica://uso":
                    self.finish_json(resposta_error(req_id, -32602, "resource inexistente"), 400)
                    return
                result = {"resultType": "complete", "contents": [{"uri": "politica://uso", "mimeType": "text/markdown", "text": POLITICA_TEXTO}]}
            elif method == "tools/call":
                try:
                    result = call_tool(params.get("name"), params.get("arguments") or {}, params)
                except LookupError:
                    self.finish_json(resposta_error(req_id, -32602, "Tool inexistente"), 400)
                    return
                except PermissionError:
                    self.finish_json(resposta_error(req_id, -32021, "capability de elicitation ausente", {"requiredCapabilities": ["elicitation.form"]}), 400)
                    return
                except ValueError as exc:
                    self.finish_json(resposta_error(req_id, -32602, str(exc)), 400)
                    return
            else:
                self.finish_json(resposta_error(req_id, -32601, "metodo inexistente"), 400)
                return
            self.finish_json({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            self.finish_json(resposta_error(None, -32700, str(exc)), 400)

    def finish_json(self, body: dict, status: int = 200) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    print(f"servidor MCP ouvindo em http://localhost:{PORT}/mcp", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
