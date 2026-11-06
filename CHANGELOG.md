# Changelog

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
