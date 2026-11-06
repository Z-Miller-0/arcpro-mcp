"""A tiny stand-in for arcpy so the bridge and server can be tested without ArcGIS Pro.

It mimics only the slices of the arcpy API the bridge touches. Passing these tests
shows the plumbing works; it does not prove every arcpy call behaves the same in Pro.
"""

import os  # noqa: F401  the real arcpy exposes these as attributes; tests rely on that
import sys  # noqa: F401
import threading
import time  # noqa: F401
import types


def _tool(name):
    """Mark a function as a geoprocessing tool, the way real arcpy's gptooldoc does."""
    def mark(func):
        func.__esri_toolname__ = name
        return func
    return mark


ROWS = [
    {"OBJECTID": 1, "NAME": "Mountain Pass", "STATE": "CA", "COMMODITY": "REE", "TONS": 45000},
    {"OBJECTID": 2, "NAME": "Bear Lodge", "STATE": "WY", "COMMODITY": "REE", "TONS": 0},
    {"OBJECTID": 3, "NAME": "Round Top", "STATE": "TX", "COMMODITY": "REE", "TONS": 1200},
    {"OBJECTID": 4, "NAME": "Halleck Creek", "STATE": "WY", "COMMODITY": "REE", "TONS": 300},
]


class ExecuteError(Exception):
    pass


class _Field(object):
    def __init__(self, name, type_):
        self.name, self.aliasName, self.type, self.length = name, name, type_, 50


FIELDS = [_Field("OBJECTID", "OID"), _Field("Shape", "Geometry"), _Field("NAME", "String"),
          _Field("STATE", "String"), _Field("COMMODITY", "String"), _Field("TONS", "Integer")]


class Layer(object):
    def __init__(self, name, source="C:/GIS/cm.gdb/deposits", parent=None):
        self.name = name
        self.parent = parent
        self.visible = True
        self.dataSource = source
        self.definitionQuery = ""
        self.transparency = 0
        self.isFeatureLayer = True
        self.isRasterLayer = False
        self.isGroupLayer = False
        self.isBasemapLayer = False
        self.minThreshold = self.maxThreshold = 0
        self.showLabels = False
        self.connectionProperties = None
        self.symbology = _Symbology()
        self._selection = set()
        self._label_classes = []

    @property
    def longName(self):  # like Pro, follows renames
        return (self.parent.longName + "\\" + self.name) if self.parent else self.name

    def supports(self, prop):
        if self.isGroupLayer or self.isBasemapLayer:
            return prop in ("VISIBLE",)
        return prop in ("DATASOURCE", "DEFINITIONQUERY", "VISIBLE", "TRANSPARENCY", "SHOWLABELS")

    def getSelectionSet(self):
        return set(self._selection)

    def setSelectionSet(self, oids=None, method="NEW"):
        self._selection = set(oids or [])

    def listLayers(self):
        return []

    def listLabelClasses(self):
        return list(self._label_classes)

    def createLabelClass(self, name, expression, sql_query=None, labelclass_language="ARCADE"):
        cls = _LabelClass(name, expression, sql_query, labelclass_language)
        self._label_classes.append(cls)
        return cls


class _Symbol(object):
    color = outlineColor = None
    size = outlineWidth = None


class _Renderer(object):
    def __init__(self, type_):
        self.type = type_
        self.symbol = _Symbol()
        self.fields = None
        self.classificationField = self.classificationMethod = self.breakCount = None
        self.colorRamp = None


class _Symbology(object):
    def __init__(self):
        self.renderer = _Renderer("SimpleRenderer")

    def updateRenderer(self, kind):
        self.renderer = _Renderer(kind)


class _LabelClass(object):
    def __init__(self, name, expression, sql, language):
        self.name, self.expression, self.SQLQuery = name, expression, sql
        self.language = language
        self.visible = True

    def getDefinition(self, version):
        return types.SimpleNamespace(expressionEngine=self.language.capitalize())


class TempLayer(Layer):
    """Stands in for a layer made by MakeFeatureLayer (GP accepts it)."""


