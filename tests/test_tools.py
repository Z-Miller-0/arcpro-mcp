"""The basic tools added in 0.3.0, through MCP client -> server -> TCP bridge -> fake arcpy.

These test the plumbing and the rules. tests/smoke_test_real_pro.py runs the same tools against
real ArcGIS Pro, which is what shows that the arcpy calls themselves work.
"""

import pytest

from test_end_to_end import call_tool, payload
from test_security import refused


@pytest.fixture
def lmap(env):
    """A scratch map with two layers and a group."""
    import fake_arcpy as fa
    m = fa.Map("Tools")
    m._layers = [fa.Layer("Top", "C:/GIS/cm.gdb/deposits"), fa.Layer("Bottom", "C:/GIS/cm.gdb/plants")]
    env["server"].bridge.aprx._maps.append(m)
    yield m
    if m in env["server"].bridge.aprx._maps:
        env["server"].bridge.aprx._maps.remove(m)


def tool(env, tool_name, **args):
    return payload(call_tool(env["mcp"], tool_name, args))


def test_rename_and_remove_map(env, lmap):
    assert tool(env, "rename_map", map="Tools", new_name="Renamed")["map"] == "Renamed"
    refused(call_tool(env["mcp"], "rename_map", {"map": "Renamed", "new_name": "US Magnet Stack"}),
            "already")
    refused(call_tool(env["mcp"], "rename_map", {"map": "Renamed", "new_name": " "}), "empty")
    assert tool(env, "remove_map", map="Renamed")["removed_map"] == "Renamed"
    assert lmap not in env["server"].bridge.aprx.listMaps()
    env["server"].bridge.removed_sources.clear()


def test_set_map_properties(env, lmap):
    r = tool(env, "set_map_properties", map="Tools", spatial_reference_wkid=54012)
    assert r["changed"]["spatial_reference"] == "World_Eckert_IV"
    assert tool(env, "set_map_properties", map="Tools", reference_scale=50000)["changed"]["reference_scale"] == 50000
    refused(call_tool(env["mcp"], "set_map_properties", {"map": "Tools"}), "Give")
    assert call_tool(env["mcp"], "set_map_properties", {"map": "Tools", "spatial_reference_wkid": -1}).isError


def test_basemaps(env, lmap):
    assert "Imagery" in tool(env, "list_basemaps")["basemaps"]
    assert tool(env, "add_basemap", basemap="Imagery", map="Tools")["added_basemap"] == "Imagery"
    refused(call_tool(env["mcp"], "add_basemap", {"basemap": "nope", "map": "Tools"}), "list_basemaps")


def test_zoom_to_extent(env, lmap):
    r = tool(env, "zoom_to_extent", xmin=1, ymin=2, xmax=3, ymax=4, map="Tools")
    assert r["map"] == "Tools"
    refused(call_tool(env["mcp"], "zoom_to_extent", {"xmin": 3, "ymin": 2, "xmax": 1, "ymax": 4}), "xmin")


def test_move_layer_and_groups(env, lmap):
    r = tool(env, "move_layer", layer="Top", reference_layer="Bottom", position="AFTER", map="Tools")
    assert r["order"] == ["Bottom", "Top"]
    r = tool(env, "move_layer", layer="Top", reference_layer="Bottom", map="Tools")
    assert r["order"] == ["Top", "Bottom"]
    refused(call_tool(env["mcp"], "move_layer", {"layer": "Top", "reference_layer": "Top", "map": "Tools"}), "itself")
    refused(call_tool(env["mcp"], "move_layer", {"layer": "Top", "reference_layer": "Bottom", "position": "UP", "map": "Tools"}), "BEFORE or AFTER")
    assert tool(env, "create_group_layer", name="Group", map="Tools")["created_group"] == "Group"
    r = tool(env, "move_layer_to_group", layer="Bottom", group="Group", map="Tools")
    assert "Group\\Bottom" in r["order"] and "Bottom" not in r["order"]
    refused(call_tool(env["mcp"], "move_layer_to_group", {"layer": "Top", "group": "Group\\Bottom", "map": "Tools"}), "not a group")
    refused(call_tool(env["mcp"], "move_layer_to_group", {"layer": "Group", "group": "Group", "map": "Tools"}), "into itself")


