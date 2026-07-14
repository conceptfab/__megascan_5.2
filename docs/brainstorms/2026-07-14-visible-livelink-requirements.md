---
date: 2026-07-14
topic: visible-self-healing-livelink
---

# Visible & Self-Healing LiveLink (MSPlugin)

## Problem Frame

Artists using Quixel Bridge → Blender have no way to see whether the livelink is alive or why an import did nothing. All plugin outcomes are `print()` calls to a console most Windows users never open (`MSPlugin/__init__.py`, 12+ except-print sites); the port-28888 listener silently fails to bind when a second Blender instance runs (Bridge then shows "Could not send data over port 28888"); the listener only starts after a `load_post` event; `unregister()` never stops the listener threads, so the addon does not survive disable/enable. A real support session (2026-07-14) was caused entirely by this invisibility — the user believed installation itself had failed.

This feature makes the livelink state visible at a glance and removes the failure modes by design.

## Requirements

**Status visibility (UI)**
- R1. An N-sidebar panel (3D Viewport, tab "Megascans") shows: listener state (`Listening on <port>` / `Port busy — another instance owns the livelink` / `Stopped`), last imported asset name + timestamp, and the most recent error.
- R2. A rolling in-session event log (~50 most recent entries: imports, warnings, errors) is viewable from the panel, with a "Copy log" button that puts the log on the clipboard and confirms via an INFO report (Blender's standard clipboard-action feedback).
- R3. Panel state updates automatically (driven by the same timer that pumps imports); no manual refresh.

**Error reporting policy (quiet success, loud failure)**
- R4. Successful imports are quiet: an entry in the panel/log and a status-bar note only — no popups. Notifications aggregate per received payload, not per asset: a 30-asset batch produces one status note ("Imported 30 assets"), never 30.
- R5. Partial failure (asset imported but some maps/meshes failed, e.g. missing texture file) produces a `WARNING` report and a per-asset breakdown in the log (which maps loaded / which failed and why).
- R6. Total failure (asset not imported) produces a visible `ERROR` popup — at most one popup per received payload, summarizing all failed assets (e.g. "5 of 30 assets failed — see Megascans panel"), with per-asset reasons in the log. Systemic batch failures (moved library, dead disk) must not spawn one popup per asset. Popups and status-bar surfacing degrade to log-only when `bpy.app.background` is set (headless runs).
- R7. Every event written to the panel/log is also appended to a persistent, size-rotated log file in Blender's per-version config location; the panel exposes the file path ("Open log folder"). Support becomes "send the file".
- R8. No silent `except: print()` paths remain: every caught exception routes through the same logging/reporting layer.

**Port conflict handling**
- R9. A failed port bind (WinError 10048 / EADDRINUSE) is detected explicitly and shown as a distinct state with a human-readable message ("Port 28888 zajęty — czy działa druga instancja Blendera?"), never as a swallowed exception. Because a bind failure is a failure, it also gets the loud treatment: a one-time proactive WARNING report at startup pointing at the panel — the affected user (who may not know the panel exists) must not need to go looking.
- R10. The panel offers a "Claim LiveLink" button: it sends the existing `Bye Megascans` protocol message to the current port owner, waits for the port to free, binds, and confirms in the panel. No automatic port stealing.
- R11. The listen port is configurable in AddonPreferences (default 28888, matching Bridge's export settings); the panel shows the active port.

**Lifecycle (self-healing by design — threadless transport)**
- R12. The livelink transport runs without worker threads: a non-blocking socket serviced from the existing `bpy.app.timers` pump on the main thread. There is no listener thread that can die, so no watchdog is needed.
- R13. The listener starts when the addon is enabled (`register()`), not only after a file load; it keeps working across File > New / file loads. (Implementation constraint: the pump must be registered with `bpy.app.timers.register(fn, persistent=True)` — the default `persistent=False` removes the timer on file load, which would silently kill the transport once the legacy `load_post` restart path is removed.)
- R14. Disabling the addon (`unregister()`) fully releases the port and the timer; enable → disable → enable in one session works cleanly (reload-safe). Queued or partially-received payloads at unregister time are either drained-and-imported or discarded with a logged WARNING ("N pending exports discarded on disable") written to the persistent log before shutdown — never dropped silently (per R8).
- R15. Receiving a payload must not block the UI: data is read in chunks per timer tick; a large multi-asset payload may take a few ticks but Blender stays responsive.
- R16. Rapid consecutive Bridge exports are not lost: incoming payloads queue and import in order (replaces the single overwritten `Megascans_DataSet` global).

## Success Criteria

- An artist can answer "is the livelink alive, and what happened to my last export?" in under 5 seconds without opening a console.
- The two failure modes from the 2026-07-14 session (silent import errors; second instance silently losing the port) are both impossible to hit without a visible explanation in the UI.
- Addon disable/enable cycle works without restarting Blender; port is always released.
- Existing behavior is preserved: the headless E2E harness (simulated Bridge JSON → material with correct node links) still passes on Blender 5.2 after the transport rewrite.
- The transport itself is exercised end-to-end by an automated check: a test client opens a real TCP connection to the bound port and sends (a) a payload split across multiple timer ticks and (b) two rapid consecutive payloads, asserting both import in order. The material-level harness alone is not sufficient — it injects below the socket layer and would pass with a broken transport.

## Scope Boundaries

- No automatic port takeover on window focus (explicitly rejected — fragile, surprising).
- No port scanning/negotiation beyond the configurable single port (Bridge only sends to its configured port).
- No changes to material building, geometry import, or the Alembic flow (separate ideation items #2-#4). Exception: the payload-queue portion of ideation #2 lands here as R16 (it is inseparable from the threadless transport); the remaining #2 scope is auto-Alembic import and per-asset undo steps.
- No module split / extension packaging (ideation rejected as premature before CI exists).
- No Bridge-side changes; the plugin must work with unmodified Quixel Bridge.

## Key Decisions

- **Full bundle in v1** (not staged): visibility and lifecycle land together — user decision.
- **Threadless transport (approach B)** over hardening the existing threads (A) or module split (C): self-healing is achieved by removing the thing that fails, not by watching it. Eliminates the `thread_checker` self-connect hack, thread races, and the need for locks; makes `unregister()` trivially correct.
- **Claim-button over auto-takeover** for port conflicts: explicit artist decision, uses the protocol's existing `Bye Megascans` message.
- **Quiet success / loud failure** notification policy: panel-only for success, WARNING for partial, popup for total failure.
- **Panel + rotating log file** (not in-memory only): post-hoc diagnosis and self-serve support.
- **Full-bundle tradeoff accepted:** bundling couples the low-risk visibility work (R1-R11) to the higher-risk transport rewrite (R12-R16); accepted because the committed test harness (see Dependencies) gates the rewrite, and the status/panel layer is designed against the same state API either transport would expose.
- **Transport-agnostic reporting layer:** the status panel, event log, and payload queue must not assume the socket transport — a future Bridge-less local-library import (ideation #6) reuses them as-is.

## Review Resolutions (2026-07-14)

- **State machine (finding 1):** the new transport honors `Bye Megascans`: it releases the port and enters a visible "Released — claimed by another instance" state with NO auto-rebind (manual "Reclaim" only). The startup "Port busy" state passively retries the bind every ~2 s and auto-transitions to Listening when the port frees. Claim = connect + Bye + bind retries for ≤3 s, with a distinct failure message when the owner does not release (foreign process).
- **Timer starvation (finding 2):** connections are accepted eagerly every tick; main-thread blockage during the plugin's own imports is an accepted limit, documented — Bridge-side timeout research deferred to the CI work (ideation #5).
- **Panel UX (finding 3):** Claim button rendered only in busy/released states; "last error" clears on the next successful import.
- **R11 (finding 4):** kept — the port preference applies live (listener rebinds when the pref changes), harmless if Bridge is hardwired to 28888.
- **Path validation (finding 5):** texture file existence is checked before load (clear per-asset error instead of a cryptic one); non-JSON payloads are rejected loudly. Directory allowlisting rejected (breaks custom library locations).
- **Log redaction (finding 6):** none — logs are for direct support handoff; accepted.
- **UI language:** English (international user base).

## Dependencies / Assumptions

- The `Bye Megascans` message reliably shuts down the legacy listener in older plugin copies too (both loops honor it — verified in current source; other instances running *modified* forks may ignore it → Claim shows a "could not claim" error state).
- Bridge's export port is user-configurable in Bridge's own export settings, so R11 is usable end-to-end (assumption based on Bridge UI; verify during planning).
- Blender is single-main-thread; `bpy.app.timers` callbacks may safely touch `bpy.data`/run operators (already relied upon by the current `newDataMonitor`).
- **Prerequisite:** the session-proven headless E2E harness is committed to the repo (`tests/`) and runnable *before* the transport rewrite starts — an uncommitted safety net is no safety net. Note: `bpy.app.timers` do not pump in `--background` script runs, so headless material tests inject below the socket layer; the transport-level test (see Success Criteria) needs a loop-driven or interactive run mode.
- Legacy plugin copies re-attempt the port bind on **every** file load (`load_post` re-runs `bpy.ops.bridge.plugin()`), so a successful Claim against a legacy peer holds only until this instance releases the port; the Claim flow's bind step must handle losing that race and report it as a distinct claim failure.

## Outstanding Questions

### Deferred to Planning
- [Affects R7][Technical] Exact log location (`bpy.utils.user_resource('CONFIG')` vs extension user dir) and rotation policy. Hard constraint: two Blender processes logging concurrently on Windows is a first-class supported state (it's the motivating scenario), and size-based rotation (rename) fails while another process holds the file — per-session/per-instance file naming (e.g. PID suffix) is the likely shape.
- [Affects R11][Technical] Behavior when the port preference changes while the listener is bound: rebind live vs require disable/enable — must be defined, not left implicit.
- [Affects R1, R2][Technical] Panel layout: grouping/ordering of the 6+ elements (state, last asset, error, port, log + Copy, Open log folder, conditional Claim) in a ~250 px sidebar; which sections are collapsed by default so the at-a-glance state stays on top.
- [Affects R10][Technical] Claim handshake timing: how long to wait for the old owner to release before reporting failure; behavior when the owner is not a Megascans plugin at all (foreign process on 28888).
- [Affects R12][Technical] Timer tick rate and per-tick read budget for large payloads (balance latency vs UI responsiveness); framing (current protocol delimits by connection close — keep or add length prefix while staying Bridge-compatible: Bridge closes the connection, so keep close-delimited).
- [Affects R15][Needs research] Largest realistic Bridge payload size (high-poly multi-asset export) to size the per-tick read budget.
- [Affects R3][Technical] Cheapest redraw strategy for the panel (tag_redraw on region vs relying on timer-driven UI updates).

## Next Steps
-> /ce:plan for structured implementation planning