class Table(object):
    """A standalone table in a map (no isFeatureLayer attribute, like Pro's Table)."""

    def __init__(self, name, source):
        self.name = name
        self.dataSource = source
        self.definitionQuery = ""
        self._selection = set()

    @property
    def longName(self):
        return self.name

    def getSelectionSet(self):
        return set(self._selection)

    def setSelectionSet(self, oids=None, method="NEW"):
        self._selection = set(oids or [])


class Map(object):
    def __init__(self, name):
        self.name = name
        self._layers = []
        self._tables = []
        self.spatialReference = SpatialReference()
        self.referenceScale = 0

    def moveLayer(self, reference, layer, position="BEFORE"):
        self._layers.remove(layer)
        at = self._layers.index(reference) + (0 if position == "BEFORE" else 1)
        self._layers.insert(at, layer)

    def createGroupLayer(self, name, parent=None):
        group = Layer(name, source=None, parent=parent)
        group.isGroupLayer, group.isFeatureLayer = True, False
        self._layers.insert(0, group)
        return group

    def addLayerToGroup(self, group, layer):
        copy = Layer(layer.name, layer.dataSource, parent=group)
        self._layers.insert(self._layers.index(group) + 1, copy)

    def addBasemap(self, name):
        if name == "nope":
            raise RuntimeError("no such basemap")

    def listLayers(self, wildcard=None):
        return list(self._layers)

    def listTables(self, wildcard=None):
        return list(self._tables)

    def removeTable(self, tbl):
        self._tables.remove(tbl)

    def addDataFromPath(self, path):
        name = path.replace("\\", "/").rstrip("/").split("/")[-1]
        if DATASETS.get(path, {}).get("type") == "Table":
            tbl = Table(name, path)
            self._tables.insert(0, tbl)
            return tbl
        lyr = Layer(name, path)
        self._layers.insert(0, lyr)
        return lyr

    def removeLayer(self, lyr):
        self._layers.remove(lyr)
        prefix = lyr.longName + "\\"
        self._layers = [l for l in self._layers if not l.longName.startswith(prefix)]

    def openView(self):
        PROJECTS[-1].activeView = MapView(self)


class _Element(object):
    def __init__(self, name, type_, **extra):
        self.name, self.type = name, type_
        self.elementPositionX = self.elementPositionY = 0.0
        self.elementWidth = self.elementHeight = 1.0
        self.visible = True
        self.__dict__.update(extra)


class Layout(object):
    def __init__(self, name):
        self.name = name
        self.pageWidth, self.pageHeight, self.pageUnits = 11, 8.5, "INCH"
        self._elements = []

    def listElements(self, element_type=None, wildcard=None):
        if element_type == "MAPFRAME_ELEMENT":
            return [e for e in self._elements if e.type == "MAPFRAME_ELEMENT"]
        return list(self._elements)

    def deleteElement(self, element):
        self._elements.remove(element)

    def createMapSurroundElement(self, geometry, kind, mapframe=None, style=None, name=None):
        el = _Element(name or kind, "MAPSURROUND_ELEMENT", surround=kind, style=style)
        self._elements.append(el)
        return el

    def exportToPNG(self, path, resolution=96):
        HANG.wait(HANG_SECONDS[0])  # a "hung render" until the test releases it
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-layout")

    def exportToPDF(self, path, resolution=300):
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")

    def exportToJPEG(self, path, resolution=96):
        with open(path, "wb") as fh:
            fh.write(b"fake-jpg")


HANG = threading.Event()
HANG.set()
HANG_SECONDS = [None]


class _Camera(object):
    extent = None

    def setExtent(self, ext):
        self.extent = ext

    def getExtent(self):
        return self.extent or Extent()


class MapView(object):
    def __init__(self, map_=None):
        self.camera = _Camera()
        self.map = map_

    def getLayerExtent(self, lyr, selection_only=False, symbolized=True):
        return Extent()

    def exportToPNG(self, path, width=None, height=None, world_file=False):
        raise AssertionError("map views must be rendered via a temporary layout")
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-view")


