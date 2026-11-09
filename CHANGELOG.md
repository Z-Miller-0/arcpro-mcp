# Changelog

## 0.4.0 (unreleased)

Easier setup, and the security checks reviewed against ArcGIS Pro 3.7.3.

### Security: Pro 3.7.3 review
The list of tools that run Python was built from Pro 3.5. A read-only scan of Pro 3.7.3's 2,198
tools (their scripts, the modules those import, and every parameter) found tools that could run
code with `allow_exec` off. They now need `allow_exec`:
- **Load Data To Preview** runs the same workbook loader, with its Python code block, as Load
  Data Using Workspace, which was already refused.
- **Batch Update Fields** runs Python functions from a `.py` file.
- **Analyze Tools For Pro** loads the Python toolbox or script it analyzes.
- **Generate Raster Collection** and **Generate Table From Raster Function** take raster
  functions, which can be Python, like Generate Raster From Raster Function.
- **Classify Point Cloud Using Trained Model**, **Detect Objects From Point Cloud Using Trained
  Model**, **Evaluate Point Cloud Classification Model**, **Forecast Using Time Series Model** and
  **Predict Using AutoML** load model files, which can carry Python. The name pattern for model
  tools now also catches `...TrainedModel` and `AutoML` tools.

Two broader guards were added:
- **Any tool** is refused when a parameter names a Python or model file (`.py`, `.pyt`, `.pyc`,
  `.pyd`, `.dlpk`, `.emd`) or a `PythonAdapter` raster function, including inside value tables
  and JSON. This covers tools nobody has reviewed, such as the oriented imagery tools'
  `raster_function` parameter.
- **Match Layer Symbology To A Style** takes only a plain field name for `match_values`, because
  its expression language is not documented.

The review found nothing else: the other hits run fixed, internal code, and the expression
parameters of the attribute rule, COGO, parcel and GeoAnalytics tools are Arcade only.

- **`arcpro-mcp install`** sets everything up in one command. It copies the bridge and toolbox
  to `%LOCALAPPDATA%\arcpro-mcp\bridge`, adds the `arcgis-pro` server to Claude Desktop's config
  (after a backup, leaving everything else in the file alone), and prints the toolbox path, the
  one-line start command, and the Claude Code command. `--dry-run` shows what it would do. Run
  from a source checkout, it points Claude at the checkout instead.
- **Packaged for PyPI.** The wheel carries the bridge files, so `uvx arcpro-mcp@0.4.0 install`
  works without cloning the repo. The Claude Desktop entry it writes is pinned to that version,
  so the server and the bridge copy always come from the same release.
- **Server and bridge versions must match.** If the bridge running in Pro has a different
  major.minor version from the server, tool calls stop with a message saying which side to
  restart, and how, instead of failing in confusing ways. `bridge_status` still answers and now
  reports `server_version` (and `version_problem` when they differ). Set
  `ARCPRO_MCP_IGNORE_VERSION=1` to skip the check.
- **"Bridge not running" names the real path.** The message now gives the toolbox location and
  a ready-to-paste start line for the bridge that matches this server.
- A release workflow publishes to PyPI from a `v*` tag, through PyPI trusted publishing.
- Licensed under MIT.
- Fixed a test that failed now and then on Windows, where a dropped connection can show up as a
  reset rather than a close.

## 0.3.0 (2026-11-06)

A security audit of 0.2.0 found that `allow_exec=False` contained nothing: `run_geoprocessing`
could call any function reachable from `arcpy`, including `os.system`. Each item marked
*confirmed* was reproduced against real ArcGIS Pro 3.5 before the fix. **0.1.x and 0.2.0 are
affected: anything that can influence Claude, such as text in a layer attribute or a portal
item, could run commands on your computer if a tool call was approved.**

