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

## Quick start (Windows, about 2 minutes)

**1. Install uv** (it runs the server and fetches the right Python and packages). In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Quit Claude Desktop** (tray icon > Quit), open a **new** PowerShell window, and run:

```powershell
uvx arcpro-mcp@latest install
```

This copies the bridge to `%LOCALAPPDATA%\arcpro-mcp\bridge`, adds the `arcgis-pro` server to
Claude Desktop's config (it backs the file up first and changes nothing else in it), and prints
the exact paths for the next step. Add `--dry-run` to see what it would do first. Using Claude
Code instead? Add `--no-claude-desktop` and run the `claude mcp add ...` line it prints.

**3. Add the toolbox in ArcGIS Pro, once.** In the **Catalog** pane, right-click **Toolboxes >
Add Toolbox** and pick the `ArcProMCP.pyt` that step 2 printed. Right-click it > **Add To
Favorites** so every project has it.

**4. Each time you open Pro**, run **Start Bridge** from that toolbox. You should see
`arcpro-mcp bridge 0.4.0 listening on 127.0.0.1:18760`. Leave *Trusted mode* unticked unless a
specific job needs it: see **Security** below.

**5. Open Claude Desktop** and ask: "Check the ArcGIS Pro connection and tell me what's in my
project."

**To update:** run `uvx arcpro-mcp@latest install` again, then restart Claude Desktop and run
Start Bridge again. The server and the bridge must be the same version. If they are not,
Claude says which one to restart instead of failing in odd ways.

## More setup details

### Starting the bridge without the toolbox

Paste this into **View > Python window** as **one line** (the Python window mangles
multi-line pastes). `arcpro-mcp install` prints it with your real path:

```python
exec(open(r"C:\Users\<you>\AppData\Local\arcpro-mcp\bridge\arcpro_bridge.py").read()); start(allow_exec=False)
```

**Stop Bridge** and **Bridge Status** are in the toolbox too. Keep the .pyt next to
`arcpro_bridge.py`.

Only one bridge can use a port. If you see "Port 18760 is already in use", another Pro window is
running the bridge: stop it there, or pass `port=18761` to `start()`.

### Setting up by hand

`arcpro-mcp install` writes this into Claude Desktop's config (Settings > Developer > Edit
Config). Paths must be absolute, because Claude Desktop does not use your PATH:

```json
{
  "mcpServers": {
    "arcgis-pro": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uvx.exe",
      "args": ["arcpro-mcp@0.4.0"]
    }
  }
}
```

Claude Code: `claude mcp add --scope user arcgis-pro -- uvx arcpro-mcp@0.4.0`

### Running from a source checkout

Clone the repo somewhere permanent, then run `uv run arcpro-mcp install` inside it. From a
checkout, the installer points Claude at the checkout (`uv --directory <repo> run --frozen
arcpro-mcp`, with exactly the versions in `uv.lock`) and uses its `bridge` folder in place, so
your edits are what runs.

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
| `set_layer_properties`, `zoom_to_layer`, `zoom_to_extent`, `create_map`, `remove_layer` | Change the map: visibility, filter, transparency, name, scale range, labels on or off. `create_map(include_basemap=false)` gives a map with no layers |
| `rename_map`, `remove_map`, `set_map_properties`, `list_basemaps`, `add_basemap` | Manage maps: coordinate system (e.g. WKID 54012, World Eckert IV), reference scale, basemap |
| `move_layer`, `create_group_layer`, `move_layer_to_group`, `list_broken_sources` | Drawing order, groups, and finding layers with a missing data source |
| `select_by_attribute`, `select_by_location`, `get_selection`, `clear_selection` | Selections. Geoprocessing tools act on them, and `query_layer(selected_only=true)` reads them |
| `list_color_ramps`, `set_symbology` | Simple, unique value, graduated color and graduated symbol styling, through Pro's own symbology |
| `set_labels` | Labels on or off, with Arcade expressions |
| `describe_layout`, `create_layout`, `remove_layout` | Layout pages and their map frames |
| `add_layout_text`, `add_layout_surround`, `update_layout_element`, `remove_layout_element`, `set_map_frame_extent` | Titles, legends, north arrows, scale bars; move and resize; frame the map |
| `export_layout`, `export_layout_pdf`, `save_project` | Output (pdf, png, jpg, tif, svg) and save. Project changes reach the .aprx only at `save_project` |
| `create_dataset` | A new table or feature class with all its fields at once |
| `insert_rows`, `update_rows`, `delete_rows` | Edit rows. Updates and deletes need a where clause and stop at a row limit |
| `tool_help`, `run_geoprocessing` | Look up and run a GP tool (`analysis.Buffer`, `analysis.SpatialJoin`, ...) |
| `run_arcpy` | Arbitrary Python in Pro. **Off by default**, see Security |

