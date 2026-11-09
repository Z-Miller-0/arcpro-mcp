"""Security rules, each a regression test for something confirmed against real ArcGIS Pro 3.5
with allow_exec off: arbitrary Python through run_geoprocessing, the sign-in token going to any
URL, a second process sharing the bridge port, and more. See CHANGELOG 0.3.0.

Uses the env fixture from conftest.py: MCP client -> server -> TCP bridge -> fake arcpy.
"""

import http.server
import json
import os
import socket
import threading

import pytest

from test_end_to_end import call_tool, payload


def gp(env, tool, args=None, **extra):
    a = {"tool": tool, "args": args or [], "add_outputs_to_map": False}
    a.update(extra)
    return call_tool(env["mcp"], "run_geoprocessing", a)


def refused(res, text="Refused"):
    assert res.isError, res.content[0].text
    assert text.lower() in res.content[0].text.lower(), res.content[0].text


# -- only real tools resolve ------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "os.getpid", "getpid_os", "os.system", "system_os", "_exit_os", "sys.exit", "exit_sys",
    "time.sleep", "time.time", "__loader__.get_data", "arcpy.arcpy.GetSigninToken",
    "GetSigninToken_arcpy", "gp.getSigninToken", "env.__setattr__", "toolbox.Delete_management",
    "analysis.__dict__", "analysis.__class__", "management.Delete.__call__",
    "analysis._private", "da.SearchCursor", "mp.ArcGISProject", "server.StageService",
    "interop.QuickExport", "agolservices.FindClosestFacilities", "ImportToolbox",
])
def test_only_real_tools_resolve(env, tool):
    res = gp(env, tool, ["x"])
    assert res.isError
    text = res.content[0].text
    assert "Unknown geoprocessing tool" in text or "Give the tool" in text, text


def test_unmarked_function_in_a_toolbox_is_not_a_tool(env):
    import fake_arcpy as fa
    fa.analysis.NotATool = lambda *a: 1 / 0
    try:
        refused(gp(env, "analysis.NotATool"), "Unknown geoprocessing tool")
    finally:
        del fa.analysis.NotATool


def test_tool_help_uses_the_same_rules(env):
    refused(call_tool(env["mcp"], "tool_help", {"tool": "os.getpid"}), "Unknown geoprocessing tool")
    assert "Buffer" in payload(call_tool(env["mcp"], "tool_help", {"tool": "analysis.Buffer"}))["usage"]


def test_all_spellings_of_a_tool_reach_the_same_guard(env):
    for tool in ("management.Delete", "arcpy.management.Delete", "Delete_management",
                 "arcpy.Delete_management"):
        refused(gp(env, tool, ["C:/GIS/cm.gdb/plants"]), "Magnet Plants")


def test_tool_output_is_never_str_of_an_arbitrary_object(env):
    import fake_arcpy as fa
    fa.analysis.Odd = fa._tool("Odd_analysis")(lambda *a: {"token": "secret"})
    try:
        assert payload(gp(env, "analysis.Odd"))["outputs"] == []
    finally:
        del fa.analysis.Odd


# -- tools that run Python or publish ----------------------------------------------------

