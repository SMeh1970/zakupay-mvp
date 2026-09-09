"""Stateless MCP endpoint for Abacus using a static bearer token.

The endpoint supports unauthenticated GET probing (no data or tools are exposed)
while all JSON-RPC POST requests remain protected by ABACUS_MCP_TOKEN.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_BODY_BYTES = 1_048_576
logger = logging.getLogger("abacus_mcp")


def _auth_state(request: Request) -> tuple[bool, str]:
    """Return auth result plus a secret-safe diagnostic reason."""
    configured = os.getenv("ABACUS_MCP_TOKEN", "").strip()
    if not configured:
        return False, "server_token_missing"
    authorization = request.headers.get("authorization", "")
    if not authorization:
        return False, "authorization_header_missing"
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False, f"unexpected_scheme:{scheme[:16]}"
    supplied = supplied.strip()
    if not supplied:
        return False, "bearer_token_empty"
    if not hmac.compare_digest(supplied, configured):
        return False, f"token_mismatch:received_len={len(supplied)}:expected_len={len(configured)}"
    return True, "ok"


def _authorized(request: Request) -> bool:
    return _auth_state(request)[0]


def _unauthorized() -> JSONResponse:
    # No OAuth metadata is advertised; this endpoint uses a static Bearer token.
    return JSONResponse(
        {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


def _tools() -> list[dict[str, Any]]:
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    return [
        {
            "name": "list_zakupay_orders",
            "title": "List Zakupay orders",
            "description": "Returns current Zakupay orders with filters. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payment": {"type": "string", "enum": ["all", "prepayment", "delay"], "default": "all"},
                    "region": {"type": "string", "default": ""},
                    "category": {"type": "string", "default": ""},
                    "min_positions": {"type": "integer", "minimum": 0, "default": 0},
                    "max_competitors": {"type": "integer", "minimum": 0},
                    "only_without_my_offer": {"type": "boolean", "default": False},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    "refresh": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "get_zakupay_order",
            "title": "Get a single Zakupay order",
            "description": "Returns the current order by ID. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "minimum": 1},
                    "refresh": {"type": "boolean", "default": False},
                },
                "required": ["order_id"],
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "get_zakupay_connection_status",
            "title": "Zakupay connection status",
            "description": "Checks the server connection to Zakupay without exposing secrets.",
            "inputSchema": {
                "type": "object",
                "properties": {"refresh": {"type": "boolean", "default": True}},
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
    ]


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_success(data: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": data,
        "isError": False,
    }


def _validated_int(arguments: dict[str, Any], name: str, default: int | None = None,
                   minimum: int = 0, maximum: int | None = None) -> int | None:
    value = arguments.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Parameter {name} must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Parameter {name} is out of the allowed range.")
    return value


def install_abacus_mcp(
    app: FastAPI,
    fetch_all_orders: Callable[..., list[dict[str, Any]]],
    filter_orders: Callable[..., list[dict[str, Any]]],
    compact_order: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_zakupay_orders":
            allowed = {"payment", "region", "category", "min_positions", "max_competitors",
                       "only_without_my_offer", "offset", "limit", "refresh"}
            unknown = set(arguments) - allowed
            if unknown:
                raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
            payment = arguments.get("payment", "all")
            if payment not in {"all", "prepayment", "delay"}:
                raise ValueError("Parameter payment is invalid.")
            for field in ("region", "category"):
                if not isinstance(arguments.get(field, ""), str):
                    raise TypeError(f"Parameter {field} must be a string.")
            for field in ("only_without_my_offer", "refresh"):
                if not isinstance(arguments.get(field, False), bool):
                    raise TypeError(f"Parameter {field} must be a boolean.")
            min_positions = _validated_int(arguments, "min_positions", 0, 0)
            max_competitors = _validated_int(arguments, "max_competitors", None, 0)
            offset = _validated_int(arguments, "offset", 0, 0)
            limit = _validated_int(arguments, "limit", 20, 1, 50)
            orders = await run_in_threadpool(fetch_all_orders, force=arguments.get("refresh", False))
            filtered = filter_orders(
                orders,
                payment=payment,
                region=arguments.get("region", ""),
                category=arguments.get("category", ""),
                min_positions=min_positions,
                max_competitors_value=max_competitors,
                only_without_my_offer=arguments.get("only_without_my_offer", False),
            )
            visible = filtered[offset: offset + limit]
            data = {
                "source": "REAL_ZAKUPAY",
                "read_only": True,
                "total_actual": len(orders),
                "filtered_count": len(filtered),
                "offset": offset,
                "returned": len(visible),
                "has_more": offset + len(visible) < len(filtered),
                "orders": [compact_order(order) for order in visible],
            }
            return _tool_success(data, f"Found {len(filtered)} orders; returned {len(visible)}.")

        if name == "get_zakupay_order":
            unknown = set(arguments) - {"order_id", "refresh"}
            if unknown:
                raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
            order_id = _validated_int(arguments, "order_id", None, 1)
            if order_id is None:
                raise ValueError("Parameter order_id is required.")
            if not isinstance(arguments.get("refresh", False), bool):
                raise TypeError("Parameter refresh must be a boolean.")
            orders = await run_in_threadpool(fetch_all_orders, force=arguments.get("refresh", False))
            order = next((item for item in orders if item.get("id") == order_id), None)
            if order is None and not arguments.get("refresh", False):
                orders = await run_in_threadpool(fetch_all_orders, force=True)
                order = next((item for item in orders if item.get("id") == order_id), None)
            if order is None:
                return {"content": [{"type": "text", "text": f"Order {order_id} not found."}], "isError": True}
            return _tool_success({"source": "REAL_ZAKUPAY", "read_only": True, "order": compact_order(order)},
                                 f"Order {order_id} retrieved.")

        if name == "get_zakupay_connection_status":
            if set(arguments) - {"refresh"}:
                raise ValueError("Unknown parameters provided.")
            if not isinstance(arguments.get("refresh", True), bool):
                raise TypeError("Parameter refresh must be a boolean.")
            orders = await run_in_threadpool(fetch_all_orders, force=arguments.get("refresh", True))
            return _tool_success(
                {"connected": True, "source": "REAL_ZAKUPAY", "read_only": True,
                 "actual_orders_count": len(orders), "api_key_exposed": False},
                f"Connection is working. Current orders: {len(orders)}.",
            )

        raise LookupError("Unknown MCP tool.")

    async def handle_rpc(message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid JSON-RPC request.")
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _rpc_error(request_id, -32600, "Invalid JSON-RPC request.")
        method = message["method"]
        if request_id is None and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "sinteka-abacus", "version": "1.0.1"},
                    "instructions": "These tools only read and analyze Zakupay orders.",
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tools()}}
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return _rpc_error(request_id, -32602, "Invalid tool parameters.")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return _rpc_error(request_id, -32602, "arguments must be an object.")
            try:
                result = await call_tool(params["name"], arguments)
            except LookupError as exc:
                return _rpc_error(request_id, -32601, str(exc))
            except (TypeError, ValueError) as exc:
                return _rpc_error(request_id, -32602, str(exc))
            except HTTPException as exc:
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": f"Zakupay API: HTTP {exc.status_code}"}], "isError": True}}
            except Exception:  # noqa: BLE001
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": "Failed to retrieve Zakupay data."}], "isError": True}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return _rpc_error(request_id, -32601, "Method not found.")

    async def process(request: Request) -> Response:
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request too large."}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request too large."}, status_code=413)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(_rpc_error(None, -32700, "JSON parse error."), status_code=400)
        if isinstance(payload, list):
            if not payload:
                return JSONResponse(_rpc_error(None, -32600, "Empty batch request."), status_code=400)
            results = []
            for message in payload:
                result = await handle_rpc(message)
                if result is not None:
                    results.append(result)
            if not results:
                return Response(status_code=202)
            response_data: Any = results
        else:
            response_data = await handle_rpc(payload)
            if response_data is None:
                return Response(status_code=202)
        return JSONResponse(response_data, headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "Cache-Control": "no-store"})

    @app.get("/mcp-abacus")
    async def abacus_mcp_get(request: Request):
        # Abacus may probe a remote MCP URL with GET before it sends JSON-RPC POST.
        # Do not require credentials for this probe; it exposes no tools or business data.
        auth_ok, auth_reason = _auth_state(request)
        logger.info(
            "MCP GET probe auth=%s reason=%s accept=%s user_agent=%s",
            auth_ok,
            auth_reason,
            request.headers.get("accept", "")[:120],
            request.headers.get("user-agent", "")[:120],
        )
        return JSONResponse(
            {
                "service": "sinteka-abacus-mcp",
                "transport": "streamable-http",
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "message": "Use POST for MCP JSON-RPC.",
            },
            status_code=405,
            headers={
                "Allow": "POST",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Cache-Control": "no-store",
            },
        )

   @app.post("/mcp-abacus")
    async def abacus_mcp_post(request: Request):
        logger.info("MCP POST received")
        return await process(request)
