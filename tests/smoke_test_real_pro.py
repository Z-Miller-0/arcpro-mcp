"""Smoke test against a REAL ArcGIS Pro bridge. Stdlib only, Python 3.9 compatible.

It changes the project the bridge is attached to: it creates scratch maps, layouts, layers
and small test datasets in that project's default geodatabase. Point it at a scratch project.

1. Open a scratch project in ArcGIS Pro and start the bridge in its Python window.
2. From a terminal (any Python 3.9+, including Pro's propy.bat), run:
       python smoke_test_real_pro.py --project "C:\\path\\to\\scratch.aprx"

It stops unless --project is the project the bridge reports, so it cannot run against your
real work by accident. It never saves the project. A JSON report with timings is written
next to this file (smoke_report.json), and snapshots beside it.
"""

import argparse
import base64
import http.server
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

STATE = os.path.join(os.path.expanduser("~"), ".arcpro_mcp", "bridge.json")
HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED_BRIDGE = "0.4.0"  # the attack cases below are only safe against guards this new
TEST_MAP = "arcpro_mcp_test"
NOWHERE = "arcpro-mcp-test.invalid"  # .invalid never resolves, so no request can leave
STATES_URL = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
              "USA_States_Generalized_Boundaries/FeatureServer/0")
RETIRED_URL = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
               "USA_States_Generalized/FeatureServer/0")

results = []


def call(cmd, **params):
    with open(STATE) as fh:
        st = json.load(fh)
    req = {"id": 1, "token": st["token"], "cmd": cmd, "params": params}
    s = socket.create_connection(("127.0.0.1", st["port"]), timeout=10)
    s.settimeout(600)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def _scrub(value):
    """Never print or store a token, even when a security check fails."""
    text = re.sub(r"(?i)(token['\"]?\s*[:=]\s*['\"]?)[^'\"&\s,}]+", r"\1<removed>", str(value))
    return text


def step(label, cmd, expect_error=False, expect_text=None, **params):
    t0 = time.time()
    try:
        resp = call(cmd, **params)
        ok = resp.get("ok") != expect_error
        detail = resp.get("result") if resp.get("ok") else resp.get("error")
        tb = resp.get("traceback")
        if ok and expect_text and expect_text.lower() not in str(detail).lower():
            ok = False
    except Exception as e:
        resp, ok, detail, tb = {}, False, "%s: %s" % (type(e).__name__, e), traceback.format_exc()
    dt = round(time.time() - t0, 2)
    if expect_error and resp.get("ok"):
        detail = "<this should have been refused; its output is not shown>"
    if isinstance(detail, dict) and "png_base64" in detail:
        detail = dict(detail, png_base64="<%d chars>" % len(detail["png_base64"]))
    if isinstance(detail, str):
        detail = _scrub(detail)
    if tb:
        tb = _scrub(tb)
    rec = {"step": label, "cmd": cmd, "ok": ok, "seconds": dt,
           "detail": detail if len(json.dumps(detail, default=str)) < 3000
           else str(detail)[:3000] + "...", "traceback": tb}
    results.append(rec)
    print("%-4s %6.2fs  %s" % ("PASS" if ok else "FAIL", dt, label))
    if not ok:
        print("       " + str(detail)[:600].replace("\n", "\n       "))
    return resp.get("result") if resp.get("ok") else None


def check(label, passed, detail=""):
    results.append({"step": label, "cmd": "(check)", "ok": bool(passed), "seconds": 0,
                    "detail": str(detail)[:600], "traceback": None})
    print("%-4s %6.2fs  %s" % ("PASS" if passed else "FAIL", 0, label))
    if not passed:
        print("       " + str(detail)[:600])


def snapshot(label, **params):
    img = step(label, "export_image", **params)
    if img and img.get("png_base64"):
        data = base64.b64decode(img["png_base64"])
        check(label + ": real PNG", data.startswith(b"\x89PNG"), len(data))
        print("       source: %s, %d bytes" % (img.get("source"), len(data)))
        keep = os.path.join(HERE, "smoke_snapshot_%d.png" % len(results))
        with open(keep, "wb") as fh:
            fh.write(data)
        print("       saved " + keep)
    return img


