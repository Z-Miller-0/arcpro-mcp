"""Talks to the bridge running inside ArcGIS Pro (newline-delimited JSON over localhost)."""

from __future__ import annotations

import itertools
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from . import __version__
from .install import bridge_script, startup_line

STATE_FILE = Path(os.environ.get("ARCPRO_MCP_STATE", Path.home() / ".arcpro_mcp" / "bridge.json"))
TIMEOUT = float(os.environ.get("ARCPRO_MCP_TIMEOUT", "900"))  # long geoprocessing runs
MAX_RESPONSE_BYTES = 96 * 1024 * 1024


def not_running() -> str:
    """How to start the bridge, with the real path of the bridge script when it is known."""
    script = bridge_script()
    toolbox = f" ({script.with_name('ArcProMCP.pyt')})" if script else ""
    return (
        "The ArcGIS Pro bridge is not running. Ask the user to run 'Start Bridge' from the "
        f"ArcPro MCP toolbox in Pro{toolbox}, or to open View > Python window in Pro and "
        "paste this as ONE line:\n"
        f"    {startup_line(script)}\n"
        "then try again."
    )


def _minor(version: Any) -> tuple[int, int] | None:
    m = re.match(r"(\d+)\.(\d+)", str(version or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def version_problem(bridge_version: Any) -> str | None:
    """None when the bridge and this server can work together (same major.minor version)."""
    if os.environ.get("ARCPRO_MCP_IGNORE_VERSION"):
        return None
    theirs, ours = _minor(bridge_version), _minor(__version__)
    if theirs == ours:
        return None
    shown = bridge_version or "an unknown (old) version"
    if theirs is not None and ours is not None and theirs > ours:
        return (f"The bridge running in ArcGIS Pro is {shown}, newer than this arcpro-mcp "
                f"server ({__version__}). Ask the user to fully quit and reopen Claude so it "
                "starts the new server.")
    script = bridge_script()
    where = f" in {script.parent}" if script else ""
    return (f"The bridge running in ArcGIS Pro is {shown}, but this arcpro-mcp server is "
            f"{__version__}, and the two must match. Ask the user to restart the bridge in Pro: "
            f"run Stop Bridge, then Start Bridge, from the ArcPro MCP toolbox{where}, or paste "
            f"this as ONE line into View > Python window:\n    {startup_line(script)}")

CRASHED = (
    "The connection to ArcGIS Pro dropped while it was running {cmd!r}. Pro may have "
    "crashed or frozen. Ask the user to check Pro; if it closed, reopen the project and "
    "restart the bridge, and look for half-finished outputs from {cmd!r} before retrying."
)


class BridgeUnavailable(RuntimeError):
    pass


class BridgeCallError(RuntimeError):
    pass


_ids = itertools.count(1)


def _state() -> dict[str, Any]:
    try:
        st = json.loads(STATE_FILE.read_text())
        port = int(st["port"])
        if not 1 <= port <= 65535 or not isinstance(st["token"], str):
            raise ValueError("bad state")
    except FileNotFoundError:
        raise BridgeUnavailable(not_running()) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise BridgeUnavailable(not_running() + "\n(The bridge state file is unreadable.)") from None
    return st


def call(cmd: str, **params: Any) -> Any:
    """Send one command to the bridge and return its result (raises on error)."""
    st = _state()
    if cmd != "ping":  # bridge_status always answers, and reports a mismatch itself
        problem = version_problem(st.get("bridge_version"))
        if problem:
            raise BridgeUnavailable(problem)
    params = {k: v for k, v in params.items() if v is not None}
    req = {"id": next(_ids), "token": st["token"], "cmd": cmd, "params": params}
    try:
        # Always loopback, whatever the state file says: the bridge binds nothing else.
        s = socket.create_connection(("127.0.0.1", int(st["port"])), timeout=10)
    except OSError as e:
        raise BridgeUnavailable(not_running() + f"\n(details: {e})") from None
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + TIMEOUT
    try:
        with s:
            s.settimeout(TIMEOUT)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            while True:
                s.settimeout(max(0.1, deadline - time.monotonic()))
                chunk = s.recv(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise BridgeCallError(f"The response to {cmd!r} was too large.")
                if chunk.endswith(b"\n"):
                    break
    except socket.timeout:
        raise BridgeCallError(
            f"ArcGIS Pro did not answer {cmd!r} within {TIMEOUT:.0f}s. Call bridge_status to "
            "see whether Pro is still busy.") from None
    except OSError as e:
        raise BridgeCallError(CRASHED.format(cmd=cmd) + f"\n(details: {e})") from None
    buf = b"".join(chunks)
    if not buf.endswith(b"\n"):
        raise BridgeCallError(CRASHED.format(cmd=cmd))
    try:
        resp = json.loads(buf.decode("utf-8"))
        if not isinstance(resp, dict):
            raise ValueError("not an object")
    except ValueError:
        raise BridgeCallError(
            "The program on the bridge port did not answer like the bridge. Restart the bridge "
            "in ArcGIS Pro.") from None
    if not resp.get("ok"):
        msg = resp.get("error", "Unknown bridge error")
        if resp.get("traceback"):
            msg += "\n\n" + resp["traceback"]
        raise BridgeCallError(msg)
    return resp.get("result")
