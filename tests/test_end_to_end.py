"""End-to-end test: MCP client -> arcpro-mcp server -> TCP bridge -> fake arcpy."""

import json
from pathlib import Path

import anyio
import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent


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
    import fake_arcpy as fa
    original = fa.GetActivePortalURL
    fa.GetActivePortalURL = lambda: base + "/"  # the signed-in portal may be a private host
    try:
        bad = call_tool(env["mcp"], "add_layer", {"source": base + "/retired/FeatureServer/0"})
        assert bad.isError and "499" in bad.content[0].text and "search_arcgis_online" in bad.content[0].text
        ok = payload(call_tool(env["mcp"], "add_layer",
                               {"source": base + "/good/FeatureServer/0", "name": "svc"}))
        assert ok["added"]["name"] == "svc"
        payload(call_tool(env["mcp"], "remove_layer", {"layer": "svc"}))
    finally:
        fa.GetActivePortalURL = original
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



def test_snapshot_uses_view_extent(env):
    """Real-Pro bug: direct MapView export padded the image with black bars."""
    from arcpro_mcp import client
    aprx = env["server"].bridge.aprx
    m = aprx.listMaps()[0]
    aprx.activeView = None
    m.openView()
    import fake_arcpy as fa
    aprx.activeView.camera.setExtent(fa.Extent(1, 2, 3, 4))
    res = client.call("export_image")
    assert res["source"] == "map:US Magnet Stack (current view extent)"
    # naming a map frames on its data (0.2.0); the open view is used only on request
    res = client.call("export_image", map="US Magnet Stack")
    assert "zoomed to visible data" in res["source"]
    res = client.call("export_image", map="US Magnet Stack", use_view_extent=True)
    assert "current view extent" in res["source"]
    assert [l.name for l in aprx.listLayouts()] == ["Main Layout"]



def test_snapshot_frame_pinned_to_page(env):
    """Real-Pro bug: temp-layout map frame came out shifted (blank band top/left)."""
    import fake_arcpy as fa
    aprx = env["server"].bridge.aprx
    made = []
    orig = aprx.createLayout

    def spy(*a, **k):
        lyt = orig(*a, **k)
        made.append(lyt)
        return lyt

    aprx.createLayout = spy
    try:
        from arcpro_mcp import client
        client.call("export_image", map="US Magnet Stack", width=960, height=480)
    finally:
        del aprx.createLayout
    mf = made[0].frame
    assert (mf.elementPositionX, mf.elementPositionY) == (0, 0)
    assert (mf.elementWidth, mf.elementHeight) == (10.0, 5.0)  # 960x480 px at 96 dpi


# ---------------------------------------------------------------------------
# 0.2.0 backlog
# ---------------------------------------------------------------------------

def _wait_idle(bridge, seconds=5):
    import time
    end = time.time() + seconds
    while time.time() < end:
        if bridge.ping()["busy"] is None:
            return
        time.sleep(0.05)
    raise AssertionError("bridge stayed busy")


def test_render_timeout_returns_error_and_reports_busy(env):
    """Backlog 1: a hung render returned nothing and took the bridge down."""
    import fake_arcpy as fa
    from arcpro_mcp import client
    bridge = env["server"].bridge
    fa.HANG.clear()
    try:
        with pytest.raises(client.BridgeCallError, match="did not finish within 1s"):
            client.call("export_image", layout="Main Layout", timeout=1)
        st = payload(call_tool(env["mcp"], "bridge_status"))  # answers while Pro is "hung"
        assert st["busy"]["command"] == "export_image" and st["busy"]["timed_out_after"] == 1
        with pytest.raises(client.BridgeCallError, match="still busy with export_image"):
            client.call("project_info")
        res = call_tool(env["mcp"], "snapshot_map", {"layout": "Main Layout", "timeout": 1})
        assert res.isError and "still busy" in res.content[0].text
    finally:
        fa.HANG.set()
    _wait_idle(bridge)
    assert payload(call_tool(env["mcp"], "project_info"))["maps"]
    assert payload(call_tool(env["mcp"], "bridge_status"))["busy"] is None