class SpatialReference(object):
    def __init__(self, code=3857):
        self.factoryCode = code
        self.name = {3857: "WGS_1984_Web_Mercator_Auxiliary_Sphere",
                     4326: "GCS_WGS_1984", 54012: "World_Eckert_IV"}.get(code, str(code))


class Extent(object):
    def __init__(self, XMin=-13000000.0, YMin=3000000.0, XMax=-11000000.0, YMax=5500000.0,
                 spatial_reference=None):
        self.XMin, self.YMin, self.XMax, self.YMax = XMin, YMin, XMax, YMax
        self.spatialReference = spatial_reference or SpatialReference()

    def projectAs(self, sr):
        # Toy projection: degrees -> "meters" by a fixed factor, enough to test that
        # extents in different coordinate systems are not mixed raw.
        f = 100000.0 if self.spatialReference.factoryCode == 4326 else 1.0
        PROJECTED.append(self.spatialReference.factoryCode)
        return Extent(self.XMin * f, self.YMin * f, self.XMax * f, self.YMax * f, sr)


PROJECTED = []
LAYER_EXTENTS = {}  # layer name -> Extent, for framing tests


class MapFrame(object):
    def __init__(self, m):
        self.map = m
        self.name = "Map Frame"
        self.type = "MAPFRAME_ELEMENT"
        self.visible = True
        self.camera = _Camera()
        self.camera.scale = 1000000
        self.elementPositionX, self.elementPositionY = 2.8, -1.56  # the real-Pro shift
        self.elementWidth = self.elementHeight = 3.0

    def getLayerExtent(self, lyr, selection_only=False, symbolized=True):
        return LAYER_EXTENTS.get(lyr.name) or Extent()

    def zoomToAllLayers(self):
        self.camera.extent = "all"


PROJECTS = []


class ArcGISProject(object):
    def __init__(self, path):
        self.filePath = "C:/GIS/CM_Magnets.aprx" if path == "CURRENT" else path
        self.homeFolder = "C:/GIS"
        self.defaultGeodatabase = "C:/GIS/cm.gdb"
        m = Map("US Magnet Stack")
        m._layers = [Layer("REE Deposits"), Layer("Magnet Plants", "C:/GIS/cm.gdb/plants")]
        self._maps = [m]
        self._layouts = [Layout("Main Layout")]
        self.activeMap = m
        self.activeView = MapView(m)
        self.saved = 0
        PROJECTS.append(self)

    def listMaps(self, wildcard=None):
        return list(self._maps)

    def listLayouts(self, wildcard=None):
        return list(self._layouts)

    def createMap(self, name, map_type="MAP"):
        m = Map(name)
        base = Layer("World Topographic Map", source=None)
        base.isBasemapLayer = True
        base.isFeatureLayer = False
        hill = Layer("World Hillshade", source=None)
        hill.isBasemapLayer = True
        hill.isFeatureLayer = False
        m._layers = [base, hill]
        self._maps.append(m)
        return m

    def createLayout(self, w, h, units, name=None):
        lyt = Layout(name or "Layout")
        def create(geom, m, name=None):
            lyt.frame = MapFrame(m)
            lyt._elements.append(lyt.frame)
            return lyt.frame
        lyt.createMapFrame = create
        self._layouts.append(lyt)
        return lyt

    def deleteItem(self, item):
        (self._maps if isinstance(item, Map) else self._layouts).remove(item)

    def createTextElement(self, container, geometry, text_type, text, size=None, font=None,
                          style=None, style_item=None, name=None):
        el = _Element(name or "Text", "TEXT_ELEMENT", text=text)
        container._elements.append(el)
        return el

    def listStyleItems(self, style, style_class=None, wildcard=None):
        return [types.SimpleNamespace(name=wildcard)] if wildcard != "nope" else []

    def listColorRamps(self, wildcard=None):
        names = ["Blues (5 Classes)", "Reds (5 Classes)"]
        if wildcard in (None, "*"):
            return [types.SimpleNamespace(name=n) for n in names]
        return [types.SimpleNamespace(name=n) for n in names if wildcard.strip("*") in n]

    def listBasemaps(self):
        return ["Topographic", "Imagery"]

    def listBrokenDataSources(self):
        return []

    def save(self):
        self.saved += 1

    def saveACopy(self, path):
        import os
        if not os.path.isdir(os.path.dirname(path)):
            raise OSError("[Errno 2] No such file or directory: %r" % path)
        self.copies = getattr(self, "copies", []) + [path]


