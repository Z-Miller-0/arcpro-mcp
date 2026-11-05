"""ArcPro MCP bridge: exposes ArcGIS Pro (arcpy) to the arcpro-mcp server over localhost.

Two ways to run it:

1. LIVE mode (recommended). Inside ArcGIS Pro, open View > Python window and run:

       exec(open(r"C:\\path\\to\\arcpro-mcp\\bridge\\arcpro_bridge.py").read())

   Claude then acts on the project you have open ("CURRENT"): layers it adds show
   up in your map immediately. Re-running the line restarts the bridge.

2. STANDALONE mode (Pro does not need to be open, but must be installed and licensed):

       "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\Scripts\\propy.bat" arcpro_bridge.py --project "C:\\GIS\\MyProject.aprx"

   Changes are written to the .aprx only when Claude calls save_project.
   Do not have the same .aprx open in Pro while using standalone mode.

Only the Python standard library and arcpy are used, so nothing has to be
installed into ArcGIS Pro's Python environment. Keep this file compatible with
Python 3.9 (older Pro releases).
"""

import builtins
import contextlib
import io
import json
import os
import secrets
import socketserver
import sys
import tempfile
import threading
import traceback
import urllib.parse
import urllib.request

import arcpy

BRIDGE_VERSION = "0.1.3"
DEFAULT_PORT = 18760
STATE_DIR = os.path.join(os.path.expanduser("~"), ".arcpro_mcp")
STATE_FILE = os.path.join(STATE_DIR, "bridge.json")
MAX_ROWS = 2000


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


