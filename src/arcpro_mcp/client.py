"""Talks to the bridge running inside ArcGIS Pro (newline-delimited JSON over localhost)."""

from __future__ import annotations

import itertools
import json
import os
import socket
from pathlib import Path
from typing import Any

STATE_FILE = Path(os.environ.get("ARCPRO_MCP_STATE", Path.home() / ".arcpro_mcp" / "bridge.json"))
TIMEOUT = float(os.environ.get("ARCPRO_MCP_TIMEOUT", "900"))  # long geoprocessing runs

NOT_RUNNING = (
    "The ArcGIS Pro bridge is not running. In ArcGIS Pro, open View > Python window and run:\n"
    '    exec(open(r"<path to arcpro-mcp>\\bridge\\arcpro_bridge.py").read())\n'
    "then try again."
)


class BridgeUnavailable(RuntimeError):
    pass


class BridgeCallError(RuntimeError):
    pass


_ids = itertools.count(1)


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        raise BridgeUnavailable(NOT_RUNNING) from None


def call(cmd: str, **params: Any) -> Any:
    """Send one command to the bridge and return its result (raises on error)."""
    st = _state()
    params = {k: v for k, v in params.items() if v is not None}
    req = {"id": next(_ids), "token": st["token"], "cmd": cmd, "params": params}
    try:
        with socket.create_connection((st.get("host", "127.0.0.1"), int(st["port"])), timeout=10) as s:
            s.settimeout(TIMEOUT)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        if isinstance(e, socket.timeout):
            raise BridgeCallError(f"ArcGIS Pro did not answer within {TIMEOUT:.0f}s.") from None
        raise BridgeUnavailable(NOT_RUNNING + f"\n(details: {e})") from None
    if not buf:
        raise BridgeCallError("The bridge closed the connection without answering.")
    resp = json.loads(buf.decode("utf-8"))
    if not resp.get("ok"):
        msg = resp.get("error", "Unknown bridge error")
        if resp.get("traceback"):
            msg += "\n\n" + resp["traceback"]
        raise BridgeCallError(msg)
    return resp.get("result")