Project changes are not saved until `save_project`, but **geoprocessing and row edits change data
on disk at once, and arcpy has no undo.**

## Standalone mode (Pro closed)

```powershell
& "C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat" C:\Users\<you>\arcpro-mcp\bridge\arcpro_bridge.py --project "C:\GIS\MyProject.aprx" --allow-exec
```

(The leading `&` is needed in PowerShell. `--allow-exec` turns on trusted mode, see Security:
drop it unless you need it.)

Works on a saved .aprx without the Pro window. `zoom_to_layer` and active-view
snapshots need live mode (snapshots fall back to the first layout). Don't have the
same .aprx open in Pro at the same time.

## Security

Claude reads text it did not write: layer attributes, portal item titles, service descriptions,
file names. Someone could put instructions in that text, hoping Claude follows them. The tools
are built so that even a fooled Claude can do limited damage. This reduces the risk; it does not
remove it.

**Keep tool approval on.** Claude Desktop asks before each tool call. Do not choose "always allow"
for `run_geoprocessing`, `insert_rows`, `update_rows`, `delete_rows`, `save_project` or `run_arcpy`.
If a request looks odd, deny it.

**With `allow_exec` off (the default), the bridge refuses:**

- any function that is not a real geoprocessing tool, and the `server`, `agolservices`, `interop`
  toolboxes
- tools that run Python or load code: CalculateValue, CalculateField and CalculateFields unless
  they use Arcade or SQL, Raster Calculator, raster function tools, Load Data Using Workspace and
  Load Data To Preview, Batch Update Fields, deep learning and other trained-model tools, and
  others. The list was checked against Pro 3.5 and 3.7.3. New Pro versions can add more, so it
  needs reviewing on upgrades.
- any tool parameter that names a Python or model file (`.py`, `.pyt`, `.dlpk`, `.emd`, ...) or a
  Python raster function, whatever the tool, so unreviewed tools cannot be handed code that way
- expressions in Match Layer Symbology To A Style: it takes a plain field name only
- tools that publish or upload to your portal
- network paths anywhere in a parameter, because opening one can send your Windows credentials to
  that host
- web requests to local or private addresses, except your signed-in portal
- edits to hosted services and enterprise databases, and through layer files
- any tool that changes its input, or a row edit, outside the project (its home folder, default
  geodatabase and memory)
- deletes on a folder, geodatabase or feature dataset
- replacing a file outside the project with `overwrite=true`
- schema changes and deletes on anything a map draws (see the guard below)

**Pro's sign-in token** is only sent to your portal and to `*.arcgis.com`, over https, and never
returned to Claude.

**`allow_exec=True` is trusted mode.** It turns on `run_arcpy` and removes the refusals above, except
that `run_geoprocessing` still will not change drawn data unless Claude also passes
`unsafe_schema_change=true`. A fooled Claude could then do anything your account can. Use it only
for a specific job, on data you trust, and restart the bridge without it afterwards. Only you can
turn it on, inside Pro, so if a tool message or a document tells you to, treat that as suspicious.

**What these rules do not stop, even with `allow_exec` off:**

- **Reading local data.** Claude can read tables, CSVs and layers anywhere on your computer, and
  then send text to a public web address in a `describe_url` request. Approval prompts are what
  catch this, so read the URL before approving a request for an unfamiliar site.
- **Writing new files.** Geoprocessing outputs and exports can create new files anywhere on a
  local drive. They cannot replace existing ones outside the project.
- **Editing rows inside the project**, including datasets a map draws. Updates and deletes need a
  where clause and stop at a row limit, but Claude sets that limit, up to 10,000 rows per call.
  Back up data you care about.
- **`.lyrx` files from strangers.** A layer file can carry a Python label expression that runs when
  Pro draws it. Only add layer files you trust. The same goes for opening a stranger's project.
