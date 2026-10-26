#!/usr/bin/env python3
"""Servidor MCP da Central de Salas (Streamable HTTP, SDK oficial `mcp` v2).

Tudo que o enunciado chama de protocolo vem do SDK: transporte, validacao do
`_meta` e dos headers, `resultType`, e a protecao do `requestState`. O que este
arquivo escreve e o dominio (tres tools, um resource) e a decisao de quando uma
reserva precisa perguntar algo ao usuario.

Variaveis de ambiente:
    REQUEST_STATE_SECRET  obrigatoria, no minimo 32 bytes de aleatoriedade.
    MCP_HOST / MCP_PORT   padrao 127.0.0.1 / 7301.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

RAIZ = Path(__file__).resolve().parents[1]
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "7301"))

# O estado do requestState vale 15 minutos: dentro da faixa de 5 a 30 que o enunciado exige.
TTL_REQUEST_STATE = 15 * 60
CHAVE_ELICITATION = "escolha_de_sala"

FUSO = timezone(timedelta(hours=-3))  # horario de Sao Paulo, o da politica
ABERTURA, FECHAMENTO = 8, 20
DURACAO_MAXIMA = timedelta(hours=2)

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def ler_segredo() -> str:
    """O segredo do requestState vem so do ambiente; sem ele o processo nao sobe."""
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere uma com:\n"
            "  export REQUEST_STATE_SECRET=\"$(python3 -c 'import secrets; print(secrets.token_hex(32))')\""
        )
    return segredo


# --------------------------------------------------------------------------- dados

SALAS: list[dict[str, Any]] = json.loads((RAIZ / "dados/salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict[str, Any]] = json.loads((RAIZ / "dados/reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (RAIZ / "dados/politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()
SALA_POR_ID = {sala["id"]: sala for sala in SALAS}
_proximo_numero = max(int(r["id"].rsplit("-", 1)[1]) for r in RESERVAS) + 1


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ------------------------------------------------------------------------ dominio


def _instante(texto: str) -> datetime:
    momento = datetime.fromisoformat(texto)
    return momento if momento.tzinfo else momento.replace(tzinfo=FUSO)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """As mesmas validacoes para consulta e reserva; devolve o intervalo ou levanta ToolError."""
    if sala not in SALA_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    try:
        a, b = _instante(inicio), _instante(fim)
    except (TypeError, ValueError):
        raise ToolError(ERRO_INTERVALO) from None
    if b <= a:
        raise ToolError(ERRO_INTERVALO)
    dia = a.astimezone(FUSO).replace(hour=0, minute=0, second=0, microsecond=0)
    abre, fecha = dia + timedelta(hours=ABERTURA), dia + timedelta(hours=FECHAMENTO)
    if a < abre or b > fecha:
        raise ToolError(ERRO_JANELA)
    if b - a > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return a, b


def conflitos_da_sala(sala: str, a: datetime, b: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < b and _instante(r["fim"]) > a
    ]


def alternativas(sala: str, a: datetime, b: datetime) -> list[str]:
    """Salas livres com capacidade igual ou maior, por capacidade e depois id, no maximo tres."""
    minima = SALA_POR_ID[sala]["capacidade"]
    livres = [s for s in SALAS if s["capacidade"] >= minima and not conflitos_da_sala(s["id"], a, b)]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    global _proximo_numero
    reserva = f"res-{_proximo_numero:04d}"
    _proximo_numero += 1
    RESERVAS.append({"id": reserva, "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel})
    return ReservaOut(
        reserva=reserva, sala=sala, inicio=inicio, fim=fim, responsavel=responsavel, politica=POLITICA_VERSAO
    )


def exigir_elicitation_form(ctx: Context) -> None:
    """-32021 quando o cliente nao declarou elicitation em form mode, nesta mesma request."""
    capacidades = ctx.client_capabilities
    elicitation = capacidades.elicitation if capacidades is not None else None
    if elicitation is not None and elicitation.form is not None:
        return
    exigida = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data=MissingRequiredClientCapabilityErrorData(required_capabilities=exigida).model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
    )


# -------------------------------------------------------------------------- tools


def criar_servidor() -> MCPServer:
    mcp = MCPServer(
        "central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[ler_segredo()], ttl=TTL_REQUEST_STATE),
    )

    @mcp.tool()
    def listar_salas() -> ListaDeSalas:
        """Lista todas as salas com capacidade e recursos."""
        return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])

    @mcp.tool()
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
        a, b = validar(sala, inicio, fim)
        achados = conflitos_da_sala(sala, a, b)
        return Disponibilidade(
            sala=sala, livre=not achados, conflitos=[ConflitoOut(**{k: r[k] for k in ConflitoOut.model_fields}) for r in achados]
        )

    @mcp.tool()
    def reservar_sala(
        sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
    ) -> ReservaOut | InputRequiredResult:
        """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
        a, b = validar(sala, inicio, fim)

        if ctx.request_state is not None:
            return concluir_reserva(ctx, sala, inicio, fim, responsavel, a, b)

        if not conflitos_da_sala(sala, a, b):
            return criar_reserva(sala, inicio, fim, responsavel)

        # Conflito. Sem elicitation declarada nao ha como perguntar: -32021.
        exigir_elicitation_form(ctx)
        opcoes = alternativas(sala, a, b)
        if not opcoes:
            raise ToolError(ERRO_SEM_ALTERNATIVA)
        # O servidor nao guarda nada: as opcoes oferecidas viajam no requestState, que o
        # SDK sela (AES-256-GCM), vincula a esta chamada e expira.
        pergunta = ElicitRequest(
            params=ElicitRequestFormParams(
                message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                requested_schema={
                    "type": "object",
                    "properties": {"sala": {"type": "string", "enum": opcoes}},
                    "required": ["sala"],
                },
            )
        )
        return InputRequiredResult(
            input_requests={CHAVE_ELICITATION: pergunta},
            request_state=json.dumps({"alternativas": opcoes}),
        )

    def concluir_reserva(
        ctx: Context, sala: str, inicio: str, fim: str, responsavel: str, a: datetime, b: datetime
    ) -> ReservaOut:
        """Retry: o estado ja foi verificado pelo SDK; aqui so se le a resposta da elicitation."""
        oferecidas = json.loads(ctx.request_state or "{}").get("alternativas", [])
        resposta = (ctx.input_responses or {}).get(CHAVE_ELICITATION)
        if resposta is None:
            raise MCPError(code=INVALID_PARAMS, message="inputResponses sem a resposta esperada")
        if resposta.action != "accept":  # decline ou cancel nao e erro: conclui sem reservar
            return ReservaOut(
                reservado=False, sala=sala, inicio=inicio, fim=fim, responsavel=responsavel,
                politica=POLITICA_VERSAO, motivo="Reserva recusada pelo usuario",
            )
        escolhida = (resposta.content or {}).get("sala")
        if escolhida not in oferecidas:
            raise MCPError(code=INVALID_PARAMS, message="Sala escolhida fora das alternativas oferecidas")
        if conflitos_da_sala(escolhida, a, b):
            raise ToolError(ERRO_SEM_ALTERNATIVA)
        return criar_reserva(escolhida, inicio, fim, responsavel)

    @mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
    def politica_de_uso() -> str:
        """Politica de uso das salas; a primeira linha declara a versao."""
        return POLITICA_TEXTO

    return mcp