mp = types.SimpleNamespace(ArcGISProject=ArcGISProject)


# Datasets in the fake geodatabase that are not the default deposits data.
LOG_FIELDS = [_Field("OBJECTID", "OID"), _Field("layer", "String"), _Field("dataset", "String"),
              _Field("data_year", "Integer"), _Field("accessed_date", "Date")]
DATASETS = {
    "C:/GIS/cm.gdb/deposits": {"type": "FeatureClass", "fields": FIELDS, "rows": ROWS},
    "C:/GIS/cm.gdb/plants": {"type": "FeatureClass", "fields": FIELDS, "rows": ROWS},
    "C:/GIS/cm.gdb/source_log": {"type": "Table", "fields": LOG_FIELDS, "rows": [
        {"OBJECTID": 1, "layer": "L1", "dataset": "Esri World Countries", "data_year": 2024,
         "accessed_date": None}]},
    "C:/GIS/cm.gdb/gaps_log": {"type": "Table", "fields": LOG_FIELDS, "rows": []},
    "C:/GIS/cm.gdb/unused_fc": {"type": "FeatureClass", "fields": FIELDS, "rows": []},
    "C:/GIS/cm.gdb/Stack/L6_flows": {"type": "FeatureClass", "fields": FIELDS, "rows": []},
}


def _src(obj):
    return obj if isinstance(obj, str) else obj.dataSource


def _rows(obj):
    return DATASETS.get(_src(obj), {"rows": ROWS})["rows"]


def _filter(where, rows=None):
    rows = ROWS if rows is None else rows
    if where:
        # Supports "FIELD = 'value'", optionally "(a) AND (b)"; enough for tests.
        for part in where.split(" AND "):
            if " IN (" in part:
                f, values = part.split(" IN (")
                wanted = [v.strip(" ()'") for v in values.split(",")]
                rows = [r for r in rows if str(r[f.strip(" ()")]) in wanted]
                continue
            f, v = [s.strip(" ()") for s in part.split("=")]
            rows = [r for r in rows if str(r[f]) == v.strip("'")]
    return rows


class SearchCursor(object):
    def __init__(self, lyr, fields, where_clause=None, sql_clause=None):
        if isinstance(lyr, Table) and STRICT["tables_by_name_fail"]:
            raise RuntimeError("cannot open '%s'" % lyr.name)
        dq = None if isinstance(lyr, str) else (lyr.definitionQuery or None)
        where = where_clause or dq
        self.rows = []
        # Real arcpy: a cursor on a layer sees only that layer's selection, if it has one.
        selection = getattr(lyr, "_selection", None)
        for r in _filter(where, _rows(lyr)):
            if selection and r["OBJECTID"] not in selection:
                continue
            self.rows.append(tuple((1.0, 2.0) if f == "SHAPE@XY" else r["OBJECTID"] if f == "OID@" else r[f] for f in fields))

    def __enter__(self):
        return iter(self.rows)

    def __exit__(self, *a):
        return False


class UpdateCursor(object):
    def __init__(self, path, fields, where_clause=None):
        self.ds = DATASETS[path]
        self.fields = fields
        self.matched = list(_filter(where_clause, self.ds["rows"]))
        self.current = None

    def __iter__(self):
        for r in self.matched:
            self.current = r
            yield r

    def updateRow(self, values):
        for f, v in zip(self.fields, values):
            if f == "explode":
                raise RuntimeError("edit failed")
            self.current[f] = v

    def deleteRow(self):
        self.ds["rows"].remove(self.current)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class InsertCursor(object):
    def __init__(self, path, fields):
        if path not in DATASETS:
            raise RuntimeError("cannot open '%s'" % path)
        self.ds, self.fields = DATASETS[path], fields

    def insertRow(self, values):
        rows = self.ds["rows"]
        oid = max([r["OBJECTID"] for r in rows] or [0]) + 1
        rec = {"OBJECTID": oid}
        rec.update(zip(self.fields, values))
        rows.append(rec)
        return oid

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


