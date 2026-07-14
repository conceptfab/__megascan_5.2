# ##### QUIXEL AB - MEGASCANS PLugin FOR BLENDER #####
#
# The Megascans Plugin  plugin for Blender is an add-on that lets
# you instantly import assets with their shader setup with one click only.
#
# Because it relies on some of the latest 2.80 features, this plugin is currently
# only available for Blender 2.80 and forward.
#
# You are free to modify, add features or tweak this add-on as you see fit, and
# don't hesitate to send us some feedback if you've done something cool with it.
#
# ##### QUIXEL AB - MEGASCANS PLUGIN FOR BLENDER #####

import bpy, os, time, json, socket, logging
from collections import deque

globals()['Megascans_DataSet'] = None

# Blender 4.0 renamed several Principled BSDF sockets.
if bpy.app.version >= (4, 0, 0):
    SPECULAR_INPUT = "Specular IOR Level"
    CLEARCOAT_INPUT = "Coat Weight"
    TRANSMISSION_INPUT = "Transmission Weight"
else:
    SPECULAR_INPUT = "Specular"
    CLEARCOAT_INPUT = "Clearcoat"
    TRANSMISSION_INPUT = "Transmission"

# This stuff is for the Alembic support
globals()['MG_Material'] = []
globals()['MG_AlembicPath'] = []
globals()['MG_ImportComplete'] = False

# ---------------------------------------------------------------------------
# LiveLink runtime state — single source of truth for the UI panel.
# The transport is threadless: a non-blocking socket serviced from a
# bpy.app.timers pump on the main thread (nothing that can die silently).
# ---------------------------------------------------------------------------

STATUS_STOPPED = 'STOPPED'
STATUS_LISTENING = 'LISTENING'
STATUS_PORT_BUSY = 'PORT_BUSY'
STATUS_RELEASED = 'RELEASED'   # port was claimed by another instance

DEFAULT_PORT = 28888
PUMP_INTERVAL = 0.2
BIND_RETRY_INTERVAL = 2.0
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024

class _LiveLinkState:
    def __init__(self):
        self.status = STATUS_STOPPED
        self.port = DEFAULT_PORT
        self.last_asset = ""
        self.last_asset_time = ""
        self.last_error = ""
        self.events = deque(maxlen=50)
        self.server = None        # listening socket, or None
        self.connections = []     # [socket, bytearray] pairs being received
        self.queue = deque()      # complete raw payloads waiting for import
        self.claimed_away = False # True after another instance claimed the port
        self.last_bind_retry = 0.0

STATE = _LiveLinkState()
_LOGGER = None

def _log_dir():
    return bpy.utils.user_resource('CONFIG', path="MSPlugin_logs", create=True)