- **Mapped network drives**, such as `Z:\`, which look like local paths.
- **DNS tricks.** A public hostname is checked when the request starts, so a host that changes its
  address in the moment before connecting could still reach a local address. The sign-in token
  never goes to such a host.
- **What Pro does with a service URL itself.** `add_layer` hands the URL to Pro, which connects by
  its own rules after the checks here.
- **Other programs running as you.** They can read the state file, which holds the bridge token.
- **Tools nobody has reviewed.** Esri ships about 2,000 tools. The ones known to run Python are
  listed from a search of Esri's scripts, but others might exist, and a tool with its own output
  is not guarded on its input.

The bridge listens on 127.0.0.1 only and requires a random token, regenerated at every start and
stored in `%USERPROFILE%\.arcpro_mcp\bridge.json`.

## Safety

- Work on a copy of a project the first few times (`save_project` with `copy_to`).
- **Schema guard.** `run_geoprocessing` refuses tools that change a dataset's schema or destroy its
  data (Delete, Rename, AddField(s), DeleteField, AlterField, JoinField, CalculateField on a new
  field, DeleteRows, TruncateTable, DefineProjection and more) when any map in the project draws
  that dataset or reads it through a join, when a layer using it was removed since Pro started, or
  when it has a schema lock. Doing that can hang or crash Pro, and a Delete that hits a lock can
  leave a half-deleted feature class. Build a new dataset with `create_dataset` instead, repoint the
  layers, and delete the old one after a Pro restart. `unsafe_schema_change=true` skips the guard
  only in trusted mode, after you agree and after a backup copy. `run_arcpy` code is not checked.
- **Pro can hold a schema lock on a dataset after a tool has used it**, even a tool that failed,
  until Pro restarts. So a refusal that mentions a lock may just need a Pro restart.
- **Render timeout.** Snapshots (120 s), layout PDFs (300 s) and zooms (60 s) stop waiting
  if Pro hangs while drawing. Claude gets a clear error, `bridge_status` shows the hung
  command, and other commands are refused until Pro finishes, instead of the bridge
  silently dropping the connection.
- Keep projects and geodatabases out of OneDrive (or pause syncing while working):
  sync can lock file-geodatabase files mid-edit.

## Troubleshooting

- **Smoke test**: open a scratch project, start the bridge in it, then run
  `propy.bat tests\smoke_test_real_pro.py --project "C:\path\to\scratch.aprx"`. It exercises every
  command, including attack cases that are refused, and writes `tests\smoke_report.json`. It leaves
  `smoke_*` datasets in that project's geodatabase, so never point it at real work. It stops unless
  the path matches the bridge's project.
- **"Refused: ... runs Python unless expression_type is ARCADE or SQL"**: rewrite the expression in
  Arcade, e.g. `$feature.TONS * 2`, and pass `expression_type="ARCADE"`.
- **"Service check failed ... error 499"**: the layer URL needs a sign-in or was retired.
  Ask Claude to find a current one with `search_arcgis_online`.

- **"The ArcGIS Pro bridge is not running"**: run Start Bridge from the toolbox, or the
  one-line `exec(...); start(...)` in Pro's Python window. The message gives both paths.
- **"The bridge running in ArcGIS Pro is 0.x, but this arcpro-mcp server is 0.y"**: one side
  was updated and the other was not. If the bridge is older, run Stop Bridge then Start Bridge
  (from the toolbox `arcpro-mcp install` printed). If it is newer, fully quit and reopen Claude.
  `ARCPRO_MCP_IGNORE_VERSION=1` skips the check, at your own risk.
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
- **Server doesn't appear in Claude Desktop**: make sure Claude Desktop was fully quit, then run
  `uvx arcpro-mcp@latest install` again (it fixes a wrong uv path). Settings > Developer shows the
  server's error log.
- **Portal search shows `signed_in: false`**: sign in to ArcGIS Online in Pro (top-right).
- Long geoprocessing runs: raise the timeout with env var `ARCPRO_MCP_TIMEOUT` (seconds, default 900).

## Status

Version 0.4.0. The automated tests run against a simulated arcpy, and the smoke test against real
ArcGIS Pro 3.5 and 3.7.3 (all 277 checks pass on 3.7.3). Errors come back to Claude with the Python traceback, which makes
them quick to fix. See [CHANGELOG.md](CHANGELOG.md).

Run the tests: `uv run --extra test pytest`.

### Releasing

1. Set the version in `src/arcpro_mcp/__init__.py` and `BRIDGE_VERSION` in
   `bridge/arcpro_bridge.py` (a test checks they match, and that CHANGELOG.md has the entry).
2. Merge to `main`, then push a tag: `git tag v0.4.0 && git push origin v0.4.0`.
3. The `release` workflow tests, builds, checks the wheel carries the bridge, and publishes to
   PyPI through trusted publishing (no token stored in GitHub).

## License

MIT. See [LICENSE](LICENSE). ArcGIS, ArcGIS Pro and arcpy are products of Esri; this project is not affiliated with or endorsed by Esri.
