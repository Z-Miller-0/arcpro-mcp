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

**Option A: one line in the Python window.** Open your project, then **View > Python
window**, and paste this as **one line** (the Python window mangles multi-line pastes;
change the path if arcpro-mcp is not in Documents):

```python
exec(open(r"C:\Users\<you>\Documents\arcpro-mcp\bridge\arcpro_bridge.py").read()); start(allow_exec=False)
```

Use `allow_exec=True` to also turn on `run_arcpy`. If unzipping made a nested
`arcpro-mcp\arcpro-mcp` folder, this one-liner finds the file anyway:

```python
import glob, os; exec(open(glob.glob(os.path.expanduser(r"~\Documents\arcpro-mcp\**\bridge\arcpro_bridge.py"), recursive=True)[0]).read()); start(allow_exec=False)
```

**Option B: the toolbox (no typing).** In the **Catalog** pane, right-click
**Toolboxes > Add Toolbox** and pick `bridge\ArcProMCP.pyt`. Right-click it and choose
**Add To Favorites** so every project has it. Then open **Start Bridge**, tick
**Allow run_arcpy** if you want it, and click Run. **Stop Bridge** and **Bridge Status**
are in the same toolbox. Keep the .pyt next to `arcpro_bridge.py`.

Either way you should see `bridge 0.2.0 listening on 127.0.0.1:18760 (live mode, run_arcpy ...)`.
Start it again each time you reopen Pro.

### 5. Try it

Ask Claude: "Check the ArcGIS Pro connection and tell me what's in my project."

## Tools

| Tool | What it does |
| --- | --- |
| `bridge_status`, `project_info`, `list_layers` | What is open: maps, layouts, layers, standalone tables, data sources. `bridge_status` answers even while Pro is busy and reports a hung command |
| `list_workspace` | Feature classes, tables and rasters in a geodatabase, with counts and which map layers use each |
| `describe_layer` | Fields, feature/row count, geometry type, spatial reference, extent (layers, tables, or gdb datasets by name/path) |
| `query_layer`, `field_summary` | Read rows with a where clause; quick stats on one field. Work on tables like `source_log` too |
| `insert_rows` | Append rows to a table or feature class (log tables, point lists) |
| `snapshot_map` | PNG of the active view, any map (framed on its visible data), one layer, or a layout, plus a line saying how it was framed |
| `search_arcgis_online` | Search ArcGIS Online / your org's portal using Pro's sign-in |
| `describe_url` | Vet a service layer or portal item (owner, real feature count, data extent, fields) without adding it to a map |
| `add_layer` | Add a file, feature class, table, .lyrx or service URL to a map |
| `set_layer_properties`, `zoom_to_layer`, `create_map`, `remove_layer` | Change the map. `create_map(include_basemap=false)` gives a map with no layers |
| `tool_help`, `run_geoprocessing` | Look up and run any GP tool (`analysis.Buffer`, `analysis.SpatialJoin`, ...) |
| `export_layout_pdf`, `save_project` | Output and save. Nothing is written to the .aprx until `save_project`; `copy_to` creates its folder |
| `run_arcpy` | Arbitrary Python in Pro (symbology, labels, layout elements). **Off by default** |

To enable `run_arcpy` after starting without it, run `start(allow_exec=True)` in Pro's
Python window, or rerun the toolbox's Start Bridge with the box ticked.

## Standalone mode (Pro closed)

```powershell
& "C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat" C:\Users\<you>\arcpro-mcp\bridge\arcpro_bridge.py --project "C:\GIS\MyProject.aprx" --allow-exec
```

(The leading `&` is needed in PowerShell. Drop `--allow-exec` if you don't need `run_arcpy`.)

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
- **Schema guard.** `run_geoprocessing` refuses tools that change a dataset's schema or
  delete it (Delete, Rename, AddField(s), DeleteField, AlterField, JoinField, CalculateField
  on a new field, AddGeometryAttributes, TruncateTable, index and GlobalID tools) when any
  map in the project draws that dataset, or when another process holds a lock on it. Doing
  that can hang or crash Pro, and a Delete that hits a lock can leave a half-deleted
  feature class. Build a new dataset with all its fields instead, repoint the layers, and
  delete the old one after a Pro restart. Claude can override with
  `unsafe_schema_change=true`, which it should only do after you agree and after a backup
  copy. `run_arcpy` code is not checked.
- **Render timeout.** Snapshots (120 s), layout PDFs (300 s) and zooms (60 s) stop waiting
  if Pro hangs while drawing. Claude gets a clear error, `bridge_status` shows the hung
  command, and other commands are refused until Pro finishes, instead of the bridge
  silently dropping the connection.
- Keep projects and geodatabases out of OneDrive (or pause syncing while working):
  sync can lock file-geodatabase files mid-edit.

## Troubleshooting

- **Smoke test**: with the bridge running, `propy.bat tests\smoke_test_real_pro.py` exercises every
  command in a scratch map and writes `tests\smoke_report.json`.
- **"Service check failed ... error 499"**: the layer URL needs a sign-in or was retired.
  Ask Claude to find a current one with `search_arcgis_online`.

- **"The ArcGIS Pro bridge is not running"**: run the one-line `exec(...); start(...)` in
  Pro's Python window, or Start Bridge from the toolbox.
- **"... did not finish within 120s"** or **"still busy with export_image"**: Pro is stuck
  drawing. Look at Pro: let it finish, or press Esc / pause drawing. If it is frozen,
  restart Pro and the bridge. Hidden layers with broken renderers or huge web services
  are the usual cause.
- **"The connection to ArcGIS Pro dropped"**: Pro probably crashed during that command.
  Reopen the project (if it crashes on open, use standalone mode to remove the bad
  layers and save), then restart the bridge.
- **"Refused: ... is drawn by ..."**: that's the schema guard (see Safety).
- **Tools time out in live mode**: some arcpy calls may not like being run from the
  bridge's background thread. Use standalone mode, and please note which tool hung.
- **Server doesn't appear in Claude Desktop**: check the uv.exe path in the config
  (`where.exe uv` in PowerShell) and look at Settings > Developer for the error log.
- **Portal search shows `signed_in: false`**: sign in to ArcGIS Online in Pro (top-right).
- Long geoprocessing runs: raise the timeout with env var `ARCPRO_MCP_TIMEOUT` (seconds, default 900).

## Status

Version 0.2.0. 0.1.4 was smoke-tested against ArcGIS Pro (Python 3.11) in live mode.
0.2.0 passes 33 automated tests against a simulated arcpy; run
`propy.bat tests\smoke_test_real_pro.py` with the bridge running to check the new
commands in real Pro. Errors come back to Claude with the Python traceback, which makes
them quick to fix. See [CHANGELOG.md](CHANGELOG.md).

Run the tests: `uv run --extra test pytest`.