def test_errors_keep_traceback_from_worker_thread(env):
    from arcpro_mcp import client
    bridge = env["server"].bridge
    orig = bridge.commands["export_layout_pdf"]
    bridge.commands["export_layout_pdf"] = lambda **k: 1 / 0
    try:
        with pytest.raises(client.BridgeCallError, match="ZeroDivisionError") as ei:
            client.call("export_layout_pdf", path="x.pdf")
        assert "Traceback" in str(ei.value)
    finally:
        bridge.commands["export_layout_pdf"] = orig


def test_schema_guard(env):
    """Backlog 2: AddFields / Delete on a drawn dataset crashed Pro."""
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    mcp = env["mcp"]

    def gp(tool, args=None, kwargs=None, **extra):
        a = {"tool": tool, "args": args or [], "add_outputs_to_map": False}
        if kwargs:
            a["kwargs"] = kwargs
        a.update(extra)
        return call_tool(mcp, "run_geoprocessing", a)

    for target in ("REE Deposits", "C:\\GIS\\CM.gdb\\Deposits", "deposits"):
        r = gp("management.AddField", [target, "src", "TEXT"])
        assert r.isError and "Refused" in r.content[0].text, target
        assert "US Magnet Stack: REE Deposits" in r.content[0].text
        assert "allow_exec" in r.content[0].text
    r = gp("management.JoinField", kwargs={"in_data": "REE Deposits", "in_field": "NAME",
                                            "join_table": "x", "join_field": "NAME"})
    assert r.isError and "adds joined fields" in r.content[0].text
    r = gp("management.Delete", ["C:/GIS/cm.gdb/plants;C:/GIS/cm.gdb/unused_fc"])
    assert r.isError and "Magnet Plants" in r.content[0].text
    assert fa.SCHEMA_CALLS == []
    # filling an existing field is a data edit; a new field is a schema change
    payload(gp("management.CalculateField", ["REE Deposits", "TONS", "0", "ARCADE"]))
    r = gp("management.CalculateField", ["REE Deposits", "NEW_FIELD", "0", "ARCADE"])
    assert r.isError and "adds a new field" in r.content[0].text
    # datasets no map draws are fine, unless something else holds a lock
    payload(gp("management.Delete", ["C:/GIS/cm.gdb/unused_fc"]))
    fa.LOCKED.add("C:/GIS/cm.gdb/unused_fc")
    try:
        r = gp("management.AddField", ["C:/GIS/cm.gdb/unused_fc", "f", "TEXT"])
        assert r.isError and "schema lock" in r.content[0].text
    finally:
        fa.LOCKED.clear()
    # the override needs trusted mode, which only the user can turn on, in Pro
    r = gp("management.AddField", ["REE Deposits", "src", "TEXT"], unsafe_schema_change=True)
    assert r.isError and "allow_exec" in r.content[0].text
    env["server"].bridge.allow_exec = True
    try:
        payload(gp("management.AddField", ["REE Deposits", "src", "TEXT"],
                   unsafe_schema_change=True))
    finally:
        env["server"].bridge.allow_exec = False
    assert [c[0] for c in fa.SCHEMA_CALLS] == ["CalculateField", "Delete", "AddField"]


def test_schema_guard_allows_undrawn_memory_datasets(env):
    """Real Pro 3.5: TestSchemaLock is False for every memory dataset, so it proves nothing."""
    for path in ("memory\\smoke_log", "memory/smoke_log", "in_memory\\smoke_log"):
        payload(call_tool(env["mcp"], "run_geoprocessing",
                          {"tool": "management.AddField", "args": [path, "layer", "TEXT"],
                           "add_outputs_to_map": False}))


