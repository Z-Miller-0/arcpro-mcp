"""MCP server that lets Claude work in ArcGIS Pro through the arcpro bridge.

Run by Claude Desktop / Claude Code over stdio. Every tool forwards to the bridge
script running inside ArcGIS Pro (see bridge/arcpro_bridge.py).
"""

from __future__ import annotations

import json
import os
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import TextContent, ToolAnnotations

from . import client

mcp = FastMCP(
    "arcgis-pro",
    instructions=(
        "Tools for working in the user's ArcGIS Pro project. Start with project_info and "
        "list_layers to learn what is open. Layer and map names are case-insensitive. "
        "Before run_geoprocessing, call tool_help to get the exact parameter order. "
        "Use snapshot_map to look at the map after changes. Prefer writing outputs to the "
        "project's default geodatabase, and give each dataset all its fields when you create "
        "it: run_geoprocessing refuses schema changes and deletes on datasets a map is "
        "drawing, because those can crash Pro. Nothing is saved to the .aprx until save_project."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


async def _call(cmd: str, **params: Any) -> Any:
    return await anyio.to_thread.run_sync(lambda: client.call(cmd, **params))


# --- Reading the project ----------------------------------------------------------

@mcp.tool(annotations=READ)
async def bridge_status() -> dict:
    """Check that ArcGIS Pro is connected. Reports Pro version, live/standalone mode and project path."""
    return await _call("ping")


@mcp.tool(annotations=READ)
async def project_info() -> dict:
    """List the open project's maps (with layer counts), layouts, home folder and default geodatabase."""
    return await _call("project_info")


@mcp.tool(annotations=READ)
async def list_layers(map: str | None = None) -> dict:
    """List layers (and standalone tables) in a map: visibility, type, data source and definition query.

    Args:
        map: Map name. Defaults to the active map (live mode) or the first map.
    """
    return await _call("list_layers", map=map)


@mcp.tool(annotations=READ)
async def list_workspace(workspace: str | None = None, include_counts: bool = True) -> dict:
    """List the feature classes, tables and rasters in a geodatabase (or folder).

    Each item has its path, type, shape type, row count and, if any map draws it,
    used_by_layers. Check used_by_layers before changing or deleting a dataset.

    Args:
        workspace: Geodatabase or folder path. Defaults to the project's default geodatabase.
        include_counts: Count rows in each feature class and table.
    """
    return await _call("list_workspace", workspace=workspace, include_counts=include_counts)


@mcp.tool(annotations=READ)
async def describe_layer(layer: str, map: str | None = None) -> dict:
    """Describe a layer, standalone table, or gdb dataset: fields, feature/row count, geometry type, spatial reference, extent.

    Args:
        layer: Layer or table name as shown in the Contents pane (or its group path, e.g.
            "Group\\Layer"), a dataset name in the project's default geodatabase
            (e.g. "source_log"), or a full dataset path.
        map: Map name (optional).
    """
    return await _call("describe_layer", layer=layer, map=map)


@mcp.tool(annotations=READ)
async def query_layer(
    layer: str,
    where: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    include_geometry: bool = False,
    order_by: str | None = None,
    map: str | None = None,
) -> dict:
    """Read attribute rows from a feature layer, standalone table, or gdb dataset (honors definition queries).

    Args:
        layer: Layer or table name, a dataset name in the default geodatabase
            (e.g. "gaps_log"), or a full dataset path.
        where: SQL where clause, e.g. "STATE = 'NC' AND TONNAGE > 1000".
        fields: Field names to return. Omit for all non-geometry fields.
        limit: Max rows (1 to 2000).
        include_geometry: Add each feature's centroid x/y.
        order_by: e.g. "TONNAGE DESC" (file and enterprise geodatabases only).
        map: Map name (optional).
    """
    return await _call("query_layer", layer=layer, where=where, fields=fields, limit=limit,
                       include_geometry=include_geometry, order_by=order_by, map=map)


@mcp.tool(annotations=READ)
async def field_summary(layer: str, field: str, where: str | None = None, top: int = 25,
                        map: str | None = None) -> dict:
    """Summarize one field of a layer, table or gdb dataset: row count, distinct values, most common values, and min/max/mean/sum if numeric."""
    return await _call("field_summary", layer=layer, field=field, where=where, top=top, map=map)


@mcp.tool(annotations=READ, structured_output=False)
async def snapshot_map(map: str | None = None, layout: str | None = None,
                       layer: str | None = None, use_view_extent: bool | None = None,
                       width: int = 1400, height: int = 900, timeout: int = 120) -> list:
    """Return a PNG of a map or layout so you can see it, plus a line saying how it was framed.

    With no arguments, captures the active map view in Pro at its current extent. Pass map
    to render a specific map (works even if its tab is not open): it is framed on the
    map's visible data layers (hidden and basemap layers are ignored). Pass layer to frame
    on one layer, or layout to capture a layout page.

    Args:
        map: Map name (optional).
        layout: Layout name (optional; takes priority over map).
        layer: Frame the snapshot on this layer's extent (optional).
        use_view_extent: With map given, use that map's open view extent instead of its data.
        width: Pixel width.
        height: Pixel height.
        timeout: Seconds to wait for Pro to draw. If Pro hangs, you get an error instead of
            a dropped connection, and bridge_status reports the hung command.
    """
    res = await _call("export_image", map=map, layout=layout, layer=layer,
                      use_view_extent=use_view_extent, width=width, height=height,
                      resolution=96, timeout=timeout)
    path = res["path"]
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(path)  # the bridge wrote it to a temp file just for us
        except OSError:
            pass
    info = {k: res[k] for k in ("source", "framed_on", "hidden_layers_ignored", "skipped",
                                "extent") if res.get(k) not in (None, [], 0)}
    return [Image(data=data, format="png"), TextContent(type="text", text=json.dumps(info))]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_arcgis_online(query: str, item_type: str | None = None, limit: int = 20,
                               my_org_only: bool = False) -> dict:
    """Search ArcGIS Online (or the portal Pro is signed in to) for maps, layers and services.

    Uses Pro's current sign-in, so it can see your organization's content. Results include a
    service URL you can pass to add_layer.

    Args:
        query: Search text, e.g. "rare earth deposits USGS".
        item_type: Optional item type filter, e.g. "Feature Service", "Map Service", "Web Map".
        limit: Max results (1 to 100).
        my_org_only: Only return items from your organization.
    """
    return await _call("search_portal", query=query, item_type=item_type, limit=limit,
                       my_org_only=my_org_only)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def describe_url(url: str | None = None, item_id: str | None = None) -> dict:
    """Inspect a web service layer or ArcGIS Online item WITHOUT adding it to a map.

    Returns the layer's fields, its real feature count (from a count query), its declared
    extent, the extent of its actual data in WGS84, and, for an item, its owner and title.
    Use this to vet a layer (owner, count, extent) against its primary source before
    add_layer. A service root (.../FeatureServer) lists its layers instead.

    Args:
        url: Service layer URL, e.g. ".../FeatureServer/0".
        item_id: ArcGIS Online / portal item id (from search_arcgis_online). The item's
            service URL is described too.
    """
    return await _call("describe_url", url=url, item_id=item_id)


@mcp.tool(annotations=READ)
async def tool_help(tool: str) -> dict:
    """Get the parameter list for a geoprocessing tool. Call this before run_geoprocessing.

    Args:
        tool: Tool as "toolbox.Tool", e.g. "analysis.Buffer", "management.SelectLayerByLocation".
    """
    return await _call("tool_help", tool=tool)


# --- Changing the map ----------------------------------------------------------------

@mcp.tool(annotations=EDIT)
async def add_layer(source: str, map: str | None = None, name: str | None = None) -> dict:
    """Add data to a map: a file path (shapefile, gdb feature class, .lyrx, CSV, raster) or a service URL.

    Args:
        source: e.g. r"C:\\GIS\\data.gdb\\mines" or "https://services.arcgis.com/.../FeatureServer/0".
        map: Map name (optional).
        name: Optional display name for the new layer.
    """
    return await _call("add_layer", source=source, map=map, name=name)


@mcp.tool(annotations=EDIT)
async def set_layer_properties(layer: str, visible: bool | None = None,
                               definition_query: str | None = None,
                               transparency: int | None = None, new_name: str | None = None,
                               map: str | None = None) -> dict:
    """Change a layer's visibility, definition query (filter; "" clears it), transparency (0-100) or name."""
    return await _call("set_layer_properties", layer=layer, visible=visible,
                       definition_query=definition_query, transparency=transparency,
                       new_name=new_name, map=map)


@mcp.tool(annotations=EDIT)
async def zoom_to_layer(layer: str, map: str | None = None) -> dict:
    """Zoom Pro's active map view to a layer's extent (live mode only)."""
    return await _call("zoom_to_layer", layer=layer, map=map)


@mcp.tool(annotations=EDIT)
async def create_map(name: str, include_basemap: bool = True) -> dict:
    """Create a new map in the project.

    Args:
        name: Map name.
        include_basemap: Pro adds its default basemap to new maps. Pass false for a map with
            no layers at all (it keeps the basemap's coordinate system until you change it).
    """
    return await _call("create_map", name=name, include_basemap=include_basemap)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_layer(layer: str, map: str | None = None) -> dict:
    """Remove a layer or standalone table from a map (the underlying data is not deleted)."""
    return await _call("remove_layer", layer=layer, map=map)


@mcp.tool(annotations=DESTRUCTIVE)
async def run_geoprocessing(tool: str, args: list[Any] | None = None,
                            kwargs: dict[str, Any] | None = None,
                            add_outputs_to_map: bool = True, map: str | None = None,
                            overwrite: bool = False,
                            unsafe_schema_change: bool = False) -> dict:
    """Run any ArcGIS geoprocessing tool, e.g. Buffer, Clip, Spatial Join, Summary Statistics.

    Call tool_help first for the parameter order. Some tools overwrite or delete data, so
    choose new output names unless the user asked to overwrite.

    Schema changes and deletes (Delete, Rename, AddField(s), DeleteField, AlterField,
    JoinField, CalculateField on a new field, AddGeometryAttributes, TruncateTable, ...)
    are refused when a map in the project draws the target dataset or it is locked,
    because that can hang or crash Pro. Build a new dataset with all its fields instead.

    Args:
        tool: "toolbox.Tool", e.g. "analysis.Buffer".
        args: Positional parameters in tool order, e.g. ["mines", "mines_buf10km", "10 Kilometers"].
        kwargs: Named parameters, e.g. {"dissolve_option": "ALL"}.
        add_outputs_to_map: Add outputs to the active map.
        map: Map whose layer names the args refer to (defaults to the active map). Layer
            names are passed to the tool as the actual layers, so filters apply.
        overwrite: Replace existing outputs. Only set this when the user asked to overwrite.
        unsafe_schema_change: Skip the schema-change guard. Only when the user has explicitly
            accepted the risk of crashing Pro, after a backup save_project(copy_to=...).
    """
    return await _call("run_geoprocessing", tool=tool, args=args, kwargs=kwargs,
                       add_outputs_to_map=add_outputs_to_map, map=map, overwrite=overwrite,
                       unsafe_schema_change=unsafe_schema_change)


@mcp.tool(annotations=EDIT)
async def insert_rows(table: str, rows: list[dict[str, Any]], map: str | None = None) -> dict:
    """Append rows to a table or feature class (e.g. a source_log or gaps_log table).

    Args:
        table: Table or layer name in the map, a dataset name in the default geodatabase,
            or a full path. The table must already exist with the needed fields.
        rows: One object per row, e.g. [{"layer": "L2", "dataset": "USGS REE", "data_year": 2024}].
            Field names are case-insensitive. Dates as ISO strings ("2026-11-05").
            For points use "SHAPE@XY": [x, y] (in the dataset's coordinate system), or
            "SHAPE@WKT" for any geometry. Up to 10,000 rows per call.
        map: Map name (optional).
    """
    return await _call("insert_rows", table=table, rows=rows, map=map)


@mcp.tool(annotations=EDIT)
async def export_layout_pdf(path: str, layout: str | None = None, resolution: int = 300,
                            timeout: int = 300) -> dict:
    """Export a layout to a PDF file on the user's computer (the folder is created if needed).

    Args:
        path: Output path, e.g. r"C:\\Users\\me\\Documents\\magnet_map.pdf".
        layout: Layout name (defaults to the first layout).
        resolution: DPI.
        timeout: Seconds to wait for Pro to draw before returning an error.
    """
    return await _call("export_layout_pdf", path=path, layout=layout, resolution=resolution,
                       timeout=timeout)


@mcp.tool(annotations=EDIT)
async def save_project(copy_to: str | None = None) -> dict:
    """Save the .aprx. Pass copy_to (a new .aprx path) to save a copy instead; its folder is created if needed."""
    return await _call("save_project", copy_to=copy_to)


@mcp.tool(annotations=DESTRUCTIVE)
async def run_arcpy(code: str) -> dict:
    """Run Python inside ArcGIS Pro, for things the other tools do not cover (symbology, labels, layout elements).

    `arcpy` and `aprx` (the open ArcGISProject) are predefined. Printed output is returned;
    set a variable named `result` to return a value. Disabled unless the user started the
    bridge with allow_exec=True. Do not use it to change the schema of, or delete, a
    dataset that a map is drawing (the guard in run_geoprocessing does not see this code).
    """
    return await _call("run_arcpy", code=code)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