def test_move_to_group_does_not_mark_the_data_removed(env, lmap):
    bridge = env["server"].bridge
    tool(env, "create_group_layer", name="G", map="Tools")
    tool(env, "move_layer_to_group", layer="Bottom", group="G", map="Tools")
    assert not bridge.removed_sources


def test_layer_scale_range_and_labels_flag(env, lmap):
    r = tool(env, "set_layer_properties", layer="Top", map="Tools", min_scale=1000000, max_scale=1000, show_labels=True)
    assert r["changed"] == {"min_scale": 1000000.0, "max_scale": 1000.0, "show_labels": True}
    assert call_tool(env["mcp"], "set_layer_properties", {"layer": "Top", "map": "Tools", "transparency": 101}).isError


def test_selection(env, lmap):
    assert tool(env, "select_by_attribute", layer="Top", where="STATE = 'WY'", map="Tools")["selected"] == 2
    r = tool(env, "get_selection", layer="Top", map="Tools")
    assert r["selected"] == 2 and r["object_ids"] == [2, 4]
    q = tool(env, "query_layer", layer="Top", map="Tools", selected_only=True, fields=["NAME"])
    assert {row["NAME"] for row in q["rows"]} == {"Bear Lodge", "Halleck Creek"}
    assert tool(env, "select_by_attribute", layer="Top", where="STATE = 'TX'", method="ADD", map="Tools")["selected"] == 3
    assert tool(env, "select_by_attribute", layer="Top", where="STATE = 'TX'", method="REMOVE", map="Tools")["selected"] == 2
    assert tool(env, "select_by_attribute", layer="Top", where="NAME = 'Bear Lodge'", method="SUBSET", map="Tools")["selected"] == 1
    # Real arcpy bug this guards against: a cursor on the layer sees only the current
    # selection, so NEW used to search inside it instead of replacing it.
    assert tool(env, "select_by_attribute", layer="Top", where="STATE = 'CA'", map="Tools")["selected"] == 1
    assert tool(env, "get_selection", layer="Top", map="Tools")["object_ids"] == [1]
    q = tool(env, "query_layer", layer="Top", map="Tools")
    assert q["row_count"] == 1 and "1 selected rows" in q["note"]
    assert "1 selected rows" in tool(env, "field_summary", layer="Top", field="STATE", map="Tools")["note"]
    refused(call_tool(env["mcp"], "select_by_attribute", {"layer": "Top", "method": "MAYBE", "map": "Tools"}), "method must be")
    tool(env, "clear_selection", layer="Top", map="Tools")
    assert tool(env, "get_selection", layer="Top", map="Tools")["selected"] == 0
    q = tool(env, "query_layer", layer="Top", map="Tools", selected_only=True)
    assert q["row_count"] == 0  # nothing selected is not the same as everything
    assert tool(env, "select_by_attribute", layer="Top", map="Tools")["selected"] == 4
    assert tool(env, "clear_selection", map="Tools")["cleared_map"] == "Tools"
    assert tool(env, "get_selection", layer="Top", map="Tools")["selected"] == 0


def test_selection_needs_a_layer_in_a_map(env):
    refused(call_tool(env["mcp"], "select_by_attribute", {"layer": "C:/GIS/cm.gdb/deposits"}), "in a map")


def test_select_by_location_cleans_up(env, lmap):
    import fake_arcpy as fa
    r = tool(env, "select_by_location", layer="Top", overlap_type="intersect", select_features="Bottom", map="Tools")
    assert r["selected"] == 2 and fa.TEMP_LAYERS == []
    assert fa.env.addOutputsToMap is True  # restored


def test_symbology(env, lmap):
    lyr = lmap.listLayers()[0]
    assert "Blues (5 Classes)" in tool(env, "list_color_ramps", wildcard="*Blue*")["color_ramps"]
    tool(env, "set_symbology", layer="Top", map="Tools", color=[200, 30, 30], size=9, outline_color=[0, 0, 0, 50], outline_width=2)
    s = lyr.symbology.renderer.symbol
    assert s.color == {"RGB": [200, 30, 30, 100]} and s.outlineColor == {"RGB": [0, 0, 0, 50]} and s.size == 9
    tool(env, "set_symbology", layer="Top", map="Tools", renderer="unique", field="state", color_ramp="Blues (5 Classes)")
    assert lyr.symbology.renderer.type == "UniqueValueRenderer" and lyr.symbology.renderer.fields == ["STATE"]
    tool(env, "set_symbology", layer="Top", map="Tools", renderer="graduated_colors", field="TONS", classes=4, method="NaturalBreaks")
    r = lyr.symbology.renderer
    assert (r.type, r.classificationField, r.breakCount) == ("GraduatedColorsRenderer", "TONS", 4)
    tool(env, "set_symbology", layer="Top", map="Tools", renderer="graduated_symbols", field="TONS", min_size=3, max_size=20)
    assert lyr.symbology.renderer.maximumSymbolSize == 20