### Security fixes
- **Only real geoprocessing tools run** *(confirmed: `os.getpid`, reading a local file, and
  Pro's sign-in token through `arcpy.arcpy.GetSigninToken` and `gp.getSigninToken`)*.
  Tools must be in a fixed list of toolboxes and carry arcpy's tool marker.
- **Python-running tools need `allow_exec`** *(confirmed: `CalculateValue`)*. That covers
  CalculateValue, GeoTaggedPhotosToPoints (it evaluates photo metadata), LoadDataUsingWorkspace,
  Raster Calculator, raster function templates and deep learning tools. `CalculateField` and
  `CalculateFields` now need `expression_type` set to `ARCADE` or `SQL`, because their default
  is `PYTHON3`. **This changes behaviour: rewrite Python expressions in Arcade.**
- **The sign-in token only goes to your portal or `*.arcgis.com`, over https**
  *(confirmed: it was sent to any URL)*. Other hosts get an anonymous request. Malformed URLs
  can no longer echo the token in an error *(confirmed)*.
- **Only `http` and `https` to public hosts** *(confirmed: `file:` URLs and localhost)*. Local,
  private and metadata addresses are refused unless they are your portal. Redirects are checked
  at each hop, cannot drop from https to http, and lose credentials when they leave a trusted
  host. Responses are limited to 20 MB and 60 seconds.
- **Network paths are refused** in every path and geoprocessing parameter, because opening one
  can send your Windows credentials to that host.
- **Exclusive port binding** *(confirmed: a second socket could share the bridge's port)*. A
  second bridge now fails with a clear message. This also found a toolbox bug that turned port 0
  into 18760.
- **Socket limits.** One request per connection, 32 MB per request, a 30 second read timeout, 32
  connections. Nothing about the bridge is revealed before the token check *(confirmed: bad JSON
  returned a traceback with local paths)*.
- **Publishing tools need `allow_exec`**, such as SharePackage and UploadFileToPortal.
  `server` and `agolservices` are not available at all without `run_arcpy`.
- **Overrides moved out of Claude's reach.** `unsafe_schema_change` now needs `allow_exec`, and
  refusals no longer spell out how to retry with it.
- **Snapshots return the image in the response**, so the MCP server no longer reads and deletes
  whatever path it is told.
- **Safer files.** The client ignores a host in the state file and always uses loopback, and
  rejects a peer that does not answer like the bridge. The state file is replaced in one step and
  removed on stop.

### Stronger schema guard
- Keyed on the tool's own name, so no alternative spelling can skip it *(confirmed)*.
- **Guards tools by their shape, not a list.** About 2,000 tools are reachable and 941 change
  something in place. Any tool whose parameters show no output of its own now counts as changing
  its input, unless it belongs to a short list of families known to be safe (Create, Export,
  Select, AddJoin and so on). This catches tools like DeleteMultiple, Near and ErasePoint, and any
  new ones Esri adds. **This changes behaviour: some harmless tools may be refused on drawn data.**
- Tools that change their input are refused outside the project (its home folder, default
  geodatabase and memory), and destructive ones on folders, geodatabases and feature datasets, or
  on anything containing drawn data.
- Fails closed: a target that cannot be identified is refused. Web services are refused.
- Sees layers in every map, tables reached through a join, temporary layers, a layer named like a
  dataset, and sources of layers removed since Pro started, since Pro can keep those locked until
  it restarts. Path comparison handles short names like `PROGRA~1`, junctions, mapped drives and
  the trailing dots and spaces Windows ignores.
- `overwrite=true` is only for outputs inside the project that no map draws, including the name a
  tool really writes (`parcels` can become `parcels.shp`).
- Row edits (`insert_rows`, `update_rows`, `delete_rows`, and CalculateField on an existing field)
  stay inside the project, and refuse hosted services, enterprise databases and layer files.
- Where clauses on row edits and selections cannot close the parentheses around them, so they
  cannot escape a layer's definition query.
- Found in real Pro: after a tool uses a dataset, even a failed tool, Pro can keep a schema lock
  on it until it restarts, and a Delete then fails with ERROR 000464. The guard's message now
  says so, because this is exactly the state that left a half-deleted feature class on
  2026-11-05.

### New tools (29)
- **Maps and layers:** `rename_map`, `remove_map`, `set_map_properties` (coordinate system,
  reference scale), `list_basemaps`, `add_basemap`, `zoom_to_extent`, `move_layer`,
  `create_group_layer`, `move_layer_to_group`, `list_broken_sources`.
- **Selection:** `select_by_attribute`, `select_by_location`, `get_selection`,
  `clear_selection`, and `query_layer(selected_only=true)`.
- **Symbology and labels:** `list_color_ramps`, `set_symbology` (simple, unique values, graduated
  colors, graduated symbols, through arcpy's symbology API), `set_labels` (Arcade only).
- **Layouts:** `describe_layout`, `create_layout`, `remove_layout`, `add_layout_text`,
  `add_layout_surround` (legend, north arrow, scale bar), `update_layout_element`,
  `remove_layout_element`, `set_map_frame_extent`, `export_layout` (pdf, png, jpg, tif, svg).
- **Data:** `create_dataset` (all fields at once, the safe alternative to adding fields),
  `update_rows` and `delete_rows` (a where clause and a row limit are required).
- `set_layer_properties` also sets the visible scale range and whether labels show.

### Other changes
- Limits on snapshots (40 megapixels), timeouts (1 to 3600 s), `top`, `limit` and `order_by`.
- `query_layer` stops at 16 MB; `field_summary` holds at most 100,000 distinct values; NaN and
  Infinity come back as null.
- A command waits at most 30 s for the one before it, then reports that it did not run, instead
  of running later after Claude gave up. Time spent waiting no longer counts as a hung render.
- `insert_rows` checks every row first, and reports how many rows were written if one fails.
- `save_project(copy_to=...)` will not replace an existing file. Exports take `overwrite=true`,
  which only works for a file inside the project that no map draws. `export_layout_pdf` requires
  `.pdf`. `save_project` removes any leftover snapshot layout before saving.
- Layout pages are limited to 200 inches a side, and raster exports to 400 megapixels. Numbers
  given to layout, map and symbology tools must be finite and in a sensible range.
- `query_layer` and `field_summary` say when a layer's selection limited what they read.
- Tool annotations now say which tools reach outside the project and which destroy data, and the
  server tells Claude to treat text from data as data, not instructions.
- The smoke test requires `--project`, and stops unless the bridge is that project on this
  version. Its attack cases use only `.invalid` hosts and files it creates, and it never prints
  a tool's output when a refusal fails.
- `uv.lock` is committed; `anyio` and `hatchling` have upper bounds.

## 0.2.0 (2026-11-06)

From the backlog collected while building the CM Magnet Stack map, where Pro hung and
crashed twice.

### Stability
- **Render timeout.** `snapshot_map` (120 s), `export_layout_pdf` (300 s) and
  `zoom_to_layer` (60 s) run on a worker thread. If Pro hangs, Claude gets a clear error
  instead of a dropped connection, `bridge_status` reports the hung command (it no longer
  needs arcpy, so it answers while Pro is busy), and other commands are refused until Pro
  finishes. `snapshot_map` and `export_layout_pdf` take a `timeout` argument.
- **Schema guard.** `run_geoprocessing` refuses Delete, Rename, AddField(s), DeleteField,
  AlterField, JoinField, CalculateField on a new field, AddGeometryAttributes,
  CalculateGeometryAttributes, TruncateTable and the index/GlobalID tools when a map in the
  project draws the target dataset or `TestSchemaLock` says something else holds a lock.
  Override with `unsafe_schema_change=true`. Lock errors (000317, 000464, 000496) now say
  not to retry. The `TestSchemaLock` check is skipped for `memory\` datasets, because it
  returns False for all of them in Pro 3.5; the check for layers drawing them still applies.
- **Clearer crash message.** If the connection drops mid-command, the client says Pro may
  have crashed during that command, instead of "bridge is not running".
- **Easier startup.** One-line start command (`exec(...); start(allow_exec=...)`) in the
  docs and error messages, and a new `bridge/ArcProMCP.pyt` toolbox with Start Bridge
  (with an "Allow run_arcpy" checkbox), Stop Bridge and Bridge Status.

### New tools
- `list_workspace`: feature classes, tables and rasters in a geodatabase (including feature
  datasets), with counts, shape types and which map layers use each one.
- `describe_url`: inspect a service layer or portal item without adding it to a map:
  owner, title, fields, real feature count, declared extent and actual data extent (WGS84).
  A service root lists its layers.
- `insert_rows`: append rows to a table or feature class (case-insensitive field names,
  ISO dates, `SHAPE@XY` / `SHAPE@WKT` geometry, up to 10,000 rows per call).
- `query_layer`, `describe_layer` and `field_summary` now read standalone tables in a map,
  and datasets in the default geodatabase (or any path) that are not in a map at all.
  `list_layers` lists standalone tables; `remove_layer` removes them.

### Fixes
- `save_project(copy_to=...)` creates the folder if needed (and requires a `.aprx` path).
  `export_layout_pdf` does the same for its output folder.
- `create_map(include_basemap=false)` gives a map with no layers.
- `snapshot_map(map=...)` framing: frames on the map's *visible* data only (hidden layers,
  layers in hidden groups and basemaps are ignored), projects every layer extent into the
  map's coordinate system before combining them (mixing degrees and meters was the likely
  cause of the zoomed-out, offset images), and adds a 3% margin. Naming a map no longer
  inherits the open view's extent unless `use_view_extent=true`. New `layer` argument
  frames on one layer. The tool now also returns a line of text saying what it framed on.

### Not covered
- `run_arcpy` code is not checked by the schema guard.
- The guard does not check outputs written with `overwrite=true`.
- The toolbox and the render timeout are tested against a simulated arcpy only; run the
  updated `tests/smoke_test_real_pro.py` in Pro to confirm.
