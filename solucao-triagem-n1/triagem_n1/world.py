"""Typed boundary to the official world, with conservative retry semantics."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import Client


READ_ONLY_TOOLS = frozenset({"listar_bases", "listar_eventos", "ler_evento", "consultar"})
NONREADMUTATIONS = frozenset({
    "proximo_evento", "registrar", "atualizar", "responder_evento",
    "enviar_mensagem", "registrar_conversa",
})


class WorldError(Exception):
    """An application or transport failure; ambiguous means a side effect may exist."""

    def __init__(self, code: int, message: str, *, ambiguous: bool = False,
                 payload: dict[str, Any] | None = None, tool: str = "") -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.ambiguous = bool(ambiguous)
        self.payload = payload or {}
        self.tool = tool


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _leaf_error(error: Exception) -> Exception:
    """Transport task groups can wrap the useful HTTP exception."""
    children = getattr(error, "exceptions", ())
    for child in children:
        if isinstance(child, Exception):
            return _leaf_error(child)
    return error


class World:
    MAX_ATTEMPTS = 3
    TIMEOUT_SECONDS = 45.0

    def __init__(self, url: str = "http://localhost:8765/mcp") -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP_URL precisa ser um endereço HTTP válido.")
        self.url = url
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    @staticmethod
    def decode_result(result: Any, tool: str = "consultar") -> dict[str, Any]:
        """Accept SDK model aliases and the world's JSON inside text blocks."""
        payload = None
        if isinstance(result, dict) and result.get("status") in {"ok", "erro"}:
            payload = result
        structured = _value(result, "structured_content", "structuredContent")
        if payload is None and isinstance(structured, dict) and structured.get("status") in {"ok", "erro"}:
            payload = structured
        for block in _value(result, "content", default=[]) or []:
            if payload is not None:
                break
            if _value(block, "type") != "text":
                continue
            text = _value(block, "text", default="")
            try:
                candidate = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("status") in {"ok", "erro"}:
                payload = candidate
        effectful = tool not in READ_ONLY_TOOLS
        if payload is None:
            raise WorldError(502, "O mundo devolveu uma resposta sem JSON de aplicação válido.",
                             ambiguous=effectful, tool=tool)
        is_error = bool(_value(result, "is_error", "isError", default=False))
        if payload["status"] == "erro" or is_error:
            try:
                code = int(payload.get("codigo", 500))
            except (TypeError, ValueError):
                code = 500
            raise WorldError(code, str(payload.get("mensagem") or "Falha informada pelo mundo."),
                             ambiguous=code == 504 and effectful, payload=payload, tool=tool)
        return payload

    async def _invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        dispatched = False
        try:
            async with Client(self.url, read_timeout_seconds=self.TIMEOUT_SECONDS, cache=None) as client:
                dispatched = True
                return await client.call_tool(tool, arguments, read_timeout_seconds=self.TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            leaf = _leaf_error(error)
            effectful = tool not in READ_ONLY_TOOLS
            if isinstance(leaf, httpx.HTTPStatusError):
                code = leaf.response.status_code
                ambiguous = dispatched and effectful and code in {500, 502, 504}
                raise WorldError(code, f"Falha HTTP {code} ao acessar o mundo.",
                                 ambiguous=ambiguous, tool=tool) from error
            if isinstance(leaf, (TimeoutError, httpx.TimeoutException)):
                raise WorldError(504, "O mundo não confirmou a chamada dentro do prazo.",
                                 ambiguous=dispatched and effectful, tool=tool) from error
            code = getattr(leaf, "code", None)
            if isinstance(code, int) and code > 0:
                raise WorldError(code, "O protocolo MCP recusou a chamada.",
                                 ambiguous=dispatched and effectful and code == 504, tool=tool) from error
            raise WorldError(504 if dispatched and effectful else 503,
                             "Não foi possível confirmar a conexão/chamada ao mundo.",
                             ambiguous=dispatched and effectful, tool=tool) from error

    async def call(self, tool: str, agent: str = "triagista", motivo: str = "", **args: Any) -> dict[str, Any]:
        if not str(agent).strip():
            raise WorldError(400, "Toda chamada precisa identificar o agente.", tool=tool)
        arguments = {**args, "agente": str(agent), "motivo": str(motivo)}
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                result = await asyncio.wait_for(self._invoke(tool, arguments), self.TIMEOUT_SECONDS)
                return self.decode_result(result, tool)
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                failure = WorldError(504, "Tempo limite de comunicação com o mundo.",
                                     ambiguous=tool not in READ_ONLY_TOOLS, tool=tool)
                if failure.ambiguous or attempt == self.MAX_ATTEMPTS - 1:
                    raise failure from error
            except WorldError as failure:
                retryable = failure.code == 503 or (failure.code == 504 and tool in READ_ONLY_TOOLS)
                if failure.ambiguous or not retryable or attempt == self.MAX_ATTEMPTS - 1:
                    raise
            await asyncio.sleep(0.25 * (2 ** attempt))
        raise AssertionError("retry loop must return or raise")

    async def query(self, base: str, agent: str = "triagista", **kwargs: Any) -> list[dict[str, Any]]:
        options = dict(kwargs)
        try:
            limit = max(1, min(100, int(options.pop("limite", 100))))
            offset = max(0, int(options.pop("offset", 0)))
        except (TypeError, ValueError) as error:
            raise WorldError(400, "Paginação inválida.", tool="consultar") from error
        records: list[dict[str, Any]] = []
        for _ in range(1000):
            payload = await self.call("consultar", agent, base=base, limite=limit, offset=offset, **options)
            page = payload.get("registros")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise WorldError(502, "Consulta sem lista válida de registros.", payload=payload, tool="consultar")
            records.extend(page)
            if not payload.get("tem_mais", False):
                return records
            if not page:
                raise WorldError(502, "Paginação sem avanço no mundo.", payload=payload, tool="consultar")
            offset += len(page)
        raise WorldError(502, "Paginação excedeu o limite de proteção.", tool="consultar")

    async def read_event(self, event_id: str, agent: str = "triagista") -> dict[str, Any]:
        payload = await self.call("ler_evento", agent, evento_id=event_id)
        event = payload.get("evento")
        if not isinstance(event, dict):
            raise WorldError(502, "O mundo não devolveu o evento solicitado.", payload=payload, tool="ler_evento")
        return event

    async def _http(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.TIMEOUT_SECONDS, connect=10.0)) as client:
                response = await client.request(method, self.base_url + path, json=data if method == "POST" else None)
                try:
                    payload = response.json()
                except ValueError as error:
                    raise WorldError(502, "O painel do mundo não devolveu JSON válido.",
                                     ambiguous=method == "POST") from error
                if response.is_error:
                    message = payload.get("erro", "Falha no painel do mundo.") if isinstance(payload, dict) else "Falha no painel do mundo."
                    raise WorldError(response.status_code, str(message), payload=payload if isinstance(payload, dict) else {})
                if not isinstance(payload, dict):
                    raise WorldError(502, "Resposta inválida do painel do mundo.", ambiguous=method == "POST")
                return payload
        except WorldError:
            raise
        except httpx.ConnectError as error:
            raise WorldError(503, "O painel do mundo está indisponível.") from error
        except httpx.TimeoutException as error:
            raise WorldError(504, "O painel do mundo não confirmou a operação.", ambiguous=method == "POST") from error
        except httpx.HTTPError as error:
            raise WorldError(504 if method == "POST" else 503, "Falha de transporte no painel do mundo.",
                             ambiguous=method == "POST") from error

    async def status(self) -> dict[str, Any]:
        return await self._http("GET", "/api/estado")

    async def post_event(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = await self._http("POST", "/api/evento", data)
        if not payload.get("id"):
            raise WorldError(502, "O mundo não confirmou o ID do novo evento.", ambiguous=True, payload=payload)
        return payload
