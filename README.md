# arcpro-mcp: Claude in ArcGIS Pro

Lets Claude (Desktop or Code) work in your open ArcGIS Pro project: read layers and
attributes, add data and services, filter layers, run any geoprocessing tool, search
ArcGIS Online with your Pro sign-in, and take snapshots of the map so Claude can see
what it did.

```
Claude Desktop  --stdio-->  arcpro-mcp server  --localhost JSON-->  bridge script inside ArcGIS Pro (arcpy)
```

The bridge only uses the Python standard library plus arcpy, so you do **not** have to
clone or modify ArcGIS Pro's Python environment.

## Setup (Windows, about 10 minutes)

### 1. Install uv (runs the server; it fetches the right Python and packages for you)

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Put this folder somewhere permanent

For example `C:\Users\<you>\arcpro-mcp`. The paths below assume that.

### 3. Add the server to Claude Desktop

Claude Desktop > Settings > Developer > Edit Config, then add (merge with anything already there):

```json
{
  "mcpServers": {
    "arcgis-pro": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\<you>\\arcpro-mcp", "run", "arcpro-mcp"]
    }
  }
}
```

Fully quit Claude Desktop (tray icon > Quit) and reopen it.

For Claude Code instead:

```powershell
claude mcp add arcgis-pro -- uv --directory C:\Users\<you>\arcpro-mcp run arcpro-mcp
```

### 4. Start the bridge in ArcGIS Pro

Open your project, then **View > Python window**, and paste (change the folder if you
put arcpro-mcp somewhere other than Documents; it finds the file even if unzipping
created a nested `arcpro-mcp\arcpro-mcp` folder):

```python
import glob, os
hits = glob.glob(os.path.expanduser(r"~\Documents\arcpro-mcp\**\bridge\arcpro_bridge.py"), recursive=True)
print(hits)
exec(open(hits[0]).read())
```

You should see `[arcpro-mcp] bridge 0.1.4 listening on 127.0.0.1:18760 (live mode ...)`.
Run it again each time you reopen Pro.

### 5. Try it

Ask Claude: "Check the ArcGIS Pro connection and tell me what's in my project."

## Tools

| Tool | What it does |
| --- | --- |
| `bridge_status`, `project_info`, `list_layers` | What is open: maps, layouts, layers, data sources |
| `describe_layer` | Fields, feature count, geometry type, spatial reference, extent |
| `query_layer`, `field_summary` | Read rows with a where clause; quick stats on one field |
| `snapshot_map` | PNG of the active view, any map (even with no tab open) or a layout, returned to Claude as an image |
| `search_arcgis_online` | Search ArcGIS Online / your org's portal using Pro's sign-in |
| `add_layer` | Add a file, feature class, .lyrx or service URL to a map |
| `set_layer_properties`, `zoom_to_layer`, `create_map`, `remove_layer` | Change the map |
| `tool_help`, `run_geoprocessing` | Look up and run any GP tool (`analysis.Buffer`, `analysis.SpatialJoin`, ...) |
| `export_layout_pdf`, `save_project` | Output and save. Nothing is written to the .aprx until `save_project` |
| `run_arcpy` | Arbitrary Python in Pro (symbology, labels, layout elements). **Off by default** |

To enable `run_arcpy`, run this in Pro's Python window after the `exec` line:

```python
start(allow_exec=True)
```

## Standalone mode (Pro closed)

```powershell
"C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat" C:\Users\<you>\arcpro-mcp\bridge\arcpro_bridge.py --project "C:\GIS\MyProject.aprx"
```

Works on a saved .aprx without the Pro window. `zoom_to_layer` and active-view
snapshots need live mode (snapshots fall back to the first layout). Don't have the
same .aprx open in Pro at the same time.

## Safety

- The bridge listens on 127.0.0.1 only and requires a random token that is regenerated
  every start (stored in `%USERPROFILE%\.arcpro_mcp\bridge.json`).
- Claude acts with your ArcGIS license and file permissions. Geoprocessing can overwrite
  data. Claude Desktop asks before each tool call: keep that on for `run_geoprocessing`,
  `remove_layer` and `run_arcpy` rather than choosing "always allow".
- Work on a copy of a project the first few times (`save_project` with `copy_to`).

## Troubleshooting

- **Smoke test**: with the bridge running, `propy.bat tests\smoke_test_real_pro.py` exercises every
  command in a scratch map and writes `tests\smoke_report.json`.
- **"Service check failed ... error 499"**: the layer URL needs a sign-in or was retired.
  Ask Claude to find a current one with `search_arcgis_online`.

- **"The ArcGIS Pro bridge is not running"**: run the `exec(...)` line in Pro's Python window.
- **Tools time out in live mode**: some arcpy calls may not like being run from the
  bridge's background thread. Use standalone mode, and please note which tool hung.
- **Server doesn't appear in Claude Desktop**: check the uv.exe path in the config
  (`where.exe uv` in PowerShell) and look at Settings > Developer for the error log.
- **Portal search shows `signed_in: false`**: sign in to ArcGIS Online in Pro (top-right).
- Long geoprocessing runs: raise the timeout with env var `ARCPRO_MCP_TIMEOUT` (seconds, default 900).

## Status

Version 0.1.4. Smoke-tested against ArcGIS Pro (Python 3.11) in live mode, plus 17
automated tests against a simulated arcpy. Errors come back to Claude with the
Python traceback, which makes them quick to fix.

Run the tests: `uv run --extra test pytest`.
