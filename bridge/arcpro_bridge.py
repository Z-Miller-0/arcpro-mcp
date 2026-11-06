"""ArcPro MCP bridge: exposes ArcGIS Pro (arcpy) to the arcpro-mcp server over localhost.

Three ways to run it:

1. LIVE mode (recommended). Inside ArcGIS Pro, open View > Python window and paste this
   ONE line (one line matters: the Python window mangles multi-line pastes):

       exec(open(r"C:\\path\\to\\arcpro-mcp\\bridge\\arcpro_bridge.py").read()); start(allow_exec=False)

   Use allow_exec=True to also turn on the run_arcpy tool. Claude then acts on the
   project you have open ("CURRENT"): layers it adds show up in your map immediately.
   Re-running the line restarts the bridge.

2. TOOLBOX (live mode without the Python window). In Pro's Catalog pane, add
   bridge\\ArcProMCP.pyt (right-click Toolboxes > Add Toolbox, or add it to Favorites
   so every project has it), then run "Start Bridge". See the README.

3. STANDALONE mode (Pro does not need to be open, but must be installed and licensed):

       & "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\Scripts\\propy.bat" arcpro_bridge.py --project "C:\\GIS\\MyProject.aprx"

   Changes are written to the .aprx only when Claude calls save_project.
   Do not have the same .aprx open in Pro while using standalone mode.

Only the Python standard library and arcpy are used, so nothing has to be
installed into ArcGIS Pro's Python environment. Keep this file compatible with
Python 3.9 (older Pro releases).
"""

import builtins
import contextlib
import datetime
import io
import json
import os
import posixpath
import re
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import arcpy

BRIDGE_VERSION = "0.2.0"
DEFAULT_PORT = 18760
STATE_DIR = os.path.join(os.path.expanduser("~"), ".arcpro_mcp")
STATE_FILE = os.path.join(STATE_DIR, "bridge.json")
MAX_ROWS = 2000
MAX_INSERT_ROWS = 10000

# Commands that draw the map. A hung render used to block the bridge for good (and the
# caller saw a dropped connection). Now each runs on a worker thread; past this many
# seconds the caller gets a clear error and the bridge reports itself busy until Pro
# finishes. Callers can pass their own "timeout" for these commands.
RENDER_TIMEOUTS = {"export_image": 120, "export_layout_pdf": 300, "zoom_to_layer": 60}

# Commands that never touch arcpy, so they answer even while Pro is busy or hung.
LOCK_FREE = {"ping"}

# Geoprocessing tools that change a dataset's schema or delete/rename it. Running these on
# a dataset that a map in the open project is drawing can hang or crash Pro (it happened
# on 2026-11-05 with AddFields and a Delete that hit a schema lock). Keyed by
# "<tool>_<toolbox>" in lower case; the value says what the tool does to its first input.
SCHEMA_TOOLS = {
    "delete_management": "deletes",
    "rename_management": "renames",
    "addfield_management": "adds a field to",
    "addfields_management": "adds fields to",
    "deletefield_management": "deletes fields from",
    "alterfield_management": "alters a field of",
    "joinfield_management": "adds joined fields to",
    "addgeometryattributes_management": "adds geometry fields to",
    "calculategeometryattributes_management": "may add fields to",
    "calculatefield_management": "adds a new field to",  # only refused if the field is new
    "addindex_management": "adds an index to",
    "removeindex_management": "removes an index from",
    "addglobalids_management": "adds a GlobalID field to",
    "truncatetable_management": "truncates",
}
TARGET_KWARGS = ("in_data", "in_table", "in_features", "Input_Features", "in_datasets",
                 "in_dataset")


class BridgeError(Exception):
    """An error with a message meant for Claude (no traceback needed)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(obj):
    """Make arcpy values JSON friendly."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_safe(o) for o in obj]
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return str(obj)


def _supports(lyr, prop):
    try:
        return bool(lyr.supports(prop))
    except Exception:
        return False


def _extent_dict(ext):
    if ext is None:
        return None
    try:
        sr = ext.spatialReference
        return {
            "xmin": ext.XMin, "ymin": ext.YMin, "xmax": ext.XMax, "ymax": ext.YMax,
            "spatial_reference": getattr(sr, "name", None) if sr else None,
        }
    except Exception:
        return str(ext)


def _norm(path):
    """Compare dataset paths regardless of slash direction and case (Windows paths)."""
    p = str(path).strip().replace("\\", "/")
    unc = p.startswith("//")
    p = posixpath.normpath(p)
    if unc and not p.startswith("//"):
        p = "/" + p
    return p.rstrip("/").lower()


def _is_memory(value):
    return str(value).lower().startswith(("memory\\", "memory/", "in_memory\\", "in_memory/"))


def _is_path(value):
    return bool(re.match(r"^([A-Za-z]:[\\/]|\\\\|/)", str(value))) or _is_memory(value)


def _join(base, *names):
    sep = "\\" if "\\" in str(base) else "/"
    return sep.join([str(base).rstrip("\\/")] + [str(n).strip("\\/") for n in names])


def _flatten(value):
    """Datasets may be given as a list or as one ';'-separated string."""
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_flatten(v))
        return out
    if isinstance(value, str):
        return [v.strip().strip("'\"") for v in value.split(";") if v.strip()]
    return [value]


def _sr_code(sr):
    for attr in ("factoryCode", "PCSCode", "GCSCode"):
        try:
            v = getattr(sr, attr, None)
            if v:
                return int(v)
        except Exception:
            pass
    return getattr(sr, "name", None)


