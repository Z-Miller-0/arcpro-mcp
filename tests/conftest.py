"""One bridge on the fake arcpy, shared by all the tests, on a port the system chooses."""

import importlib
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("state")
    state = tmp / "bridge.json"
    os.environ["ARCPRO_MCP_STATE"] = str(state)
    sys.path[:0] = [str(HERE), str(ROOT / "bridge"), str(ROOT / "src")]
    import fake_arcpy
    sys.modules["arcpy"] = fake_arcpy
    bridge = importlib.import_module("arcpro_bridge")
    bridge.STATE_DIR, bridge.STATE_FILE = str(tmp), str(state)
    server = bridge.start("CURRENT", port=0, allow_exec=False, background=True)
    from arcpro_mcp import server as mcp_server
    yield {"bridge": bridge, "server": server, "mcp": mcp_server.mcp, "state": state}
    bridge.stop()