def test_lock_error_says_do_not_retry(env):
    r = call_tool(env["mcp"], "run_geoprocessing",
                  {"tool": "conversion.ExportFeatures", "args": ["C:/GIS/cm.gdb/unused_fc", "out"]})
    assert r.isError and "000464" in r.content[0].text and "Do not retry" in r.content[0].text


def test_startup_hint_is_one_line(env):
    """Backlog 3: two-line pastes break in Pro's Python window."""
    from arcpro_mcp import client
    line = [l for l in client.not_running().splitlines() if "exec(" in l][0]
    assert "; start(" in line
    import ast
    ast.parse(line.strip().replace("<path to arcpro-mcp>", "C:/x"))
    src = (ROOT / "bridge" / "ArcProMCP.pyt").read_text()
    ast.parse(src, feature_version=(3, 9))
    assert "allow_exec" in src and "class StartBridge" in src


def test_toolbox_starts_and_stops_bridge(env, tmp_path):
    import builtins
    import types
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("pyt", str(ROOT / "bridge" / "ArcProMCP.pyt"))
    pyt = types.ModuleType("pyt")
    pyt.__file__ = str(ROOT / "bridge" / "ArcProMCP.pyt")
    import fake_arcpy as fa
    msgs = []
    fa.AddMessage = msgs.append
    fa.Parameter = lambda **k: types.SimpleNamespace(value=None, **k)
    exec(compile(loader.get_data(pyt.__file__), pyt.__file__, "exec"), pyt.__dict__)
    original = builtins._arcpro_mcp_bridge
    old_state = env["bridge"].STATE_FILE
    try:
        tool = pyt.StartBridge()
        params = tool.getParameterInfo()
        params[0].value, params[1].value = True, 0
        # the toolbox loads its own copy of the bridge module; point its state file here
        real = pyt._bridge

        def patched():
            mod = real()
            mod.STATE_DIR, mod.STATE_FILE = str(tmp_path), str(tmp_path / "bridge.json")
            return mod
        pyt._bridge = patched
        tool.execute(params, None)
        server = builtins._arcpro_mcp_bridge
        assert server is not original and server.bridge.allow_exec is True
        assert any("run_arcpy ON" in m for m in msgs)
        pyt.BridgeStatus().execute([], None)
        assert any(m.startswith("mode: live") for m in msgs)
        pyt.StopBridge().execute([], None)
        assert builtins._arcpro_mcp_bridge is None
    finally:
        # bring the test bridge back for the remaining tests
        env["server"] = env["bridge"].start("CURRENT", port=0, background=True, quiet=True)
        env["bridge"].STATE_FILE = old_state


def test_tables(env):
    """Backlog 4: query_layer could not read standalone tables like source_log."""
    mcp = env["mcp"]
    added = payload(call_tool(mcp, "add_layer", {"source": "C:/GIS/cm.gdb/source_log"}))
    assert added["added"]["is_table"] is True
    ll = payload(call_tool(mcp, "list_layers"))
    assert [t["name"] for t in ll["tables"]] == ["source_log"]
    q = payload(call_tool(mcp, "query_layer", {"layer": "source_log"}))
    assert q["row_count"] == 1 and q["rows"][0]["dataset"] == "Esri World Countries"
    d = payload(call_tool(mcp, "describe_layer", {"layer": "source_log"}))
    assert d["row_count"] == 1 and d["dataset_type"] == "Table"
    assert "feature_count" not in d and any(f["name"] == "layer" for f in d["fields"])
    s = payload(call_tool(mcp, "field_summary", {"layer": "source_log", "field": "layer"}))
    assert s["top_values"] == [{"value": "L1", "count": 1}]
    import fake_arcpy as fa
    fa.STRICT["tables_by_name_fail"] = True  # table not in Pro's active map
    try:
        q = payload(call_tool(mcp, "query_layer", {"layer": "source_log"}))
        assert q["row_count"] == 1
    finally:
        fa.STRICT["tables_by_name_fail"] = False
    # not in any map: read straight from the default geodatabase, or by full path
    q = payload(call_tool(mcp, "query_layer", {"layer": "gaps_log"}))
    assert q["row_count"] == 0 and q["layer"].endswith("gaps_log")
    q = payload(call_tool(mcp, "query_layer", {"layer": "C:/GIS/cm.gdb/source_log"}))
    assert q["row_count"] == 1
    r = call_tool(mcp, "query_layer", {"layer": "nothing_here"})
    assert r.isError and "list_workspace" in r.content[0].text
    assert payload(call_tool(mcp, "remove_layer", {"layer": "source_log"}))["removed"] == "source_log"