class Bridge(object):
    def __init__(self, project="CURRENT", allow_exec=False):
        self.project_arg = project
        self.live = project.upper() == "CURRENT"
        self.allow_exec = allow_exec
        self.lock = threading.RLock()  # arcpy is not thread safe
        self.aprx = arcpy.mp.ArcGISProject(project)
        self.commands = {
            "ping": self.ping,
            "project_info": self.project_info,
            "list_layers": self.list_layers,
            "describe_layer": self.describe_layer,
            "query_layer": self.query_layer,
            "field_summary": self.field_summary,
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

    # arcpy.ListFields / GetCount / Describe on a layer object resolve it by name in Pro's
    # *active* map, so they fail for layers in other maps. Fall back to the data source.
    @staticmethod
    def _fields(lyr):
        try:
            return arcpy.ListFields(lyr)
        except (OSError, RuntimeError, IOError):
            return arcpy.ListFields(lyr.dataSource)

    @staticmethod
    def _count(lyr):
        try:
            return int(arcpy.management.GetCount(lyr)[0])
        except Exception:
            n = 0
            with arcpy.da.SearchCursor(lyr, ["OID@"]) as cur:  # honors the definition query
                for _ in cur:
                    n += 1
            return n

    @staticmethod
    def _describe(lyr):
        try:
            return arcpy.Describe(lyr)
        except (OSError, RuntimeError, IOError):
            return arcpy.Describe(lyr.dataSource)

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

    # -- read commands -----------------------------------------------------------

    def ping(self):
        info = {}
        try:
            info = arcpy.GetInstallInfo()
        except Exception:
            pass
        return {
            "bridge_version": BRIDGE_VERSION,
            "mode": "live" if self.live else "standalone",
            "project": self.aprx.filePath,
            "arcgis_pro_version": info.get("Version"),
            "run_arcpy_enabled": self.allow_exec,
        }

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
                {"name": m.name, "layer_count": len(m.listLayers())} for m in self.aprx.listMaps()
            ],
            "layouts": [l.name for l in self.aprx.listLayouts()],
        }

    def list_layers(self, map=None):
        m = self._map(map)
        return {"map": m.name, "layers": [self._layer_summary(l) for l in m.listLayers()]}

    def describe_layer(self, layer, map=None):
        m, lyr = self._layer(map, layer)
        out = self._layer_summary(lyr)
        out["map"] = m.name
        if lyr.isFeatureLayer or lyr.isRasterLayer:
            try:
                d = self._describe(lyr)
                out["dataset_type"] = getattr(d, "datasetType", None)
                out["shape_type"] = getattr(d, "shapeType", None)
                sr = getattr(d, "spatialReference", None)
                out["spatial_reference"] = getattr(sr, "name", None) if sr else None
                out["extent"] = _extent_dict(getattr(d, "extent", None))
            except Exception as e:
                out["describe_error"] = str(e)
        if lyr.isFeatureLayer:
            out["feature_count"] = self._count(lyr)
            out["fields"] = [
                {"name": f.name, "alias": f.aliasName, "type": f.type, "length": f.length}
                for f in self._fields(lyr)
            ]
        return out

    def query_layer(self, layer, map=None, where=None, fields=None, limit=50,
                    include_geometry=False, order_by=None):
        m, lyr = self._layer(map, layer)
        if not lyr.isFeatureLayer and not getattr(lyr, "isTable", False):
            raise BridgeError("%r is not a feature layer or table." % layer)
        limit = max(1, min(int(limit or 50), MAX_ROWS))
        if not fields or fields == ["*"]:
            fields = [f.name for f in self._fields(lyr)
                      if f.type not in ("Geometry", "Blob", "Raster")]
        fields = list(fields)
        cursor_fields = fields + (["SHAPE@XY"] if include_geometry else [])
        sql = (None, "ORDER BY %s" % order_by) if order_by else None
        rows = []
        truncated = False
        with arcpy.da.SearchCursor(lyr, cursor_fields, where_clause=where, sql_clause=sql) as cur:
            for row in cur:
                if len(rows) >= limit:
                    truncated = True
                    break
                rec = dict(zip(fields, [_safe(v) for v in row[: len(fields)]]))
                if include_geometry:
                    rec["_centroid_xy"] = _safe(row[-1])
                rows.append(rec)
        return {"layer": lyr.longName, "where": where, "row_count": len(rows),
                "truncated": truncated, "rows": rows}

    def field_summary(self, layer, field, map=None, where=None, top=25):
        m, lyr = self._layer(map, layer)
        counts = {}
        nums = []
        n = 0
        with arcpy.da.SearchCursor(lyr, [field], where_clause=where) as cur:
            for (v,) in cur:
                n += 1
                counts[v] = counts.get(v, 0) + 1
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nums.append(v)
        top_vals = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: int(top)]
        out = {"layer": lyr.longName, "field": field, "rows": n,
               "distinct_values": len(counts),
               "top_values": [{"value": _safe(k), "count": c} for k, c in top_vals]}
        if nums:
            out["numeric"] = {"min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums),
                              "sum": sum(nums)}
        return out

    def tool_help(self, tool):
        func, usage_name = self._resolve_tool(tool)
        return {"tool": tool, "usage": arcpy.Usage(usage_name)}

    def search_portal(self, query, item_type=None, limit=20, my_org_only=False):
        portal = None
        token_info = None
        try:
            portal = arcpy.GetActivePortalURL()
            token_info = arcpy.GetSigninToken()
        except Exception:
            pass
        portal = (portal or "https://www.arcgis.com/").rstrip("/") + "/"
        q = query
        if item_type:
            q += ' type:"%s"' % item_type
        params = {"q": q, "num": max(1, min(int(limit), 100)), "f": "json"}
        headers = {"User-Agent": "arcpro-mcp-bridge"}
        if token_info and token_info.get("token"):
            params["token"] = token_info["token"]
            if token_info.get("referer"):
                headers["Referer"] = token_info["referer"]
        if my_org_only:
            try:
                req = urllib.request.Request(
                    portal + "sharing/rest/portals/self?" + urllib.parse.urlencode(
                        {"f": "json", "token": params.get("token", "")}), headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    org_id = json.loads(r.read().decode("utf-8")).get("id")
                if org_id:
                    params["q"] += " orgid:%s" % org_id
            except Exception:
                pass
        req = urllib.request.Request(
            portal + "sharing/rest/search?" + urllib.parse.urlencode(params), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        if "error" in data:
            raise BridgeError("Portal search failed: %s" % data["error"])
        items = []
        for it in data.get("results", []):
            items.append({
                "id": it.get("id"), "title": it.get("title"), "type": it.get("type"),
                "owner": it.get("owner"), "url": it.get("url"),
                "snippet": it.get("snippet"), "access": it.get("access"),
                "item_page": portal + "home/item.html?id=%s" % it.get("id"),
            })
        return {"portal": portal, "signed_in": bool(params.get("token")),
                "total": data.get("total"), "items": items}

    # -- write commands ---------------------------------------------------------

    def _check_service_url(self, url):
        """Ask a web service about itself first: arcpy's own error for a bad URL is just
        'Failed to add data. Possible credentials issue.'"""
        params = {"f": "json"}
        headers = {"User-Agent": "arcpro-mcp-bridge"}
        try:
            tok = arcpy.GetSigninToken()
            if tok and tok.get("token"):
                params["token"] = tok["token"]
                if tok.get("referer"):
                    headers["Referer"] = tok["referer"]
        except Exception:
            pass
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(url + sep + urllib.parse.urlencode(params), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return  # offline, not JSON, odd server: let arcpy try anyway
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
        return {"map": m.name, "added": self._layer_summary(lyr) if lyr is not None else None}

    def remove_layer(self, layer, map=None):
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

    def create_map(self, name):
        m = self.aprx.createMap(name, "MAP")
        return {"created_map": m.name}

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

    def run_geoprocessing(self, tool, args=None, kwargs=None, add_outputs_to_map=True,
                          map=None):
        func, _ = self._resolve_tool(tool)
        args = list(args or [])
        kwargs = dict(kwargs or {})
        lookup = {}
        if self.aprx.listMaps():
            for lyr in self._map(map).listLayers():
                lookup.setdefault(str(lyr.longName).lower(), lyr)
                lookup.setdefault(str(lyr.name).lower(), lyr)
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

        prev = getattr(arcpy.env, "addOutputsToMap", None)
        note = None
        try:
            if prev is not None:
                arcpy.env.addOutputsToMap = bool(add_outputs_to_map)
            try:
                result = run("layer")
            except arcpy.ExecuteError:
                msgs = arcpy.GetMessages(2)
                if not used or "000732" not in msgs:
                    raise BridgeError("Geoprocessing failed:\n" + msgs)
                del used[:]
                try:
                    result = run("temp")
                except arcpy.ExecuteError:
                    raise BridgeError("Geoprocessing failed:\n" + arcpy.GetMessages(2))
                note = ("Inputs %s were passed as temporary layers built from their data "
                        "sources (definition queries kept, selections not)." % sorted(set(used)))
        finally:
            if prev is not None:
                arcpy.env.addOutputsToMap = prev
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

    def export_image(self, map=None, layout=None, width=1400, height=900, resolution=96,
                     path=None):
        if path is None:
            fd, path = tempfile.mkstemp(prefix="arcpro_mcp_", suffix=".png")
            os.close(fd)
        if layout:
            self._layout(layout).exportToPNG(path, resolution=int(resolution))
            return {"path": path, "source": "layout:" + layout}
        # Map views are never exported directly: MapView.exportToPNG pads with black when
        # the requested size differs from the on-screen tab. Instead render through a
        # temporary layout at the exact size, using the open view's extent when there is one.
        view = None
        if map is None and self.live:
            av = self.aprx.activeView
            if av is not None and hasattr(av, "camera") and hasattr(av, "map"):
                view = av
                m = av.map
        if view is None:
            m = self._map(map)
            view = self._view_for(m)
        extent = None
        if view is not None:
            try:
                extent = view.camera.getExtent()
            except Exception:
                extent = None
        return self._snapshot_via_temp_layout(m, path, width, height, resolution, extent)

    def _snapshot_via_temp_layout(self, m, path, width, height, resolution, extent=None):
        """Render a map with no open view: temporary layout + map frame, then delete it."""
        if not hasattr(self.aprx, "createLayout"):
            raise BridgeError("Snapshots of maps without an open view need ArcGIS Pro 3.0+.")
        dpi = float(resolution)
        w_in, h_in = int(width) / dpi, int(height) / dpi
        lyt = self.aprx.createLayout(w_in, h_in, "INCH", "_arcpro_mcp_snapshot")
        try:
            mf = lyt.createMapFrame(arcpy.Extent(0, 0, w_in, h_in), m, "snapshot")
            ext = None
            for lyr in ([] if extent is not None else m.listLayers()):
                if getattr(lyr, "isBasemapLayer", False) or getattr(lyr, "isGroupLayer", False):
                    continue
                if not (lyr.isFeatureLayer or lyr.isRasterLayer):
                    continue
                try:
                    e = mf.getLayerExtent(lyr, False, True)
                except Exception:
                    continue
                if e is None:
                    continue
                if ext is None:
                    ext = [e.XMin, e.YMin, e.XMax, e.YMax, e.spatialReference]
                else:
                    ext = [min(ext[0], e.XMin), min(ext[1], e.YMin),
                           max(ext[2], e.XMax), max(ext[3], e.YMax), ext[4]]
            if extent is not None:
                mf.camera.setExtent(extent)
            elif ext is not None:
                mf.camera.setExtent(arcpy.Extent(ext[0], ext[1], ext[2], ext[3],
                                                 spatial_reference=ext[4]))
            else:
                mf.zoomToAllLayers()
            lyt.exportToPNG(path, resolution=int(resolution))
        finally:
            try:
                self.aprx.deleteItem(lyt)
            except Exception:
                pass
        how = "current view extent" if extent is not None else "zoomed to data"
        return {"path": path, "source": "map:%s (%s)" % (m.name, how)}

    def export_layout_pdf(self, path, layout=None, resolution=300):
        lyt = self._layout(layout)
        lyt.exportToPDF(path, resolution=int(resolution))
        return {"path": path, "layout": lyt.name}

    def save_project(self, copy_to=None):
        if copy_to:
            self.aprx.saveACopy(copy_to)
            return {"saved_copy": copy_to}
        self.aprx.save()
        return {"saved": self.aprx.filePath}

    def run_arcpy(self, code):
        if not self.allow_exec:
            raise BridgeError(
                "run_arcpy is turned off. Start the bridge with allow_exec=True "
                "(or --allow-exec) to enable arbitrary Python.")
        ns = {"arcpy": arcpy, "aprx": self.aprx, "result": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
        return {"stdout": buf.getvalue()[-20000:], "result": _safe(ns.get("result"))}

    # -- dispatch --------------------------------------------------------------

    def handle(self, cmd, params):
        func = self.commands.get(cmd)
        if func is None:
            raise BridgeError("Unknown command %r" % cmd)
        with self.lock:
            return func(**(params or {}))


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
                resp["traceback"] = traceback.format_exc()[-4000:]
            self.wfile.write((json.dumps(resp, default=str) + "\n").encode("utf-8"))
            self.wfile.flush()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(project="CURRENT", port=DEFAULT_PORT, allow_exec=False, background=True):
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
                   "project": bridge.aprx.filePath, "pid": os.getpid()}, fh)
    builtins._arcpro_mcp_bridge = server
    msg = "[arcpro-mcp] bridge %s listening on 127.0.0.1:%s (%s mode, project: %s)" % (
        BRIDGE_VERSION, server.server_address[1], "live" if bridge.live else "standalone",
        bridge.aprx.filePath)
    if background:
        t = threading.Thread(target=server.serve_forever, name="arcpro-mcp-bridge")
        t.daemon = True
        t.start()
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
        start("CURRENT", a.port, a.allow_exec, background=True)


if __name__ == "__main__":
    # Inside Pro's Python window sys.argv is empty, so this starts live mode in the
    # background. From the command line, --project starts standalone mode.
    _main([a for a in sys.argv[1:] if a])
