"""Smoke test against a REAL ArcGIS Pro bridge. Stdlib only, Python 3.9 compatible.

1. In ArcGIS Pro's Python window, start the bridge (exec(open(...arcpro_bridge.py).read())).
2. From a terminal (any Python 3.9+, including Pro's propy.bat), run:
       python smoke_test_real_pro.py
It works in a scratch map named "arcpro_mcp_test" and never saves the project.
A JSON report with timings is written next to this file (smoke_report.json).
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback

STATE = os.path.join(os.path.expanduser("~"), ".arcpro_mcp", "bridge.json")
HERE = os.path.dirname(os.path.abspath(__file__))
TEST_MAP = "arcpro_mcp_test"
STATES_URL = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
              "USA_States_Generalized_Boundaries/FeatureServer/0")
RETIRED_URL = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
               "USA_States_Generalized/FeatureServer/0")

results = []


def call(cmd, timeout=600, **params):
    st = json.load(open(STATE))
    req = {"id": 1, "token": st["token"], "cmd": cmd, "params": params}
    s = socket.create_connection(("127.0.0.1", st["port"]), timeout=10)
    s.settimeout(timeout)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def step(label, cmd, timeout=600, expect_error=False, **params):
    t0 = time.time()
    try:
        resp = call(cmd, timeout=timeout, **params)
        ok = resp.get("ok") != expect_error
        detail = resp.get("result") if resp.get("ok") else resp.get("error")
        tb = resp.get("traceback")
    except Exception as e:
        resp, ok, detail, tb = {}, False, "%s: %s" % (type(e).__name__, e), traceback.format_exc()
    dt = round(time.time() - t0, 2)
    rec = {"step": label, "cmd": cmd, "ok": ok, "seconds": dt,
           "detail": detail if len(json.dumps(detail, default=str)) < 3000
           else str(detail)[:3000] + "...", "traceback": tb}
    results.append(rec)
    print("%-4s %6.2fs  %s" % ("PASS" if ok else "FAIL", dt, label))
    if not ok:
        print("       " + str(detail)[:400].replace("\n", "\n       "))
    return resp.get("result") if resp.get("ok") else None


def main():
    if not os.path.exists(STATE):
        sys.exit("No bridge state file. Start the bridge in ArcGIS Pro first.")

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
    if info.get("default_geodatabase"):
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
    d = step("describe_layer (states)", "describe_layer", map=TEST_MAP, layer="smoke_states")
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
    for label, params in (("export_image (test map, temp layout or its view)", {"map": TEST_MAP}),
                          ("export_image (no args)", {})):
        img = step(label, "export_image", **params)
        if img and img.get("path") and os.path.exists(img["path"]):
            results[-1]["bytes"] = os.path.getsize(img["path"])
            results[-1]["source"] = img.get("source")
            results[-1]["frame"] = img.get("frame")
            print("       source: %s, %d bytes" % (img.get("source"), results[-1]["bytes"]))
            keep = os.path.join(HERE, "smoke_snapshot_%d.png" % len(results))
            shutil.move(img["path"], keep)
            print("       saved " + keep)
    step("zoom_to_layer (opens the test map view)", "zoom_to_layer", map=TEST_MAP,
         layer="smoke_states")
    img = step("export_image (active view after zoom)", "export_image")
    if img and img.get("path") and os.path.exists(img["path"]):
        print("       source: %s" % img.get("source"))
        os.remove(img["path"])
    img = step("export_image (framed on one layer)", "export_image", map=TEST_MAP,
               layer="smoke_states")
    if img and img.get("path") and os.path.exists(img["path"]):
        print("       source: %s, framed_on: %s" % (img.get("source"), img.get("framed_on")))
        keep = os.path.join(HERE, "smoke_snapshot_%d.png" % len(results))
        shutil.move(img["path"], keep)
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
    if os.path.exists(backup):
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
    step("remove_layer", "remove_layer", map=TEST_MAP, layer="smoke_states")

    passed = sum(1 for r in results if r["ok"])
    print("\n%d/%d passed" % (passed, len(results)))
    out = os.path.join(HERE, "smoke_report.json")
    with open(out, "w") as fh:
        json.dump({"bridge": st, "results": results}, fh, indent=2, default=str)
    print("Report: " + out)
    print("You can delete the '%s' and '%s_empty' maps from the project (it was not saved)."
          % (TEST_MAP, TEST_MAP))


if __name__ == "__main__":
    main()