def _get_logger():
    # Per-process log file (PID suffix): two Blender instances logging
    # concurrently on Windows is a first-class supported scenario.
    global _LOGGER
    if _LOGGER is None:
        logger = logging.getLogger("MSPlugin")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                path = os.path.join(_log_dir(), "msplugin_%d.log" % os.getpid())
                handler = logging.FileHandler(path, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
                logger.addHandler(handler)
            except Exception:
                pass
        _LOGGER = logger
    return _LOGGER

def log_event(level, message):
    STATE.events.append("%s [%s] %s" % (time.strftime("%H:%M:%S"), level, message))
    if level in ("ERROR", "WARNING"):
        STATE.last_error = message
    try:
        py_level = {"ERROR": logging.ERROR, "WARNING": logging.WARNING}.get(level, logging.INFO)
        _get_logger().log(py_level, message)
    except Exception:
        pass
    print("MSPlugin [%s] %s" % (level, message))
    _redraw_panels()

def _redraw_panels():
    if bpy.app.background:
        return
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass

def _notify_error(message):
    # Loud-failure policy: popup on total failure, at most one per payload.
    if bpy.app.background:
        return
    def draw(menu, context):
        menu.layout.label(text=message)
    try:
        bpy.context.window_manager.popup_menu(draw, title="Megascans Plugin", icon='ERROR')
    except Exception:
        pass

def _get_port():
    try:
        return bpy.context.preferences.addons[__name__].preferences.port
    except Exception:
        return DEFAULT_PORT

# --------------------------- transport ------------------------------------

def start_listener(quiet=False):
    if STATE.server is not None:
        return True
    port = _get_port()
    STATE.port = port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Deliberately no SO_REUSEADDR: on Windows it would let two instances
        # bind the same port and break conflict detection.
        sock.bind(('localhost', port))
        sock.listen(5)
        sock.setblocking(False)
        STATE.server = sock
        STATE.status = STATUS_LISTENING
        STATE.claimed_away = False
        log_event("INFO", "LiveLink listening on port %d" % port)
        return True
    except OSError as e:
        try:
            sock.close()
        except OSError:
            pass
        in_use = getattr(e, 'winerror', None) == 10048 or getattr(e, 'errno', None) in (48, 98, 10048)
        if in_use:
            if STATE.status != STATUS_PORT_BUSY:
                STATE.status = STATUS_PORT_BUSY
                if not quiet:
                    log_event("WARNING", "Port %d is busy — is another Blender instance running? Imports go to the instance that owns the port." % port)
        else:
            STATE.status = STATUS_STOPPED
            if not quiet:
                log_event("ERROR", "LiveLink failed to start: %s" % e)
        return False

def stop_listener(reason=""):
    for entry in STATE.connections:
        try:
            entry[0].close()
        except OSError:
            pass
    STATE.connections = []
    if STATE.server is not None:
        try:
            STATE.server.close()
        except OSError:
            pass
        STATE.server = None
    if STATE.status != STATUS_RELEASED:
        STATE.status = STATUS_STOPPED
    if reason:
        log_event("INFO", reason)

def _service_sockets():
    if STATE.server is None:
        return
    # Accept eagerly every tick — Bridge must never see a refused connection
    # just because an import is queued.
    while True:
        try:
            client, _addr = STATE.server.accept()
            client.setblocking(False)
            STATE.connections.append([client, bytearray()])
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            break
    finished = []
    for entry in STATE.connections:
        sock, buf = entry
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    finished.append(entry)  # peer closed -> payload complete
                    break
                buf += chunk
                if len(buf) > MAX_PAYLOAD_BYTES:
                    log_event("ERROR", "Incoming payload exceeded %d MB — dropped." % (MAX_PAYLOAD_BYTES // (1024 * 1024)))
                    buf.clear()
                    finished.append(entry)
                    break
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            finished.append(entry)
    for entry in finished:
        sock, buf = entry
        try:
            sock.close()
        except OSError:
            pass
        if entry in STATE.connections:
            STATE.connections.remove(entry)
        data = bytes(buf)
        if not data:
            continue
        if data == b'Bye Megascans':
            # Another instance claimed the port. Release it VISIBLY and do not
            # auto-rebind (no claim ping-pong) — user can press Reclaim.
            stop_listener()
            STATE.status = STATUS_RELEASED
            STATE.claimed_away = True
            log_event("WARNING", "LiveLink released — claimed by another instance.")
            continue
        STATE.queue.append(data)
        log_event("INFO", "Payload received (%d bytes), %d in queue." % (len(data), len(STATE.queue)))

def _maybe_retry_bind():
    # Live port-preference change: rebind on the new port.
    if STATE.server is not None and STATE.port != _get_port():
        stop_listener("Port preference changed — rebinding.")
        start_listener()
        return
    # Startup conflict self-heal: passively retry while Port-busy. Never after
    # being claimed (STATUS_RELEASED) — that would steal the port back.
    if STATE.server is None and STATE.status == STATUS_PORT_BUSY and not STATE.claimed_away:
        now = time.time()
        if now - STATE.last_bind_retry >= BIND_RETRY_INTERVAL:
            STATE.last_bind_retry = now
            start_listener(quiet=True)

def _import_payload(data):
    try:
        assets = json.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        log_event("ERROR", "Received payload is not valid Bridge JSON: %s" % e)
        _notify_error("Megascans: received invalid data — see Megascans panel")
        return
    if not isinstance(assets, list):
        assets = [assets]
    ok, failed = [], []
    for asset in assets:
        name = asset.get("name", asset.get("id", "?")) if isinstance(asset, dict) else "?"
        try:
            globals()['Megascans_DataSet'] = json.dumps([asset])
            importer = MS_Init_ImportProcess()
            if importer.import_error:
                failed.append((name, importer.import_error))
            else:
                ok.append(name)
        except Exception as e:
            failed.append((name, str(e)))
            log_event("ERROR", "Failed to import %s: %s" % (name, e))
    # Aggregate per payload: one summary, never one popup per asset.
    if failed and not ok:
        msg = "Megascans: all %d asset(s) failed to import — see Megascans panel" % len(failed)
        log_event("ERROR", msg)
        _notify_error(msg)
    elif failed:
        log_event("WARNING", "Megascans: imported %d, failed %d — see log for details." % (len(ok), len(failed)))
    elif ok:
        log_event("INFO", "Megascans: imported %d asset(s)." % len(ok))
    if ok:
        STATE.last_asset = ok[-1]
        STATE.last_asset_time = time.strftime("%H:%M:%S")
        if not failed:
            STATE.last_error = ""  # error display clears on next clean import

def _pump():
    try:
        _service_sockets()
        if STATE.queue:
            _import_payload(STATE.queue.popleft())
        _maybe_retry_bind()
    except Exception as e:
        log_event("ERROR", "LiveLink pump error: %s" % e)
    return PUMP_INTERVAL

bl_info = {
    "name": "Megascans Plugin",
    "description": "Connects Blender to Quixel Bridge for one-click imports with shader setup and geometry. Updated for Blender 4.x/5.2.",
    "author": "CONCEPTFAB (original: Quixel)",
    "version": (3, 9, 0),
    "blender": (4, 0, 0),
    "location": "File > Import",
    "warning": "", # used for warning icon and text in addons panel
    "wiki_url": "https://docs.quixel.org/bridge/livelinks/blender/info_quickstart.html",
    "tracker_url": "https://docs.quixel.org/bridge/livelinks/blender/info_quickstart#release_notes",
    "support": "COMMUNITY",
    "category": "Import-Export"
}


# MS_Init_ImportProcess is the main asset import class.
# This class is invoked whenever a new asset is set from Bridge.

class MS_Init_ImportProcess():

    # This initialization method create the data structure to process our assets
    # later on in the initImportProcess method. The method loops on all assets
    # that have been sent by Bridge.
    def __init__(self):
        self.import_error = None
        try:
            # Check if there's any incoming data
            if globals()['Megascans_DataSet'] != None:

                globals()['MG_AlembicPath'] = []
                globals()['MG_Material'] = []
                globals()['MG_ImportComplete'] = False

                self.json_Array = json.loads(globals()['Megascans_DataSet'])

                # Start looping over each asset in the self.json_Array list
                for js in self.json_Array:

                    self.json_data = js

                

                    self.selectedObjects = []
                    
                    self.IOR = 1.45
                    self.assetType = self.json_data["type"]
                    self.assetPath = self.json_data["path"]
                    self.assetID = self.json_data["id"]
                    self.isMetal = bool(self.json_data["category"] == "Metal")
                    # Workflow setup.
                    self.isHighPoly = bool(self.json_data["activeLOD"] == "high")
                    self.activeLOD = self.json_data["activeLOD"]
                    self.minLOD = self.json_data["minLOD"]
                    self.RenderEngine = bpy.context.scene.render.engine.lower() # Get the current render engine. i.e. blender_eevee or cycles
                    self.Workflow = self.json_data.get('pbrWorkflow', 'specular')
                    self.DisplacementSetup = 'regular'
                    self.isCycles = bool(self.RenderEngine == 'cycles')
                    self.isScatterAsset = self.CheckScatterAsset()
                    self.textureList = []
                    self.isBillboard = self.CheckIsBillboard()
                    self.ApplyToSelection = False
                    self.isSpecularWorkflow = True
                    self.isAlembic = False

                    self.NormalSetup = False
                    self.BumpSetup = False

                    if "workflow" in self.json_data.keys():
                        self.isSpecularWorkflow = bool(self.json_data["workflow"] == "specular")

                    if "applyToSelection" in self.json_data.keys():
                        self.ApplyToSelection = bool(self.json_data["applyToSelection"])

                    if (self.isCycles):
                        # scene.cycles.feature_set was removed in Blender 5.x - displacement no longer needs the experimental set there.
                        if(getattr(bpy.context.scene.cycles, "feature_set", "EXPERIMENTAL") == 'EXPERIMENTAL'):
                            self.DisplacementSetup = 'adaptive'
                    
                    texturesListName = "components"
                    if(self.isBillboard):
                        texturesListName = "components"

                    # Get a list of all available texture maps. item[1] returns the map type (albedo, normal, etc...).
                    self.textureTypes = [obj["type"] for obj in self.json_data[texturesListName]]
                    self.textureList = []

                    for obj in self.json_data[texturesListName]:
                        texFormat = obj["format"]
                        texType = obj["type"]
                        texPath = obj["path"]

                        if texType == "displacement" and texFormat != "exr":
                            texDir = os.path.dirname(texPath)
                            texName = os.path.splitext(os.path.basename(texPath))[0]

                            if os.path.exists(os.path.join(texDir, texName + ".exr")):
                                texPath = os.path.join(texDir, texName + ".exr")
                                texFormat = "exr"
                        # Replace diffuse texture type with albedo so we don't have to add more conditions to handle diffuse map.
                        if texType == "diffuse" and "albedo" not in self.textureTypes:
                            texType = "albedo"
                            self.textureTypes.append("albedo")
                            self.textureTypes.remove("diffuse")

                        # Normal / Bump setup checks
                        if texType == "normal":
                            self.NormalSetup = True
                        if texType == "bump":
                            self.BumpSetup = True

                        self.textureList.append((texFormat, texType, texPath))

                    # Create a tuple list of all the 3d meshes  available.
                    # This tuple is composed of (meshFormat, meshPath)
                    self.geometryList = [(obj["format"], obj["path"]) for obj in self.json_data["meshList"]]

                    # Create name of our asset. Multiple conditions are set here
                    # in order to make sure the asset actually has a name and that the name
                    # is short enough for us to use it. We compose a name with the ID otherwise.
                    if "name" in self.json_data.keys():
                        self.assetName = self.json_data["name"].replace(" ", "_")
                    else:
                        self.assetName = os.path.basename(self.json_data["path"]).replace(" ", "_")
                    if len(self.assetName.split("_")) > 2:
                        self.assetName = "_".join(self.assetName.split("_")[:-1])

                    self.materialName = self.assetName + '_' + self.assetID
                    self.colorSpaces = ["sRGB", "Non-Color", "Linear"]

                    # Initialize the import method to start building our shader and import our geometry
                    self.initImportProcess()
                    if not self.import_error:
                        log_event("INFO", "Imported asset %s from Quixel Bridge" % self.assetName)

            if len(globals()['MG_AlembicPath']) > 0:
                globals()['MG_ImportComplete'] = True
        except Exception as e:
            self.import_error = str(e)
            log_event("ERROR", "Import process failed: %s" % e)

        globals()['Megascans_DataSet'] = None
    
    # this method is used to import the geometry and create the material setup.
    def initImportProcess(self):
        try:
            if len(self.textureList) >= 1:
                
                if(self.ApplyToSelection and self.assetType not in ["3dplant", "3d"]):
                    self.CollectSelectedObjects()

                self.ImportGeometry()
                self.CreateMaterial()
                self.ApplyMaterialToGeometry()
                if(self.isScatterAsset and len(self.selectedObjects) > 1):
                    self.ScatterAssetSetup()
                elif (self.assetType == "3dplant" and len(self.selectedObjects) > 1):
                    self.PlantAssetSetup()

                self.SetupMaterial()

                if self.isAlembic:
                    globals()['MG_Material'].append(self.mat)

        except Exception as e:
            self.import_error = str(e)
            log_event("ERROR", "Error importing %s (textures/geometry/material): %s" % (getattr(self, 'assetName', '?'), e))

    def ImportGeometry(self):
        try:
            # Import geometry
            abcPaths = []
            if len(self.geometryList) >= 1:
                for obj in self.geometryList:
                    meshPath = obj[1]
                    meshFormat = obj[0]

                    if meshFormat.lower() == "fbx":
                        bpy.ops.import_scene.fbx(filepath=meshPath)
                        # get selected objects
                        obj_objects = [ o for o in bpy.context.scene.objects if o.select_get() ]
                        self.selectedObjects += obj_objects

                    elif meshFormat.lower() == "obj":
                        if bpy.app.version >= (4, 0, 0):
                            # import_scene.obj (python importer) was removed in Blender 4.0.
                            bpy.ops.wm.obj_import(filepath=meshPath, use_split_objects = True, use_split_groups = True, clamp_size = 1.0)
                        elif bpy.app.version < (2, 92, 0):
                            bpy.ops.import_scene.obj(filepath=meshPath, use_split_objects = True, use_split_groups = True, global_clight_size = 1.0)
                        else:
                            bpy.ops.import_scene.obj(filepath=meshPath, use_split_objects = True, use_split_groups = True, global_clamp_size  = 1.0)
                        # get selected objects
                        obj_objects = [ o for o in bpy.context.scene.objects if o.select_get() ]
                        self.selectedObjects += obj_objects

                    elif meshFormat.lower() == "abc":
                        self.isAlembic = True
                        abcPaths.append(meshPath)
            
            if self.isAlembic:
                globals()['MG_AlembicPath'].append(abcPaths)
        except Exception as e:
            self.import_error = str(e)
            log_event("ERROR", "Error importing geometry for %s: %s" % (getattr(self, 'assetName', '?'), e))

    def dump(self, obj):
        for attr in dir(obj):
            print("obj.%s = %r" % (attr, getattr(obj, attr)))

    def CollectSelectedObjects(self):
        try:
            sceneSelectedObjects = [ o for o in bpy.context.scene.objects if o.select_get() ]
            for obj in sceneSelectedObjects:
                if obj.type == "MESH":
                    self.selectedObjects.append(obj)
        except Exception as e:
            print("Megascans Plugin Error::CollectSelectedObjects::", str(e) )

    def ApplyMaterialToGeometry(self):
        for obj in self.selectedObjects:
            # assign material to obj
            obj.active_material = self.mat

    def CheckScatterAsset(self):
        if('scatter' in self.json_data['categories'] or 'scatter' in self.json_data['tags'] or 'cmb_asset' in self.json_data['categories'] or 'cmb_asset' in self.json_data['tags']):
            return True
        return False

    def CheckIsBillboard(self):
        # Use billboard textures if importing the Billboard LOD.
        if(self.assetType == "3dplant"):
            if (self.activeLOD == self.minLOD):
                return True
        return False

    #Add empty parent for the scatter assets.
    def ScatterAssetSetup(self):
        bpy.ops.object.empty_add(type='ARROWS')
        emptyRefList = [ o for o in bpy.context.scene.objects if o.select_get() and o not in self.selectedObjects ]
        for scatterParentObject in emptyRefList:
            scatterParentObject.name = self.assetID + "_" + self.assetName
            for obj in self.selectedObjects:
                obj.parent = scatterParentObject
            break
    
    #Add empty parent for plants.
    def PlantAssetSetup(self):
        bpy.ops.object.empty_add(type='ARROWS')
        emptyRefList = [ o for o in bpy.context.scene.objects if o.select_get() and o not in self.selectedObjects ]
        for plantParentObject in emptyRefList:
            plantParentObject.name = self.assetID + "_" + self.assetName
            for obj in self.selectedObjects:
                obj.parent = plantParentObject
            break

    # def AddModifiersToGeomtry(self, geo_list, mat):
    #     for obj in geo_list:
    #         # assign material to obj
    #         bpy.ops.object.modifier_add(type='SOLIDIFY')

    #Shader setups for all asset types. Some type specific functionality is also handled here.
    def SetupMaterial (self):
        if "albedo" in self.textureTypes:
            if "ao" in self.textureTypes:
                self.CreateTextureMultiplyNode("albedo", "ao", -250, 320, -640, 460, -640, 200, 0, 1, True, "Base Color")
            else:
                self.CreateTextureNode("albedo", -640, 420, 0, True, "Base Color")
        
        if self.isSpecularWorkflow:
            if "specular" in self.textureTypes:
                self.CreateTextureNode("specular", -1150, 200, 0, True, SPECULAR_INPUT)
            
            if "gloss" in self.textureTypes:
                glossNode = self.CreateTextureNode("gloss", -1150, -60)
                invertNode = self.CreateGenericNode("ShaderNodeInvert", -250, 60)
                # Add glossNode to invertNode connection
                self.mat.node_tree.links.new(invertNode.inputs["Color"], glossNode.outputs["Color"])
                # Connect roughness node to the material parent node.
                self.mat.node_tree.links.new(self.nodes.get(self.parentName).inputs["Roughness"], invertNode.outputs["Color"])
            elif "roughness" in self.textureTypes:
                self.CreateTextureNode("roughness", -1150, -60, 1, True, "Roughness")
        else:
            if "metalness" in self.textureTypes:
                self.CreateTextureNode("metalness", -1150, 200, 1, True, "Metallic")
            
            if "roughness" in self.textureTypes:
                self.CreateTextureNode("roughness", -1150, -60, 1, True, "Roughness")
            elif "gloss" in self.textureTypes:
                glossNode = self.CreateTextureNode("gloss", -1150, -60)
                invertNode = self.CreateGenericNode("ShaderNodeInvert", -250, 60)
                # Add glossNode to invertNode connection
                self.mat.node_tree.links.new(invertNode.inputs["Color"], glossNode.outputs["Color"])
                # Connect roughness node to the material parent node.
                self.mat.node_tree.links.new(self.nodes.get(self.parentName).inputs["Roughness"], invertNode.outputs["Color"])
            
        if "opacity" in self.textureTypes:
            self.CreateTextureNode("opacity", -1550, -160, 1, True, "Alpha")
            self.mat.blend_method = 'HASHED'

        if "translucency" in self.textureTypes:
            self.CreateTextureNode("translucency", -1550, -420, 0, True, TRANSMISSION_INPUT)
        elif "transmission" in self.textureTypes:
            self.CreateTextureNode("transmission", -1550, -420, 1, True, TRANSMISSION_INPUT)

        # If HIGH POLY selected > use normal_bump and no displacement
        # If LODs selected > use corresponding LODs normal + displacement
        if self.isHighPoly:
            self.BumpSetup = False
        self.CreateNormalNodeSetup(True, "Normal")

        if "displacement" in self.textureTypes and not self.isHighPoly:
            self.CreateDisplacementSetup(True)

    def CreateMaterial(self):
        self.mat = (bpy.data.materials.get( self.materialName ) or bpy.data.materials.new( self.materialName ))
        self.mat.use_nodes = True
        self.nodes = self.mat.node_tree.nodes
        self.parentName = "Principled BSDF"
        self.materialOutputName = "Material Output"

        self.mat.node_tree.nodes[self.parentName].distribution = 'MULTI_GGX'
        self.mat.node_tree.nodes[self.parentName].inputs["Metallic"].default_value = 1 if self.isMetal else 0 # Metallic value
        self.mat.node_tree.nodes[self.parentName].inputs["IOR"].default_value = self.IOR
        self.mat.node_tree.nodes[self.parentName].inputs[SPECULAR_INPUT].default_value = 0
        self.mat.node_tree.nodes[self.parentName].inputs[CLEARCOAT_INPUT].default_value = 0
        
        
        self.mappingNode = None

        if self.isCycles and self.assetType not in ["3d", "3dplant"]:
            # Create mapping node.
            self.mappingNode = self.CreateGenericNode("ShaderNodeMapping", -1950, 0)
            self.mappingNode.vector_type = 'TEXTURE'
            # Create texture coordinate node.
            texCoordNode = self.CreateGenericNode("ShaderNodeTexCoord", -2150, -200)
            # Connect texCoordNode to the mappingNode
            self.mat.node_tree.links.new(self.mappingNode.inputs["Vector"], texCoordNode.outputs["UV"])

    def CreateTextureNode(self, textureType, PosX, PosY, colorspace = 1, connectToMaterial = False, materialInputIndex = ""):
        texturePath = self.GetTexturePath(textureType)
        if not texturePath or not os.path.exists(texturePath):
            raise RuntimeError("Texture file not found: %s (%s)" % (texturePath, textureType))
        textureNode = self.CreateGenericNode('ShaderNodeTexImage', PosX, PosY)
        textureNode.image = bpy.data.images.load(texturePath)
        textureNode.show_texture = True
        textureNode.image.colorspace_settings.name = self.colorSpaces[colorspace] # "sRGB", "Non-Color", "Linear"
        
        if textureType in ["albedo", "specular", "translucency"]:
            if self.GetTextureFormat(textureType) in "exr":
                textureNode.image.colorspace_settings.name = self.colorSpaces[2] # "sRGB", "Non-Color", "Linear"

        if connectToMaterial:
            self.ConnectNodeToMaterial(materialInputIndex, textureNode)
        # If it is Cycles render we connect it to the mapping node.
        if self.isCycles and self.assetType not in ["3d", "3dplant"]:
            self.mat.node_tree.links.new(textureNode.inputs["Vector"], self.mappingNode.outputs["Vector"])
        return textureNode

    def CreateTextureMultiplyNode(self, aTextureType, bTextureType, PosX, PosY, aPosX, aPosY, bPosX, bPosY, aColorspace, bColorspace, connectToMaterial, materialInputIndex):
        #Add Color>MixRGB node, transform it in the node editor, change it's operation to Multiply and finally we colapse the node.
        multiplyNode = self.CreateGenericNode('ShaderNodeMixRGB', PosX, PosY)
        multiplyNode.blend_type = 'MULTIPLY'
        #Setup A and B nodes
        textureNodeA = self.CreateTextureNode(aTextureType, aPosX, aPosY, aColorspace)
        textureNodeB = self.CreateTextureNode(bTextureType, bPosX, bPosY, bColorspace)
        # Conned albedo and ao node to the multiply node.
        self.mat.node_tree.links.new(multiplyNode.inputs["Color1"], textureNodeA.outputs["Color"])
        self.mat.node_tree.links.new(multiplyNode.inputs["Color2"], textureNodeB.outputs["Color"])

        if connectToMaterial:
            self.ConnectNodeToMaterial(materialInputIndex, multiplyNode)

        return multiplyNode

    def CreateNormalNodeSetup(self, connectToMaterial, materialInputIndex):
        
        bumpNode = None
        normalNode = None
        bumpMapNode = None
        normalMapNode = None

        if self.NormalSetup and self.BumpSetup:
            bumpMapNode = self.CreateTextureNode("bump", -640, -130)
            normalMapNode = self.CreateTextureNode("normal", -1150, -580)
            bumpNode = self.CreateGenericNode("ShaderNodeBump", -250, -170)
            bumpNode.inputs["Strength"].default_value = 0.1
            normalNode = self.CreateGenericNode("ShaderNodeNormalMap", -640, -400)
            # Add normalMapNode to normalNode connection
            self.mat.node_tree.links.new(normalNode.inputs["Color"], normalMapNode.outputs["Color"])
            # Add bumpMapNode and normalNode connection to the bumpNode
            self.mat.node_tree.links.new(bumpNode.inputs["Height"], bumpMapNode.outputs["Color"])
            if (2, 81, 0) > bpy.app.version:
                self.mat.node_tree.links.new(bumpNode.inputs["Normal"], normalNode.outputs["Normal"])
            else:
                self.mat.node_tree.links.new(bumpNode.inputs["Normal"], normalNode.outputs["Normal"])
            # Add bumpNode connection to the material parent node
            if connectToMaterial:
                self.ConnectNodeToMaterial(materialInputIndex, bumpNode)
        elif self.NormalSetup:
            normalMapNode = self.CreateTextureNode("normal", -640, -207)
            normalNode = self.CreateGenericNode("ShaderNodeNormalMap", -250, -170)
            # Add normalMapNode to normalNode connection
            self.mat.node_tree.links.new(normalNode.inputs["Color"], normalMapNode.outputs["Color"])
            # Add normalNode connection to the material parent node
            if connectToMaterial:
                self.ConnectNodeToMaterial(materialInputIndex, normalNode)
        elif self.BumpSetup:
            bumpMapNode = self.CreateTextureNode("bump", -640, -207)
            bumpNode = self.CreateGenericNode("ShaderNodeBump", -250, -170)
            bumpNode.inputs["Strength"].default_value = 0.1
            # Add bumpMapNode and normalNode connection to the bumpNode
            self.mat.node_tree.links.new(bumpNode.inputs["Height"], bumpMapNode.outputs["Color"])
            # Add bumpNode connection to the material parent node
            if connectToMaterial:
                self.ConnectNodeToMaterial(materialInputIndex, bumpNode)

    def CreateDisplacementSetup(self, connectToMaterial):
        if self.DisplacementSetup == "adaptive":
            # Add vector>displacement map node
            displacementNode = self.CreateGenericNode("ShaderNodeDisplacement", 10, -400)
            displacementNode.inputs["Scale"].default_value = 0.1
            displacementNode.inputs["Midlevel"].default_value = 0
            # Add converter>RGB Separator node (ShaderNodeSeparateRGB was removed in Blender 4.0)
            if bpy.app.version >= (3, 3, 0):
                RGBSplitterNode = self.CreateGenericNode("ShaderNodeSeparateColor", -250, -499)
                RGBSplitterNode.mode = 'RGB'
                splitterInput, splitterOutput = "Color", "Red"
            else:
                RGBSplitterNode = self.CreateGenericNode("ShaderNodeSeparateRGB", -250, -499)
                splitterInput, splitterOutput = "Image", "R"
            # Import normal map and normal map node setup.
            displacementMapNode = self.CreateTextureNode("displacement", -640, -740)
            # Add displacementMapNode to RGBSplitterNode connection
            self.mat.node_tree.links.new(RGBSplitterNode.inputs[splitterInput], displacementMapNode.outputs["Color"])
            # Add RGBSplitterNode to displacementNode connection
            self.mat.node_tree.links.new(displacementNode.inputs["Height"], RGBSplitterNode.outputs[splitterOutput])
            # Add normalNode connection to the material output displacement node
            if connectToMaterial:
                self.mat.node_tree.links.new(self.nodes.get(self.materialOutputName).inputs["Displacement"], displacementNode.outputs["Displacement"])
                # Blender 4.1 moved displacement_method from material.cycles to the material itself.
                if hasattr(self.mat, "displacement_method"):
                    self.mat.displacement_method = 'BOTH'
                else:
                    self.mat.cycles.displacement_method = 'BOTH'

        if self.DisplacementSetup == "regular":
            pass        

    def ConnectNodeToMaterial(self, materialInputIndex, textureNode):
        self.mat.node_tree.links.new(self.nodes.get(self.parentName).inputs[materialInputIndex], textureNode.outputs[0])

    def CreateGenericNode(self, nodeName, PosX, PosY):
        genericNode = self.nodes.new(nodeName)
        genericNode.location = (PosX, PosY)
        return genericNode

    def GetTexturePath(self, textureType):
        for item in self.textureList:
            if item[1] == textureType:
                return item[2].replace("\\", "/")

    def GetTextureFormat(self, textureType):
        for item in self.textureList:
            if item[1] == textureType:
                return item[0].lower()

class MSPluginPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    port: bpy.props.IntProperty(
        name="LiveLink Port",
        description="TCP port Quixel Bridge sends to (must match Bridge's export settings)",
        default=DEFAULT_PORT, min=1024, max=65535)

    def draw(self, context):
        self.layout.prop(self, "port")


class MS_Init_LiveLink(bpy.types.Operator):
    # Legacy entry point (kept for compatibility) - ensures the livelink runs.
    bl_idname = "bridge.plugin"
    bl_label = "Megascans Plugin"

    def execute(self, context):
        STATE.claimed_away = False
        start_listener()
        _ensure_pump()
        return {'FINISHED'}


class MS_OT_StartLiveLink(bpy.types.Operator):
    bl_idname = "megascans.start_livelink"
    bl_label = "Start LiveLink"
    bl_description = "Start listening for Quixel Bridge exports"

    def execute(self, context):
        STATE.claimed_away = False
        _ensure_pump()
        if start_listener():
            self.report({'INFO'}, "LiveLink listening on port %d" % STATE.port)
            return {'FINISHED'}
        self.report({'WARNING'}, "Could not start LiveLink - see Megascans panel")
        return {'CANCELLED'}


class MS_OT_ClaimLiveLink(bpy.types.Operator):
    bl_idname = "megascans.claim_livelink"
    bl_label = "Claim LiveLink"
    bl_description = "Ask the instance that owns the port to release it, then take over"

    def execute(self, context):
        port = _get_port()
        stop_listener()
        STATE.claimed_away = False
        try:
            peer = socket.create_connection(('localhost', port), timeout=1.0)
            peer.sendall(b'Bye Megascans')
            peer.close()
        except OSError as e:
            self.report({'ERROR'}, "Could not reach the port %d owner: %s" % (port, e))
            STATE.status = STATUS_PORT_BUSY
            return {'CANCELLED'}
        for _ in range(15):  # wait up to ~3 s for the owner to release
            time.sleep(0.2)
            if start_listener(quiet=True):
                log_event("INFO", "LiveLink claimed - listening on port %d." % port)
                self.report({'INFO'}, "LiveLink claimed (port %d)" % port)
                return {'FINISHED'}
        STATE.status = STATUS_PORT_BUSY
        msg = "Port %d owner did not release the port (a foreign process, or a modified plugin?)" % port
        log_event("ERROR", msg)
        self.report({'ERROR'}, msg)
        return {'CANCELLED'}


class MS_OT_CopyLog(bpy.types.Operator):
    bl_idname = "megascans.copy_log"
    bl_label = "Copy Log"
    bl_description = "Copy the recent event log to the clipboard"

    def execute(self, context):
        context.window_manager.clipboard = "\n".join(STATE.events)
        self.report({'INFO'}, "Megascans log copied to clipboard")
        return {'FINISHED'}


class MS_OT_OpenLogFolder(bpy.types.Operator):
    bl_idname = "megascans.open_log_folder"
    bl_label = "Open Log Folder"
    bl_description = "Open the folder containing the persistent MSPlugin log files"

    def execute(self, context):
        try:
            bpy.ops.wm.path_open(filepath=_log_dir())
        except Exception as e:
            self.report({'ERROR'}, "Could not open log folder: %s" % e)
            return {'CANCELLED'}
        return {'FINISHED'}


class MS_PT_LiveLink(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Megascans"
    bl_label = "Megascans LiveLink"

    def draw(self, context):
        layout = self.layout
        if STATE.status == STATUS_LISTENING:
            layout.label(text="Listening on port %d" % STATE.port, icon='CHECKMARK')
        elif STATE.status == STATUS_PORT_BUSY:
            layout.label(text="Port %d busy - another instance?" % STATE.port, icon='ERROR')
            layout.operator(MS_OT_ClaimLiveLink.bl_idname, icon='FILE_REFRESH')
        elif STATE.status == STATUS_RELEASED:
            layout.label(text="Released - claimed by another instance", icon='INFO')
            layout.operator(MS_OT_ClaimLiveLink.bl_idname, text="Reclaim LiveLink", icon='FILE_REFRESH')
        else:
            layout.label(text="Stopped", icon='X')
            layout.operator(MS_OT_StartLiveLink.bl_idname, icon='PLAY')
        if STATE.last_asset:
            layout.label(text="Last import: %s (%s)" % (STATE.last_asset, STATE.last_asset_time), icon='IMPORT')
        if STATE.last_error:
            box = layout.box()
            box.alert = True
            box.label(text="Last error:", icon='ERROR')
            box.label(text=STATE.last_error[:120])
        row = layout.row(align=True)
        row.operator(MS_OT_CopyLog.bl_idname, icon='COPYDOWN')
        row.operator(MS_OT_OpenLogFolder.bl_idname, icon='FILE_FOLDER')
        if STATE.events:
            box = layout.box()
            box.label(text="Recent events:")
            col = box.column(align=True)
            for event in list(STATE.events)[-5:]:
                col.label(text=event[:120])


class MS_Init_Abc(bpy.types.Operator):

    bl_idname = "ms_livelink_abc.py"
    bl_label = "Import ABC"

    def execute(self, context):

        try:
            if globals()['MG_ImportComplete']:
                
                assetMeshPaths = globals()['MG_AlembicPath']
                assetMaterials = globals()['MG_Material']
                
                if len(assetMeshPaths) > 0 and len(assetMaterials) > 0:

                    materialIndex = 0
                    old_materials = []
                    for meshPaths in assetMeshPaths:
                        for meshPath in meshPaths:
                            bpy.ops.wm.alembic_import(filepath=meshPath, as_background_job=False)
                            for o in bpy.context.scene.objects:
                                if o.select_get():
                                    old_materials.append(o.active_material)
                                    o.active_material = assetMaterials[materialIndex]
                                    
                        
                        materialIndex += 1
                    
                    for mat in old_materials:
                        try:
                            if mat is not None:
                                bpy.data.materials.remove(mat)
                        except:
                            pass

                    globals()['MG_AlembicPath'] = []
                    globals()['MG_Material'] = []
                    globals()['MG_ImportComplete'] = False

            return {'FINISHED'}
        except Exception as e:
            log_event("ERROR", "Alembic import failed: %s" % e)
            return {"CANCELLED"}

def _ensure_pump():
    if not bpy.app.timers.is_registered(_pump):
        # persistent=True: the default (False) removes the timer on file load,
        # which would silently kill the transport.
        bpy.app.timers.register(_pump, first_interval=PUMP_INTERVAL, persistent=True)

def menu_func_import(self, context):
    self.layout.operator(MS_Init_Abc.bl_idname, text="Megascans: Import Alembic Files")

classes = (
    MSPluginPreferences,
    MS_Init_LiveLink,
    MS_Init_Abc,
    MS_OT_StartLiveLink,
    MS_OT_ClaimLiveLink,
    MS_OT_CopyLog,
    MS_OT_OpenLogFolder,
    MS_PT_LiveLink,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    _ensure_pump()
    start_listener()  # starts at enable time, not on load_post

def unregister():
    pending = len(STATE.queue) + len(STATE.connections)
    if pending:
        log_event("WARNING", "%d pending export(s) discarded on disable." % pending)
    STATE.queue.clear()
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    stop_listener("LiveLink stopped (addon disabled).")
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
