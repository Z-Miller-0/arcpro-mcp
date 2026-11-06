# -*- coding: utf-8 -*-
"""ArcGIS Pro toolbox that starts and stops the arcpro-mcp bridge (live mode).

Add it once: Catalog pane > Toolboxes > right-click > Add Toolbox > this file. To have it
in every project, right-click it afterwards > Add To Favorites (or add it to a project
template). Then run "Start Bridge" from the Geoprocessing pane instead of pasting code into
the Python window.

The tools load arcpro_bridge.py from the same folder as this .pyt, so keep the two files
together. Keep this file compatible with Python 3.9.
"""

import builtins
import importlib.util
import os

import arcpy

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_PY = os.path.join(HERE, "arcpro_bridge.py")


def _bridge():
    spec = importlib.util.spec_from_file_location("arcpro_mcp_bridge", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # __name__ is not "__main__", so nothing auto-starts
    return mod


class Toolbox(object):
    def __init__(self):
        self.label = "ArcPro MCP"
        self.alias = "arcpromcp"
        self.tools = [StartBridge, StopBridge, BridgeStatus]


class StartBridge(object):
    def __init__(self):
        self.label = "Start Bridge"
        self.description = ("Starts the arcpro-mcp bridge for the open project so Claude "
                            "can work in it. Restarts it if it is already running.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        allow = arcpy.Parameter(displayName="Allow run_arcpy (arbitrary Python)",
                                name="allow_exec", datatype="GPBoolean",
                                parameterType="Optional", direction="Input")
        allow.value = False
        port = arcpy.Parameter(displayName="Port", name="port", datatype="GPLong",
                               parameterType="Optional", direction="Input")
        port.value = 18760
        return [allow, port]

    def execute(self, parameters, messages):
        allow = bool(parameters[0].value)
        port = int(parameters[1].value or 18760)
        mod = _bridge()
        server = mod.start("CURRENT", port=port, allow_exec=allow, background=True, quiet=True)
        arcpy.AddMessage("arcpro-mcp bridge %s listening on 127.0.0.1:%s, run_arcpy %s, "
                         "project: %s" % (mod.BRIDGE_VERSION, server.server_address[1],
                                          "ON" if allow else "off", server.bridge.aprx.filePath))
        arcpy.AddMessage("Leave Pro open. Run Stop Bridge, or close Pro, to stop it.")


class StopBridge(object):
    def __init__(self):
        self.label = "Stop Bridge"
        self.description = "Stops the arcpro-mcp bridge."
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def execute(self, parameters, messages):
        server = getattr(builtins, "_arcpro_mcp_bridge", None)
        if server is None:
            arcpy.AddMessage("The bridge is not running.")
            return
        server.shutdown()
        server.server_close()
        builtins._arcpro_mcp_bridge = None
        arcpy.AddMessage("arcpro-mcp bridge stopped.")


class BridgeStatus(object):
    def __init__(self):
        self.label = "Bridge Status"
        self.description = "Reports whether the arcpro-mcp bridge is running."
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def execute(self, parameters, messages):
        server = getattr(builtins, "_arcpro_mcp_bridge", None)
        if server is None:
            arcpy.AddMessage("The bridge is not running.")
            return
        info = server.bridge.ping()
        for k in ("bridge_version", "mode", "project", "run_arcpy_enabled", "busy"):
            arcpy.AddMessage("%s: %s" % (k, info.get(k)))
        arcpy.AddMessage("port: %s" % server.server_address[1])
