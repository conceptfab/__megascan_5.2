# Material E2E test: simulated Bridge JSON -> material with correct node links.
# Run: blender.exe --background --factory-startup --python tests/test_material_e2e.py
# Injects below the socket layer (MS_Init_ImportProcess) — transport is covered
# by test_transport_e2e.py.
import bpy, json, os, sys

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

bpy.ops.preferences.addon_enable(module="MSPlugin")
import MSPlugin

tex_types = ["albedo", "ao", "roughness", "normal", "displacement", "opacity",
             "translucency", "specular", "gloss", "metalness", "bump"]
paths = {t: make_tex(t) for t in tex_types}

def components(types):
    return [{"type": t, "format": "png", "path": paths[t]} for t in types]

asset_specular = {
    "type": "surface", "path": SCRATCH, "id": "spec01", "category": "Stone",
    "activeLOD": "lod0", "minLOD": "lod4", "name": "TestSpec Surface",
    "categories": ["surface"], "tags": [], "workflow": "specular", "pbrWorkflow": "specular",
    "components": components(["albedo", "ao", "specular", "gloss", "opacity", "translucency", "normal", "bump", "displacement"]),
    "meshList": [],
}
asset_metal = {
    "type": "surface", "path": SCRATCH, "id": "metal01", "category": "Metal",
    "activeLOD": "lod0", "minLOD": "lod4", "name": "TestMetal Surface",
    "categories": ["surface"], "tags": [], "workflow": "metalness", "pbrWorkflow": "metalness",
    "components": components(["albedo", "metalness", "roughness", "normal", "displacement"]),
    "meshList": [],
}

bpy.context.scene.render.engine = 'CYCLES'
if hasattr(bpy.context.scene.cycles, "feature_set"):
    bpy.context.scene.cycles.feature_set = 'EXPERIMENTAL'

MSPlugin._import_payload(json.dumps([asset_specular, asset_metal]).encode())

expected = {
    "TestSpec_Surface_spec01": [MSPlugin.SPECULAR_INPUT, MSPlugin.TRANSMISSION_INPUT, "Alpha", "Base Color", "Roughness", "Normal"],
    "TestMetal_Surface_metal01": ["Metallic", "Roughness", "Base Color", "Normal"],
}
for matname, inputs in expected.items():
    mat = bpy.data.materials.get(matname)
    check("material %s exists" % matname, mat is not None)
    if not mat:
        continue
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    for inp in inputs:
        check("%s input '%s' linked" % (matname, inp), bsdf.inputs[inp].is_linked)
    out = mat.node_tree.nodes.get("Material Output")
    check("%s displacement linked" % matname, out.inputs['Displacement'].is_linked)
    check("%s displacement_method BOTH" % matname, mat.displacement_method == 'BOTH')

check("import counted as success", MSPlugin.STATE.last_asset == "TestMetal Surface")
check("no error left on state", MSPlugin.STATE.last_error == "")

print("=" * 60)
if FAILURES:
    print("MATERIAL E2E: FAIL (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("MATERIAL E2E: PASS (all checks)")
