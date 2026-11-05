"""End-to-end test: MCP client -> arcpro-mcp server -> TCP bridge -> fake arcpy."""

import importlib
import json
import os
import sys
from pathlib import Path

import anyio
import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent


@pytest.fixture(scope="module")
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


def call_tool(mcp, name, args=None):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(mcp._mcp_server) as s:
            return await s.call_tool(name, args or {})

    return anyio.run(go)


def payload(res):
    assert not res.isError, res.content[0].text
    if res.structuredContent is not None:
        return res.structuredContent.get("result", res.structuredContent)
    return json.loads(res.content[0].text)


def test_state_file(env):
    st = json.loads(env["state"].read_text())
    assert st["mode"] == "live" and len(st["token"]) == 48


def test_tools_listed(env):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(env["mcp"]._mcp_server) as s:
            return await s.list_tools()

    names = {t.name for t in anyio.run(go).tools}
    assert {"project_info", "query_layer", "run_geoprocessing", "snapshot_map",
            "search_arcgis_online", "run_arcpy"} <= names


def test_status_and_project(env):
    st = payload(call_tool(env["mcp"], "bridge_status"))
    assert st["mode"] == "live" and st["arcgis_pro_version"] == "3.5"
    info = payload(call_tool(env["mcp"], "project_info"))
    assert info["active_map"] == "US Magnet Stack"
    assert info["maps"][0]["layer_count"] == 2


def test_describe_and_query(env):
    d = payload(call_tool(env["mcp"], "describe_layer", {"layer": "ree deposits"}))
    assert d["feature_count"] == 4 and d["shape_type"] == "Point"
    q = payload(call_tool(env["mcp"], "query_layer",
                          {"layer": "REE Deposits", "where": "STATE = 'WY'",
                           "include_geometry": True}))
    assert q["row_count"] == 2 and "Shape" not in q["rows"][0]
    assert q["rows"][0]["_centroid_xy"] == [1.0, 2.0]
    lim = payload(call_tool(env["mcp"], "query_layer", {"layer": "REE Deposits", "limit": 3}))
    assert lim["row_count"] == 3 and lim["truncated"] is True


def test_field_summary(env):
    s = payload(call_tool(env["mcp"], "field_summary", {"layer": "REE Deposits", "field": "STATE"}))
    assert s["top_values"][0] == {"value": "WY", "count": 2}
    n = payload(call_tool(env["mcp"], "field_summary", {"layer": "REE Deposits", "field": "TONS"}))
    assert n["numeric"]["max"] == 45000


def test_edit_flow(env):
    a = payload(call_tool(env["mcp"], "add_layer",
                          {"source": "C:\\GIS\\cm.gdb\\separation", "name": "Separation"}))
    assert a["added"]["name"] == "Separation"
    p = payload(call_tool(env["mcp"], "set_layer_properties",
                          {"layer": "Separation", "visible": False, "definition_query": "STATE = 'TX'"}))
    assert p["changed"] == {"visible": False, "definition_query": "STATE = 'TX'"}
    payload(call_tool(env["mcp"], "zoom_to_layer", {"layer": "Separation"}))
    r = payload(call_tool(env["mcp"], "remove_layer", {"layer": "Separation"}))
    assert r["removed"] == "Separation"
    assert payload(call_tool(env["mcp"], "save_project"))["saved"].endswith(".aprx")


def test_geoprocessing(env):
    h = payload(call_tool(env["mcp"], "tool_help", {"tool": "analysis.Buffer"}))
    assert h["usage"].startswith("Buffer_analysis")
    g = payload(call_tool(env["mcp"], "run_geoprocessing",
                          {"tool": "arcpy.analysis.Buffer",
                           "args": ["REE Deposits", "C:/GIS/cm.gdb/buf", "10 Kilometers"]}))
    assert g["outputs"] == ["C:/GIS/cm.gdb/buf"]
    bad = call_tool(env["mcp"], "run_geoprocessing",
                    {"tool": "analysis.Buffer", "args": ["a", "b", "bad"]})
    assert bad.isError and "Geoprocessing failed" in bad.content[0].text
    unknown = call_tool(env["mcp"], "run_geoprocessing", {"tool": "analysis.Nope"})
    assert unknown.isError and "Unknown geoprocessing tool" in unknown.content[0].text


def test_snapshot_returns_image(env):
    res = call_tool(env["mcp"], "snapshot_map")
    assert not res.isError and res.content[0].type == "image"
    assert res.content[0].mimeType == "image/png"
    res2 = call_tool(env["mcp"], "snapshot_map", {"layout": "main layout"})
    assert res2.content[0].type == "image"


def test_errors_are_helpful(env):
    r = call_tool(env["mcp"], "describe_layer", {"layer": "Nope"})
    assert r.isError and "REE Deposits" in r.content[0].text  # lists real layer names
    r = call_tool(env["mcp"], "run_arcpy", {"code": "print(1)"})
    assert r.isError and "turned off" in r.content[0].text