def registrar(corpo: bytes) -> None:
    """Uma linha no stderr por request: metodo, id e traceparent (quando vier no _meta)."""
    try:
        mensagem = json.loads(corpo)
        params = mensagem.get("params") or {}
        meta = params.get("_meta") or {}
        linha = f"metodo={mensagem.get('method')} id={mensagem.get('id')} traceparent={meta.get('traceparent', '-')}"
    except (ValueError, AttributeError):
        linha = "metodo=? id=? traceparent=- (corpo nao e um request JSON-RPC)"
    print(f"request {linha}", file=sys.stderr, flush=True)


class RegistroDeRequests:
    """Camada ASGI externa ao SDK.

    O log fica aqui, e nao num middleware do SDK, porque o SDK recusa no transporte
    (antes de qualquer middleware) um request sem `_meta` ou com header divergente, e
    esses tambem precisam aparecer no stderr.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        while True:
            evento = await receive()
            if evento["type"] != "http.request":
                break
            corpo += evento.get("body", b"")
            if not evento.get("more_body"):
                break
        registrar(corpo)
        entregue = False

        async def reenviar() -> Any:
            nonlocal entregue
            if entregue:
                return await receive()
            entregue = True
            return {"type": "http.request", "body": corpo, "more_body": False}

        await self.app(scope, reenviar, send)


def criar_app() -> Any:
    # stateless_http: nenhum request depende de sessao ou de request anterior.
    return RegistroDeRequests(criar_servidor().streamable_http_app(host=HOST, stateless_http=True))


if __name__ == "__main__":
    import uvicorn

    app = criar_app()  # falha cedo, antes de abrir a porta, se REQUEST_STATE_SECRET faltar
    print(f"servidor MCP em http://{HOST}:{PORT}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