da = types.SimpleNamespace(SearchCursor=SearchCursor, InsertCursor=InsertCursor,
                           UpdateCursor=UpdateCursor)


STRICT = {"on": False, "gp_rejects_layer_objects": False, "tables_by_name_fail": False}


def _strict(obj):
    """Real Pro: layer objects outside the active map are looked up by name and fail."""
    if STRICT["on"] and isinstance(obj, Layer):
        raise OSError('"%s" does not exist' % obj.name)


def GetCount(lyr):
    _strict(lyr)
    dq = None if isinstance(lyr, str) else (lyr.definitionQuery or None)
    return [str(len(_filter(dq, _rows(lyr))))]




class Result(object):
    def __init__(self, outputs):
        self._outputs = outputs
        self.outputCount = len(outputs)

    def getOutput(self, i):
        return self._outputs[i]

    def getMessages(self):
        return "Succeeded"


LAST_GP_ERROR = ["ERROR 000800: The value is not a member of the list."]


@_tool("Buffer_analysis")
def Buffer(in_features, out_features, distance, *rest, **kw):
    if distance == "bad":
        LAST_GP_ERROR[0] = "ERROR 000800: The value is not a member of the list."
        raise ExecuteError("ERROR 000800")
    bad = isinstance(in_features, str) and not ("/" in in_features or "\\" in in_features)
    if (STRICT["gp_rejects_layer_objects"] and isinstance(in_features, Layer)
            and not isinstance(in_features, TempLayer)):
        bad = True
    if bad:
        LAST_GP_ERROR[0] = "ERROR 000732: Input Features: Dataset %s does not exist" % in_features
        raise ExecuteError("ERROR 000732")
    CALLS.append(in_features)
    return Result([out_features])


CALLS = []
TEMP_LAYERS = []


@_tool("MakeFeatureLayer_management")
def MakeFeatureLayer(source, name, where=None):
    lyr = TempLayer(name, source)
    lyr.definitionQuery = where or ""
    TEMP_LAYERS.append(name)
    return Result([lyr])


@_tool("Delete_management")
def Delete(name, data_type=None):
    if name in TEMP_LAYERS:
        TEMP_LAYERS.remove(name)
        return Result([True])
    SCHEMA_CALLS.append(("Delete", name))
    return Result([True])


SCHEMA_CALLS = []


@_tool("AddField_management")
def AddField(in_table, field_name, field_type, *rest, **kw):
    SCHEMA_CALLS.append(("AddField", in_table, field_name))
    return Result([in_table])


@_tool("CalculateField_management")
def CalculateField(in_table, field, expression, *rest, **kw):
    SCHEMA_CALLS.append(("CalculateField", in_table, field))
    return Result([in_table])


@_tool("JoinField_management")
def JoinField(in_data, in_field, join_table, join_field, *rest, **kw):
    SCHEMA_CALLS.append(("JoinField", in_data))
    return Result([in_data])


@_tool("DeleteRows_management")
def DeleteRows(in_rows):
    SCHEMA_CALLS.append(("DeleteRows", in_rows))
    return Result([in_rows])


@_tool("CopyFeatures_management")
def CopyFeatures(in_features, out_feature_class, *rest, **kw):
    SCHEMA_CALLS.append(("CopyFeatures", in_features, out_feature_class))
    return Result([out_feature_class])


@_tool("CalculateValue_management")
def CalculateValue(expression, code_block=None, data_type=None):
    SCHEMA_CALLS.append(("CalculateValue", expression))
    return Result([expression])


@_tool("SharePackage_management")
def SharePackage(in_package, *rest, **kw):
    SCHEMA_CALLS.append(("SharePackage", in_package))
    return Result([True])


@_tool("TrainDeepLearningModel_ia")
def TrainDeepLearningModel(*args, **kw):
    SCHEMA_CALLS.append(("TrainDeepLearningModel",))
    return Result([True])