@pytest.mark.parametrize("args,text", [
    ({"renderer": "heatmap"}, "renderer must be"), ({"renderer": "unique"}, "needs a field"),
    ({"renderer": "unique", "field": "nope"}, "No field"), ({"color": [300, 0, 0]}, "0-255"),
    ({"color": [1, 2]}, "0-255"), ({"color": [1, 2, 3, 200]}, "0-255"),
    ({"renderer": "unique", "field": "STATE", "color_ramp": "nope"}, "list_color_ramps"),
    ({"renderer": "graduated_colors", "field": "TONS", "classes": 99}, "classes"),
])
def test_symbology_errors(env, lmap, args, text):
    refused(call_tool(env["mcp"], "set_symbology", dict(args, layer="Top", map="Tools")), text)


def test_labels_are_arcade_only(env, lmap):
    lyr = lmap.listLayers()[0]
    r = tool(env, "set_labels", layer="Top", map="Tools", expression="$feature.NAME")
    assert r["created_label_class"] == "Claude labels" and lyr.showLabels is True
    assert lyr.listLabelClasses()[0].language == "ARCADE"
    r = tool(env, "set_labels", layer="Top", map="Tools", expression="$feature.STATE")
    assert r["updated_label_class"] == "Claude labels" and lyr.listLabelClasses()[0].expression == "$feature.STATE"
    tool(env, "set_labels", layer="Top", map="Tools", enabled=False)
    assert lyr.showLabels is False


def test_labels_refuse_to_edit_a_python_class(env, lmap):
    """Setting an expression on a Python label class would run it as code when Pro draws."""
    lyr = lmap.listLayers()[0]
    lyr.createLabelClass("Old", "[NAME]", None, "PYTHON")
    refused(call_tool(env["mcp"], "set_labels", {"layer": "Top", "map": "Tools", "expression": "x", "label_class": "Old"}), "not Arcade")
    assert lyr.listLabelClasses()[0].expression == "[NAME]"


def test_other_label_classes_are_turned_off(env, lmap):
    lyr = lmap.listLayers()[0]
    lyr.createLabelClass("Old", "$feature.NAME", None, "ARCADE")
    tool(env, "set_labels", layer="Top", map="Tools", expression="$feature.STATE")
    assert {c.name: c.visible for c in lyr.listLabelClasses()} == {"Old": False, "Claude labels": True}


def test_layout_lifecycle(env, lmap, tmp_path):
    aprx = env["server"].bridge.aprx
    r = tool(env, "create_layout", name="Poster", map="Tools")
    assert r["map_frame"] == "Map Frame" and r["map"] == "Tools"
    refused(call_tool(env["mcp"], "create_layout", {"name": "poster"}), "already")
    refused(call_tool(env["mcp"], "create_layout", {"name": "Tiny", "width": 1, "height": 1, "margin": 2}), "margins")
    refused(call_tool(env["mcp"], "create_layout", {"name": "Odd", "units": "FURLONG"}), "units")
    assert tool(env, "add_layout_text", layout="Poster", text="Title", x=1, y=8, size=24, name="title")["element"]["text"] == "Title"
    tool(env, "add_layout_surround", layout="Poster", kind="legend", x=8, y=1, name="legend")
    tool(env, "add_layout_surround", layout="Poster", kind="north_arrow", x=9, y=7, width=1)
    tool(env, "add_layout_surround", layout="Poster", kind="scale_bar", x=1, y=1)
    refused(call_tool(env["mcp"], "add_layout_surround", {"layout": "Poster", "kind": "compass", "x": 1, "y": 1}), "kind must be")
    refused(call_tool(env["mcp"], "add_layout_surround", {"layout": "Poster", "kind": "north_arrow", "x": 1, "y": 1, "style_item": "nope"}), "style")
    d = tool(env, "describe_layout", layout="Poster")
    assert len(d["elements"]) == 5 and d["page_width"] == 11
    r = tool(env, "update_layout_element", layout="Poster", element="title", text="New", x=2, visible=False, new_name="t2")
    assert r["element"]["text"] == "New" and r["element"]["x"] == 2 and r["element"]["name"] == "t2"
    refused(call_tool(env["mcp"], "update_layout_element", {"layout": "Poster", "element": "legend", "text": "x"}), "not a text element")
    refused(call_tool(env["mcp"], "update_layout_element", {"layout": "Poster", "element": "nope"}), "No element")
    assert tool(env, "remove_layout_element", layout="Poster", element="t2")["removed_element"] == "t2"
    png = tmp_path / "out" / "poster.png"
    tool(env, "export_layout", path=str(png), layout="Poster", resolution=150)
    assert png.exists()
    assert tool(env, "remove_layout", layout="Poster")["removed_layout"] == "Poster"
    assert "Poster" not in [l.name for l in aprx.listLayouts()]