def test_list_workspace(env):
    """Backlog 5."""
    env["server"].bridge.removed_sources.clear()  # layers removed by earlier tests
    w = payload(call_tool(env["mcp"], "list_workspace"))
    by = {i["name"]: i for i in w["items"]}
    assert w["workspace"] == "C:/GIS/cm.gdb"
    assert by["deposits"]["type"] == "FeatureClass" and by["deposits"]["count"] == 4
    assert by["deposits"]["used_by_layers"] == ["US Magnet Stack: REE Deposits"]
    assert by["source_log"]["type"] == "Table" and "used_by_layers" not in by["source_log"]
    assert by["L6_flows"]["feature_dataset"] == "Stack"
    assert by["L6_flows"]["path"] == "C:/GIS/cm.gdb/Stack/L6_flows"
    import fake_arcpy as fa
    assert fa.env.workspace is None  # restored
    r = call_tool(env["mcp"], "list_workspace", {"workspace": "C:/nope.gdb"})
    assert r.isError and "does not exist" in r.content[0].text


def _json_server(routes):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            body = routes(path, query)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def test_describe_url(env):
    """Backlog 6: vet a service (owner, count, extent) without adding it to a map."""
    layer = {"name": "Rare Earth Occurrences", "type": "Feature Layer",
             "geometryType": "esriGeometryPoint", "maxRecordCount": 2000,
             "extent": {"xmin": -125, "ymin": 24, "xmax": -66, "ymax": 49,
                        "spatialReference": {"wkid": 4326}},
             "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OID"}]}

    def routes(path, query):
        if path.endswith("/FeatureServer/0/query"):
            if "returnCountOnly" in query:
                return {"count": 5}
            return {"extent": {"xmin": -101, "ymin": 29, "xmax": -97, "ymax": 31}}
        if path.endswith("/FeatureServer/0"):
            return layer
        if path.endswith("/FeatureServer"):
            return {"serviceDescription": "x", "layers": [{"id": 0, "name": "REE",
                                                          "geometryType": "esriGeometryPoint"}]}
        if "/sharing/rest/content/items/" + ITEM in path:
            return {"id": ITEM, "title": "Rare Earth Occurrences", "owner": "mconway",
                    "type": "Feature Service", "created": 1700000000000,
                    "url": BASE[0] + "/svc/FeatureServer"}
        return {"error": {"code": 400, "message": "Invalid URL"}}

    ITEM = "a" * 32
    BASE = [None]
    srv, base = _json_server(routes)
    BASE[0] = base
    import fake_arcpy as fa
    fa.GetActivePortalURL = lambda: base + "/"  # the signed-in portal may be a private host
    try:
        d = payload(call_tool(env["mcp"], "describe_url",
                              {"url": base + "/svc/FeatureServer/0"}))
        assert d["feature_count"] == 5 and d["layer"]["name"] == "Rare Earth Occurrences"
        assert d["data_extent_wgs84"]["xmin"] == -101
        assert d["layer"]["declared_extent"]["xmin"] == -125
        assert d["layer"]["spatial_reference_wkid"] == 4326
        root = payload(call_tool(env["mcp"], "describe_url", {"url": base + "/svc/FeatureServer"}))
        assert root["service"]["layers"][0]["name"] == "REE" and "Append" in root["note"]
        it = payload(call_tool(env["mcp"], "describe_url", {"item_id": ITEM}))
        assert it["item"]["owner"] == "mconway" and it["item"]["created"] == "2023-11-14"
        assert "service" in it
        bad = call_tool(env["mcp"], "describe_url", {"url": base + "/wrong"})
        assert bad.isError and "error 400" in bad.content[0].text
        lyrs = payload(call_tool(env["mcp"], "list_layers"))["layers"]
        assert not any("Rare" in l["name"] for l in lyrs)  # nothing was added
    finally:
        def _no_portal():
            raise RuntimeError("not signed in")
        fa.GetActivePortalURL = _no_portal
        srv.shutdown()