def test_bad_token_rejected(env):
    from arcpro_mcp import client
    st = json.loads(env["state"].read_text())
    good = st["token"]
    st["token"] = "wrong"
    env["state"].write_text(json.dumps(st))
    try:
        with pytest.raises(client.BridgeCallError, match="Bad bridge token"):
            client.call("ping")
    finally:
        st["token"] = good
        env["state"].write_text(json.dumps(st))


def test_bridge_down_message(env, tmp_path):
    from arcpro_mcp import client
    old = client.STATE_FILE
    client.STATE_FILE = tmp_path / "missing.json"
    try:
        with pytest.raises(client.BridgeUnavailable, match="Python window"):
            client.call("ping")
    finally:
        client.STATE_FILE = old


def test_run_arcpy_when_enabled(env):
    env["server"].bridge.allow_exec = True
    try:
        out = payload(call_tool(env["mcp"], "run_arcpy",
                                {"code": "print(aprx.filePath)\nresult = len(aprx.listMaps())"}))
        assert out["stdout"].strip().endswith(".aprx") and out["result"] >= 1
    finally:
        env["server"].bridge.allow_exec = False


def test_bridge_py39_syntax():
    """The bridge must parse on Python 3.9 grammar (older ArcGIS Pro)."""
    import ast
    src = (ROOT / "bridge" / "arcpro_bridge.py").read_text()
    ast.parse(src, feature_version=(3, 9))


def test_snapshot_without_open_view(env):
    """Blank-project case from the real Pro run: no map view open."""
    aprx = env["server"].bridge.aprx
    saved_view = aprx.activeView
    aprx.activeView = None
    try:
        payload(call_tool(env["mcp"], "create_map", {"name": "Scratch"}))
        res = call_tool(env["mcp"], "snapshot_map", {"map": "US Magnet Stack"})
        assert not res.isError and res.content[0].type == "image"
        # the temporary layout is cleaned up
        assert [l.name for l in aprx.listLayouts()] == ["Main Layout"]
        res = call_tool(env["mcp"], "snapshot_map")  # no view, no args -> first map
        assert not res.isError
    finally:
        aprx.activeView = saved_view


def test_zoom_opens_view(env):
    aprx = env["server"].bridge.aprx
    aprx.activeView = None
    z = payload(call_tool(env["mcp"], "zoom_to_layer", {"layer": "REE Deposits"}))
    assert z["map"] == "US Magnet Stack" and aprx.activeView is not None


def test_add_layer_service_check(env):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b'{"error":{"code":499,"message":"Token Required"}}' if "retired" in self.path
                    else b'{"type":"Feature Layer","name":"ok"}')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        bad = call_tool(env["mcp"], "add_layer", {"source": base + "/retired/FeatureServer/0"})
        assert bad.isError and "499" in bad.content[0].text and "search_arcgis_online" in bad.content[0].text
        ok = payload(call_tool(env["mcp"], "add_layer",
                               {"source": base + "/good/FeatureServer/0", "name": "svc"}))
        assert ok["added"]["name"] == "svc"
        payload(call_tool(env["mcp"], "remove_layer", {"layer": "svc"}))
    finally:
        srv.shutdown()


def test_layers_outside_active_map(env):
    """Real-Pro failure #2: ListFields/GetCount/Describe and GP by name fail for layers
    that are not in Pro's active map. The bridge must fall back."""
    import fake_arcpy as fa
    fa.STRICT["on"] = True
    try:
        d = payload(call_tool(env["mcp"], "describe_layer", {"layer": "REE Deposits"}))
        assert d["feature_count"] == 4 and any(f["name"] == "TONS" for f in d["fields"])
        q = payload(call_tool(env["mcp"], "query_layer",
                              {"layer": "REE Deposits", "where": "STATE = 'WY'",
                               "include_geometry": True}))
        assert q["row_count"] == 2
        # GP by layer name -> bridge passes the Layer object
        del fa.CALLS[:]
        g = payload(call_tool(env["mcp"], "run_geoprocessing",
                              {"tool": "analysis.Buffer",
                               "args": ["REE Deposits", "memory/buf", "10 Kilometers"]}))
        assert isinstance(fa.CALLS[-1], fa.Layer) and "note" not in g
        # if Pro also rejects layer objects -> temporary layer keeping the definition query
        fa.STRICT["gp_rejects_layer_objects"] = True
        lyr = env["server"].bridge.aprx.listMaps()[0].listLayers()[0]
        lyr.definitionQuery = "STATE = 'WY'"
        g = payload(call_tool(env["mcp"], "run_geoprocessing",
                              {"tool": "analysis.Buffer",
                               "args": ["REE Deposits", "memory/buf", "10 Kilometers"]}))
        assert isinstance(fa.CALLS[-1], fa.TempLayer)
        assert fa.CALLS[-1].definitionQuery == "STATE = 'WY'"
        assert "temporary layers" in g["note"] and fa.TEMP_LAYERS == []  # cleaned up
    finally:
        fa.STRICT["on"] = False
        fa.STRICT["gp_rejects_layer_objects"] = False
        env["server"].bridge.aprx.listMaps()[0].listLayers()[0].definitionQuery = ""