def test_map_frame_extent(env, lmap):
    tool(env, "create_layout", name="Frame", map="Tools")
    r = tool(env, "set_map_frame_extent", layout="Frame", layer="Top")
    assert r["map_frame"] == "Map Frame"
    tool(env, "set_map_frame_extent", layout="Frame", xmin=0, ymin=0, xmax=1, ymax=1)
    assert tool(env, "set_map_frame_extent", layout="Frame", scale=25000)["scale"] == 25000
    tool(env, "set_map_frame_extent", layout="Frame")
    refused(call_tool(env["mcp"], "set_map_frame_extent", {"layout": "Frame", "map_frame": "nope"}), "No map frame")
    refused(call_tool(env["mcp"], "set_map_frame_extent", {"layout": "Main Layout"}), "no map frame")
    tool(env, "remove_layout", layout="Frame")


def test_create_dataset(env):
    import fake_arcpy as fa
    r = tool(env, "create_dataset", name="sites_v2", geometry_type="POINT", spatial_reference_wkid=54012,
             fields=[{"name": "site", "type": "text", "length": 100}, {"name": "tons", "type": "LONG"}])
    assert r["created"] == "C:/GIS/cm.gdb/sites_v2" and r["fields"] == ["site", "tons"]
    assert [f.name for f in fa.DATASETS[r["created"]]["fields"]] == ["OBJECTID", "Shape", "site", "tons"]
    assert fa.env.addOutputsToMap is True
    assert fa.DATASETS["C:/GIS/cm.gdb/sites_v2"]["type"] == "FeatureClass"
    assert fa.DATASETS[tool(env, "create_dataset", name="log_v2")["created"]]["type"] == "Table"
    refused(call_tool(env["mcp"], "create_dataset", {"name": "sites_v2"}), "already exists")
    del fa.DATASETS["C:/GIS/cm.gdb/sites_v2"], fa.DATASETS["C:/GIS/cm.gdb/log_v2"]


@pytest.mark.parametrize("args,text", [
    ({"name": "bad name"}, "name must"), ({"name": "../x"}, "name must"), ({"name": "1x"}, "name must"),
    ({"name": "x", "workspace": "D:/other.gdb"}, "outside the open project"),
    ({"name": "x", "geometry_type": "BLOB"}, "geometry_type"),
    ({"name": "x", "fields": [{"name": "a"}]}, "name and a type"),
    ({"name": "x", "fields": [{"name": "a b", "type": "TEXT"}]}, "field name"),
    ({"name": "x", "fields": [{"name": "a", "type": "TEXT"}, {"name": "A", "type": "TEXT"}]}, "repeated"),
    ({"name": "x", "fields": [{"name": "a", "type": "SQL"}]}, "Field type"),
])
def test_create_dataset_errors(env, args, text):
    refused(call_tool(env["mcp"], "create_dataset", args), text)