def test_python_tools_are_refused_with_allow_exec_off(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    refused(gp(env, "management.CalculateValue", ["6*7"]), "arbitrary Python")
    refused(gp(env, "ia.TrainDeepLearningModel", ["x"]), "arbitrary Python")
    refused(gp(env, "management.SharePackage", ["x"]), "publishes")
    assert fa.SCHEMA_CALLS == []


def test_python_tools_work_in_trusted_mode(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    env["server"].bridge.allow_exec = True
    try:
        payload(gp(env, "management.CalculateValue", ["6*7"]))
    finally:
        env["server"].bridge.allow_exec = False
    assert fa.SCHEMA_CALLS == [("CalculateValue", "6*7")]


@pytest.mark.parametrize("args,kwargs", [
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1"], None),  # the real default is PYTHON3
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1", "PYTHON3"], None),
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1", "python_9.3"], None),
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1", "VB"], None),
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1"], {"expression_type": "PYTHON3"}),
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1", None], None),
    (["C:/GIS/cm.gdb/unused_fc", "TONS", "1", 3], None),
])
def test_calculate_field_needs_arcade_or_sql(env, args, kwargs):
    res = gp(env, "management.CalculateField", args, **({"kwargs": kwargs} if kwargs else {}))
    refused(res, "ARCADE")


def test_calculate_field_in_arcade_runs(env):
    payload(gp(env, "management.CalculateField",
               ["C:/GIS/cm.gdb/unused_fc", "TONS", "$feature.TONS", "ARCADE"]))
    payload(gp(env, "management.CalculateField",
               ["C:/GIS/cm.gdb/unused_fc", "TONS", "1"], kwargs={"expression_type": "sql"}))


# -- the guard fails closed ---------------------------------------------------------------

def test_delete_on_containers_and_outside_the_project(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    refused(gp(env, "management.Delete", ["C:/GIS/cm.gdb"]), "drawn by")
    refused(gp(env, "management.Delete", ["C:/GIS"]), "drawn by")
    refused(gp(env, "management.Delete", ["C:/"]), "drawn by")
    refused(gp(env, "management.Delete", ["D:/other/other.gdb/fc"]), "outside the open project")
    assert fa.SCHEMA_CALLS == []


def test_delete_on_a_folder_in_the_project_is_refused(env):
    import fake_arcpy as fa
    fa.FOLDERS.add("c:/gis/exports")
    try:
        refused(gp(env, "management.Delete", ["C:/GIS/exports"]), "holding other data")
    finally:
        fa.FOLDERS.clear()


def test_guard_covers_more_tools_and_keyword_targets(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    refused(gp(env, "management.DeleteRows", ["REE Deposits"]), "drawn by")
    refused(gp(env, "management.DeleteRows", [], kwargs={"in_rows": "C:/GIS/cm.gdb/plants"}), "drawn by")
    assert fa.SCHEMA_CALLS == []


def test_guard_sees_layers_in_every_map(env):
    """A layer name resolves across maps, not only in the map= argument's map."""
    payload(call_tool(env["mcp"], "create_map", {"name": "Other", "include_basemap": False}))
    refused(gp(env, "management.Delete", ["REE Deposits"], map="Other"), "drawn by")
    payload(call_tool(env["mcp"], "remove_map", {"map": "Other"}))


def test_guard_refuses_a_target_it_cannot_identify(env):
    refused(gp(env, "management.Delete", ["some_name_no_one_knows"]), "could not be identified")


def test_guard_sees_through_a_temporary_view(env):
    import fake_arcpy as fa
    fa.TEMP_VIEWS["tmp_view"] = "C:/GIS/cm.gdb/plants"
    try:
        refused(gp(env, "management.AddField", ["tmp_view", "x", "TEXT"]), "drawn by")
    finally:
        fa.TEMP_VIEWS.clear()


def test_guard_sees_joined_tables(env):
    m = env["server"].bridge.aprx.listMaps()[0]
    lyr = m.listLayers()[0]
    lyr.connectionProperties = {
        "source": {"connection_info": {"database": "C:/GIS/cm.gdb"}, "dataset": "deposits"},
        "destination": {"connection_info": {"database": "C:/GIS/cm.gdb"}, "dataset": "source_log"},
    }
    try:
        refused(gp(env, "management.AddField", ["C:/GIS/cm.gdb/source_log", "x", "TEXT"]), "joined")
    finally:
        lyr.connectionProperties = None


def test_guard_remembers_layers_removed_this_session(env):
    """Pro can keep a removed layer's data locked until it restarts."""
    src = "C:/GIS/cm.gdb/gaps_log"
    bridge = env["server"].bridge
    payload(call_tool(env["mcp"], "add_layer", {"source": src, "name": "gaps"}))
    payload(call_tool(env["mcp"], "remove_layer", {"layer": "gaps"}))
    try:
        refused(gp(env, "management.Delete", [src]), "removed in this Pro session")
    finally:
        bridge.removed_sources.clear()


def test_web_service_targets_are_refused(env):
    url = "https://services.arcgis.com/x/arcgis/rest/services/y/FeatureServer/0"
    refused(gp(env, "management.DeleteRows", [url]), "web service")
    refused(gp(env, "management.AddField", [url, "x", "TEXT"]), "web service")
    refused(call_tool(env["mcp"], "insert_rows", {"table": url, "rows": [{"a": 1}]}), "dataset")


def test_overwrite_cannot_replace_a_drawn_dataset(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    plants = "C:/GIS/cm.gdb/plants"
    refused(gp(env, "management.CopyFeatures", ["C:/GIS/cm.gdb/unused_fc", plants],
               overwrite=True), "drawn by")
    refused(gp(env, "management.CopyFeatures", ["C:/GIS/cm.gdb/unused_fc", plants.upper()],
               overwrite=True), "drawn by")
    assert fa.SCHEMA_CALLS == []
    # a drawn INPUT with overwrite is fine; only the output is checked
    payload(gp(env, "management.CopyFeatures", [plants, "C:/GIS/cm.gdb/new_copy"], overwrite=True))
    payload(gp(env, "management.CopyFeatures", [plants, "C:/GIS/cm.gdb/unused_fc"], overwrite=True))


def test_overwrite_cannot_replace_data_outside_the_project(env):
    import fake_arcpy as fa
    fa.DATASETS["D:/other.gdb/keep"] = {"type": "FeatureClass", "fields": fa.FIELDS, "rows": []}
    try:
        refused(gp(env, "management.CopyFeatures", ["C:/GIS/cm.gdb/unused_fc", "D:/other.gdb/keep"],
                   overwrite=True), "outside the open project")
    finally:
        del fa.DATASETS["D:/other.gdb/keep"]


# -- network paths ------------------------------------------------------------------------

UNC = "\\\\arcpro-mcp-test.invalid\\share\\x.gdb\\fc"


@pytest.mark.parametrize("tool,args", [
    ("query_layer", {"layer": UNC}),
    ("query_layer", {"layer": UNC.replace("\\", "/")}),
    ("describe_layer", {"layer": UNC}),
    ("field_summary", {"layer": UNC, "field": "x"}),
    ("list_workspace", {"workspace": "\\\\arcpro-mcp-test.invalid\\share"}),
    ("add_layer", {"source": UNC}),
    ("insert_rows", {"table": UNC, "rows": [{"a": 1}]}),
    ("export_layout_pdf", {"path": "\\\\arcpro-mcp-test.invalid\\share\\x.pdf"}),
    ("export_layout", {"path": "\\\\arcpro-mcp-test.invalid\\share\\x.pdf"}),
    ("save_project", {"copy_to": "\\\\arcpro-mcp-test.invalid\\share\\x.aprx"}),
    ("create_dataset", {"name": "x", "workspace": "\\\\arcpro-mcp-test.invalid\\share\\x.gdb"}),
])
def test_network_paths_are_refused(env, tool, args):
    refused(call_tool(env["mcp"], tool, args), "network path")


def test_network_paths_in_geoprocessing_parameters(env):
    refused(gp(env, "analysis.Buffer", [UNC, "memory/x", "1 Meter"]), "network path")
    refused(gp(env, "analysis.Buffer", ["REE Deposits", UNC, "1 Meter"]), "network path")
    refused(gp(env, "analysis.Buffer", ["memory/a;" + UNC, "memory/x", "1 Meter"]), "network path")
    refused(gp(env, "analysis.Buffer", [["memory/a", UNC], "memory/x", "1 Meter"]), "network path")
    refused(gp(env, "analysis.Buffer", ["memory/a", "memory/x", "1 Meter"],
               kwargs={"field_mapping": 'a "a" true false 4 ' + UNC + ',a,-1,-1'}), "network path")


def test_add_layer_refuses_other_schemes(env):
    for source in ("file:///C:/Windows/win.ini", "ftp://x/y", "smb://x/y"):
        refused(call_tool(env["mcp"], "add_layer", {"source": source}), "Only local paths")


# -- writing files ------------------------------------------------------------------------

def test_exports_need_the_right_extension_and_will_not_replace(env, tmp_path):
    mcp = env["mcp"]
    refused(call_tool(mcp, "export_layout", {"path": str(tmp_path / "x.bat")}), "must end in")
    refused(call_tool(mcp, "export_layout_pdf", {"path": str(tmp_path / "x.png")}), ".pdf")
    refused(call_tool(mcp, "export_layout", {"path": "relative.pdf"}), "full path")
    target = tmp_path / "x.pdf"
    target.write_bytes(b"precious")
    refused(call_tool(mcp, "export_layout", {"path": str(target)}), "already exists")
    assert target.read_bytes() == b"precious"
    # replacing a file is only allowed inside the project
    refused(call_tool(mcp, "export_layout", {"path": str(target), "overwrite": True}), "outside the open project")
    assert target.read_bytes() == b"precious"
    aprx = env["server"].bridge.aprx
    original = aprx.homeFolder
    aprx.homeFolder = str(tmp_path)
    try:
        payload(call_tool(mcp, "export_layout", {"path": str(target), "overwrite": True}))
    finally:
        aprx.homeFolder = original
    assert target.read_bytes() != b"precious"


def test_export_path_tricks(env, tmp_path):
    folder = tmp_path / "x.pdf"
    folder.mkdir()
    refused(call_tool(env["mcp"], "export_layout", {"path": str(folder), "overwrite": True}), "is a folder")
    refused(call_tool(env["mcp"], "export_layout", {"path": str(tmp_path / "a.txt:hidden.pdf")}), "colon")


def test_overwrite_cannot_replace_a_drawn_raster_by_export(env, tmp_path):
    import fake_arcpy as fa
    target = tmp_path / "drawn.jpg"
    target.write_bytes(b"precious")
    m = fa.Map("Rasters")
    m._layers = [fa.Layer("Drawn", str(target))]
    aprx = env["server"].bridge.aprx
    aprx._maps.append(m)
    original = aprx.homeFolder
    aprx.homeFolder = str(tmp_path)
    try:
        refused(call_tool(env["mcp"], "export_layout", {"path": str(target), "overwrite": True}), "a map draws")
    finally:
        aprx.homeFolder = original
        aprx._maps.remove(m)
    assert target.read_bytes() == b"precious"


def test_huge_layout_cannot_be_rendered(env):
    aprx = env["server"].bridge.aprx
    lyt = aprx.createLayout(1, 1, "INCH", "Huge")
    lyt.pageWidth, lyt.pageHeight = 200, 200
    try:
        from arcpro_mcp import client
        with pytest.raises(client.BridgeCallError, match="pixels"):
            client.call("export_image", layout="Huge")
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "huge.png")
        refused(call_tool(env["mcp"], "export_layout", {"path": path, "layout": "Huge", "resolution": 1200}), "pixels")
    finally:
        aprx.deleteItem(lyt)


# -- findings from the attack on the 0.3.0 patch ---------------------------------------------------

def test_a_denylist_is_not_needed_to_stop_in_place_tools(env):
    """DeleteMultiple was missing from the table and could delete any folder. Tools with no output
    of their own are now guarded by their shape, not by being on a list."""
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    refused(gp(env, "management.DeleteMultiple", ["C:/GIS"]), "Refused")
    refused(gp(env, "management.DeleteMultiple", ["D:/other/project"]), "outside the open project")
    refused(gp(env, "management.DeleteMultiple", ["C:/GIS/cm.gdb/plants"]), "drawn by")
    refused(gp(env, "analysis.Near", ["REE Deposits", "x"]), "drawn by")
    refused(gp(env, "analysis.Near", ["D:/other.gdb/fc", "x"]), "outside the open project")
    assert fa.SCHEMA_CALLS == []


def test_tools_with_their_own_output_are_not_guarded(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    payload(gp(env, "analysis.Clip", ["REE Deposits", "REE Deposits", "C:/GIS/cm.gdb/clip_out"]))
    assert fa.SCHEMA_CALLS == [("Clip", "C:/GIS/cm.gdb/clip_out")]


def test_a_tool_of_unknown_shape_counts_as_changing_its_input(env):
    import fake_arcpy as fa
    fa.analysis.Mystery = fa._tool("Mystery_analysis")(lambda *a: fa.Result([]))
    try:
        refused(gp(env, "analysis.Mystery", ["REE Deposits"]), "drawn by")
    finally:
        del fa.analysis.Mystery


def test_keyword_target_of_unknown_tool_fails_closed(env):
    import fake_arcpy as fa
    fa.analysis.Mystery = fa._tool("Mystery_analysis")(lambda **k: fa.Result([]))
    try:
        refused(gp(env, "analysis.Mystery", [], kwargs={"odd_name": "REE Deposits"}), "could not be identified")
    finally:
        del fa.analysis.Mystery


def test_imports_python_tool_is_refused(env):
    refused(gp(env, "oi.AddImagesFromCustomInputType", ["x"]), "arbitrary Python")


# -- found reviewing Pro 3.7.3 ----------------------------------------------------------------

def _fake_tool(monkeypatch, toolbox, name, calls):
    """Register a stand-in tool on the fake arcpy that records whether it ran."""
    import types
    import fake_arcpy as fa
    suffix = {"ddd": "3d"}.get(toolbox, toolbox)
    func = fa._tool("%s_%s" % (name, suffix))(lambda *a, **k: calls.append((name, a, k)) or "done")
    box = getattr(fa, toolbox, None)
    if box is None:
        box = types.SimpleNamespace()
        monkeypatch.setattr(fa, toolbox, box, raising=False)
    monkeypatch.setattr(box, name, func, raising=False)


@pytest.mark.parametrize("toolbox,name", [
    ("management", "LoadDataToPreview"),  # same WorkbookLoader as LoadDataUsingWorkspace
    ("management", "BatchUpdateFields"),
    ("management", "AnalyzeToolsForPro"),
    ("management", "GenerateRasterCollection"),
    ("management", "GenerateTableFromRasterFunction"),
    ("ddd", "ClassifyPointCloudUsingTrainedModel"),
    ("ddd", "DetectObjectsFromPointCloudUsingTrainedModel"),
    ("ddd", "EvaluatePointCloudClassificationModel"),
    ("geoai", "ForecastUsingTimeSeriesModel"),
    ("geoai", "PredictUsingAutoML"),
    ("ia", "SegmentUsingTrainedModel"),  # not a real tool: the family pattern catches new ones
    ("geoai", "ClassifyUsingAutoMLv2"),
])
def test_pro_37_python_tools_are_refused(env, monkeypatch, toolbox, name):
    calls = []
    _fake_tool(monkeypatch, toolbox, name, calls)
    refused(gp(env, "%s.%s" % (toolbox, name), ["C:/GIS/cm.gdb/unused_fc"]), "arbitrary Python")
    assert calls == []


@pytest.mark.parametrize("args,kwargs", [
    (["C:/GIS/cm.gdb/unused_fc", r"C:\models\roads.dlpk"], None),
    (["C:/GIS/cm.gdb/unused_fc", "C:/models/roads.EMD"], None),
    (["C:/GIS/cm.gdb/unused_fc", "x"], {"script_file": "C:/GIS/calc.py"}),
    (["C:/GIS/cm.gdb/unused_fc", [["field", "C:/GIS/tools.pyt"]]], None),  # inside a value table
    (["C:/GIS/cm.gdb/unused_fc", '{"type": "PythonAdapter", "pythonModule": "x"}'], None),
    (["C:/GIS/cm.gdb/unused_fc", "x"], {"raster_function": {"f": "C:/rf/custom.py"}}),
])
def test_any_tool_refuses_code_and_model_files(env, monkeypatch, args, kwargs):
    calls = []
    _fake_tool(monkeypatch, "management", "UnreviewedTool", calls)
    res = gp(env, "management.UnreviewedTool", args, **({"kwargs": kwargs} if kwargs else {}))
    refused(res, "Python or model file")
    assert calls == []


def test_code_file_check_ignores_lookalikes(env):
    bridge = type(env["server"].bridge)
    for ok in ("C:/GIS/pyramids.gdb/roads", "C:/GIS/data.pyramid", "copy", "C:/GIS/py/roads.shp",
               "C:/GIS/emdat.gdb/x", "!SHAPE.area!", 3, None):
        assert bridge._code_value(ok) is None, ok
    assert bridge._code_value(["a", ["b", {"k": "C:/x/m.dlpk"}]]) == "C:/x/m.dlpk"


def test_code_files_work_in_trusted_mode(env, monkeypatch):
    calls = []
    _fake_tool(monkeypatch, "management", "UnreviewedTool", calls)
    monkeypatch.setattr(env["server"].bridge, "allow_exec", True)
    payload(gp(env, "management.UnreviewedTool", ["C:/GIS/cm.gdb/unused_fc", "C:/GIS/calc.py"]))
    assert len(calls) == 1


def test_match_symbology_takes_only_a_field_name(env, monkeypatch):
    calls = []
    _fake_tool(monkeypatch, "management", "MatchLayerSymbologyToAStyle", calls)
    for expression in ("!TYPE!.upper()", "__import__('os').getpid()", "$feature.TYPE", 7):
        refused(gp(env, "management.MatchLayerSymbologyToAStyle",
                   ["REE Deposits", expression, "C:/styles/s.stylx"]), "field name")
        refused(gp(env, "management.MatchLayerSymbologyToAStyle", ["REE Deposits"],
                   kwargs={"match_values": expression, "in_style": "C:/styles/s.stylx"}), "field name")
    assert calls == []
    payload(gp(env, "management.MatchLayerSymbologyToAStyle", ["REE Deposits", "TYPE", "C:/styles/s.stylx"]))
    assert len(calls) == 1


def test_overwrite_checks_the_name_the_tool_really_writes(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    # CreateFileGDB adds .gdb, and a folder output writes a .shp
    refused(gp(env, "management.CreateFileGDB", ["C:/GIS", "cm"], overwrite=True), "Refused")
    refused(gp(env, "management.CreateFileGDB", ["D:/other", "important"], overwrite=True), "outside the open project")
    refused(gp(env, "management.CopyFeatures", ["memory/x", "D:/other/parcels"], overwrite=True), "outside the open project")
    m = fa.Map("Shapes")
    m._layers = [fa.Layer("Shape", "C:/GIS/parcels.shp")]
    env["server"].bridge.aprx._maps.append(m)
    try:
        refused(gp(env, "management.CopyFeatures", ["memory/x", "C:/GIS/parcels"], overwrite=True), "drawn by")
    finally:
        env["server"].bridge.aprx._maps.remove(m)
    assert fa.SCHEMA_CALLS == []


def test_a_layer_shadowing_a_dataset_name_is_seen(env):
    """If a temporary layer has the same name as a dataset, arcpy may use the layer."""
    import fake_arcpy as fa
    fa.DATASETS["C:/GIS/cm.gdb/scratch"] = {"type": "Table", "fields": fa.FIELDS, "rows": []}
    original = fa.Describe
    fa.Describe = lambda o: (types_ns(catalogPath="D:/other.gdb/parcels", dataType="FeatureLayer")
                             if o == "scratch" else original(o))
    try:
        refused(gp(env, "management.DeleteRows", ["scratch"]), "outside the open project")
    finally:
        fa.Describe = original
        del fa.DATASETS["C:/GIS/cm.gdb/scratch"]


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def test_row_edits_stay_in_the_project(env):
    import fake_arcpy as fa
    fa.DATASETS["D:/other.gdb/t"] = {"type": "Table", "fields": fa.FIELDS, "rows": list(fa.ROWS)}
    try:
        for tool, args in (("update_rows", {"where": "STATE = 'WY'", "values": {"TONS": 1}}),
                           ("delete_rows", {"where": "STATE = 'WY'"}),
                           ("insert_rows", {"rows": [{"TONS": 1}]})):
            refused(call_tool(env["mcp"], tool, dict(args, table="D:/other.gdb/t")), "outside the open project")
        refused(gp(env, "management.CalculateField", ["D:/other.gdb/t", "TONS", "1", "ARCADE"]), "outside the open project")
        assert fa.DATASETS["D:/other.gdb/t"]["rows"] == fa.ROWS
    finally:
        del fa.DATASETS["D:/other.gdb/t"]


def test_row_edits_refuse_layer_files_that_point_elsewhere(env):
    import fake_arcpy as fa
    fa.DATASETS["C:/GIS/h.lyrx"] = {"type": "Table", "fields": fa.FIELDS, "rows": []}
    try:
        for tool, args in (("delete_rows", {"where": "a = 1"}),
                           ("update_rows", {"where": "a = 1", "values": {"TONS": 1}})):
            refused(call_tool(env["mcp"], tool, dict(args, table="C:/GIS/h.lyrx")), "layer file")
    finally:
        del fa.DATASETS["C:/GIS/h.lyrx"]


@pytest.mark.parametrize("where", ["1=1) OR (1=1", "a = 1; DROP TABLE x", "a = 1 -- x", "a = 1 /* x", "((a = 1)", ")"])
def test_where_clauses_cannot_escape_their_parentheses(env, where):
    refused(call_tool(env["mcp"], "delete_rows", {"table": "gaps_log", "where": where}), "where clause")
    refused(call_tool(env["mcp"], "select_by_attribute", {"layer": "REE Deposits", "where": where}), "where clause")


def test_where_clauses_may_quote_those_characters(env):
    assert env["bridge"]._check_where("NAME = 'a) OR (b; -- c'") is not None
    assert env["bridge"]._check_where("(a = 1) AND (b = 2)") is not None


@pytest.mark.parametrize("path", [
    "memory\\..\\..\\Other.gdb\\T", "memory/../x", "in_memory\\..\\x", "memory\\a\\b", "memory\\..", "memory\\.",
])
def test_memory_paths_cannot_climb_out(env, path):
    assert env["bridge"]._is_memory(path) is False
    assert env["server"].bridge._in_project(path) is False


@pytest.mark.parametrize("path", ["memory\\x", "memory/x", "in_memory\\x", "MEMORY\\table_1"])
def test_memory_paths_still_work(env, path):
    assert env["bridge"]._is_memory(path) is True


@pytest.mark.parametrize("path", [
    "\\\\host\\share", "//host/share", "\\/host/share", "/\\host\\share", "\\\\?\\UNC\\host\\share",
    "\\\\.\\device", "\\\\host@SSL\\share", " \\\\host\\share", "'\\\\host\\share'", "file://host/share", "FILE:///x",
])
def test_all_network_path_forms_are_recognized(env, path):
    assert env["bridge"]._is_unc(path) is True


@pytest.mark.parametrize("path", ["C:\\x", "C:/x", "memory\\x", "name", "\\single", "/single"])
def test_local_paths_are_not_network_paths(env, path):
    assert env["bridge"]._is_unc(path) is False


@pytest.mark.parametrize("value", [
    "\\/host/x", "file://host/x", "a;\\\\host\\x", 'a "a" true //host/x', "x=\\\\host\\y",
])
def test_network_forms_inside_parameters(env, value):
    refused(gp(env, "analysis.Buffer", ["memory/a", "memory/x", "1 Meter"], kwargs={"f": value}), "network path")


def test_ordinary_text_in_parameters_is_not_a_network_path(env):
    for value in ("https://services.arcgis.com/x/FeatureServer/0", "10 Kilometers", "a/b", "a//b"):
        env["server"].bridge._check_local_values(value)


def test_norm_ignores_what_windows_ignores(env):
    norm = env["bridge"]._norm
    assert norm("C:\\Windows\\.") == norm("C:\\Windows") == norm("c:/WINDOWS/")
    assert norm("C:\\Windows.") == norm("C:\\Windows ") == norm("C:\\Windows")
    assert norm("C:\\Windows\\System32\\..") == norm("C:\\Windows")


@pytest.mark.parametrize("path", [
    "C:\\GIS\\..\\other\\x.gdb\\t", "C:/GIS/../other/x.gdb/t", "C:\\GIS\\sub\\..\\..\\other",
    "C:\\GIS\\.\\..\\other", "C:\\GIS.\\..\\other", "C:\\GIS\\..\\GIS2\\x",
])
def test_dot_dot_cannot_look_like_inside_the_project(env, path):
    """A bug in this patch stripped the dots from .. and read C:\\GIS\\..\\other as inside C:\\GIS."""
    assert env["server"].bridge._in_project(path) is False
    refused(gp(env, "management.DeleteRows", [path]), "outside the open project")


def test_trusted_mode_lifts_the_project_limits_but_not_the_drawn_data_guard(env):
    import fake_arcpy as fa
    del fa.SCHEMA_CALLS[:]
    bridge = env["server"].bridge
    bridge.allow_exec = True
    try:
        payload(gp(env, "management.DeleteRows", ["D:/other.gdb/t"]))
        payload(gp(env, "management.CopyFeatures", ["memory/x", "D:/other.gdb/t"], overwrite=True))
        refused(gp(env, "management.DeleteRows", ["C:/GIS/cm.gdb/plants"]), "drawn by")
        refused(gp(env, "management.CopyFeatures", ["memory/x", "C:/GIS/cm.gdb/plants"], overwrite=True), "drawn by")
    finally:
        bridge.allow_exec = False
    assert [c[0] for c in fa.SCHEMA_CALLS] == ["DeleteRows", "CopyFeatures"]


def test_short_names_match_long_names(env, tmp_path):
    """Only meaningful if this volume has short names on, which is the default on C:."""
    import ctypes
    folder = tmp_path / "a folder with a long name"
    folder.mkdir()
    buf = ctypes.create_unicode_buffer(600)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(folder), buf, 600) or buf.value == str(folder):
        pytest.skip("this volume does not use short names")
    norm = env["bridge"]._norm
    assert norm(buf.value + "\\data.gdb\\fc") == norm(str(folder) + "\\data.gdb\\fc")


@pytest.mark.parametrize("tool,args", [
    ("create_layout", {"name": "N", "width": 1e6}), ("create_layout", {"name": "N", "margin": -1}),
    ("set_map_properties", {"reference_scale": -5}),
    ("set_map_properties", {"reference_scale": 1e300}),
    ("set_layer_properties", {"layer": "REE Deposits", "min_scale": -1}),
    ("add_layout_text", {"layout": "Main Layout", "text": "t", "x": 1e300, "y": 0}),
    ("add_layout_text", {"layout": "Main Layout", "text": "t", "x": 0, "y": 0, "size": -3}),
])
def test_out_of_range_numbers_are_refused(env, tool, args):
    assert call_tool(env["mcp"], tool, args).isError, args


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "1e999", None, [1]])
def test_number_helper_refuses_non_finite(env, value):
    with pytest.raises(env["bridge"].BridgeError):
        env["bridge"]._number(value, 0, 10, "x")


def test_exports_cannot_touch_the_bridge_own_files(env):
    bridge = env["bridge"]
    refused(call_tool(env["mcp"], "export_layout",
                      {"path": os.path.join(bridge.STATE_DIR, "x.pdf")}), "depends on")
    refused(call_tool(env["mcp"], "save_project",
                      {"copy_to": env["server"].bridge.project_path}), "depends on")


def test_copy_will_not_replace_a_project(env, tmp_path):
    target = tmp_path / "keep.aprx"
    target.write_bytes(b"precious")
    refused(call_tool(env["mcp"], "save_project", {"copy_to": str(target)}), "already exists")
    assert target.read_bytes() == b"precious"


def test_export_image_has_no_caller_chosen_path(env):
    from arcpro_mcp import client
    with pytest.raises(client.BridgeCallError, match="Bad parameters"):
        client.call("export_image", path="C:/Windows/x.png")


def test_snapshot_returns_bytes_and_leaves_no_file(env):
    import base64
    from arcpro_mcp import client
    res = client.call("export_image", layout="Main Layout")
    assert "path" not in res
    assert base64.b64decode(res["png_base64"]).startswith(b"\x89PNG")


def test_snapshot_explains_an_older_bridge(env):
    """Seen in real use: mixing versions gave only the error 'path'."""
    bridge = env["server"].bridge
    original = bridge.commands["export_image"]
    bridge.commands["export_image"] = lambda **k: {"path": "C:/x.png", "source": "old bridge"}
    try:
        res = call_tool(env["mcp"], "snapshot_map")
    finally:
        bridge.commands["export_image"] = original
    assert res.isError and "older than this server" in res.content[0].text


def test_save_sweeps_leftover_snapshot_layouts(env):
    aprx = env["server"].bridge.aprx
    aprx.createLayout(1, 1, "INCH", "_arcpro_mcp_snapshot")
    payload(call_tool(env["mcp"], "save_project"))
    assert not any(l.name.startswith("_arcpro_mcp") for l in aprx.listLayouts())


# -- sign-in token and URLs ------------------------------------------------------------------

@pytest.fixture
def signed_in():
    import fake_arcpy as fa
    original = fa.GetSigninToken
    fa.GetSigninToken = lambda: {"token": "SECRET-TOKEN", "referer": "http://r"}
    try:
        yield fa
    finally:
        fa.GetSigninToken = original


class _Capture(http.server.BaseHTTPRequestHandler):
    seen = []
    redirect = None

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Referer")))
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", type(self).redirect)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"name":"x","type":"Feature Layer","fields":[]}')

    def log_message(self, *a):
        pass


@pytest.fixture
def capture():
    _Capture.seen = []
    _Capture.redirect = None
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_private_hosts_get_no_request_at_all(env, signed_in, capture):
    _srv, base = capture
    refused(call_tool(env["mcp"], "describe_url", {"url": base + "/a/FeatureServer/0"}), "private")
    refused(call_tool(env["mcp"], "add_layer", {"source": base + "/a/FeatureServer/0"}), "private")
    assert _Capture.seen == []


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini#", "ftp://arcpro-mcp-test.invalid/x", "data:,x",
    "services.arcgis.com/x", "https://www.arcgis.com@arcpro-mcp-test.invalid/x",
    "https://arcpro-mcp-test.invalid/x#frag", "https://arcpro-mcp-test.invalid/a b",
    "https://arcpro-mcp-test.invalid\n/x", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/x", "http://0.0.0.0/x", "http://localhost/x",
])
def test_bad_urls_are_refused_without_the_token(env, signed_in, url):
    res = call_tool(env["mcp"], "describe_url", {"url": url})
    assert res.isError and "SECRET-TOKEN" not in res.content[0].text


def _bridge(env):
    return env["server"].bridge


@pytest.mark.parametrize("url,allowed", [
    ("https://www.arcgis.com/sharing/rest/x", True),
    ("https://services.arcgis.com/abc/arcgis/rest/services/y/FeatureServer/0", True),
    ("https://myorg.maps.arcgis.com/x", True),
    ("https://WWW.ARCGIS.COM/x", True),
    ("http://www.arcgis.com/x", False),  # never over plain http
    ("https://arcgis.com.evil.example/x", False),
    ("https://evilarcgis.com/x", False),
    ("https://www.arcgis.com@evil.example/x", False),
    ("https://evil.example/www.arcgis.com/x", False),
    ("https://evil.example/?x=.arcgis.com", False),
    ("https://evil.example#.arcgis.com", False),
    ("https://evil.example\\.arcgis.com/x", False),
])
def test_token_only_goes_to_esri_over_https(env, url, allowed):
    assert _bridge(env)._token_allowed(url) is allowed


def test_token_goes_to_the_active_portal(env):
    import fake_arcpy as fa
    original = fa.GetActivePortalURL
    fa.GetActivePortalURL = lambda: "https://gis.myorg.example/portal/"
    try:
        assert _bridge(env)._token_allowed("https://gis.myorg.example/server/rest/x") is True
        assert _bridge(env)._token_allowed("http://gis.myorg.example/server/rest/x") is False
        assert _bridge(env)._token_allowed("https://gis.myorg.example.evil.test/x") is False
    finally:
        fa.GetActivePortalURL = original


def test_portal_on_a_private_host_gets_no_token_over_http(env, signed_in, capture):
    _srv, base = capture
    original = signed_in.GetActivePortalURL
    signed_in.GetActivePortalURL = lambda: base + "/"
    try:
        payload(call_tool(env["mcp"], "describe_url", {"url": base + "/a/FeatureServer/0"}))
    finally:
        signed_in.GetActivePortalURL = original
    assert _Capture.seen and not any("token=" in p for p, _ in _Capture.seen)


def test_redirects_are_checked_and_cannot_reach_private_hosts(env, signed_in, capture):
    _srv, base = capture
    original = signed_in.GetActivePortalURL
    signed_in.GetActivePortalURL = lambda: base + "/"
    _Capture.redirect = "http://169.254.169.254/latest/meta-data"
    try:
        refused(call_tool(env["mcp"], "describe_url", {"url": base + "/a/FeatureServer/0"}), "private")
        _Capture.redirect = "file:///C:/Windows/win.ini"
        refused(call_tool(env["mcp"], "describe_url", {"url": base + "/a/FeatureServer/0"}), "http")
    finally:
        signed_in.GetActivePortalURL = original


def test_redirect_handler_strips_credentials_and_refuses_downgrade(env, signed_in):
    import urllib.request
    bridge = env["bridge"]
    handler = bridge._Redirects(_bridge(env), True)
    req = urllib.request.Request("https://www.arcgis.com/x?f=json&token=SECRET-TOKEN",
                                 headers={"Referer": "http://r"})
    with pytest.raises(bridge.BridgeError, match="https to plain http"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://www.arcgis.com/y")
    original = _bridge(env)._check_url
    _bridge(env)._check_url = lambda u: u  # no DNS in tests
    try:
        new = handler.redirect_request(req, None, 302, "Found", {},
                                       "https://elsewhere.example/y?token=SECRET-TOKEN&f=json")
    finally:
        _bridge(env)._check_url = original
    assert "SECRET-TOKEN" not in new.full_url and "f=json" in new.full_url
    assert not new.has_header("Referer")


def test_item_id_must_be_an_id(env):
    for bad in ("../../portals/self", "abc", "a" * 31, "a" * 32 + "#", "g" * 32):
        refused(call_tool(env["mcp"], "describe_url", {"item_id": bad}), "32 hexadecimal")


def test_my_org_only_never_widens_silently(env):
    res = call_tool(env["mcp"], "search_arcgis_online", {"query": "x", "my_org_only": True})
    assert res.isError and "Nothing was searched" in res.content[0].text


def test_responses_have_a_size_limit(env, capture, monkeypatch):
    _srv, base = capture
    import fake_arcpy as fa
    monkeypatch.setattr(env["bridge"], "MAX_JSON_BYTES", 10)
    monkeypatch.setattr(fa, "GetActivePortalURL", lambda: base + "/")
    refused(call_tool(env["mcp"], "describe_url", {"url": base + "/a/FeatureServer/0"}), "more than")


def test_errors_redact_tokens():
    import importlib
    b = importlib.import_module("arcpro_bridge")
    assert "abc" not in b._redact("https://x/y?f=json&token=abc&z=1")
    assert "abc" not in b._redact("unknown url type: 'x?token=abc'")


# -- the socket --------------------------------------------------------------------------------

def _raw(env, payload_bytes, timeout=5):
    st = json.loads(env["state"].read_text())
    s = socket.create_connection(("127.0.0.1", st["port"]), timeout=timeout)
    s.settimeout(timeout)
    got = b""
    try:
        s.sendall(payload_bytes)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return got, True
            got += chunk
    except socket.timeout:
        return got, False
    except (ConnectionResetError, ConnectionAbortedError):
        # Windows resets, rather than closes, a connection dropped with unread data
        return got, True
    finally:
        s.close()


@pytest.mark.parametrize("line", [
    b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", b"POST / HTTP/1.1\r\n\r\n{}\n", b"{nope\n", b"[1,2]\n",
    b"5\n", b'"x"\n', b"null\n", b'{"token":"wrong","cmd":"ping"}\n', b'{"token":5,"cmd":"ping"}\n',
    b'{"token":null,"cmd":"ping"}\n', b'{"cmd":"ping"}\n', b"\xff\xfe\n",
    '{"token":"\u00e9","cmd":"ping"}\n'.encode(),
])
def test_unauthenticated_input_is_refused_and_reveals_nothing(env, line):
    got, closed = _raw(env, line)
    assert closed, "the connection must close"
    assert b'"ok": false' in got
    assert b"Traceback" not in got and b"bridge_version" not in got and b".py" not in got


def test_one_request_per_connection(env):
    st = json.loads(env["state"].read_text())
    req = (json.dumps({"id": 1, "token": st["token"], "cmd": "ping", "params": {}}) + "\n").encode()
    got, closed = _raw(env, req + req)
    assert closed and got.count(b'"ok": true') == 1


def test_authenticated_garbage_is_handled(env):
    st = json.loads(env["state"].read_text())
    for cmd, params in ((5, {}), (None, {}), ("nope", {}), ("ping", 5), ("ping", [1]),
                        (["ping"], {}), ({"a": 1}, {}), ("_map", {}), ("__init__", {}),
                        ("commands", {}), ("handle", {})):
        req = (json.dumps({"id": 1, "token": st["token"], "cmd": cmd, "params": params}) + "\n").encode()
        got, closed = _raw(env, req)
        assert closed and b'"ok": false' in got, (cmd, params, got)


def test_second_listener_cannot_share_the_port(env):
    st = json.loads(env["state"].read_text())
    other = socket.socket()
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", st["port"]))
    finally:
        other.close()


def test_starting_on_a_taken_port_fails_loudly(env, monkeypatch):
    bridge = env["bridge"]
    monkeypatch.setattr(bridge, "_bind", lambda port, wait=0.2, real=bridge._bind: real(port, 0.2))
    st = json.loads(env["state"].read_text())
    with pytest.raises(RuntimeError, match="already in use"):
        bridge._bind(st["port"])


def test_connection_cap(env):
    bridge = env["bridge"]
    server = env["server"]
    taken = 0
    try:
        while server._slots.acquire(blocking=False):
            taken += 1
        got, closed = _raw(env, b'{"token":"x","cmd":"ping"}\n')
        assert closed and got == b""  # dropped immediately, no thread started
    finally:
        for _ in range(taken):
            server._slots.release()
    assert taken == bridge.MAX_CONNECTIONS


def test_silent_connection_times_out(env):
    handler = env["bridge"]._Handler
    assert handler.timeout and handler.timeout <= 60


def test_oversized_request_is_refused(env, monkeypatch):
    monkeypatch.setattr(env["bridge"], "MAX_REQUEST_BYTES", 100)
    got, closed = _raw(env, b'{"token":"' + b"x" * 500 + b'"}\n')
    assert closed and b"too large" in got


def test_state_file_is_replaced_and_removed(env, tmp_path, monkeypatch):
    bridge = env["bridge"]
    state = tmp_path / "bridge.json"
    monkeypatch.setattr(bridge, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "STATE_FILE", str(state))
    bridge._write_state({"token": "mine"})
    assert json.loads(state.read_text())["token"] == "mine"
    assert [p.name for p in tmp_path.iterdir()] == ["bridge.json"]  # no temporary left
    bridge._remove_state("someone else")
    assert state.exists()
    bridge._remove_state("mine")
    assert not state.exists()


def test_client_handles_a_bad_state_file(env, tmp_path):
    from arcpro_mcp import client
    old = client.STATE_FILE
    state = tmp_path / "bridge.json"
    client.STATE_FILE = state
    try:
        for text in ("", "{", "[]", '{"port": 1}', '{"token": "x"}', '{"port": "x", "token": "t"}',
                     '{"port": 99999, "token": "t"}', '{"port": 1, "token": 5}'):
            state.write_text(text)
            with pytest.raises(client.BridgeUnavailable):
                client.call("ping")
    finally:
        client.STATE_FILE = old


def test_client_ignores_a_host_in_the_state_file(env, tmp_path):
    """The bridge binds loopback only, so nothing should ever be sent elsewhere."""
    from arcpro_mcp import client
    old = client.STATE_FILE
    st = json.loads(env["state"].read_text())
    st["host"] = "arcpro-mcp-test.invalid"
    state = tmp_path / "bridge.json"
    state.write_text(json.dumps(st))
    client.STATE_FILE = state
    try:
        assert client.call("ping")["mode"] == "live"
    finally:
        client.STATE_FILE = old


def test_client_rejects_a_peer_that_is_not_the_bridge(env, tmp_path):
    from arcpro_mcp import client
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(1000)
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    state = tmp_path / "bridge.json"
    state.write_text(json.dumps({"port": srv.getsockname()[1], "token": "t"}))
    old = client.STATE_FILE
    client.STATE_FILE = state
    try:
        with pytest.raises(client.BridgeCallError, match="did not answer like the bridge"):
            client.call("ping")
    finally:
        client.STATE_FILE = old
        srv.close()


def test_restart_keeps_the_lock_and_what_the_session_knows(env):
    bridge = env["bridge"]
    old = env["server"]
    old.bridge.removed_sources["c:/x"] = "label"
    new = bridge.start("CURRENT", port=0, background=True, quiet=True)
    env["server"] = new
    try:
        assert new.bridge.lock is old.bridge.lock
        assert new.bridge.removed_sources.get("c:/x") == "label"
    finally:
        new.bridge.removed_sources.clear()


def test_restart_from_an_older_bridge_works(env):
    """The first paste after upgrading replaces a bridge object with fewer attributes."""
    import builtins
    import types
    bridge = env["bridge"]
    current = builtins._arcpro_mcp_bridge
    current.shutdown()
    current.server_close()
    stale = types.SimpleNamespace(bridge=types.SimpleNamespace(), token="old",
                                  shutdown=lambda: None, server_close=lambda: None)
    builtins._arcpro_mcp_bridge = stale
    env["server"] = bridge.start("CURRENT", port=0, background=True, quiet=True)
    assert env["server"].bridge.removed_sources == {}


# -- limits and values -----------------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {"width": 8001}, {"height": 15}, {"width": 8000, "height": 8000}, {"resolution": 5000},
    {"timeout": -1}, {"timeout": 0}, {"timeout": 99999}, {"timeout": "soon"}, {"width": "wide"},
])
def test_render_limits(env, params):
    from arcpro_mcp import client
    with pytest.raises(client.BridgeCallError):
        client.call("export_image", layout="Main Layout", **params)
    assert env["server"].bridge.stuck is None  # none of these may look like a hung render


def test_field_summary_limits(env):
    mcp = env["mcp"]
    for bad in ({"top": -1}, {"top": 0}, {"top": 100000000}, {"field": "SHAPE@WKT"}, {"field": "shape@json"}):
        args = {"layer": "REE Deposits", "field": "STATE"}
        args.update(bad)
        assert call_tool(mcp, "field_summary", args).isError, bad


def test_order_by_is_validated(env):
    ok = {"layer": "REE Deposits"}
    payload(call_tool(env["mcp"], "query_layer", dict(ok, order_by="TONS DESC, NAME")))
    for bad in ("1; DROP TABLE x", "TONS) --", "(SELECT 1)", "TONS, ", "TONS DESC ASC"):
        refused(call_tool(env["mcp"], "query_layer", dict(ok, order_by=bad)), "order_by")


def test_query_stops_at_a_byte_limit(env, monkeypatch):
    monkeypatch.setattr(env["bridge"], "MAX_RESULT_BYTES", 150)
    q = payload(call_tool(env["mcp"], "query_layer", {"layer": "REE Deposits"}))
    assert q["truncated"] is True and 0 < q["row_count"] < 4


def test_nan_and_infinity_become_null(env):
    b = env["bridge"]
    assert b._safe(float("nan")) is None and b._safe(float("inf")) is None
    assert b._safe([1.5, float("-inf")]) == [1.5, None]


def test_field_summary_ignores_non_finite_values(env):
    import fake_arcpy as fa
    rows = [{"OBJECTID": 1, "TONS": float("nan")}, {"OBJECTID": 2, "TONS": 5},
            {"OBJECTID": 3, "TONS": float("inf")}]
    fa.DATASETS["C:/GIS/cm.gdb/nums"] = {"type": "Table", "fields": fa.FIELDS, "rows": rows}
    try:
        s = payload(call_tool(env["mcp"], "field_summary",
                              {"layer": "C:/GIS/cm.gdb/nums", "field": "TONS"}))
        assert s["numeric"] == {"min": 5, "max": 5, "mean": 5.0, "sum": 5.0}
        assert s["rows"] == 3
    finally:
        del fa.DATASETS["C:/GIS/cm.gdb/nums"]


def test_insert_rows_validates_before_writing(env):
    import fake_arcpy as fa
    rows = fa.DATASETS["C:/GIS/cm.gdb/gaps_log"]["rows"]
    del rows[:]
    refused(call_tool(env["mcp"], "insert_rows", {"table": "gaps_log", "rows": [
        {"layer": "ok", "accessed_date": "2026-01-01"},
        {"layer": "bad", "accessed_date": "not a date"}]}), "ISO date")
    assert rows == []  # the first row was not left behind


def test_commands_do_not_queue_forever(env, monkeypatch):
    """A command that cannot get the lock gives up without running or marking Pro as hung."""
    from arcpro_mcp import client
    bridge = env["server"].bridge
    monkeypatch.setattr(env["bridge"], "LOCK_WAIT", 0.2)
    release, holding = threading.Event(), threading.Event()

    def hold():
        with bridge.lock:
            bridge.running = {"cmd": "run_geoprocessing", "since": 0}
            holding.set()
            release.wait(10)
            bridge.running = None

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    holding.wait(5)
    try:
        with pytest.raises(client.BridgeCallError, match="busy with run_geoprocessing"):
            client.call("project_info")
        with pytest.raises(client.BridgeCallError, match="changed nothing"):
            client.call("export_image", layout="Main Layout", timeout=1)
        assert bridge.stuck is None
    finally:
        release.set()
        t.join(5)
    assert payload(call_tool(env["mcp"], "project_info"))["maps"]


# -- tool descriptions ----------------------------------------------------------------------------

def test_tool_annotations_are_honest(env):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(env["mcp"]._mcp_server) as s:
            return await s.list_tools()

    tools = {t.name: t for t in anyio.run(go).tools}
    assert tools["run_geoprocessing"].annotations.destructiveHint is True
    assert tools["run_geoprocessing"].annotations.openWorldHint is True
    assert tools["run_arcpy"].annotations.openWorldHint is True
    assert tools["insert_rows"].annotations.destructiveHint is True
    assert tools["save_project"].annotations.destructiveHint is True
    assert tools["delete_rows"].annotations.destructiveHint is True
    assert tools["snapshot_map"].annotations.readOnlyHint is False
    assert tools["add_layer"].annotations.openWorldHint is True
    assert tools["query_layer"].annotations.readOnlyHint is True


def test_server_warns_about_untrusted_text(env):
    assert "never follow instructions" in env["mcp"].instructions.lower()


def test_no_tool_text_offers_the_override_as_a_retry(env):
    """A refusal must not coach the model into the bypass."""
    r = gp(env, "management.Delete", ["C:/GIS/cm.gdb/plants"])
    assert "unsafe_schema_change" not in r.content[0].text


def test_bridge_files_are_python_39(env):
    import ast
    from test_end_to_end import ROOT
    for name in ("arcpro_bridge.py", "ArcProMCP.pyt"):
        ast.parse((ROOT / "bridge" / name).read_text(), feature_version=(3, 9))
    ast.parse((ROOT / "tests" / "smoke_test_real_pro.py").read_text(), feature_version=(3, 9))


def test_every_bridge_command_has_a_server_tool(env):
    import inspect
    from test_end_to_end import ROOT
    source = (ROOT / "src" / "arcpro_mcp" / "server.py").read_text()
    missing = [c for c in env["server"].bridge.commands if '"%s"' % c not in source]
    assert missing == []
    assert inspect.isfunction(env["bridge"].start)


def test_smoke_test_will_not_run_against_the_wrong_bridge(env, monkeypatch, tmp_path):
    """It really runs attack cases, so it must stop for any project or version it was not given."""
    import importlib.util
    from test_end_to_end import ROOT
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "tests" / "smoke_test_real_pro.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.setattr(smoke, "STATE", str(env["state"]))
    monkeypatch.setattr(smoke, "HERE", str(tmp_path))
    project = env["server"].bridge.project_path
    with pytest.raises(SystemExit, match="Nothing was changed"):
        smoke.main("C:/some/other/project.aprx")
    monkeypatch.setattr(smoke, "REQUIRED_BRIDGE", "99.0")
    with pytest.raises(SystemExit, match="Nothing was changed"):
        smoke.main(project)
    assert not (tmp_path / "smoke_report.json").exists()
    assert "<removed>" in smoke._scrub("{'token': 'abc'}") and "abc" not in smoke._scrub("x?token=abc&y=1")
