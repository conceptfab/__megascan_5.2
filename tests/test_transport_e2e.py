# Transport E2E test for MSPlugin's threadless LiveLink.
# Run: blender.exe --background --factory-startup --python tests/test_transport_e2e.py
#
# bpy.app.timers do not pump in --background script runs, so this test drives
# the pump manually (MSPlugin._pump()) — the socket path itself is real TCP.
import bpy, json, os, socket, sys, time

FAILURES = []

def check(name, cond):
    print(("PASS" if cond else "FAIL") + ": " + name)
    if not cond:
        FAILURES.append(name)

SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fixtures")
os.makedirs(SCRATCH, exist_ok=True)

def make_tex(name):
    path = os.path.join(SCRATCH, name + ".png")
    if not os.path.exists(path):
        img = bpy.data.images.new(name, 8, 8)
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
    return path

def payload(asset_id, name, tex_types):
    comps = [{"type": t, "format": "png", "path": make_tex(t)} for t in tex_types]
    return json.dumps([{
        "type": "surface", "path": SCRATCH, "id": asset_id, "category": "Stone",
        "activeLOD": "lod0", "minLOD": "lod4", "name": name,
        "categories": ["surface"], "tags": [], "workflow": "metalness", "pbrWorkflow": "metalness",
        "components": comps, "meshList": [],
    }]).encode()

# IMPORTANT: tests always run on TEST_PORT, never 28888 — a real Blender
# instance with the plugin may be running on this machine.
TEST_PORT = 28899

bpy.ops.preferences.addon_enable(module="MSPlugin")
import MSPlugin

def pump(n=30):
    for _ in range(n):
        MSPlugin._pump()
        time.sleep(0.02)

def send(data):
    assert MSPlugin.STATE.port == TEST_PORT, "tests must never touch port 28888"
    s = socket.create_connection(('localhost', TEST_PORT), timeout=2.0)
    s.sendall(data)
    s.close()

# 1. switch to the test port via the preference (also exercises live rebind)
bpy.context.preferences.addons["MSPlugin"].preferences.port = TEST_PORT
pump(3)
check("live port change rebinds to %d" % TEST_PORT, MSPlugin.STATE.port == TEST_PORT)
check("listener LISTENING", MSPlugin.STATE.status == MSPlugin.STATUS_LISTENING)
check("port bound", MSPlugin.STATE.server is not None)

# 2. real TCP payload -> import
send(payload("t01", "Transport One", ["albedo", "roughness", "normal"]))
pump()
check("asset imported via real socket", bpy.data.materials.get("Transport_One_t01") is not None)
check("last_asset updated", MSPlugin.STATE.last_asset == "Transport One")

# 3. two rapid consecutive payloads -> both import, in order
send(payload("t02", "Transport Two", ["albedo"]))
send(payload("t03", "Transport Three", ["albedo"]))
pump()
check("rapid payload A imported", bpy.data.materials.get("Transport_Two_t02") is not None)
check("rapid payload B imported", bpy.data.materials.get("Transport_Three_t03") is not None)

# 4. payload split across ticks (chunked send with pumps in between)
data = payload("t04", "Transport Four", ["albedo", "normal"])
s = socket.create_connection(('localhost', MSPlugin.STATE.port), timeout=2.0)
half = len(data) // 2
s.sendall(data[:half]); pump(3)
s.sendall(data[half:]); pump(3)
s.close(); pump()
check("chunked payload imported", bpy.data.materials.get("Transport_Four_t04") is not None)

# 5. invalid JSON -> loud error, no crash
send(b"this is not json")
pump()
check("invalid JSON logged as ERROR", any("not valid Bridge JSON" in e for e in MSPlugin.STATE.events))

# 6. missing texture file -> per-asset failure, clear reason
bad = json.dumps([{
    "type": "surface", "path": SCRATCH, "id": "t05", "category": "Stone",
    "activeLOD": "lod0", "minLOD": "lod4", "name": "Missing Tex",
    "categories": ["surface"], "tags": [], "workflow": "metalness", "pbrWorkflow": "metalness",
    "components": [{"type": "albedo", "format": "png", "path": os.path.join(SCRATCH, "nope.png")}],
    "meshList": [],
}]).encode()
send(bad)
pump()
check("missing texture reported", any("Texture file not found" in e for e in MSPlugin.STATE.events))

# 7. Bye Megascans -> RELEASED state, port freed, no auto-rebind
send(b"Bye Megascans")
pump()
check("state RELEASED after Bye", MSPlugin.STATE.status == MSPlugin.STATUS_RELEASED)
check("server closed after Bye", MSPlugin.STATE.server is None)
port_free = False
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    probe.bind(('localhost', TEST_PORT))
    port_free = True
except OSError:
    pass
finally:
    probe.close()
check("port actually freed", port_free)
pump(15)  # bind retry interval would fire here if auto-rebind was wrongly active
check("no auto-rebind after claim", MSPlugin.STATE.server is None)

# 8. manual restart works (Start LiveLink path)
MSPlugin.STATE.claimed_away = False
check("manual restart rebinds", MSPlugin.start_listener())
send(payload("t06", "Transport Six", ["albedo"]))
pump()
check("import works after restart", bpy.data.materials.get("Transport_Six_t06") is not None)

# 9. unregister releases everything (reload-safe)
bpy.ops.preferences.addon_disable(module="MSPlugin")
freed = False
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    probe.bind(('localhost', TEST_PORT))
    freed = True
except OSError:
    pass
finally:
    probe.close()
check("port freed after disable", freed)
bpy.ops.preferences.addon_enable(module="MSPlugin")
check("re-enable works (reload-safe)", MSPlugin.STATE.status == MSPlugin.STATUS_LISTENING)
bpy.ops.preferences.addon_disable(module="MSPlugin")

print("=" * 60)
if FAILURES:
    print("TRANSPORT E2E: FAIL (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("TRANSPORT E2E: PASS (all checks)")
