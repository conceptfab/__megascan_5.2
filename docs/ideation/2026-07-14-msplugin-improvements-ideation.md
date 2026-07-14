---
date: 2026-07-14
topic: msplugin-improvements
focus: Quixel Megascans MSPlugin for Blender 5.2 (legacy bl_info addon, socket listener on port 28888, Principled BSDF materials, FBX/OBJ/ABC import)
---

# Ideation: MSPlugin (Megascans for Blender) — Improvements

## Codebase Context

- Single-file legacy `bl_info` addon (~700 lines, `MSPlugin/__init__.py`), maintained by CONCEPTFAB, freshly patched for Blender 5.2 (Principled BSDF socket renames, SeparateColor node, `displacement_method`, `wm.obj_import`, removed `cycles.feature_set`).
- Architecture: threading socket server on hardcoded port 28888 receives JSON from Quixel Bridge → `bpy.app.timers` polls global `Megascans_DataSet` → manual node-graph construction + synchronous FBX/OBJ import; Alembic deferred via hidden File>Import menu step and globals.
- Observed pain (real debugging session 2026-07-14): zero UI feedback (user believed install failed while errors went to console); only one Blender instance can bind 28888 (second silently loses livelink; Bridge shows "Could not send data over port 28888"); all errors are `print()`; listener starts only on `load_post`; unsynchronized global state across threads; hardcoded node coordinates; scattered `bpy.app.version` checks (one if/else at lines 418-421 has identical branches); `blend_method`/`use_nodes` deprecated for removal in Blender 6.0.
- Context: Quixel migrated to Fab; Bridge classic is legacy but widely used; a headless test harness (blender.exe --background + simulated Bridge JSON + node-link assertions) was proven this session (PASS on 5.2).
- No docs/, no tests, no institutional knowledge base in repo.

## Ranked Ideas

### 1. Visible & self-healing LiveLink (status panel + error reporting + robust port lifecycle)
**Description:** N-sidebar panel showing listener state (Listening / Port busy / Stopped), last imported asset, last error with copy-log button; route all `print()` errors additionally to `self.report()` and a rolling log. Plus lifecycle hardening: explicit `WinError 10048` detection with a human message ("port zajęty — drugi Blender?"), listener start in `register()` (not only `load_post`), watchdog that restarts a dead listener thread, configurable port in AddonPreferences, proper thread shutdown in `unregister()` (reload-safe addon).
**Rationale:** The entire 2026-07-14 support session was caused by invisibility: silent console errors + silently lost port binding. This bundle converts both into 5-second self-diagnosis.
**Downsides:** Adds first UI surface to a UI-less addon (maintenance); watchdog needs care to avoid thread leaks.
**Confidence:** 90%
**Complexity:** Medium
**Status:** Explored

### 2. Import pipeline correctness: queue instead of globals + auto-Alembic + undo step
**Description:** Replace `globals()['Megascans_DataSet']` (written by socket thread, read/cleared by timer — two rapid Bridge exports within the 1.0 s timer window lose the first payload) with `queue.Queue` of parsed jobs; import Alembic automatically from the timer (main thread) instead of the undiscoverable File>Import second step; wrap each asset in a single named undo step.
**Note (2026-07-14):** the payload-queue portion moved into idea #1's requirements (R16 in `docs/brainstorms/2026-07-14-visible-livelink-requirements.md`) — it is inseparable from the threadless transport. Remaining scope here: auto-Alembic import + per-asset undo steps.
**Rationale:** Fixes silent payload loss and removes a hidden ritual; makes "plugin is flaky" reputation issues mechanical to eliminate.
**Downsides:** Touches the core flow — needs the test harness as safety net.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 3. Re-import deduplication fix (bug)
**Description:** `bpy.data.images.load()` without `check_existing=True` (line ~370) duplicates image datablocks on every re-import; `materials.get() or new()` grabs an existing material and stacks a second node graph on top. Fix: reuse images, deliberately clear/rebuild (or version) the material.
**Rationale:** Re-exporting the same asset from Bridge is a normal iteration loop; today it bloats files and corrupts materials.
**Downsides:** Clearing an existing material may discard user edits — needs a policy decision (rebuild vs version-suffix).
**Confidence:** 95%
**Complexity:** Low
**Status:** Unexplored

### 4. Node-group "Megascans Shader" template
**Description:** One reusable node group (exposed: AO strength, gloss invert, normal+bump, displacement scale, translucency) instantiated per material; import code shrinks to "load textures, plug into group". Kills ~200 lines of hardcoded coordinates and duplicated gloss-invert logic.
**Rationale:** Users edit the template once, all Megascans materials inherit it; future socket renames concentrate in one mapping layer.
**Downsides:** Changes the material structure users know; migration story needed for old scenes.
**Confidence:** 75%
**Complexity:** High
**Status:** Unexplored

### 5. Headless CI test matrix + compat shim (maintainer flywheel)
**Description:** Commit the session-proven harness as `tests/` (simulated Bridge JSON payloads as a fixture corpus: surface/3d/3dplant/billboard/scatter/specular/metalness/abc) + CI matrix across Blender LTS/current/alpha with a deprecation canary; extract all `bpy.app.version` branches into `compat.py` and pre-empt Blender 6.0 removals (`blend_method`, `use_nodes`).
**Rationale:** Converts every future Blender release from a reactive debugging session into a red CI run with months of lead time.
**Downsides:** CI needs Blender binaries per version (download cache); initial setup cost.
**Confidence:** 90%
**Complexity:** Medium
**Status:** Unexplored

### 6. Local-library import without Bridge (strategic)
**Description:** Operator "import Megascans asset folder" that reads the on-disk `asset.json` metadata and synthesizes the payload consumed by the existing `MS_Init_ImportProcess` unchanged. Bridge stops being a single point of failure; enables batch/headless import and free "re-import after error".
**Rationale:** Post-Fab, Bridge classic is on life support; the disk library outlives the app.
**Downsides:** Bridge's export JSON and the on-disk `asset.json` schema are similar but not identical — mapping layer needs real-library verification.
**Confidence:** 70%
**Complexity:** Medium-High
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Standalone AddonPreferences panel | Empty container — concrete knobs land inside survivors (port → #1, shader params → #4) |
| 2 | Placement at 3D cursor + collection organization | Polish; cheap add-on to #2 later, not a direction |
| 3 | "Test LiveLink" preflight operator | ~90% of value covered by #1's status panel + port detection |
| 4 | Replay from cached JSON | Generalized and superseded by #6 |
| 5 | Asset Browser integration (asset_mark, catalogs, previews) | Heavy; depends on #6 direction — future brainstorm candidate |
| 6 | Extension packaging (blender_manifest.toml) | Premature before the #5 test safety net exists; restructure without cover |
| 7 | Fab-era re-link/re-path utility | Niche standalone tool, separate effort |
| 8 | README/PROTOCOL.md/CHANGELOG scaffolding | Hygiene to do opportunistically alongside #5, not a ranked product idea |
| 9 | Health-check handshake with Bridge | Requires unknown Bridge-side behavior; risky |
| 10 | Port negotiation across 28888-28899 | Bridge only sends to 28888; detection + message (in #1) is the honest scope |
| 11 | Modularization into packages as its own idea | Subsumed by #4/#5 refactors |

## Session Log
- 2026-07-14: Initial ideation — 48 raw ideas generated across 4 frames (user pain, automation/inversion, assumption-breaking, leverage), 21 after dedupe, 6 survivors (ideas #1+#2 from presentation merged into survivor #1).
- 2026-07-14: Survivor #1 (Visible & self-healing LiveLink) selected for brainstorm.
