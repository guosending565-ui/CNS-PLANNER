# Round37-T3D-BMAP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user forbids commits; every task ends with tests and scoped diff review instead.

**Goal:** Deliver a visually reviewable CesiumJS Zhoushan satellite basemap with authorized ArcGIS imagery, streamed multilevel FABDEM terrain, clean defaults, camera presets, and reproducible visual/performance evidence.

**Architecture:** A localhost-only read-only API exposes a fixed-host imagery proxy and Cesium `heightmap-1.0` terrain pyramid. Cesium loads imagery and terrain independently, keeps all business layers lazy/off by default, and exposes deterministic camera/evidence hooks. Missing credentials remain an explicit blocked state.

**Tech Stack:** Python 3, GDAL/PROJ, CesiumJS 1.126.0, native JavaScript ES modules, QGIS Python runtime, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-12-round37-t3d-basemap-design.md`

## Global Constraints

- Work only in `D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER`; preserve every existing B0/T3D-1 change.
- Do not modify canonical `ProjectState`, production writers, invalidation semantics, planning algorithms, FABDEM, buildings v3, or EGM2008 source files.
- All new product endpoints are GET-only and localhost-only; never request `/api/workflow` from the 3D page.
- `CNS_ARCGIS_TOKEN` and `CNS_TIANDITU_TK` must never appear in responses, logs, URLs recorded in evidence, screenshots, state, or Git.
- No anonymous ArcGIS fallback, commercial tile persistence, arbitrary upstream URL, SSRF, or cross-host redirect.
- Default view shows only imagery, terrain, atmosphere/sky/water; all route, tower, grid, CNS, protection, RID, Radar, intrusion, and building layers are off and not requested.
- Terrain exaggeration remains exactly `1.0`; FABDEM EGM2008 orthometric heights convert by canonical pointwise `h = H + N`.
- Do not commit, push, merge, reset, clean, rebase, or create another repository/worktree copy.

## Review Focus

- Token missing/invalid/expired: return sanitized `blocked`, never leak upstream query strings or silently use an anonymous endpoint.
- A 200 response containing JSON/HTML, repeated placeholder imagery, or a wrong-size image: reject it as an invalid tile.
- FABDEM NoData or coverage boundary: render explicit orthometric-sea-level background without creating a cliff or feeding planning facts.
- Shared tile borders and parent/child transitions: decoded ellipsoidal heights agree within encoding tolerance.
- Default boot and refresh: zero business-layer requests/entities and zero `/api/workflow` requests.

---

### Task 1: Freeze T3D-1 visual baseline and camera state

**Files:**
- Create: `tools/capture_t3d_basemap_evidence.py`
- Modify: `cns_planner/web/js/visualization3d/app.js`
- Test: `tests/visualization3d_basemap.test.mjs`

**Interfaces:**
- Produces: `window.__CNS_3D__.cameraState() -> {longitude, latitude, height, heading, pitch, roll}` and `setCameraState(state) -> boolean`.
- Produces: before PNG plus JSON camera state under the delivery evidence directory.

- [ ] Write a Node test asserting the two camera hooks exist, serialize only numbers, and do not expose the Cesium viewer.
- [ ] Run `node tests/visualization3d_basemap.test.mjs`; verify RED.
- [ ] Add the minimal read-only hooks without changing scene styling or default visibility.
- [ ] Run the Node test and `node --check cns_planner/web/js/visualization3d/app.js`; verify GREEN.
- [ ] Start the existing T3D-1 service on port 8808, capture the current default viewport at the fixed acceptance size, save camera JSON and network log, then stop the service.
- [ ] Verify the state hash/revision and non-GET count are unchanged; inspect only this task's diff.

### Task 2: Authorized fixed-host imagery proxy

**Files:**
- Create: `cns_planner/application/visualization_3d_imagery_service.py`
- Modify: `cns_planner/api/router.py`
- Modify: `cns_planner/application/visualization_3d_service.py`
- Test: `tests/test_visualization_3d_imagery_proxy.py`

**Interfaces:**
- Produces: `Imagery3DService.configuration() -> dict` with sanitized source/status/zoom/tile-size/attribution/billing fields.
- Produces: `Imagery3DService.tile(source_id: str, z: int, x: int, y: int) -> ImageryTileResponse`.
- Produces endpoints `GET /api/visualization-3d/imagery/config` and `GET /api/visualization-3d/imagery/{source}/{z}/{x}/{y}`.

- [ ] Write failing tests for missing credentials, ArcGIS/Tianditu source selection, bbox/zxy validation, fixed host/path, redirect refusal, concurrency cap, timeouts, MIME/magic/dimension checks, 401/403 sanitization, no secret in repr/errors/log metadata, and no persistent cache headers.
- [ ] Run the targeted pytest file with the mandated Python command; verify RED.
- [ ] Implement runtime-only credential loading and fixed official endpoint definitions; ArcGIS default must use the one verified World Imagery interface family, never the legacy anonymous endpoint.
- [ ] Implement bounded upstream requests with a no-cross-host redirect handler, byte limit, image validation, sanitized metrics, and attribution metadata lookup.
- [ ] Add the two GET routes to the existing whitelist and include sanitized imagery status in manifest identity/ETag.
- [ ] Run targeted tests and `git diff --check`; inspect that no token literal or arbitrary URL forwarding exists.

### Task 3: Standard multilevel FABDEM heightmap terrain

**Files:**
- Create: `cns_planner/gis/terrain_tile_adapter.py`
- Modify: `cns_planner/application/visualization_3d_service.py`
- Modify: `cns_planner/api/router.py`
- Test: `tests/test_visualization_3d_terrain_tiles.py`

**Interfaces:**
- Produces: `TerrainTileSource.layer_json(base_url: str) -> dict`.
- Produces: `TerrainTileSource.tile(z: int, x: int, y: int) -> TerrainTile` containing `body`, `etag`, and source/NoData/water-mask statistics.
- Produces endpoints `GET /api/visualization-3d/terrain/layer.json` and `GET /api/visualization-3d/terrain/{z}/{x}/{y}.terrain`.

- [ ] Write failing protocol tests for EPSG:4326 TMS, 65×65 encoding, maxzoom 13, z/x/y bounds, ETag, source hashes, and height decode.
- [ ] Add numerical tests asserting `h-H=N`, common same-level edges, parent/child common samples, steep-slope fidelity, coast transition, NoData background, and FABDEM-boundary continuity.
- [ ] Run the targeted pytest file; verify RED.
- [ ] Implement direct bounded FABDEM sampling plus canonical EGM2008 window conversion; encode heightmap-1.0 with explicit child mask and optional water mask.
- [ ] Implement global orthometric-sea-level background (`H=0`, `h=N`) and source declarations; do not mutate or call planning services.
- [ ] Add an in-memory bounded LRU with request de-duplication, private terrain cache headers, routes, and manifest metadata.
- [ ] Run targeted tests, existing T3D-1 geoid/window verification, and `git diff --check`.

### Task 4: Cesium clean basemap scene and automatic LOD

**Files:**
- Create: `cns_planner/web/js/visualization3d/basemap.js`
- Create: `cns_planner/web/js/visualization3d/camera_presets.js`
- Modify: `cns_planner/web/js/visualization3d/app.js`
- Modify: `cns_planner/web/visualization-3d.html`
- Test: `tests/visualization3d_basemap.test.mjs`

**Interfaces:**
- Produces: `createBasemap(viewer, config)`, `setImageryVisible(bool)`, `setTerrainVisible(bool)`, `setLabelsVisible(bool)`, and `setQuality('normal'|'high')`.
- Produces: immutable `CAMERA_PRESETS` and `flyToPreset(viewer, id)` for six named views.

- [ ] Extend Node tests for CesiumTerrainProvider usage, no `refineTerrain`, terrain exaggeration 1.0, source switches, quality settings, lighting/atmosphere/fog, and six deterministic presets.
- [ ] Add tests asserting every business checkbox defaults false and startup code does not request routes/points/buildings/workflow.
- [ ] Run the Node test; verify RED.
- [ ] Implement authorized imagery through the local proxy, independent annotation layer, heightmap terrain, normal/high screen-space-error settings, and explicit blocked UI.
- [ ] Enable natural light, terrain shadows, sky atmosphere, mild fog, and water effect only when the water mask status is available.
- [ ] Replace the large always-open panels with compact controls and unobscured copyright/disclosure strips; add preset/overhead/45-degree/low-altitude/reset controls.
- [ ] Retain existing acceptance APIs and make route/tower/CNS loading lazy when their switches are first enabled.
- [ ] Run Node tests and `node --check` for every changed/new entry module.

### Task 5: Bounded near-view buildings

**Files:**
- Create: `cns_planner/application/visualization_3d_building_service.py`
- Create: `cns_planner/web/js/visualization3d/buildings.js`
- Modify: `cns_planner/api/router.py`
- Modify: `cns_planner/web/js/visualization3d/app.js`
- Test: `tests/test_visualization_3d_buildings.py`
- Test: `tests/visualization3d_basemap.test.mjs`

**Interfaces:**
- Produces endpoint `GET /api/visualization-3d/buildings?bbox=...&limit=...`.
- Produces features with `height_m`, `height_status` (`measured|predicted|unknown`), `source`, and ground/reference metadata.

- [ ] Write failing tests for bbox/limit guards, RTree-bounded reads, measured/predicted labels, unknown-height omission, and zero state writes.
- [ ] Implement the smallest read-only adapter by reusing existing building-footprint source discovery/query code.
- [ ] Implement an off-by-default building switch; request only below the near-view altitude threshold and within the camera bbox; unload outside the bounded view.
- [ ] Clamp display bases to terrain, label predicted heights, and never extrude unknown heights.
- [ ] Run targeted Python/Node tests and inspect for state/writer imports.

### Task 6: Real-browser visual, network, and performance acceptance

**Files:**
- Modify: `tools/capture_t3d_basemap_evidence.py`
- Create: `tools/verify_t3d_basemap_http.py`
- Create: `tools/measure_t3d_basemap_disk.py`
- Create in delivery: evidence JSON/PNG and reports `01` through `08`.

**Interfaces:**
- Consumes the six `CAMERA_PRESETS`, saved T3D-1 camera state, and `window.__CNS_3D__` summaries.
- Produces same-size before/after PNG, six fixed-view PNG, network/FPS/GPU/cache JSON, and self-judging PASS/BLOCKED results.

- [ ] Run HTTP acceptance without tokens: assert config blocked, proxy does not use anonymous fallback, terrain tiles/ETag work, all responses are GET-only, and state/revision remain unchanged.
- [ ] If `CNS_ARCGIS_TOKEN` is present, validate official metadata identity and request a small controlled matrix of real Zhoushan tiles at overview/mid/near LOD; inspect content, size, duplicate hashes, blankness, CORS/proxy behavior, attribution, count, and billing estimate.
- [ ] Launch the real browser on AMD Radeon 610M, save the same-camera after PNG and six preset PNGs at the same viewport size; record tile completion and visual limitations.
- [ ] Record moving/stationary FPS, first-screen timing, request count/bytes, cache hits, browser/GPU/process memory, failed requests, and `/api/workflow` count.
- [ ] Manually inspect coastline continuity, natural relief, tile seams/blanks, label separation, building grounding, terrain alignment, and clean default. Do not mark imagery PASS when credential or visual quality is blocked.
- [ ] Run all targeted tests, frontend syntax checks, BOM check for changed JSON/YAML, scoped `git diff --check`, and compare state/source hashes before/after.

### Task 7: Closeout and handoff

**Files:**
- Modify: `D:\Projects\_cns_workspaces\CONTROL.md`
- Modify: `D:\Projects\_cns_workspaces\BASELINE.json`
- Create: `D:\Projects\_cns_workspaces\deliverables\round37_t3d_basemap\MANIFEST.sha256.json`

**Interfaces:**
- Produces the final source choice, terrain implementation result, visual verdict, performance verdict, known issues, T3D-2 read-only extension points, and disk delta.

- [ ] Assemble reports for source licensing/quality/performance, terrain protocol/numerics, browser visuals, performance, known issues, T3D-2 interfaces, disk growth, and handoff.
- [ ] Generate the delivery manifest without copying commercial tiles, DEMs, geoid, buildings, project state, caches, or credentials.
- [ ] Stop the service and verify ports 8765–8810 are clear.
- [ ] Update CONTROL/BASELINE using UTF-8 without BOM, preserve existing keys/content, and record the exact dirty worktree rather than claiming a commit.
- [ ] Release only this session's write lock if one was claimed; verify source hashes, project revision, and authoritative state hash are unchanged.
- [ ] Report PASS/BLOCKED honestly against the seven final user questions and stop before any T3D-2 coverage work.
