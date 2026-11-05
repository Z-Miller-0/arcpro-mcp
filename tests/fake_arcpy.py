"""A tiny stand-in for arcpy so the bridge and server can be tested without ArcGIS Pro.

It mimics only the slices of the arcpy API the bridge touches. Passing these tests
shows the plumbing works; it does not prove every arcpy call behaves the same in Pro.
"""

import types

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
    def __init__(self, name, source="C:/GIS/cm.gdb/deposits"):
        self.name = name
        self.visible = True
        self.dataSource = source
        self.definitionQuery = ""
        self.transparency = 0
        self.isFeatureLayer = True
        self.isRasterLayer = False
        self.isGroupLayer = False
        self.isBasemapLayer = False

    @property
    def longName(self):  # like Pro, follows renames
        return self.name

    def supports(self, prop):
        return prop in ("DATASOURCE", "DEFINITIONQUERY", "VISIBLE", "TRANSPARENCY")


class TempLayer(Layer):
    """Stands in for a layer made by MakeFeatureLayer (GP accepts it)."""


class Map(object):
    def __init__(self, name):
        self.name = name
        self._layers = []

    def listLayers(self, wildcard=None):
        return list(self._layers)

    def addDataFromPath(self, path):
        lyr = Layer(path.replace("\\", "/").rstrip("/").split("/")[-1], path)
        self._layers.insert(0, lyr)
        return lyr

    def removeLayer(self, lyr):
        self._layers.remove(lyr)

    def openView(self):
        PROJECTS[-1].activeView = MapView(self)


class Layout(object):
    def __init__(self, name):
        self.name = name

    def exportToPNG(self, path, resolution=96):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-layout")

    def exportToPDF(self, path, resolution=300):
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")


class _Camera(object):
    def setExtent(self, ext):
        self.extent = ext


class MapView(object):
    def __init__(self, map_=None):
        self.camera = _Camera()
        self.map = map_

    def getLayerExtent(self, lyr, selection_only=False, symbolized=True):
        return Extent()

    def exportToPNG(self, path, width=None, height=None, world_file=False):
        assert not world_file, "world_file must not be set"
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-view")


class SpatialReference(object):
    name = "WGS_1984_Web_Mercator_Auxiliary_Sphere"


class Extent(object):
    def __init__(self, XMin=-13000000.0, YMin=3000000.0, XMax=-11000000.0, YMax=5500000.0,
                 spatial_reference=None):
        self.XMin, self.YMin, self.XMax, self.YMax = XMin, YMin, XMax, YMax
        self.spatialReference = spatial_reference or SpatialReference()


class MapFrame(object):
    def __init__(self, m):
        self.map = m
        self.camera = _Camera()

    def getLayerExtent(self, lyr, selection_only=False, symbolized=True):
        return Extent()

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
        self._maps.append(m)
        return m

    def createLayout(self, w, h, units, name=None):
        lyt = Layout(name or "Layout")
        lyt.createMapFrame = lambda geom, m, name=None: MapFrame(m)
        self._layouts.append(lyt)
        return lyt

    def deleteItem(self, item):
        self._layouts.remove(item)

    def save(self):
        self.saved += 1

    def saveACopy(self, path):
        pass


mp = types.SimpleNamespace(ArcGISProject=ArcGISProject)


def _filter(where):
    rows = ROWS
    if where:
        # Supports "FIELD = 'value'" only; enough for tests.
        f, v = [s.strip() for s in where.split("=")]
        rows = [r for r in rows if str(r[f]) == v.strip("'")]
    return rows


class SearchCursor(object):
    def __init__(self, lyr, fields, where_clause=None, sql_clause=None):
        where = where_clause or lyr.definitionQuery or None
        self.rows = []
        for r in _filter(where):
            self.rows.append(tuple((1.0, 2.0) if f == "SHAPE@XY" else r["OBJECTID"] if f == "OID@" else r[f] for f in fields))

    def __enter__(self):
        return iter(self.rows)

    def __exit__(self, *a):
        return False


da = types.SimpleNamespace(SearchCursor=SearchCursor)


STRICT = {"on": False, "gp_rejects_layer_objects": False}


def _strict(obj):
    """Real Pro: layer objects outside the active map are looked up by name and fail."""
    if STRICT["on"] and isinstance(obj, Layer):
        raise OSError('"%s" does not exist' % obj.name)


def GetCount(lyr):
    _strict(lyr)
    return [str(len(_filter(lyr.definitionQuery or None)))]




class Result(object):
    def __init__(self, outputs):
        self._outputs = outputs
        self.outputCount = len(outputs)

    def getOutput(self, i):
        return self._outputs[i]

    def getMessages(self):
        return "Succeeded"


LAST_GP_ERROR = ["ERROR 000800: The value is not a member of the list."]


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


def MakeFeatureLayer(source, name, where=None):
    lyr = TempLayer(name, source)
    lyr.definitionQuery = where or ""
    TEMP_LAYERS.append(name)
    return Result([lyr])


def Delete(name):
    TEMP_LAYERS.remove(name)


management = types.SimpleNamespace(GetCount=GetCount, MakeFeatureLayer=MakeFeatureLayer,
                                   Delete=Delete)


analysis = types.SimpleNamespace(Buffer=Buffer)
env = types.SimpleNamespace(addOutputsToMap=True)


def ListFields(lyr):
    _strict(lyr)
    return FIELDS


def Describe(obj):
    _strict(obj)
    return types.SimpleNamespace(datasetType="FeatureClass", shapeType="Point",
                                 spatialReference=SpatialReference(), extent=Extent())


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