@_tool("CreateTable_management")
def CreateTable(path, name, *rest, **kw):
    DATASETS["%s/%s" % (path, name)] = {"type": "Table", "fields": [_Field("OBJECTID", "OID")],
                                        "rows": []}
    return Result(["%s/%s" % (path, name)])


@_tool("CreateFeatureclass_management")
def CreateFeatureclass(path, name, kind, *rest, **kw):
    DATASETS["%s/%s" % (path, name)] = {"type": "FeatureClass",
                                        "fields": [_Field("OBJECTID", "OID"),
                                                   _Field("Shape", "Geometry")], "rows": []}
    return Result(["%s/%s" % (path, name)])


@_tool("AddFields_management")
def AddFields(path, spec, *rest, **kw):
    for s in spec:
        DATASETS[path]["fields"] = DATASETS[path]["fields"] + [_Field(s[0], s[1])]
    return Result([path])


@_tool("SelectLayerByLocation_management")
def SelectLayerByLocation(layer, overlap, other, distance=None):
    layer.definitionQuery = "STATE = 'WY'"  # stands in for a spatial result: two rows
    return Result([layer])


@_tool("DeleteMultiple_management")
def DeleteMultiple(in_data):
    SCHEMA_CALLS.append(("DeleteMultiple", in_data))
    return Result([True])


@_tool("Near_analysis")
def Near(in_features, near_features):
    SCHEMA_CALLS.append(("Near", in_features))
    return Result([in_features])


@_tool("Clip_analysis")
def Clip(in_features, clip_features, out_feature_class):
    SCHEMA_CALLS.append(("Clip", out_feature_class))
    return Result([out_feature_class])


@_tool("CreateFileGDB_management")
def CreateFileGDB(out_folder_path, out_name):
    SCHEMA_CALLS.append(("CreateFileGDB", out_name))
    return Result([out_name])


@_tool("AddImagesFromCustomInputType_oi")
def AddImagesFromCustomInputType(*args, **kw):
    SCHEMA_CALLS.append(("AddImagesFromCustomInputType",))
    return Result([True])


@_tool("ExportFeatures_conversion")
def ExportFeatures(in_features, out_features, *rest, **kw):
    LAST_GP_ERROR[0] = "ERROR 000464: Cannot get exclusive schema lock."
    raise ExecuteError("ERROR 000464")


management = types.SimpleNamespace(GetCount=GetCount, MakeFeatureLayer=MakeFeatureLayer,
                                   Delete=Delete, AddField=AddField, AddFields=AddFields,
                                   CalculateField=CalculateField, JoinField=JoinField,
                                   DeleteRows=DeleteRows, CopyFeatures=CopyFeatures,
                                   CalculateValue=CalculateValue, SharePackage=SharePackage,
                                   CreateTable=CreateTable, CreateFeatureclass=CreateFeatureclass,
                                   SelectLayerByLocation=SelectLayerByLocation,
                                   DeleteMultiple=DeleteMultiple, CreateFileGDB=CreateFileGDB)
conversion = types.SimpleNamespace(ExportFeatures=ExportFeatures)
ia = types.SimpleNamespace(TrainDeepLearningModel=TrainDeepLearningModel)
oi = types.SimpleNamespace(AddImagesFromCustomInputType=AddImagesFromCustomInputType)


analysis = types.SimpleNamespace(Buffer=Buffer, Near=Near, Clip=Clip)
env = types.SimpleNamespace(addOutputsToMap=True, overwriteOutput=False, workspace=None)


def _gp_token():
    raise AssertionError("gp.getSigninToken must not be reachable")


gp = types.SimpleNamespace(getSigninToken=_gp_token)  # real arcpy exposes gp, and itself
arcpy = sys.modules[__name__]
toolbox = types.SimpleNamespace(Delete_management=Delete)


def _param(name, direction="Input", kind="Required"):
    return types.SimpleNamespace(name=name, direction=direction, parameterType=kind)