class Bridge(object):
    def __init__(self, project="CURRENT", allow_exec=False):
        self.project_arg = project
        self.live = project.upper() == "CURRENT"
        self.allow_exec = allow_exec
        self.lock = threading.RLock()  # arcpy is not thread safe
        self.aprx = arcpy.mp.ArcGISProject(project)
        # Cached so ping can answer without touching arcpy while Pro is busy.
        self.project_path = self.aprx.filePath
        try:
            self.pro_version = arcpy.GetInstallInfo().get("Version")
        except Exception:
            self.pro_version = None
        self.running = None   # {"cmd", "since"} while a command holds the lock
        self.stuck = None     # {"cmd", "since", "limit", "thread"} after a render timeout
        self.commands = {
            "ping": self.ping,
            "project_info": self.project_info,
            "list_layers": self.list_layers,
            "list_workspace": self.list_workspace,
            "describe_layer": self.describe_layer,
            "describe_url": self.describe_url,
            "query_layer": self.query_layer,
            "field_summary": self.field_summary,
            "insert_rows": self.insert_rows,
            "add_layer": self.add_layer,
            "remove_layer": self.remove_layer,
            "set_layer_properties": self.set_layer_properties,
            "zoom_to_layer": self.zoom_to_layer,
            "create_map": self.create_map,
            "tool_help": self.tool_help,
            "run_geoprocessing": self.run_geoprocessing,
            "export_image": self.export_image,
            "export_layout_pdf": self.export_layout_pdf,
            "save_project": self.save_project,
            "search_portal": self.search_portal,
            "run_arcpy": self.run_arcpy,
        }

    # -- lookups ------------------------------------------------------------

    def _map(self, name=None):
        maps = self.aprx.listMaps()
        if not maps:
            raise BridgeError("This project has no maps. Use create_map first.")
        if name:
            for m in maps:
                if m.name.lower() == name.lower():
                    return m
            raise BridgeError("No map named %r. Maps: %s" % (name, [m.name for m in maps]))
        if self.live:
            try:
                active = self.aprx.activeMap
                if active is not None:
                    return active
            except Exception:
                pass
        return maps[0]

    @staticmethod
    def _tables(m):
        try:
            return list(m.listTables())
        except Exception:
            return []

    def _layer(self, map_name, layer_name):
        m = self._map(map_name)
        layers = m.listLayers()
        for attr in ("longName", "name"):
            for lyr in layers:
                if str(getattr(lyr, attr, "")).lower() == layer_name.lower():
                    return m, lyr
        raise BridgeError(
            "No layer named %r in map %r. Layers: %s"
            % (layer_name, m.name, [l.longName for l in layers])
        )

    def _dataset_path(self, name):
        """Resolve a dataset name or path to an existing path, or None."""
        candidates = []
        if _is_path(name):
            candidates.append(name)
        else:
            for base in (getattr(arcpy.env, "workspace", None),
                         getattr(self.aprx, "defaultGeodatabase", None)):
                if base:
                    candidates.append(_join(base, name))
        for c in candidates:
            try:
                if arcpy.Exists(c):
                    return c
            except Exception:
                pass
        return None

    def _find(self, map_name, name):
        """A layer or standalone table in a map, else a dataset path (gdb tables and
        feature classes that are not in any map). Returns (map or None, obj, label)."""
        m = None
        layers, tables = [], []
        if self.aprx.listMaps():
            m = self._map(map_name)
            layers, tables = m.listLayers(), self._tables(m)
        for attr in ("longName", "name"):
            for obj in list(layers) + list(tables):
                if str(getattr(obj, attr, "")).lower() == name.lower():
                    return m, obj, getattr(obj, "longName", obj.name)
        path = self._dataset_path(name)
        if path:
            return None, path, path
        raise BridgeError(
            "No layer or table named %r in map %r, and no dataset %r in the project's "
            "geodatabase. Layers: %s. Tables: %s. Use list_workspace to see what is in "
            "the geodatabase, or give a full path."
            % (name, m.name if m else None, name, [l.longName for l in layers],
               [getattr(t, "longName", t.name) for t in tables]))

    def _layout(self, name=None):
        layouts = self.aprx.listLayouts()
        if not layouts:
            raise BridgeError("This project has no layouts.")
        if name:
            for lyt in layouts:
                if lyt.name.lower() == name.lower():
                    return lyt
            raise BridgeError("No layout named %r. Layouts: %s" % (name, [l.name for l in layouts]))
        return layouts[0]

    def _dataset_users(self):
        """{normalized dataset path: ["Map: layer", ...]} for every map in the project."""
        users = {}
        for m in self.aprx.listMaps():
            for obj in list(m.listLayers()) + self._tables(m):
                if hasattr(obj, "isFeatureLayer") and not _supports(obj, "DATASOURCE"):
                    continue
                try:
                    src = obj.dataSource
                except Exception:
                    continue
                if not src:
                    continue
                label = "%s: %s" % (m.name, getattr(obj, "longName", obj.name))
                users.setdefault(_norm(src), []).append(label)
        return users

    # arcpy.ListFields / GetCount / Describe on a layer object resolve it by name in Pro's
    # *active* map, so they fail for layers in other maps. Fall back to the data source.
    @staticmethod
    def _fields(lyr):
        if isinstance(lyr, str):
            return arcpy.ListFields(lyr)
        try:
            return arcpy.ListFields(lyr)
        except (OSError, RuntimeError, IOError):
            return arcpy.ListFields(lyr.dataSource)

    @staticmethod
    def _count(lyr):
        try:
            return int(arcpy.management.GetCount(lyr)[0])
        except Exception:
            if isinstance(lyr, str):
                raise
            n = 0
            with arcpy.da.SearchCursor(lyr, ["OID@"]) as cur:  # honors the definition query
                for _ in cur:
                    n += 1
            return n

    @staticmethod
    def _describe(lyr):
        if isinstance(lyr, str):
            return arcpy.Describe(lyr)
        try:
            return arcpy.Describe(lyr)
        except (OSError, RuntimeError, IOError):
            return arcpy.Describe(lyr.dataSource)

    @staticmethod
    def _search(obj, fields, where=None, sql_clause=None):
        """SearchCursor on a layer/table, falling back to its data source plus definition
        query when Pro cannot resolve the object (it is not in the active map)."""
        try:
            return arcpy.da.SearchCursor(obj, fields, where_clause=where, sql_clause=sql_clause)
        except (OSError, RuntimeError, IOError):
            if isinstance(obj, str):
                raise
            dq = getattr(obj, "definitionQuery", None)
            both = " AND ".join("(%s)" % w for w in (dq, where) if w) or None
            return arcpy.da.SearchCursor(obj.dataSource, fields, where_clause=both,
                                         sql_clause=sql_clause)

    @staticmethod
    def _layer_summary(lyr):
        info = {
            "name": lyr.name,
            "long_name": getattr(lyr, "longName", lyr.name),
            "visible": getattr(lyr, "visible", None),
            "is_group": bool(getattr(lyr, "isGroupLayer", False)),
            "is_feature_layer": bool(getattr(lyr, "isFeatureLayer", False)),
            "is_raster_layer": bool(getattr(lyr, "isRasterLayer", False)),
            "is_basemap": bool(getattr(lyr, "isBasemapLayer", False)),
        }
        if _supports(lyr, "DATASOURCE"):
            info["data_source"] = lyr.dataSource
        if _supports(lyr, "DEFINITIONQUERY"):
            info["definition_query"] = lyr.definitionQuery or None
        return info

    @staticmethod
    def _table_summary(tbl):
        info = {"name": tbl.name, "long_name": getattr(tbl, "longName", tbl.name),
                "is_table": True}
        for key, attr in (("data_source", "dataSource"), ("definition_query", "definitionQuery")):
            try:
                info[key] = getattr(tbl, attr) or None
            except Exception:
                pass
        return info

    # -- read commands -----------------------------------------------------------

    def ping(self):
        out = {
            "bridge_version": BRIDGE_VERSION,
            "mode": "live" if self.live else "standalone",
            "project": self.project_path,
            "arcgis_pro_version": self.pro_version,
            "run_arcpy_enabled": self.allow_exec,
            "busy": None,
        }
        run, stuck = self.running, self.stuck
        if run:
            out["busy"] = {"command": run["cmd"], "seconds": round(time.time() - run["since"], 1)}
        if stuck and stuck["thread"].is_alive():
            out["busy"] = {"command": stuck["cmd"],
                           "seconds": round(time.time() - stuck["since"], 1),
                           "timed_out_after": stuck["limit"],
                           "note": "Pro looks hung on this command. Other commands are "
                                   "refused until it finishes."}
        return out

    def project_info(self):
        active = None
        if self.live:
            try:
                active = self.aprx.activeMap.name if self.aprx.activeMap else None
            except Exception:
                active = None
        return {
            "project": self.aprx.filePath,
            "home_folder": getattr(self.aprx, "homeFolder", None),
            "default_geodatabase": getattr(self.aprx, "defaultGeodatabase", None),
            "active_map": active,
            "maps": [
                {"name": m.name, "layer_count": len(m.listLayers()),
                 "table_count": len(self._tables(m))} for m in self.aprx.listMaps()
            ],
            "layouts": [l.name for l in self.aprx.listLayouts()],
        }

    def list_layers(self, map=None):
        m = self._map(map)
        out = {"map": m.name, "layers": [self._layer_summary(l) for l in m.listLayers()]}
        tables = self._tables(m)
        if tables:
            out["tables"] = [self._table_summary(t) for t in tables]
        return out

    def list_workspace(self, workspace=None, include_counts=True):
        ws = workspace or getattr(self.aprx, "defaultGeodatabase", None)
        if not ws:
            raise BridgeError("No workspace given and the project has no default geodatabase.")
        if not arcpy.Exists(ws):
            raise BridgeError("Workspace %r does not exist." % ws)
        users = self._dataset_users()
        items = []

        def add(name, kind, path, dataset=None):
            rec = {"name": name, "type": kind, "path": path}
            if dataset:
                rec["feature_dataset"] = dataset
            if kind == "FeatureClass":
                try:
                    rec["shape_type"] = arcpy.Describe(path).shapeType
                except Exception:
                    pass
            if include_counts and kind in ("FeatureClass", "Table"):
                try:
                    rec["count"] = int(arcpy.management.GetCount(path)[0])
                except Exception as e:
                    rec["count_error"] = str(e)[:200]
            used = users.get(_norm(path))
            if used:
                rec["used_by_layers"] = used
            items.append(rec)

        prev = getattr(arcpy.env, "workspace", None)
        try:
            arcpy.env.workspace = ws
            for fc in arcpy.ListFeatureClasses() or []:
                add(fc, "FeatureClass", _join(ws, fc))
            for t in arcpy.ListTables() or []:
                add(t, "Table", _join(ws, t))
            for r in arcpy.ListRasters() or []:
                add(r, "Raster", _join(ws, r))
            for ds in arcpy.ListDatasets(feature_type="Feature") or []:
                for fc in arcpy.ListFeatureClasses(feature_dataset=ds) or []:
                    add(fc, "FeatureClass", _join(ws, ds, fc), dataset=ds)
        finally:
            arcpy.env.workspace = prev
        return {"workspace": ws, "count": len(items), "items": items}

    def describe_layer(self, layer, map=None):
        m, obj, label = self._find(map, layer)
        if isinstance(obj, str):
            out = {"name": label, "data_source": obj, "map": None, "in_map": False}
            d = self._describe(obj)
            kind = getattr(d, "datasetType", None)
            out["dataset_type"] = kind
            is_features = kind == "FeatureClass"
            is_table = kind == "Table" or is_features
        elif not hasattr(obj, "isFeatureLayer"):  # standalone table in a map
            out = self._table_summary(obj)
            out["map"] = m.name
            d, is_features, is_table = None, False, True
            out["dataset_type"] = "Table"
        else:
            out = self._layer_summary(obj)
            out["map"] = m.name
            d = None
            is_features = obj.isFeatureLayer
            is_table = obj.isFeatureLayer
            if obj.isFeatureLayer or obj.isRasterLayer:
                try:
                    d = self._describe(obj)
                    out["dataset_type"] = getattr(d, "datasetType", None)
                except Exception as e:
                    out["describe_error"] = str(e)
        if is_features and d is not None:
            out["shape_type"] = getattr(d, "shapeType", None)
            sr = getattr(d, "spatialReference", None)
            out["spatial_reference"] = getattr(sr, "name", None) if sr else None
            out["extent"] = _extent_dict(getattr(d, "extent", None))
        elif d is not None and getattr(d, "datasetType", None) not in ("Table",):
            sr = getattr(d, "spatialReference", None)
            if sr:
                out["spatial_reference"] = getattr(sr, "name", None)
                out["extent"] = _extent_dict(getattr(d, "extent", None))
        if is_table:
            out["feature_count" if is_features else "row_count"] = self._count(obj)
            out["fields"] = [
                {"name": f.name, "alias": f.aliasName, "type": f.type, "length": f.length}
                for f in self._fields(obj)
            ]
        return out

    def query_layer(self, layer, map=None, where=None, fields=None, limit=50,
                    include_geometry=False, order_by=None):
        m, obj, label = self._find(map, layer)
        if hasattr(obj, "isFeatureLayer") and not obj.isFeatureLayer:
            raise BridgeError("%r is not a feature layer or table." % layer)
        limit = max(1, min(int(limit or 50), MAX_ROWS))
        all_fields = self._fields(obj)
        if not fields or fields == ["*"]:
            fields = [f.name for f in all_fields if f.type not in ("Geometry", "Blob", "Raster")]
        fields = list(fields)
        has_shape = any(f.type == "Geometry" for f in all_fields)
        if include_geometry and not has_shape:
            include_geometry = False
        cursor_fields = fields + (["SHAPE@XY"] if include_geometry else [])
        sql = (None, "ORDER BY %s" % order_by) if order_by else None
        rows = []
        truncated = False
        with self._search(obj, cursor_fields, where, sql) as cur:
            for row in cur:
                if len(rows) >= limit:
                    truncated = True
                    break
                rec = dict(zip(fields, [_safe(v) for v in row[: len(fields)]]))
                if include_geometry:
                    rec["_centroid_xy"] = _safe(row[-1])
                rows.append(rec)
        return {"layer": label, "where": where, "row_count": len(rows),
                "truncated": truncated, "rows": rows}

    def field_summary(self, layer, field, map=None, where=None, top=25):
        m, obj, label = self._find(map, layer)
        counts = {}
        nums = []
        n = 0
        with self._search(obj, [field], where) as cur:
            for (v,) in cur:
                n += 1
                counts[v] = counts.get(v, 0) + 1
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nums.append(v)
        top_vals = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: int(top)]
        out = {"layer": label, "field": field, "rows": n,
               "distinct_values": len(counts),
               "top_values": [{"value": _safe(k), "count": c} for k, c in top_vals]}
        if nums:
            out["numeric"] = {"min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums),
                              "sum": sum(nums)}
        return out

    def tool_help(self, tool):
        func, usage_name = self._resolve_tool(tool)
        return {"tool": tool, "usage": arcpy.Usage(usage_name)}

    # -- web services ----------------------------------------------------------------

    @staticmethod
    def _portal():
        portal = None
        try:
            portal = arcpy.GetActivePortalURL()
        except Exception:
            pass
        return (portal or "https://www.arcgis.com/").rstrip("/") + "/"

    @staticmethod
    def _token():
        try:
            tok = arcpy.GetSigninToken()
            if tok and tok.get("token"):
                return tok
        except Exception:
            pass
        return None

    def _get_json(self, url, params=None, timeout=30):
        """GET url?f=json (plus Pro's sign-in token). Raises BridgeError on failure or on
        an ArcGIS error payload."""
        params = dict(params or {})
        params["f"] = "json"
        headers = {"User-Agent": "arcpro-mcp-bridge"}
        tok = self._token()
        if tok:
            params["token"] = tok["token"]
            if tok.get("referer"):
                headers["Referer"] = tok["referer"]
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(url + sep + urllib.parse.urlencode(params), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise BridgeError("HTTP %s from %s" % (e.code, url))
        except ValueError:
            raise BridgeError("%s did not return JSON. Is it an ArcGIS REST endpoint?" % url)
        except Exception as e:
            raise BridgeError("Could not reach %s: %s" % (url, e))
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            code = err.get("code")
            hint = ""
            if code in (498, 499, 403):
                hint = (" The service is not shared publicly, needs a sign-in Pro does not "
                        "have, or has been retired.")
            elif code in (400, 404):
                hint = " The URL looks wrong, or the service was moved or retired."
            raise BridgeError(
                "Service check failed for %s: error %s, %s.%s Try search_arcgis_online to "
                "find a current URL." % (url, code, err.get("message"), hint))
        return data

    def search_portal(self, query, item_type=None, limit=20, my_org_only=False):
        portal = self._portal()
        q = query
        if item_type:
            q += ' type:"%s"' % item_type
        params = {"q": q, "num": max(1, min(int(limit), 100))}
        signed_in = self._token() is not None
        if my_org_only:
            try:
                org_id = self._get_json(portal + "sharing/rest/portals/self").get("id")
                if org_id:
                    params["q"] += " orgid:%s" % org_id
            except BridgeError:
                pass
        try:
            data = self._get_json(portal + "sharing/rest/search", params, timeout=60)
        except BridgeError as e:
            raise BridgeError("Portal search failed: %s" % str(e).split(" Try search_")[0])
        items = []
        for it in data.get("results", []):
            items.append({
                "id": it.get("id"), "title": it.get("title"), "type": it.get("type"),
                "owner": it.get("owner"), "url": it.get("url"),
                "snippet": it.get("snippet"), "access": it.get("access"),
                "item_page": portal + "home/item.html?id=%s" % it.get("id"),
            })
        return {"portal": portal, "signed_in": signed_in,
                "total": data.get("total"), "items": items}

    def describe_url(self, url=None, item_id=None):
        """Inspect a service layer (fields, real feature count, extent) or a portal item
        (owner, title) without adding anything to a map."""
        if not url and not item_id:
            raise BridgeError("Give a service url, an item_id, or both.")
        out = {}
        if item_id:
            portal = self._portal()
            it = self._get_json(portal + "sharing/rest/content/items/%s" % item_id)
            out["item"] = {k: it.get(k) for k in ("id", "title", "type", "owner", "url",
                                                  "access", "snippet")}
            for k in ("created", "modified"):
                if it.get(k):
                    out["item"][k] = datetime.datetime.fromtimestamp(
                        it[k] / 1000.0, datetime.timezone.utc).strftime("%Y-%m-%d")
            out["item"]["item_page"] = portal + "home/item.html?id=%s" % item_id
            url = url or it.get("url")
            if not url:
                out["note"] = "This item has no service URL."
                return out
        url = url.rstrip("/")
        info = self._get_json(url)
        out["url"] = url
        if "fields" not in info and ("layers" in info or "tables" in info):
            out["service"] = {
                "description": (info.get("serviceDescription") or info.get("description")
                                or "")[:500],
                "layers": [{"id": l.get("id"), "name": l.get("name"),
                            "geometry_type": l.get("geometryType")}
                           for l in info.get("layers", [])],
                "tables": [{"id": t.get("id"), "name": t.get("name")}
                           for t in info.get("tables", [])],
            }
            out["note"] = "This is a service root. Append /<id> to describe one layer."
            return out
        ext = info.get("extent") or {}
        sr = ext.get("spatialReference") or info.get("spatialReference") or {}
        layer = {
            "name": info.get("name"), "type": info.get("type"),
            "geometry_type": info.get("geometryType"),
            "description": (info.get("description") or "")[:500],
            "copyright": info.get("copyrightText"),
            "spatial_reference_wkid": sr.get("latestWkid") or sr.get("wkid"),
            "declared_extent": {k: ext.get(k) for k in ("xmin", "ymin", "xmax", "ymax")}
            if ext else None,
            "max_record_count": info.get("maxRecordCount"),
            "capabilities": info.get("capabilities"),
            "fields": [{"name": f.get("name"), "type": f.get("type"), "alias": f.get("alias")}
                       for f in info.get("fields") or []],
        }
        out["layer"] = layer
        if info.get("type") in ("Feature Layer", "Table") or info.get("fields"):
            try:
                c = self._get_json(url + "/query", {"where": "1=1", "returnCountOnly": "true"})
                out["feature_count"] = c.get("count")
            except BridgeError as e:
                out["count_error"] = str(e)
            if info.get("geometryType"):
                try:
                    e = self._get_json(url + "/query", {"where": "1=1",
                                                        "returnExtentOnly": "true",
                                                        "outSR": "4326"}).get("extent")
                    if e and e.get("xmin") is not None:
                        out["data_extent_wgs84"] = {k: e.get(k) for k in
                                                    ("xmin", "ymin", "xmax", "ymax")}
                except BridgeError:
                    pass  # not every service supports returnExtentOnly
        return out

    # -- write commands ---------------------------------------------------------

    def _check_service_url(self, url):
        """Ask a web service about itself first: arcpy's own error for a bad URL is just
        'Failed to add data. Possible credentials issue.'"""
        try:
            self._get_json(url)
        except BridgeError as e:
            if str(e).startswith("Service check failed"):
                raise
            # offline, not JSON, odd server: let arcpy try anyway

    def add_layer(self, source, map=None, name=None):
        m = self._map(map)
        if source.lower().startswith(("http://", "https://")):
            self._check_service_url(source)
        try:
            lyr = m.addDataFromPath(source)
        except RuntimeError as e:
            raise BridgeError(
                "ArcGIS Pro could not add %r: %s. Check that the path exists (use raw "
                "Windows paths) or that the service URL ends at a layer, e.g. "
                ".../FeatureServer/0." % (source, e))
        if name and lyr is not None:
            lyr.name = name
        if lyr is not None and not hasattr(lyr, "isFeatureLayer"):
            return {"map": m.name, "added": self._table_summary(lyr)}
        return {"map": m.name, "added": self._layer_summary(lyr) if lyr is not None else None}

    def remove_layer(self, layer, map=None):
        m = self._map(map)
        for obj in self._tables(m):
            if layer.lower() in (str(getattr(obj, "longName", "")).lower(), obj.name.lower()):
                m.removeTable(obj)
                return {"map": m.name, "removed": obj.name}
        m, lyr = self._layer(map, layer)
        name = lyr.longName
        m.removeLayer(lyr)
        return {"map": m.name, "removed": name}

    def set_layer_properties(self, layer, map=None, visible=None, definition_query=None,
                             new_name=None, transparency=None):
        m, lyr = self._layer(map, layer)
        changed = {}
        if visible is not None:
            lyr.visible = bool(visible)
            changed["visible"] = lyr.visible
        if definition_query is not None:
            if not _supports(lyr, "DEFINITIONQUERY"):
                raise BridgeError("Layer %r does not support definition queries." % layer)
            lyr.definitionQuery = definition_query
            changed["definition_query"] = definition_query
        if transparency is not None:
            lyr.transparency = int(transparency)
            changed["transparency"] = int(transparency)
        if new_name:
            lyr.name = new_name
            changed["name"] = new_name
        return {"map": m.name, "layer": lyr.longName, "changed": changed}

    def zoom_to_layer(self, layer, map=None):
        m, lyr = self._layer(map, layer)
        if not self.live:
            raise BridgeError("zoom_to_layer needs live mode (bridge started inside ArcGIS Pro).")
        view = self._view_for(m, open_if_needed=True)
        if view is None:
            raise BridgeError("Could not get a map view for %r. Open that map's tab in Pro." % m.name)
        view.camera.setExtent(view.getLayerExtent(lyr, False, True))
        return {"zoomed_to": lyr.longName, "map": m.name}

    def _view_for(self, m, open_if_needed=False):
        """The active map view if it shows map m; optionally open a view of m (Pro 3.x)."""
        if not self.live:
            return None
        view = self.aprx.activeView
        if view is not None and hasattr(view, "camera"):
            try:
                if view.map.name == m.name:
                    return view
            except Exception:
                pass
        if open_if_needed and hasattr(m, "openView"):
            try:
                m.openView()
                view = self.aprx.activeView
                if view is not None and hasattr(view, "camera"):
                    return view
            except Exception:
                pass
        return None

    def create_map(self, name, include_basemap=True):
        m = self.aprx.createMap(name, "MAP")
        out = {"created_map": m.name}
        if not include_basemap:
            removed = []
            for lyr in list(m.listLayers()):
                if "\\" in str(getattr(lyr, "longName", "")):
                    continue  # goes away with its parent group
                try:
                    m.removeLayer(lyr)
                    removed.append(lyr.name)
                except Exception:
                    pass
            out["removed_default_layers"] = removed
            out["layer_count"] = len(m.listLayers())
        return out

    def _resolve_tool(self, tool):
        """Accept 'analysis.Buffer', 'Buffer_analysis' or 'arcpy.analysis.Buffer'."""
        t = tool.strip()
        if t.startswith("arcpy."):
            t = t[len("arcpy."):]
        if "." in t:
            toolbox, name = t.split(".", 1)
        elif "_" in t:
            name, toolbox = t.rsplit("_", 1)
        else:
            raise BridgeError("Give the tool as 'toolbox.Tool', for example 'analysis.Buffer'.")
        mod = getattr(arcpy, toolbox, None)
        func = getattr(mod, name, None) if mod is not None else None
        if func is None:
            raise BridgeError("Unknown geoprocessing tool %r." % tool)
        return func, "%s_%s" % (name, toolbox)

    def _schema_guard(self, tool, usage_name, args, kwargs, lookup):
        """Refuse schema changes and deletes on datasets a map is drawing."""
        key = usage_name.lower()
        if key not in SCHEMA_TOOLS:
            return
        if args:
            target = args[0]
        else:
            target = next((kwargs[k] for k in TARGET_KWARGS if k in kwargs), None)
        if target is None:
            return
        users = self._dataset_users()
        for t in _flatten(target):
            if not isinstance(t, str):
                t = getattr(t, "dataSource", None)
                if not t:
                    continue
            if t.lower() in lookup:
                obj = lookup[t.lower()]
                path = getattr(obj, "dataSource", None) or t
            elif _is_path(t):
                path = t
            else:
                path = self._dataset_path(t) or t
            if key == "calculatefield_management":
                field = args[1] if len(args) > 1 else kwargs.get("field")
                try:
                    names = [f.name.lower() for f in arcpy.ListFields(path)]
                except Exception:
                    names = None
                if not field or names is None or str(field).lower() in names:
                    continue  # filling an existing field is a data edit, not a schema change
            used = users.get(_norm(path))
            if t.lower() in lookup and not used:
                used = ["(layer %r in the current map)" % t]
            locked = False
            if not used:
                # TestSchemaLock is False for every memory dataset in Pro 3.5, even a new one.
                test = getattr(arcpy, "TestSchemaLock", None)
                if test is not None and _is_path(path) and not _is_memory(path):
                    try:
                        locked = arcpy.Exists(path) and not test(path)
                    except Exception:
                        locked = False
            if not used and not locked:
                continue
            why = ("is drawn by %s" % used) if used else (
                "is locked by another process (OneDrive sync, another Pro or Catalog "
                "session, or a layer in another project)")
            raise BridgeError(
                "Refused: %s %s %s, which %s. Changing the schema of (or deleting) a dataset "
                "that ArcGIS Pro is drawing or that is locked can hang or crash Pro, and a "
                "Delete that hits a lock can leave a half-deleted feature class.\n"
                "Safer: build a new dataset with every field it needs (run the export with "
                "add_outputs_to_map=false, add fields to the copy, then add_layer it), point "
                "the layers at it, and delete the old one only after removing its layers and "
                "restarting Pro. To take a layer off the map, use remove_layer.\n"
                "If the user explicitly accepts the risk (save a backup copy of the project "
                "first), rerun with unsafe_schema_change=true."
                % (tool, SCHEMA_TOOLS[key], path, why))

    def run_geoprocessing(self, tool, args=None, kwargs=None, add_outputs_to_map=True,
                          map=None, overwrite=False, unsafe_schema_change=False):
        func, usage_name = self._resolve_tool(tool)
        args = list(args or [])
        kwargs = dict(kwargs or {})
        lookup = {}
        if self.aprx.listMaps():
            m = self._map(map)
            for lyr in list(m.listLayers()) + self._tables(m):
                lookup.setdefault(str(getattr(lyr, "longName", lyr.name)).lower(), lyr)
                lookup.setdefault(str(lyr.name).lower(), lyr)
        if not unsafe_schema_change:
            self._schema_guard(tool, usage_name, args, kwargs, lookup)
        used = []
        temps = []

        def swap(v, mode):
            if isinstance(v, (list, tuple)):
                return [swap(x, mode) for x in v]
            if not isinstance(v, str) or v.lower() not in lookup:
                return v
            lyr = lookup[v.lower()]
            used.append(v)
            if mode == "layer":
                return lyr
            # mode "temp": a fresh layer from the source, keeping the definition query
            if getattr(lyr, "isFeatureLayer", False):
                tmp = "_mcp_tmp_%d" % (len(temps) + 1)
                res = arcpy.management.MakeFeatureLayer(
                    lyr.dataSource, tmp, lyr.definitionQuery or None)
                temps.append(tmp)
                return res.getOutput(0)
            return lyr.dataSource

        def run(mode):
            a = [swap(x, mode) for x in args]
            k = dict((key, swap(val, mode)) for key, val in kwargs.items())
            return func(*a, **k)

        def fail(msgs):
            if "000725" in msgs:
                msgs += ("\nThe output already exists. Pick a new output name, or pass "
                         "overwrite=true if the user wants it replaced.")
            if any(code in msgs for code in ("000317", "000464", "000496")):
                msgs += ("\nThe dataset is locked. Do not retry: a Delete or schema change "
                         "that hits a lock can leave the dataset half-deleted. Remove the "
                         "layers that use it, save, and ask the user to restart Pro first.")
            raise BridgeError("Geoprocessing failed:\n" + msgs)

        prev = getattr(arcpy.env, "addOutputsToMap", None)
        prev_ow = getattr(arcpy.env, "overwriteOutput", None)
        note = None
        try:
            if prev is not None:
                arcpy.env.addOutputsToMap = bool(add_outputs_to_map)
            if prev_ow is not None:
                arcpy.env.overwriteOutput = bool(overwrite)
            try:
                result = run("layer")
            except arcpy.ExecuteError:
                msgs = arcpy.GetMessages(2)
                if not used or "000732" not in msgs:
                    fail(msgs)
                del used[:]
                try:
                    result = run("temp")
                except arcpy.ExecuteError:
                    fail(arcpy.GetMessages(2))
                note = ("Inputs %s were passed as temporary layers built from their data "
                        "sources (definition queries kept, selections not)." % sorted(set(used)))
        finally:
            if prev is not None:
                arcpy.env.addOutputsToMap = prev
            if prev_ow is not None:
                arcpy.env.overwriteOutput = prev_ow
            for tmp in temps:
                try:
                    arcpy.management.Delete(tmp)
                except Exception:
                    pass
        outputs, messages = [], None
        try:
            outputs = [_safe(result.getOutput(i)) for i in range(result.outputCount)]
            messages = result.getMessages()
        except Exception:
            outputs = [_safe(result)]
        out = {"tool": tool, "outputs": outputs, "messages": messages}
        if note:
            out["note"] = note
        return out

    def insert_rows(self, table, rows, map=None):
        """Append rows (list of {field: value}) to a table or feature class."""
        if not isinstance(rows, list) or not rows:
            raise BridgeError("rows must be a non-empty list of {field: value} objects.")
        if len(rows) > MAX_INSERT_ROWS:
            raise BridgeError("At most %d rows per call; split the insert." % MAX_INSERT_ROWS)
        m, obj, label = self._find(map, table)
        path = obj if isinstance(obj, str) else obj.dataSource
        fields = {f.name.lower(): f for f in arcpy.ListFields(path)}
        cols, real, norm_rows = [], [], []
        for rec in rows:
            if not isinstance(rec, dict):
                raise BridgeError("Each row must be an object like {\"layer\": \"L2\"}.")
            low = {}
            for k, v in rec.items():
                key = k.upper() if k.upper().startswith("SHAPE@") else k.lower()
                if key in low:
                    raise BridgeError("Row %r names field %r twice." % (rec, k))
                low[key] = v
                if key not in cols:
                    cols.append(key)
            norm_rows.append(low)
        for k in cols:
            if k.startswith("SHAPE@"):
                real.append(k)
                continue
            f = fields.get(k)
            if f is None:
                raise BridgeError("No field %r in %s. Fields: %s"
                                  % (k, path, [x.name for x in fields.values()]))
            if f.type in ("OID", "GlobalID", "Geometry"):
                raise BridgeError("Field %r (%s) is set automatically; leave it out (use "
                                  "SHAPE@XY or SHAPE@WKT for geometry)." % (f.name, f.type))
            real.append(f.name)
        dates = set(i for i, k in enumerate(cols)
                    if not k.startswith("SHAPE@") and fields[k].type == "Date")

        def value(i, v):
            if i in dates and isinstance(v, str) and v:
                try:
                    return datetime.datetime.fromisoformat(v.replace("Z", ""))
                except ValueError:
                    raise BridgeError("Date field %r needs an ISO date like 2026-11-05, "
                                      "got %r." % (real[i], v))
            if real[i] == "SHAPE@XY" and isinstance(v, list):
                return tuple(v)
            return v

        def write():
            oids = []
            with arcpy.da.InsertCursor(path, real) as cur:
                for rec in norm_rows:
                    oids.append(cur.insertRow([value(i, rec.get(k)) for i, k in enumerate(cols)]))
            return oids

        try:
            oids = write()
        except RuntimeError as e:
            ws = re.match(r"^(.*?\.(gdb|sde))([\\/]|$)", path, re.I)
            if "edit session" not in str(e).lower() or not ws or not hasattr(arcpy.da, "Editor"):
                raise BridgeError("Insert into %s failed: %s" % (path, e))
            with arcpy.da.Editor(ws.group(1)):
                oids = write()
        return {"table": path, "inserted": len(oids), "fields": real,
                "object_ids": _safe(oids[:MAX_ROWS])}

    # -- rendering ----------------------------------------------------------------

    def export_image(self, map=None, layout=None, width=1400, height=900, resolution=96,
                     path=None, layer=None, use_view_extent=None):
        if path is None:
            fd, path = tempfile.mkstemp(prefix="arcpro_mcp_", suffix=".png")
            os.close(fd)
        if layout:
            self._layout(layout).exportToPNG(path, resolution=int(resolution))
            return {"path": path, "source": "layout:" + layout}
        # Map views are never exported directly: MapView.exportToPNG pads with black when
        # the requested size differs from the on-screen tab. Instead render through a
        # temporary layout at the exact size.
        # Framing: with no map named, use the active view's extent (what the user sees).
        # With a map named, frame on its visible data, unless use_view_extent is set.
        view = None
        if map is None and self.live:
            av = self.aprx.activeView
            if av is not None and hasattr(av, "camera") and hasattr(av, "map"):
                view = av
                m = av.map
        if view is None:
            m = self._map(map)
            view = self._view_for(m)
        if use_view_extent is None:
            use_view_extent = map is None
        extent = None
        frame_lyr = None
        if layer:
            m, frame_lyr = self._layer(m.name, layer)
        elif view is not None and use_view_extent:
            try:
                extent = view.camera.getExtent()
            except Exception:
                extent = None
        return self._snapshot_via_temp_layout(m, path, width, height, resolution, extent,
                                              frame_lyr)

    @staticmethod
    def _effectively_visible(lyr, by_long):
        """Visible itself and every parent group visible; not part of a basemap."""
        if not getattr(lyr, "visible", True):
            return False
        parts = str(getattr(lyr, "longName", lyr.name)).split("\\")
        for i in range(1, len(parts)):
            parent = by_long.get("\\".join(parts[:i]).lower())
            if parent is None:
                continue
            if not getattr(parent, "visible", True) or getattr(parent, "isBasemapLayer", False):
                return False
        return True

    def _data_extent(self, m, mf, only=None):
        """Union of layer extents, projected to the map's coordinate system. Hidden and
        basemap layers are skipped (they used to pull snapshots out to the whole world)."""
        msr = getattr(m, "spatialReference", None)
        all_layers = list(m.listLayers())
        by_long = dict((str(getattr(l, "longName", l.name)).lower(), l) for l in all_layers)
        candidates = []
        for lyr in ([only] if only is not None else all_layers):
            if getattr(lyr, "isBasemapLayer", False) or getattr(lyr, "isGroupLayer", False):
                continue
            if not (getattr(lyr, "isFeatureLayer", False) or getattr(lyr, "isRasterLayer", False)):
                continue
            candidates.append(lyr)
        if only is not None:
            pool, hidden = candidates, 0
        else:
            visible = [l for l in candidates if self._effectively_visible(l, by_long)]
            # if nothing is visible, frame on everything rather than on nothing
            pool = visible or candidates
            hidden = len(candidates) - len(visible) if visible else 0
        ext, used, skipped = None, [], []
        for lyr in pool:
            try:
                e = mf.getLayerExtent(lyr, False, True)
            except Exception:
                e = None
            if e is None:
                skipped.append(lyr.name)
                continue
            esr = getattr(e, "spatialReference", None)
            if msr is not None and esr is not None and _sr_code(esr) != _sr_code(msr):
                try:
                    e = e.projectAs(msr)
                except Exception:
                    skipped.append(lyr.name)
                    continue
            vals = [e.XMin, e.YMin, e.XMax, e.YMax]
            if any(v is None or v != v or abs(v) == float("inf") for v in vals):
                skipped.append(lyr.name)
                continue
            if ext is None:
                ext = vals
            else:
                ext = [min(ext[0], vals[0]), min(ext[1], vals[1]),
                       max(ext[2], vals[2]), max(ext[3], vals[3])]
            used.append(getattr(lyr, "longName", lyr.name))
        if ext is not None:
            # a small margin so symbols on the edge are not cut off
            dx, dy = (ext[2] - ext[0]) * 0.03, (ext[3] - ext[1]) * 0.03
            ext = [ext[0] - dx, ext[1] - dy, ext[2] + dx, ext[3] + dy]
        return ext, msr, {"framed_on": used, "hidden_layers_ignored": hidden,
                          "skipped": skipped}

    def _snapshot_via_temp_layout(self, m, path, width, height, resolution, extent=None,
                                  frame_lyr=None):
        """Render a map with no open view: temporary layout + map frame, then delete it."""
        if not hasattr(self.aprx, "createLayout"):
            raise BridgeError("Snapshots of maps without an open view need ArcGIS Pro 3.0+.")
        dpi = float(resolution)
        w_in, h_in = int(width) / dpi, int(height) / dpi
        lyt = self.aprx.createLayout(w_in, h_in, "INCH", "_arcpro_mcp_snapshot")
        info = {}
        try:
            frame = arcpy.Polygon(arcpy.Array([arcpy.Point(0, 0), arcpy.Point(0, h_in),
                                               arcpy.Point(w_in, h_in), arcpy.Point(w_in, 0),
                                               arcpy.Point(0, 0)]))
            mf = lyt.createMapFrame(frame, m, "snapshot")
            # Pin the frame to the page explicitly: in the real-Pro test the frame came out
            # shifted (blank band on the top and left) when only the geometry was given.
            for attr, val in (("elementWidth", w_in), ("elementHeight", h_in),
                              ("elementPositionX", 0), ("elementPositionY", 0)):
                try:
                    setattr(mf, attr, val)
                except Exception:
                    pass
            frame_info = {"page_in": [round(w_in, 3), round(h_in, 3)]}
            for attr in ("elementPositionX", "elementPositionY", "elementWidth", "elementHeight"):
                try:
                    frame_info[attr] = round(float(getattr(mf, attr)), 3)
                except Exception:
                    pass
            if extent is not None:
                mf.camera.setExtent(extent)
                how = "current view extent"
                info["extent"] = _extent_dict(extent)
            else:
                ext, msr, info = self._data_extent(m, mf, frame_lyr)
                if ext is not None:
                    e = arcpy.Extent(ext[0], ext[1], ext[2], ext[3], spatial_reference=msr)
                    mf.camera.setExtent(e)
                    info["extent"] = _extent_dict(e)
                    how = ("zoomed to layer %s" % frame_lyr.longName if frame_lyr is not None
                           else "zoomed to visible data")
                else:
                    mf.zoomToAllLayers()
                    how = "zoomed to all layers"
            lyt.exportToPNG(path, resolution=int(resolution))
        finally:
            try:
                self.aprx.deleteItem(lyt)
            except Exception:
                pass
        out = {"path": path, "source": "map:%s (%s)" % (m.name, how), "frame": frame_info}
        out.update(info)
        return out

    def export_layout_pdf(self, path, layout=None, resolution=300):
        lyt = self._layout(layout)
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        lyt.exportToPDF(path, resolution=int(resolution))
        return {"path": path, "layout": lyt.name}

    def save_project(self, copy_to=None):
        if copy_to:
            if not copy_to.lower().endswith(".aprx"):
                raise BridgeError("copy_to must be a path ending in .aprx, got %r." % copy_to)
            folder = os.path.dirname(copy_to)
            created = None
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
                created = folder
            self.aprx.saveACopy(copy_to)
            out = {"saved_copy": copy_to}
            if created:
                out["created_folder"] = created
            return out
        self.aprx.save()
        return {"saved": self.aprx.filePath}

    def run_arcpy(self, code):
        if not self.allow_exec:
            raise BridgeError(
                "run_arcpy is turned off. Ask the user to run this one line in Pro's Python "
                "window: start(allow_exec=True)  (or start the bridge with --allow-exec, or "
                "tick 'Allow run_arcpy' in the toolbox's Start Bridge tool).")
        ns = {"arcpy": arcpy, "aprx": self.aprx, "result": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
        return {"stdout": buf.getvalue()[-20000:], "result": _safe(ns.get("result"))}

    # -- dispatch --------------------------------------------------------------

    def _check_not_stuck(self):
        st = self.stuck
        if st is None:
            return
        if st["thread"].is_alive():
            raise BridgeError(
                "ArcGIS Pro is still busy with %s, which passed its %ss limit; it started "
                "%ds ago. Pro is probably hung drawing the map. Call bridge_status to check, "
                "and ask the user to look at Pro: wait for drawing to finish or press Esc / "
                "pause drawing. If Pro is frozen, it needs a restart (then restart the bridge)."
                % (st["cmd"], st["limit"], time.time() - st["since"]))
        self.stuck = None

    def handle(self, cmd, params):
        func = self.commands.get(cmd)
        if func is None:
            raise BridgeError("Unknown command %r" % cmd)
        params = dict(params or {})
        if cmd in LOCK_FREE:
            return func(**params)
        limit = None
        if cmd in RENDER_TIMEOUTS:
            limit = params.pop("timeout", None) or RENDER_TIMEOUTS[cmd]
        self._check_not_stuck()
        box = {}

        def work():
            with self.lock:
                self.running = {"cmd": cmd, "since": time.time()}
                try:
                    box["result"] = func(**params)
                except BaseException as e:
                    box["error"] = e
                    box["traceback"] = traceback.format_exc()
                finally:
                    self.running = None

        if limit is None:
            work()
        else:
            started = time.time()
            t = threading.Thread(target=work, name="arcpro-mcp-" + cmd)
            t.daemon = True
            t.start()
            t.join(float(limit))
            if t.is_alive():
                self.stuck = {"cmd": cmd, "since": started, "limit": limit, "thread": t}
                raise BridgeError(
                    "%s did not finish within %ss, so the bridge stopped waiting. ArcGIS Pro "
                    "may be stuck drawing the map (a large or broken layer, a slow web "
                    "service, or a renderer Pro cannot draw). The bridge refuses other "
                    "commands until Pro finishes; call bridge_status to check. Ask the user "
                    "to look at Pro before trying again." % (cmd, limit))
        if "error" in box:
            e = box["error"]
            try:
                e._bridge_traceback = box["traceback"]
            except Exception:
                pass
            raise e
        return box["result"]


# ---------------------------------------------------------------------------
# Socket server (newline-delimited JSON)
# ---------------------------------------------------------------------------

class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        for raw in self.rfile:
            if not raw.strip():
                continue
            resp = {"id": None, "ok": False}
            try:
                req = json.loads(raw.decode("utf-8"))
                resp["id"] = req.get("id")
                if not secrets.compare_digest(str(req.get("token", "")), self.server.token):
                    raise BridgeError("Bad bridge token. Restart the MCP server or the bridge.")
                resp["result"] = bridge.handle(req.get("cmd"), req.get("params"))
                resp["ok"] = True
            except BridgeError as e:
                resp["error"] = str(e)
            except TypeError as e:
                resp["error"] = "Bad parameters: %s" % e
            except Exception as e:
                resp["error"] = "%s: %s" % (type(e).__name__, e)
                tb = getattr(e, "_bridge_traceback", None) or traceback.format_exc()
                resp["traceback"] = tb[-4000:]
            try:
                self.wfile.write((json.dumps(resp, default=str) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                return  # the caller went away


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(project="CURRENT", port=DEFAULT_PORT, allow_exec=False, background=True,
          quiet=False):
    """Start the bridge. Returns the server object."""
    prev = getattr(builtins, "_arcpro_mcp_bridge", None)
    if prev is not None:
        try:
            prev.shutdown()
            prev.server_close()
        except Exception:
            pass
    bridge = Bridge(project=project, allow_exec=allow_exec)
    server = _Server(("127.0.0.1", int(port)), _Handler)
    server.bridge = bridge
    server.token = secrets.token_hex(24)
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    with open(STATE_FILE, "w") as fh:
        json.dump({"host": "127.0.0.1", "port": server.server_address[1], "token": server.token,
                   "mode": "live" if bridge.live else "standalone",
                   "project": bridge.aprx.filePath, "pid": os.getpid(),
                   "bridge_version": BRIDGE_VERSION, "run_arcpy_enabled": bool(allow_exec)}, fh)
    builtins._arcpro_mcp_bridge = server
    msg = "[arcpro-mcp] bridge %s listening on 127.0.0.1:%s (%s mode, run_arcpy %s, project: %s)" % (
        BRIDGE_VERSION, server.server_address[1], "live" if bridge.live else "standalone",
        "ON" if allow_exec else "off", bridge.aprx.filePath)
    if background:
        t = threading.Thread(target=server.serve_forever, name="arcpro-mcp-bridge")
        t.daemon = True
        t.start()
        if not quiet:
            print(msg)
    else:
        print(msg + "  (Ctrl+C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return server


def stop():
    prev = getattr(builtins, "_arcpro_mcp_bridge", None)
    if prev is not None:
        prev.shutdown()
        prev.server_close()
        builtins._arcpro_mcp_bridge = None
        print("[arcpro-mcp] bridge stopped")


def _main(argv):
    import argparse
    p = argparse.ArgumentParser(description="arcpro-mcp bridge")
    p.add_argument("--project", default=None, help="Path to an .aprx (standalone mode)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--allow-exec", action="store_true", help="Enable the run_arcpy tool")
    a, _unknown = p.parse_known_args(argv)  # Pro's window may pass odd argv entries
    if a.project:
        start(a.project, a.port, a.allow_exec, background=False)
    else:
        # Live mode from Pro's Python window. Usually the same line goes on to call
        # start(allow_exec=...), which restarts the bridge with that setting; this first
        # start keeps the plain exec(...) line working on its own.
        start("CURRENT", a.port, a.allow_exec, background=True)


if __name__ == "__main__":
    # Inside Pro's Python window sys.argv is empty, so this starts live mode in the
    # background. From the command line, --project starts standalone mode.
    _main([a for a in sys.argv[1:] if a])