def test_insert_rows(env):
    """Backlog 7: append rows to log tables without writing a CSV."""
    import datetime
    import fake_arcpy as fa
    rows = fa.DATASETS["C:/GIS/cm.gdb/gaps_log"]["rows"]
    del rows[:]
    r = payload(call_tool(env["mcp"], "insert_rows", {"table": "gaps_log", "rows": [
        {"Layer": "L3", "dataset": "Myanmar ionic clay mines", "data_year": 2024,
         "accessed_date": "2026-11-05"},
        {"layer": "L4", "dataset": "Refinery points"},
    ]}))
    assert r["inserted"] == 2 and r["object_ids"] == [1, 2]
    assert rows[0]["layer"] == "L3" and rows[0]["accessed_date"] == datetime.datetime(2026, 11, 5)
    assert rows[1]["data_year"] is None
    q = payload(call_tool(env["mcp"], "query_layer", {"layer": "gaps_log"}))
    assert q["row_count"] == 2 and q["rows"][0]["accessed_date"] == "2026-11-05T00:00:00"
    bad = call_tool(env["mcp"], "insert_rows", {"table": "gaps_log", "rows": [{"nope": 1}]})
    assert bad.isError and "No field 'nope'" in bad.content[0].text
    bad = call_tool(env["mcp"], "insert_rows", {"table": "gaps_log", "rows": [{"OBJECTID": 9}]})
    assert bad.isError and "set automatically" in bad.content[0].text
    bad = call_tool(env["mcp"], "insert_rows", {"table": "gaps_log",
                                                "rows": [{"accessed_date": "last week"}]})
    assert bad.isError and "ISO date" in bad.content[0].text
    bad = call_tool(env["mcp"], "insert_rows", {"table": "gaps_log",
                                                "rows": [{"layer": "a", "LAYER": "b"}]})
    assert bad.isError and "twice" in bad.content[0].text
    assert len(rows) == 2


def test_save_copy_creates_folder(env, tmp_path):
    """Backlog 8."""
    target = tmp_path / "backups" / "nested" / "CM_Magnet_Stack_2026-11-05.aprx"
    r = payload(call_tool(env["mcp"], "save_project", {"copy_to": str(target)}))
    assert r["saved_copy"] == str(target) and r["created_folder"] == str(target.parent)
    r = payload(call_tool(env["mcp"], "save_project", {"copy_to": str(target)}))
    assert "created_folder" not in r
    bad = call_tool(env["mcp"], "save_project", {"copy_to": str(tmp_path / "backup.zip")})
    assert bad.isError and ".aprx" in bad.content[0].text
    pdf = tmp_path / "exports" / "map.pdf"
    payload(call_tool(env["mcp"], "export_layout_pdf", {"path": str(pdf)}))
    assert pdf.exists()


def test_create_map_without_basemap(env):
    """Backlog 9."""
    r = payload(call_tool(env["mcp"], "create_map", {"name": "With Basemap"}))
    assert "removed_default_layers" not in r
    assert payload(call_tool(env["mcp"], "list_layers", {"map": "With Basemap"}))["layers"]
    r = payload(call_tool(env["mcp"], "create_map", {"name": "Empty", "include_basemap": False}))
    assert r["layer_count"] == 0
    assert set(r["removed_default_layers"]) == {"World Topographic Map", "World Hillshade"}