PARAMS = {
    "copyfeatures_management": [_param("in_features"), _param("out_feature_class", "Output")],
    "buffer_analysis": [_param("in_features"), _param("out_feature_class", "Output"),
                        _param("buffer_distance_or_field")],
    "delete_management": [_param("in_data"), _param("data_type", kind="Optional"),
                          _param("out_results", "Output", "Derived")],
    "deleterows_management": [_param("in_rows"), _param("out_table", "Output", "Derived")],
    "addfield_management": [_param("in_table"), _param("field_name"), _param("field_type")],
    "deletemultiple_management": [_param("in_data"), _param("out_results", "Output", "Derived")],
    "near_analysis": [_param("in_features"), _param("near_features"),
                      _param("out_feature_class", "Output", "Derived")],
    "clip_analysis": [_param("in_features"), _param("clip_features"),
                      _param("out_feature_class", "Output")],
    "createfilegdb_management": [_param("out_folder_path"), _param("out_name"),
                                 _param("out_file_gdb", "Output", "Derived")],
}


def GetParameterInfo(name):
    return PARAMS[name.lower()]
LOCKED = set()  # dataset paths TestSchemaLock reports as locked
MEMORY_PREFIXES = ("memory/", "in_memory/")


class Point(object):
    def __init__(self, x, y):
        self.X, self.Y = x, y


def Array(points):
    return list(points)


def Polygon(arr):
    return ("polygon", arr)


def ListFields(lyr):
    _strict(lyr)
    return DATASETS.get(_src(lyr), {"fields": FIELDS})["fields"]


def Describe(obj):
    _strict(obj)
    src = _src(obj)
    if isinstance(obj, str) and "/" not in src and "\\" not in src:
        if src not in TEMP_VIEWS:
            raise OSError('"%s" does not exist' % src)
        src = TEMP_VIEWS[src]
    if str(src).replace("\\", "/").rstrip("/").lower() in FOLDERS:
        return types.SimpleNamespace(dataType="Folder", catalogPath=src)
    kind = DATASETS.get(src, {"type": "FeatureClass"})["type"]
    if kind == "Table":
        return types.SimpleNamespace(datasetType="Table", dataType="Table", catalogPath=src)
    return types.SimpleNamespace(datasetType="FeatureClass", dataType="FeatureClass",
                                 catalogPath=src, shapeType="Point",
                                 spatialReference=SpatialReference(), extent=Extent())


TEMP_VIEWS = {}  # temporary layer or view name -> the dataset it reads
FOLDERS = set()  # lower-case paths Describe reports as folders


def Exists(path):
    p = str(path).replace("\\", "/")
    return p in DATASETS or p == "C:/GIS/cm.gdb" or p.lower().startswith(MEMORY_PREFIXES)


def TestSchemaLock(path):
    p = str(path).replace("\\", "/")
    if p.lower().startswith(MEMORY_PREFIXES):
        return False  # real Pro 3.5: False for every memory dataset, even an unused new one
    return p not in LOCKED


def _in_ws(feature_dataset=None):
    ws = env.workspace.replace("\\", "/").rstrip("/")
    base = ws + "/" + feature_dataset if feature_dataset else ws
    out = []
    for p, d in DATASETS.items():
        head, _, name = p.rpartition("/")
        if head == base:
            out.append((name, d))
    return out


def ListFeatureClasses(wild_card=None, feature_type=None, feature_dataset=None):
    return [n for n, d in _in_ws(feature_dataset) if d["type"] == "FeatureClass"]


def ListTables(wild_card=None, table_type=None):
    return [n for n, d in _in_ws() if d["type"] == "Table"]


def ListRasters(wild_card=None, raster_type=None):
    return []


def ListDatasets(wild_card=None, feature_type=None):
    ws = env.workspace.replace("\\", "/").rstrip("/") + "/"
    return sorted(set(p[len(ws):].split("/")[0] for p in DATASETS
                      if p.startswith(ws) and p[len(ws):].count("/") == 1))


def Usage(name):
    return "%s(in_features, out_feature_class, buffer_distance_or_field, ...)" % name


def GetMessages(severity=0):
    return LAST_GP_ERROR[0]


def GetInstallInfo():
    return {"ProductName": "ArcGISPro", "Version": "3.5"}


def GetActivePortalURL():
    raise RuntimeError("not signed in")


def GetSigninToken():
    return None
