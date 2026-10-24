"""Add an MCP server to an existing FastAPI app with ONE line: ``mcp.mount(app)``.

Run:   uvicorn fastapi_app:app --app-dir examples --port 8000
REST:  http://127.0.0.1:8000/orders/1        (normal FastAPI endpoint, docs at /docs)
MCP:   http://127.0.0.1:8000/mcp             (MCP Inspector / Antigravity / Claude / Cursor)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))  # repo-only shim

import asyncio  # noqa: E402
from typing import Dict, List  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402

from py_http_mcp_server import McpServer  # noqa: E402

app = FastAPI(title="Orders API")
mcp = McpServer(server_name="orders-mcp", instructions="Look up and create orders in the Orders API.")

ORDERS: Dict[int, dict] = {1: {"id": 1, "item": "Keyboard", "quantity": 2, "status": "shipped"}}


# ---- shared business logic -------------------------------------------------------------------
def find_order(order_id: int) -> dict:
    if order_id not in ORDERS:
        raise KeyError(f"Order {order_id} not found")
    return ORDERS[order_id]


# ---- regular FastAPI endpoints (unchanged) ----------------------------------------------------
@app.get("/orders/{order_id}")
def get_order_rest(order_id: int) -> dict:
    try:
        return find_order(order_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/orders")
def list_orders_rest() -> List[dict]:
    return list(ORDERS.values())


# ---- MCP tools: the same logic, exposed to AI agents -------------------------------------------
@mcp.tool(read_only_hint=True)
def get_order(order_id: int) -> dict:
    """Returns an order by id."""
    return find_order(order_id)


@mcp.tool(read_only_hint=True)
def list_orders() -> dict:
    """Lists all orders."""
    return {"orders": list(ORDERS.values())}


@mcp.tool()
async def create_order(item: str, quantity: int = 1) -> dict:
    """Creates a new order and returns it."""
    await asyncio.sleep(0)  # async tools run on FastAPI's own event loop
    order = {"id": max(ORDERS) + 1, "item": item, "quantity": quantity, "status": "new"}
    ORDERS[order["id"]] = order
    return order


mcp.mount(app)  # <- the only integration line: serves /mcp next to your API