def test_snapshot_framing(env):
    """Backlog 10: snapshots of a named map came out zoomed out and offset."""
    import fake_arcpy as fa
    aprx = env["server"].bridge.aprx
    m = fa.Map("Framing")
    m.spatialReference = fa.SpatialReference(54012)
    world = fa.Layer("Hidden world layer")
    world.visible = False
    group = fa.Layer("Hidden group")
    group.isGroupLayer, group.isFeatureLayer, group.visible = True, False, False
    child = fa.Layer("Child of hidden group", parent=group)
    base = fa.Layer("Basemap", source=None)
    base.isBasemapLayer, base.isFeatureLayer = True, False
    deg = fa.Layer("Deposits in degrees")
    met = fa.Layer("Plants in meters")
    m._layers = [deg, met, world, group, child, base]
    fa.LAYER_EXTENTS.clear()
    fa.LAYER_EXTENTS.update({
        "Hidden world layer": fa.Extent(-1.7e7, -8e6, 1.7e7, 8e6, fa.SpatialReference(54012)),
        "Child of hidden group": fa.Extent(-1.7e7, -8e6, 1.7e7, 8e6, fa.SpatialReference(54012)),
        "Deposits in degrees": fa.Extent(-10, 20, 10, 40, fa.SpatialReference(4326)),
        "Plants in meters": fa.Extent(-500000, 1000000, 2000000, 3000000,
                                      fa.SpatialReference(54012)),
    })
    aprx._maps.append(m)
    del fa.PROJECTED[:]
    try:
        from arcpro_mcp import client
        res = client.call("export_image", map="Framing")
        assert res["framed_on"] == ["Deposits in degrees", "Plants in meters"]
        assert res["hidden_layers_ignored"] == 2 and fa.PROJECTED == [4326]
        e = res["extent"]  # union in Eckert IV units (degrees * 1e5 in the toy projection)
        assert round(e["xmin"]) == round(-1000000 - 3000000 * 0.03)
        assert round(e["xmax"]) == round(2000000 + 3000000 * 0.03)
        assert e["spatial_reference"] == "World_Eckert_IV"
        one = client.call("export_image", map="Framing", layer="Plants in meters")
        assert one["framed_on"] == ["Plants in meters"] and "zoomed to layer" in one["source"]
        # through MCP: image plus a line of framing info
        res = call_tool(env["mcp"], "snapshot_map", {"map": "Framing"})
        assert [c.type for c in res.content] == ["image", "text"]
        assert json.loads(res.content[1].text)["framed_on"][0] == "Deposits in degrees"
        # nothing visible: frame on everything instead of nothing
        deg.visible = met.visible = False
        res = client.call("export_image", map="Framing")
        assert len(res["framed_on"]) == 4
    finally:
        aprx._maps.remove(m)
        fa.LAYER_EXTENTS.clear()


def test_client_reports_dropped_connection(env, tmp_path):
    """A crash mid-call used to read as 'bridge not running'."""
    import socket
    import threading
    from arcpro_mcp import client
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def accept_and_drop():
        conn, _ = srv.accept()
        conn.recv(1000)
        conn.close()

    threading.Thread(target=accept_and_drop, daemon=True).start()
    state = tmp_path / "bridge.json"
    state.write_text(json.dumps({"port": srv.getsockname()[1], "token": "t",
                                 "bridge_version": client.__version__}))
    old = client.STATE_FILE
    client.STATE_FILE = state
    try:
        with pytest.raises(client.BridgeCallError, match="dropped while it was running 'export_image'"):
            client.call("export_image")
    finally:
        client.STATE_FILE = old
        srv.close()