def security_steps(info):
    """Every one of these got through, or leaked, before 0.3.0."""
    print("\n--- security: only real tools resolve")
    for tool in ("os.getpid", "getpid_os", "time.time", "os.system", "sys.exit",
                 "__loader__.get_data", "arcpy.arcpy.GetSigninToken", "gp.getSigninToken",
                 "env.__setattr__", "numpy.fromfile", "toolbox.Delete_management",
                 "analysis.__dict__", "management.Delete.__call__", "da.SearchCursor",
                 "server.StageService", "mp.ArcGISProject", "utils.gp"):
        step("refused: %s" % tool, "run_geoprocessing", expect_error=True,
             expect_text="Unknown geoprocessing tool", tool=tool, args=[])
    for tool in ("ImportToolbox", "arcpy.ImportToolbox", "Foo"):
        step("refused: %s" % tool, "run_geoprocessing", expect_error=True, tool=tool, args=[])
    step("refused: tool_help on os.getpid", "tool_help", expect_error=True, tool="os.getpid")

    print("\n--- security: Python-running and publishing tools")
    step("refused: CalculateValue", "run_geoprocessing", expect_error=True, expect_text="Refused",
         tool="management.CalculateValue", args=["6*7"])
    for tool, args in (
            ("management.GeoTaggedPhotosToPoints", ["C:/x", "memory/x"]),
            ("management.LoadDataUsingWorkspace", ["x"]),
            ("management.SharePackage", ["x", "y", "z"]),
            ("management.UploadFileToPortal", ["x"]),
            ("ia.TrainDeepLearningModel", ["x", "y"])):
        step("refused: %s" % tool, "run_geoprocessing", expect_error=True, expect_text="Refused",
             tool=tool, args=args)
    step("refused: sa.RasterCalculator", "run_geoprocessing", expect_error=True,
         tool="sa.RasterCalculator", args=[["x"], ["y"], "x"])

    print("\n--- security: sign-in token and URLs")
    seen = []

    class Listener(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(sorted(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"name":"x","type":"Feature Layer","fields":[]}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Listener)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    local = "http://127.0.0.1:%d/arcgis/rest/services/x/FeatureServer/0" % srv.server_address[1]
    step("refused: describe_url to localhost", "describe_url", expect_error=True,
         expect_text="Refused", url=local)
    check("localhost listener received no request at all", seen == [], seen)
    srv.shutdown()
    for label, url in (("file scheme", "file:///C:/Windows/win.ini#"),
                       ("ftp scheme", "ftp://%s/x" % NOWHERE),
                       ("no scheme", "services.arcgis.com/x"),
                       ("metadata address", "http://169.254.169.254/latest/meta-data/"),
                       ("user info trick", "https://www.arcgis.com@%s/x/FeatureServer/0" % NOWHERE),
                       ("spaces", "https://%s/a b" % NOWHERE)):
        step("refused: describe_url %s" % label, "describe_url", expect_error=True, url=url)
    step("refused: bad item id", "describe_url", expect_error=True, item_id="../../portals/self")
    r = call("describe_url", url="not-a-url")
    check("a malformed URL error does not contain a token", "token=" not in str(r.get("error")),
          "<hidden>" if "token=" in str(r.get("error")) else r.get("error"))

    print("\n--- security: network paths")
    unc = "\\\\%s\\share\\x.gdb\\fc" % NOWHERE
    step("refused: query_layer on a network path", "query_layer", expect_error=True,
         expect_text="network path", layer=unc)
    step("refused: add_layer on a network path", "add_layer", expect_error=True,
         expect_text="network path", source=unc, map=TEST_MAP)
    step("refused: list_workspace on a network path", "list_workspace", expect_error=True,
         expect_text="network path", workspace="\\\\%s\\share" % NOWHERE)
    step("refused: GP output on a network path", "run_geoprocessing", expect_error=True,
         expect_text="network path", tool="management.CopyFeatures", args=["x", unc])
    step("refused: network path inside a field mapping", "run_geoprocessing", expect_error=True,
         expect_text="network path", tool="management.CopyFeatures",
         args=["x", "memory\\y"], kwargs={"config_keyword": "a 1 %s" % unc})
    step("refused: export to a network path", "export_layout", expect_error=True,
         expect_text="network path", path="\\\\%s\\share\\x.pdf" % NOWHERE)
    step("refused: project copy to a network path", "save_project", expect_error=True,
         expect_text="network path", copy_to="\\\\%s\\share\\x.aprx" % NOWHERE)

    print("\n--- security: override flag and output files")
    step("refused: unsafe_schema_change without allow_exec", "run_geoprocessing",
         expect_error=True, expect_text="allow_exec", tool="management.AddField",
         args=["memory\\nope", "x", "TEXT"], unsafe_schema_change=True)
    step("refused: export with a non-layout extension", "export_layout", expect_error=True,
         path=os.path.join(tempfile.gettempdir(), "x.bat"))
    step("refused: relative export path", "export_layout", expect_error=True, path="x.pdf")
    step("refused: order_by with SQL in it", "query_layer", expect_error=True,
         layer="memory\\nope", order_by="1; DROP TABLE x")
    step("refused: huge snapshot", "export_image", expect_error=True, width=8000, height=8000)
    step("refused: negative render timeout", "export_image", expect_error=True, timeout=-1)

    print("\n--- security: socket")
    with open(STATE) as fh:
        st = json.load(fh)

    def raw(payload):
        s = socket.create_connection(("127.0.0.1", st["port"]), timeout=5)
        s.settimeout(5)
        s.sendall(payload)
        got = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    return got, True  # the bridge closed the connection
                got += chunk
        except socket.timeout:
            return got, False
        finally:
            s.close()

    for label, payload in (("HTTP request", b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"),
                           ("bad JSON", b"{nope\n"), ("JSON array", b"[1,2]\n"),
                           ("wrong token", b'{"id":1,"token":"wrong","cmd":"ping"}\n'),
                           ("non-string token", b'{"id":1,"token":5,"cmd":"ping"}\n')):
        got, closed = raw(payload)
        check("%s: refused, connection closed, no traceback" % label,
              closed and b'"ok": false' in got and b"Traceback" not in got and b"arcpro" not in got,
              got[:200])
    two = socket.socket()
    two.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        two.bind(("127.0.0.1", st["port"]))
        two.listen(1)
        check("another socket cannot share the bridge port", False, "it bound the port")
    except OSError as e:
        check("another socket cannot share the bridge port", True, e)
    finally:
        two.close()


def guard_steps(gdb):
    print("\n--- schema guard: datasets a map draws, and containers")
    stamp = int(time.time())
    fc = "smoke_drawn_%d" % stamp
    path = gdb + "\\" + fc
    tbl = "smoke_join_%d" % stamp
    step("create_dataset (feature class with fields)", "create_dataset", name=fc,
         geometry_type="POINT", spatial_reference_wkid=4326,
         fields=[{"name": "site", "type": "TEXT", "length": 50}, {"name": "tons", "type": "LONG"}])
    step("create_dataset (table)", "create_dataset", name=tbl,
         fields=[{"name": "site", "type": "TEXT", "length": 50}, {"name": "note", "type": "TEXT"}])
    step("refused: create_dataset over an existing dataset", "create_dataset", expect_error=True,
         expect_text="already exists", name=fc)
    step("refused: create_dataset outside the project", "create_dataset", expect_error=True,
         expect_text="outside the open project", name="smoke_out", workspace="C:\\Windows")
    step("insert_rows with geometry", "insert_rows", table=path, rows=[
        {"site": "a", "tons": 5, "SHAPE@XY": [-79.0, 35.0]},
        {"site": "b", "tons": 50, "SHAPE@XY": [-78.0, 36.0]},
        {"site": "c", "tons": 500, "SHAPE@XY": [-77.0, 37.0]}])
    step("insert_rows into the table", "insert_rows", table=gdb + "\\" + tbl,
         rows=[{"site": "a", "note": "x"}])
    step("add_layer (drawn dataset)", "add_layer", map=TEST_MAP, source=path, name="smoke_drawn")

    for tool in ("AddField", "DeleteField", "Delete", "DeleteRows", "TruncateTable", "Rename",
                 "DeleteFeatures", "DefineProjection", "AddXY"):
        args = {"AddField": [path, "x", "TEXT"], "DeleteField": [path, "tons"],
                "Rename": [path, path + "_r"], "DefineProjection": [path, 4326]}.get(tool, [path])
        step("refused on a drawn dataset: %s" % tool, "run_geoprocessing", expect_error=True,
             expect_text="drawn by", tool="management." + tool, args=args,
             add_outputs_to_map=False)
    step("refused: Delete by layer name", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.Delete", args=["smoke_drawn"], map=TEST_MAP)
    step("refused: Delete by keyword", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.Delete", kwargs={"in_data": path})
    step("refused: Delete_management spelling", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="Delete_management", args=[path])
    step("refused: Delete in a ';' list", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.Delete", args=["memory\\nope;" + path])
    step("refused: Delete the geodatabase", "run_geoprocessing", expect_error=True,
         expect_text="Refused", tool="management.Delete", args=[gdb])
    step("refused: Delete the project folder", "run_geoprocessing", expect_error=True,
         expect_text="Refused", tool="management.Delete", args=[os.path.dirname(gdb)])
    decoy_folder = tempfile.mkdtemp(prefix="arcpro_mcp_decoy_")
    decoy = os.path.join(decoy_folder, "decoy.csv")  # a file of ours, not a real one
    with open(decoy, "w") as fh:
        fh.write("a,b\n1,2\n")
    step("refused: Delete outside the project", "run_geoprocessing", expect_error=True,
         expect_text="outside the open project", tool="management.Delete", args=[decoy])
    check("the decoy file is still there", os.path.exists(decoy), decoy)
    step("reading a table outside the project still works", "query_layer", layer=decoy)
    os.remove(decoy)
    os.rmdir(decoy_folder)
    step("refused: a temporary layer over a drawn dataset", "run_geoprocessing", expect_error=True,
         expect_text="Refused", tool="management.AddField",
         args=[_temp_layer(path), "x", "TEXT"], add_outputs_to_map=False)
    step("refused: Delete on a name that cannot be identified", "run_geoprocessing",
         expect_error=True, expect_text="could not be identified", tool="management.Delete",
         args=["no_such_dataset_anywhere"])
    step("refused: overwrite a drawn output", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.CopyFeatures",
         args=[path, path], overwrite=True, add_outputs_to_map=False)
    other = "smoke_copy_%d" % stamp
    step("CopyFeatures with a drawn input is fine", "run_geoprocessing",
         tool="management.CopyFeatures", args=[path, gdb + "\\" + other], overwrite=True,
         add_outputs_to_map=False)
    step("overwrite an undrawn output is fine", "run_geoprocessing",
         tool="management.CopyFeatures", args=[path, gdb + "\\" + other], overwrite=True,
         add_outputs_to_map=False)
    step("refused: CalculateField in Python (the default)", "run_geoprocessing", expect_error=True,
         expect_text="ARCADE", tool="management.CalculateField",
         args=[gdb + "\\" + other, "tons", "1"])
    step("refused: CalculateField PYTHON3 by keyword", "run_geoprocessing", expect_error=True,
         expect_text="ARCADE", tool="management.CalculateField", args=[gdb + "\\" + other, "tons", "1"],
         kwargs={"expression_type": "PYTHON3", "code_block": "import os"})
    step("CalculateField in Arcade works", "run_geoprocessing", tool="management.CalculateField",
         args=[gdb + "\\" + other, "tons", "$feature.tons * 2", "ARCADE"], add_outputs_to_map=False)
    q = step("Arcade result is right", "query_layer", layer=gdb + "\\" + other, order_by="tons")
    check("tons doubled", q and q["rows"][0]["tons"] == 10, q)
    # Pro keeps a schema lock after an earlier tool used a dataset, so this needs a fresh one.
    spare = "smoke_spare_%d" % stamp
    step("create_dataset (nothing else has touched it)", "create_dataset", name=spare)
    step("AddField on an undrawn dataset works", "run_geoprocessing", tool="management.AddField",
         args=[gdb + "\\" + spare, "extra", "TEXT"], add_outputs_to_map=False)
    step("Delete an undrawn dataset in the project works", "run_geoprocessing",
         tool="management.Delete", args=[gdb + "\\" + spare], add_outputs_to_map=False)

    print("\n--- schema guard: tools no list names, found by their shape")
    decoy_folder2 = tempfile.mkdtemp(prefix="arcpro_mcp_decoy_")
    decoy2 = os.path.join(decoy_folder2, "keep.csv")
    with open(decoy2, "w") as fh:
        fh.write("a,b\n1,2\n")
    step("refused: DeleteMultiple on a drawn dataset", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.DeleteMultiple", args=[path])
    step("refused: DeleteMultiple on a folder outside the project", "run_geoprocessing",
         expect_error=True, expect_text="outside the open project",
         tool="management.DeleteMultiple", args=[decoy_folder2])
    check("the decoy folder is still there", os.path.exists(decoy2), decoy2)
    os.remove(decoy2)
    os.rmdir(decoy_folder2)
    step("refused: Near adds fields to a drawn layer", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="analysis.Near", args=[path, path], add_outputs_to_map=False)
    step("refused: ErasePoint on a drawn layer", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="edit.ErasePoint", args=[path, path, "INSIDE"])
    step("refused: Compact the geodatabase while a map draws from it", "run_geoprocessing",
         expect_error=True, expect_text="drawn by", tool="management.Compact", args=[gdb])
    step("refused: AddImagesFromCustomInputType", "run_geoprocessing", expect_error=True,
         expect_text="arbitrary Python", tool="oi.AddImagesFromCustomInputType", args=["x"])
    step("an output tool on a drawn input still works", "run_geoprocessing", tool="analysis.Clip",
         args=[path, _state_polygon(gdb, stamp), gdb + "\\smoke_clip_%d" % stamp],
         add_outputs_to_map=False)
    step("SelectLayerByAttribute on a drawn layer still works", "run_geoprocessing",
         tool="management.SelectLayerByAttribute", args=["smoke_drawn", "CLEAR_SELECTION"],
         map=TEST_MAP, add_outputs_to_map=False)
    step("GetCount on a drawn layer still works", "run_geoprocessing",
         tool="management.GetCount", args=["smoke_drawn"], map=TEST_MAP, add_outputs_to_map=False)
    step("refused: path tricks reach the same guard", "run_geoprocessing", expect_error=True,
         expect_text="drawn by", tool="management.DeleteRows", args=[path + "."])
    step("refused: a memory path climbing out", "delete_rows", expect_error=True, table=
         "memory\\..\\..\\Other.gdb\\T", where="a = 1")
    step("refused: a where clause escaping its parentheses", "delete_rows", expect_error=True,
         expect_text="where clause", table=path, where="1=1) OR (1=1")

    print("\n--- schema guard: joined tables and removed layers")
    joined = step("AddJoin on the drawn layer", "run_geoprocessing", tool="management.AddJoin",
                  args=["smoke_drawn", "site", gdb + "\\" + tbl, "site"], map=TEST_MAP,
                  add_outputs_to_map=False)
    if joined is not None:
        step("refused: AddField on a joined table", "run_geoprocessing", expect_error=True,
             expect_text="drawn by", tool="management.AddField", args=[gdb + "\\" + tbl, "y", "TEXT"],
             add_outputs_to_map=False)
        step("RemoveJoin", "run_geoprocessing", tool="management.RemoveJoin",
             args=["smoke_drawn"], map=TEST_MAP, add_outputs_to_map=False)
    return path, fc, tbl


def _state_polygon(gdb, stamp):
    """A polygon feature class to clip with."""
    name = "smoke_poly_%d" % stamp
    call("create_dataset", name=name, geometry_type="POLYGON", spatial_reference_wkid=4326)
    call("insert_rows", table=gdb + "\\" + name,
         rows=[{"SHAPE@WKT": "POLYGON((-80 34,-76 34,-76 38,-80 38,-80 34))"}])
    return gdb + "\\" + name


def _temp_layer(path):
    name = "smoke_tmp_%d" % int(time.time())
    call("run_geoprocessing", tool="management.MakeFeatureLayer", args=[path, name],
         add_outputs_to_map=False)
    return name


def row_steps(path):
    print("\n--- row edits")
    step("refused: update_rows without where", "update_rows", expect_error=True, table=path,
         where="", values={"tons": 1})
    step("refused: delete_rows without where", "delete_rows", expect_error=True, table=path, where=" ")
    step("refused: update_rows past max_rows", "update_rows", expect_error=True,
         expect_text="nothing was changed", table=path, where="tons > 0", values={"tons": 1},
         max_rows=1)
    step("refused: delete_rows past max_rows", "delete_rows", expect_error=True,
         expect_text="nothing was deleted", table=path, where="tons > 0", max_rows=1)
    step("refused: update_rows on the object id", "update_rows", expect_error=True, table=path,
         where="tons = 5", values={"OBJECTID": 1})
    r = step("update_rows", "update_rows", table=path, where="site = 'a'", values={"tons": 6})
    check("updated one row", r and r["updated"] == 1, r)
    r = step("delete_rows", "delete_rows", table=path, where="site = 'c'")
    check("deleted one row", r and r["deleted"] == 1, r)
    s = step("field_summary after edits", "field_summary", layer=path, field="tons")
    check("two rows remain", s and s["rows"] == 2 and s["numeric"]["min"] == 6, s)
    step("refused: field_summary on geometry", "field_summary", expect_error=True, layer=path,
         field="SHAPE@WKT")
    step("refused: field_summary with a negative top", "field_summary", expect_error=True,
         layer=path, field="tons", top=-1)
    step("insert_rows with one bad date writes nothing", "insert_rows", expect_error=True,
         table=path, rows=[{"site": "z"}, {"site": 5, "nope": 1}])


def map_steps():
    print("\n--- maps and layers")
    step("list_basemaps", "list_basemaps")
    step("set_map_properties (World Eckert IV)", "set_map_properties", map=TEST_MAP + "_empty",
         spatial_reference_wkid=54012)
    step("set_map_properties (reference scale)", "set_map_properties", map=TEST_MAP,
         reference_scale=1000000)
    step("set_map_properties (clear scale)", "set_map_properties", map=TEST_MAP, reference_scale=0)
    step("refused: bad wkid", "set_map_properties", expect_error=True, map=TEST_MAP,
         spatial_reference_wkid=-5)
    step("create_group_layer", "create_group_layer", map=TEST_MAP, name="smoke_group")
    step("move_layer_to_group", "move_layer_to_group", map=TEST_MAP, layer="smoke_states",
         group="smoke_group")
    lay = step("list_layers after grouping", "list_layers", map=TEST_MAP)
    names = [l["long_name"] for l in (lay or {}).get("layers", [])]
    check("the layer is in the group, once", names.count("smoke_group\\smoke_states") == 1
          and "smoke_states" not in names, names)
    r = step("move_layer", "move_layer", map=TEST_MAP, layer="smoke_drawn",
             reference_layer="smoke_group", position="AFTER")
    if r:
        order = r["order"]
        check("drawing order changed", order.index("smoke_drawn") > order.index("smoke_group"), order)
    step("refused: move a layer relative to itself", "move_layer", expect_error=True, map=TEST_MAP,
         layer="smoke_drawn", reference_layer="smoke_drawn")
    step("set_layer_properties (scale range and labels)", "set_layer_properties", map=TEST_MAP,
         layer="smoke_drawn", min_scale=0, max_scale=0, show_labels=False)
    step("refused: transparency out of range", "set_layer_properties", expect_error=True,
         map=TEST_MAP, layer="smoke_drawn", transparency=500)
    step("list_broken_sources", "list_broken_sources")
    name = "arcpro_mcp_tmp_%d" % int(time.time())
    step("create_map to rename", "create_map", name=name, include_basemap=False)
    step("rename_map", "rename_map", map=name, new_name=name + "_r")
    step("refused: rename_map to a taken name", "rename_map", expect_error=True, map=name + "_r",
         new_name=TEST_MAP)
    step("remove_map", "remove_map", map=name + "_r")


def selection_steps():
    print("\n--- selection")
    # After row_steps the dataset holds site a (tons 6) and site b (tons 50).
    r = step("select_by_attribute", "select_by_attribute", map=TEST_MAP, layer="smoke_drawn",
             where="tons > 10")
    check("selected one", r and r["selected"] == 1, r)
    r = step("get_selection", "get_selection", map=TEST_MAP, layer="smoke_drawn")
    check("selection is readable", r and r["selected"] == 1 and len(r["object_ids"]) == 1, r)
    r = step("query_layer selected_only", "query_layer", map=TEST_MAP, layer="smoke_drawn",
             selected_only=True)
    check("only the selected row", r and r["row_count"] == 1 and r["rows"][0]["site"] == "b", r)
    r = step("select ADD", "select_by_attribute", map=TEST_MAP, layer="smoke_drawn",
             where="site = 'a'", method="ADD")
    check("two selected", r and r["selected"] == 2, r)
    r = step("select REMOVE", "select_by_attribute", map=TEST_MAP, layer="smoke_drawn",
             where="site = 'a'", method="REMOVE")
    check("one left", r and r["selected"] == 1, r)
    step("refused: bad method", "select_by_attribute", expect_error=True, map=TEST_MAP,
         layer="smoke_drawn", method="MAYBE")
    r = step("select_by_location", "select_by_location", map=TEST_MAP, layer="smoke_drawn",
             overlap_type="WITHIN_A_DISTANCE", select_features="smoke_drawn",
             distance="10 Kilometers")
    check("selected by location", r and r["selected"] == 2, r)
    lay = step("list_layers after select_by_location", "list_layers", map=TEST_MAP)
    names = [l["long_name"] for l in (lay or {}).get("layers", [])]
    check("no temporary layer left in the map", not any("_mcp_" in n for n in names), names)
    step("clear_selection (layer)", "clear_selection", map=TEST_MAP, layer="smoke_drawn")
    r = step("get_selection after clear", "get_selection", map=TEST_MAP, layer="smoke_drawn")
    check("nothing selected", r and r["selected"] == 0, r)
    step("clear_selection (map)", "clear_selection", map=TEST_MAP)


def symbology_steps():
    print("\n--- symbology and labels")
    ramps = step("list_color_ramps", "list_color_ramps", wildcard="*Blue*") or {}
    ramp = (ramps.get("color_ramps") or [None])[0]
    step("set_symbology simple", "set_symbology", map=TEST_MAP, layer="smoke_drawn",
         color=[200, 30, 30], size=12, outline_color=[0, 0, 0], outline_width=1)
    snapshot("snapshot after simple symbology", map=TEST_MAP, layer="smoke_drawn")
    step("set_symbology unique", "set_symbology", map=TEST_MAP, layer="smoke_drawn",
         renderer="unique", field="site", color_ramp=ramp)
    snapshot("snapshot after unique values", map=TEST_MAP, layer="smoke_drawn")
    step("set_symbology graduated colors", "set_symbology", map=TEST_MAP, layer="smoke_drawn",
         renderer="graduated_colors", field="tons", classes=2, method="NaturalBreaks",
         color_ramp=ramp)
    snapshot("snapshot after graduated colors", map=TEST_MAP, layer="smoke_drawn")
    step("set_symbology graduated symbols", "set_symbology", map=TEST_MAP, layer="smoke_drawn",
         renderer="graduated_symbols", field="tons", classes=2, min_size=6, max_size=24)
    snapshot("snapshot after graduated symbols", map=TEST_MAP, layer="smoke_drawn")
    step("refused: unknown field", "set_symbology", expect_error=True, map=TEST_MAP,
         layer="smoke_drawn", renderer="unique", field="nope")
    step("refused: bad color", "set_symbology", expect_error=True, map=TEST_MAP,
         layer="smoke_drawn", color=[999, 0, 0])
    step("refused: unknown renderer", "set_symbology", expect_error=True, map=TEST_MAP,
         layer="smoke_drawn", renderer="heatmap")
    step("set_labels creates an Arcade class", "set_labels", map=TEST_MAP, layer="smoke_drawn",
         expression="$feature.site")
    step("set_labels updates the Arcade class", "set_labels", map=TEST_MAP, layer="smoke_drawn",
         expression="$feature.site + '!'")
    snapshot("snapshot with labels", map=TEST_MAP, layer="smoke_drawn")
    step("set_labels off", "set_labels", map=TEST_MAP, layer="smoke_drawn", enabled=False)
    step("snapshot still renders after all of that", "export_image", map=TEST_MAP)


def layout_steps(gdb):
    print("\n--- layouts")
    name = "arcpro_mcp_layout_%d" % int(time.time())
    step("create_layout", "create_layout", name=name, map=TEST_MAP)
    step("refused: duplicate layout", "create_layout", expect_error=True, name=name)
    step("refused: margins bigger than the page", "create_layout", expect_error=True,
         name=name + "x", width=1, height=1, margin=2)
    step("add_layout_text", "add_layout_text", layout=name, text="Smoke test", x=1, y=8, size=20,
         name="title")
    step("add_layout_surround legend", "add_layout_surround", layout=name, kind="legend", x=8, y=1)
    step("add_layout_surround north arrow", "add_layout_surround", layout=name, kind="north_arrow",
         x=9.5, y=6.5, width=0.5)
    step("add_layout_surround scale bar", "add_layout_surround", layout=name, kind="scale_bar",
         x=1, y=1)
    step("refused: unknown surround", "add_layout_surround", expect_error=True, layout=name,
         kind="compass", x=1, y=1)
    d = step("describe_layout", "describe_layout", layout=name)
    kinds = [e["type"] for e in (d or {}).get("elements", [])]
    check("all elements present", len(kinds) >= 5, kinds)
    step("update_layout_element", "update_layout_element", layout=name, element="title",
         text="Smoke test, edited", x=1.5)
    step("set_map_frame_extent (layer)", "set_map_frame_extent", layout=name, layer="smoke_drawn")
    step("set_map_frame_extent (scale)", "set_map_frame_extent", layout=name, scale=50000000)
    step("set_map_frame_extent (all)", "set_map_frame_extent", layout=name)
    snapshot("snapshot of the layout", layout=name)
    # In the project, because only files in the project can be replaced with overwrite.
    folder = os.path.join(os.path.dirname(gdb), "smoke_exports_%d" % int(time.time()))
    for ext in (".pdf", ".png", ".jpg", ".tif", ".svg"):
        path = os.path.join(folder, "new_folder", "smoke" + ext)
        step("export_layout %s" % ext, "export_layout", path=path, layout=name, resolution=96)
        check("%s written" % ext, os.path.exists(path) and os.path.getsize(path) > 0, path)
    pdf = os.path.join(folder, "new_folder", "smoke.pdf")
    step("refused: replacing a file without overwrite", "export_layout", expect_error=True,
         expect_text="already exists", path=pdf, layout=name)
    step("export_layout with overwrite", "export_layout", path=pdf, layout=name, overwrite=True,
         resolution=96)
    outside = os.path.join(tempfile.mkdtemp(prefix="arcpro_mcp_decoy_"), "keep.pdf")
    with open(outside, "w") as fh:
        fh.write("keep")
    step("refused: overwrite a file outside the project", "export_layout", expect_error=True,
         expect_text="outside the open project", path=outside, layout=name, overwrite=True)
    check("the outside file was kept", open(outside).read() == "keep", outside)
    os.remove(outside)
    os.rmdir(os.path.dirname(outside))
    step("refused: a huge page", "create_layout", expect_error=True, name=name + "h", width=100000,
         height=100000)
    step("export_layout_pdf", "export_layout_pdf", path=os.path.join(folder, "p.pdf"), layout=name,
         resolution=96)
    step("refused: export_layout_pdf to .png", "export_layout_pdf", expect_error=True,
         path=os.path.join(folder, "p.png"), layout=name)
    step("remove_layout_element", "remove_layout_element", layout=name, element="title")
    step("remove_layout", "remove_layout", layout=name)
    i = step("project_info after layouts", "project_info")
    check("no leftover snapshot layouts",
          not any(str(l).startswith("_arcpro_mcp") for l in (i or {}).get("layouts", [])), i)
    try:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def main(expected_project):
    if not os.path.exists(STATE):
        sys.exit("No bridge state file. Start the bridge in ArcGIS Pro first.")
    st = call("ping")["result"]
    if os.path.normcase(os.path.normpath(st["project"])) != os.path.normcase(
            os.path.normpath(expected_project)):
        sys.exit("Stopped: the bridge is attached to %s, not %s. Nothing was changed."
                 % (st["project"], expected_project))
    if st.get("run_arcpy_enabled"):
        sys.exit("Stopped: run this with the bridge started with allow_exec=False, because the "
                 "security checks test that mode.")
    if st.get("bridge_version") != REQUIRED_BRIDGE:
        sys.exit("Stopped: the bridge is version %s and this test needs %s. Its attack cases "
                 "really would run against an older bridge. Nothing was changed. Restart the "
                 "bridge from this folder." % (st.get("bridge_version"), REQUIRED_BRIDGE))

    st = step("ping", "ping")
    info = step("project_info", "project_info")
    existing = [m["name"] for m in (info or {}).get("maps", [])]

    if existing:
        first = existing[0]
        lay = step("list_layers (first map)", "list_layers", map=first)
        feats = [l for l in (lay or {}).get("layers", []) if l["is_feature_layer"]]
        if feats:
            name = feats[0]["long_name"]
            d = step("describe_layer (existing)", "describe_layer", map=first, layer=name)
            step("query_layer (existing, 5 rows)", "query_layer", map=first, layer=name, limit=5)
            if d and d.get("fields"):
                f = [x["name"] for x in d["fields"] if x["type"] not in ("Geometry", "OID")]
                if f:
                    step("field_summary (existing)", "field_summary", map=first, layer=name,
                         field=f[0])

    info = info or {}
    gdb = info.get("default_geodatabase")
    if gdb:
        step("list_workspace (default gdb)", "list_workspace", include_counts=False)
    step("describe_url (states service, nothing added)", "describe_url", url=STATES_URL)
    step("describe_url retired URL (expect error)", "describe_url", expect_error=True,
         url=RETIRED_URL)

    if TEST_MAP not in existing:
        step("create_map", "create_map", name=TEST_MAP)
    empty_map = TEST_MAP + "_empty"
    if empty_map not in existing:
        r = step("create_map without basemap", "create_map", name=empty_map,
                 include_basemap=False)
        if r and r.get("layer_count"):
            results[-1]["ok"] = False
            print("       FAIL: the map still has %s layers" % r["layer_count"])
    step("add_layer retired URL gives clear error (expect error)", "add_layer",
         expect_error=True, map=TEST_MAP, source=RETIRED_URL, name="smoke_retired")
    step("add_layer (USA states service)", "add_layer", map=TEST_MAP, source=STATES_URL,
         name="smoke_states")
    step("list_layers (test map)", "list_layers", map=TEST_MAP)
    step("describe_layer (states)", "describe_layer", map=TEST_MAP, layer="smoke_states")
    step("query_layer (where + geometry)", "query_layer", map=TEST_MAP, layer="smoke_states",
         where="STATE_ABBR = 'NC'", include_geometry=True, limit=5)
    step("query_layer (order_by, service)", "query_layer", map=TEST_MAP, layer="smoke_states",
         fields=["STATE_NAME", "POPULATION"], order_by="POPULATION DESC", limit=3)
    step("field_summary (numeric)", "field_summary", map=TEST_MAP, layer="smoke_states",
         field="POPULATION")
    step("set_layer_properties", "set_layer_properties", map=TEST_MAP, layer="smoke_states",
         definition_query="STATE_ABBR IN ('NC', 'SC', 'VA')", transparency=30)
    step("query honors definition query", "query_layer", map=TEST_MAP, layer="smoke_states",
         fields=["STATE_NAME"], limit=20)
    step("clear definition query", "set_layer_properties", map=TEST_MAP, layer="smoke_states",
         definition_query="")
    snapshot("export_image (test map, temp layout or its view)", map=TEST_MAP)
    snapshot("export_image (no args)")
    if st and st.get("mode") == "live":
        step("zoom_to_layer (opens the test map view)", "zoom_to_layer", map=TEST_MAP,
             layer="smoke_states")
        step("zoom_to_extent", "zoom_to_extent", map=TEST_MAP, xmin=-90, ymin=30, xmax=-70, ymax=45,
             wkid=4326)
        snapshot("export_image (active view after zoom)")
    else:
        step("zoom_to_layer needs live mode (expect error)", "zoom_to_layer", expect_error=True,
             map=TEST_MAP, layer="smoke_states")
    snapshot("export_image (framed on one layer)", map=TEST_MAP, layer="smoke_states")
    step("schema guard refuses AddField on a drawn layer (expect error)", "run_geoprocessing",
         expect_error=True, tool="management.AddField", args=["smoke_states", "x", "TEXT"],
         map=TEST_MAP)
    tbl = "smoke_log_%d" % int(time.time())
    step("CreateTable in memory", "run_geoprocessing", tool="management.CreateTable",
         args=["memory", tbl], add_outputs_to_map=False)
    step("AddField on a dataset no map draws", "run_geoprocessing", tool="management.AddField",
         args=["memory\\" + tbl, "layer", "TEXT"], add_outputs_to_map=False)
    step("AddField (date)", "run_geoprocessing", tool="management.AddField",
         args=["memory\\" + tbl, "accessed", "DATE"], add_outputs_to_map=False)
    step("insert_rows", "insert_rows", table="memory\\" + tbl,
         rows=[{"layer": "L1", "accessed": "2026-11-05"}, {"layer": "L2"}])
    q = step("query_layer on a table path", "query_layer", layer="memory\\" + tbl)
    if q is not None and q.get("row_count") != 2:
        results[-1]["ok"] = False
        print("       FAIL: expected 2 rows, got %s" % q.get("row_count"))
    backup = os.path.join(tempfile.mkdtemp(), "new_folder", "smoke_copy.aprx")
    step("save_project copy_to a new folder", "save_project", copy_to=backup)
    step("refused: save_project copy over an existing file", "save_project", expect_error=True,
         expect_text="already exists", copy_to=backup)
    if os.path.exists(backup):
        import shutil
        shutil.rmtree(os.path.dirname(os.path.dirname(backup)), ignore_errors=True)
    step("tool_help", "tool_help", tool="analysis.Buffer")
    step("run_geoprocessing (Buffer to memory)", "run_geoprocessing",
         tool="analysis.Buffer", args=["smoke_states", r"memory\smoke_buf_%d" % int(time.time()), "10 Kilometers"],
         add_outputs_to_map=False, map=TEST_MAP)
    step("run_geoprocessing bad param (expect error)", "run_geoprocessing", expect_error=True,
         tool="analysis.Buffer", args=["smoke_states", r"memory\smoke_buf2_%d" % int(time.time()), "not a distance"])
    step("search_portal", "search_portal", query="rare earth", item_type="Feature Service",
         limit=5)
    step("unknown layer (expect error)", "describe_layer", expect_error=True, map=TEST_MAP,
         layer="does_not_exist")

    security_steps(info)
    if gdb:
        path, fc, tbl_name = guard_steps(gdb)
        row_steps(path)
        map_steps()
        selection_steps()
        symbology_steps()
        layout_steps(gdb)
        step("remove_layer (drawn dataset)", "remove_layer", map=TEST_MAP, layer="smoke_drawn")
        step("refused: Delete a just-removed dataset before Pro restarts", "run_geoprocessing",
             expect_error=True, expect_text="removed in this Pro session",
             tool="management.Delete", args=[path])
    step("remove_layer", "remove_layer", map=TEST_MAP, layer="smoke_states")

    passed = sum(1 for r in results if r["ok"])
    print("\n%d/%d passed" % (passed, len(results)))
    out = os.path.join(HERE, "smoke_report.json")
    with open(out, "w") as fh:
        json.dump({"bridge": st, "results": results}, fh, indent=2, default=str)
    print("Report: " + out)
    print("Nothing was saved to the .aprx. Close the project without saving to drop the test "
          "maps. Datasets named smoke_* stay in its default geodatabase; delete them after Pro "
          "restarts.")
    return passed == len(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True,
                        help="Full path of the scratch .aprx the bridge is attached to.")
    sys.exit(0 if main(parser.parse_args().project) else 1)