@pytest.fixture
def rows(env):
    import fake_arcpy as fa
    path = "C:/GIS/cm.gdb/edit_me"
    data = [{"OBJECTID": i, "site": s, "tons": t} for i, (s, t) in enumerate((("a", 1), ("b", 2), ("b", 3)), 1)]
    fa.DATASETS[path] = {"type": "Table", "rows": data, "fields": [
        fa._Field("OBJECTID", "OID"), fa._Field("site", "String"), fa._Field("tons", "Integer"),
        fa._Field("when", "Date"), fa._Field("explode", "String")]}
    for r in data:
        r["when"] = None
        r["explode"] = None
    yield path, data
    del fa.DATASETS[path]


def test_update_rows(env, rows):
    path, data = rows
    r = tool(env, "update_rows", table=path, where="site = 'a'", values={"TONS": 9, "when": "2026-11-06"})
    assert r["updated"] == 1 and data[0]["tons"] == 9 and data[0]["when"].year == 2026
    refused(call_tool(env["mcp"], "update_rows", {"table": path, "where": "site = 'b'", "values": {"tons": 0}, "max_rows": 1}), "nothing was changed")
    assert data[1]["tons"] == 2  # the refusal changed nothing
    assert tool(env, "update_rows", table=path, where="site = 'b'", values={"tons": 0}, max_rows=2)["updated"] == 2


@pytest.mark.parametrize("args,text", [
    ({"where": "", "values": {"tons": 1}}, "needs a where"), ({"where": "   ", "values": {"tons": 1}}, "needs a where"),
    ({"where": "site = 'a'", "values": {}}, "values must"), ({"where": "site = 'a'", "values": {"nope": 1}}, "No field"),
    ({"where": "site = 'a'", "values": {"OBJECTID": 5}}, "cannot be set"), ({"where": "site = 'a'", "values": {"when": "soon"}}, "ISO date"),
    ({"where": "site = 'a'", "values": {"tons": 1}, "max_rows": 0}, "max_rows"), ({"where": "site = 'a'", "values": {"tons": 1}, "max_rows": 100000}, "max_rows"),
])
def test_update_rows_errors(env, rows, args, text):
    path, data = rows
    refused(call_tool(env["mcp"], "update_rows", dict(args, table=path)), text)
    assert [r["tons"] for r in data] == [1, 2, 3]


def test_failed_edit_is_reported(env, rows):
    path, _data = rows
    refused(call_tool(env["mcp"], "update_rows", {"table": path, "where": "site = 'a'", "values": {"explode": "x"}}), "failed")


def test_delete_rows(env, rows):
    path, data = rows
    refused(call_tool(env["mcp"], "delete_rows", {"table": path, "where": ""}), "needs a where")
    refused(call_tool(env["mcp"], "delete_rows", {"table": path, "where": "site = 'b'", "max_rows": 1}), "nothing was deleted")
    assert len(data) == 3
    assert tool(env, "delete_rows", table=path, where="site = 'b'")["deleted"] == 2
    assert [r["site"] for r in data] == ["a"]


def test_row_edits_refuse_hosted_and_enterprise_data(env):
    import fake_arcpy as fa
    for path in ("C:/GIS/server.sde/db.owner.t",):
        fa.DATASETS[path] = {"type": "Table", "fields": fa.FIELDS, "rows": []}
        try:
            refused(call_tool(env["mcp"], "update_rows", {"table": path, "where": "a = 1", "values": {"TONS": 1}}), "enterprise")
            refused(call_tool(env["mcp"], "delete_rows", {"table": path, "where": "a = 1"}), "enterprise")
            refused(call_tool(env["mcp"], "insert_rows", {"table": path, "rows": [{"TONS": 1}]}), "enterprise")
        finally:
            del fa.DATASETS[path]


def test_a_layer_definition_query_limits_row_edits(env, rows):
    import fake_arcpy as fa
    path, data = rows
    m = fa.Map("Edits")
    lyr = fa.Layer("EditLayer", path)
    lyr.definitionQuery = "site = 'b'"
    m._layers = [lyr]
    aprx = env["server"].bridge.aprx
    aprx._maps.append(m)
    try:
        r = tool(env, "update_rows", table="EditLayer", where="tons = 2", values={"tons": 20}, map="Edits")
        assert r["updated"] == 1 and data[1]["tons"] == 20
    finally:
        aprx._maps.remove(m)


def test_list_broken_sources(env):
    assert tool(env, "list_broken_sources") == {"count": 0, "broken": []}
