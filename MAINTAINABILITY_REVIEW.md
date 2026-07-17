# Maintainability Review — ffmpeg + MediaMTX rewrite branch

**Scope:** readability/maintainability review of the repository as it stands on
branch `claude/rewrite-feasibility-review-y7bbql` (commit `85f270e`, "Rewrite
streaming stack: GStreamer/webrtcsink → ffmpeg + MediaMTX").

**Intent of this document:** a work brief for an implementing agent. Each item
states the problem, the evidence (file:line where useful), the suggested fix,
and acceptance criteria. Items are ordered by expected payoff. None of these
change runtime behavior unless explicitly noted.

**Ground rules for the implementer:**

- The service is deliberately **stdlib-only Python** (no third-party imports in
  `service/*.py`). Keep it that way — do not introduce runtime dependencies.
- All media work happens in external processes (ffmpeg, MediaMTX). The Python
  layer is argv construction, process supervision, file management, and a small
  HTTP server. Preserve that separation.
- Do not change the ffmpeg command semantics, MediaMTX config, env-var
  behavior, or HTTP API as a side effect of refactoring. Existing tests in
  `tests/` must keep passing.
- Item 1 requires a human decision before implementation. Everything else can
  proceed independently; items are independent of each other unless noted.

---

## 1. Consolidate the two browser-test stacks (largest win — needs a decision first)

**Problem:** the repo carries two complete, overlapping integration-test
stacks that exercise largely the same surface (container boots, endpoints
respond, video renders in a browser):

- `tests/` — pytest + `testcontainers` + Selenium. `tests/conftest.py` is 567
  lines of container orchestration and browser/driver setup. Deps in
  `tests/requirements.txt` (pytest, testcontainers, selenium,
  webdriver-manager, requests).
- `functional-tests/` — a separate Java/Gradle JUnit 5 suite with its own
  container harness (`functional-tests/src/test/java/functests/support/ServiceStack.java`),
  browser recording (`BrowserRecorderExtension.java`), and Allure-style
  evidence attachments (`EvidenceAttachmentExtension.java`).

Every change to the service's startup logs, endpoint paths, or page structure
must be mirrored in two languages, two container harnesses, and two Selenium
setups.

**Suggested fix:** pick one stack for browser/container integration tests and
fold the unique value of the other into it, then delete the loser.
Considerations for the decision (do not decide unilaterally — ask the repo
owner):

- The Java suite has the richer harness (recording, evidence attachments,
  live-feed color verification) — features worth keeping.
- The pytest suite is co-located with the unit tests and shares CI wiring
  (`.github/workflows/container-build.yml` line ~220 runs `pytest tests/`).
- Whichever survives: unit tests (pure-Python, no containers) stay in
  `tests/` regardless; this item only concerns the container/browser tests.

**Acceptance:** one browser-test stack remains; its CI job runs green; any
unique test scenario from the removed stack (e.g. multi-segment live-feed
color checks) exists in the surviving stack; the removed stack's build files
and dependencies are gone.

## 2. Deduplicate encoder detection and selection

**Problem:** the one-frame NVENC probe exists twice, nearly identically:

- `service/pipeline.py:82` — `_nvenc_works()`
- `service/video_transcode.py:42` — `_nvenc_works()`

Encoder-argument selection is also split across `service/archive_encoder.py`
(`archive_encoder_args`) and `service/video_transcode.py:56`
(`_detect_encoder_args`), and the two have already drifted (different presets
and fallback behavior). A GPU-detection fix can silently land in one copy only.

**Suggested fix:** create `service/encoders.py` owning:

- `nvenc_works()` — the single probe (keep the existing behavior of
  `pipeline.py`'s copy: hard-exit with a clear message when ffmpeg is missing).
- The per-use-case arg builders (live tiers, archive quality modes, transcode)
  or at minimum re-export the existing builders so callers import from one
  place.

Callers (`pipeline.py`, `video_transcode.py`) import from it. Preserve current
behavior of each call site exactly — where the copies differ, keep each
caller's existing arguments; the goal is one probe implementation, not
unified encoder settings.

**Acceptance:** exactly one definition of the NVENC probe in `service/`;
`tests/test_archive_encoder.py` and `tests/test_video_transcode_encoder.py`
pass unmodified (or with import-path-only updates).

## 3. Retire or mark the rewrite plan; de-overlap the docs

**Problem:** three large overlapping documents — `README.md` (~18 KB),
`PIPELINE.md` (~16 KB), `REWRITE_PLAN.md` (~11 KB). The plan is now a
historical artifact and already disagrees with what shipped: it specifies
stream path names `desktop` / `desktop_half`, while `PIPELINE.md` documents
`full_t0` / `full_t1` / `<name>_t0` …. Stale design docs actively mislead
future readers.

**Suggested fix:**

- Either delete `REWRITE_PLAN.md` or add a prominent header: *"Historical
  design document for the GStreamer→ffmpeg rewrite. Superseded by PIPELINE.md;
  details below may not match the implementation."* Deleting is preferred once
  the branch merges; ask the owner if unsure.
- Establish single ownership per topic: `README.md` = overview, quick start,
  running/ops, configuration reference; `PIPELINE.md` = internals (ffmpeg
  graph, MediaMTX, archive lifecycle, HTTP API). Remove duplicated sections
  from whichever doc doesn't own the topic, leaving a link.

**Acceptance:** no contradictions between docs and code (spot-check stream
path names, env var lists, port numbers against `service/desktop_config.py`
and `service/stream_command.py`); each topic explained in exactly one place.

## 4. Decide the end-state for legacy `WEBRTC_*` / `GST_*` naming

**Problem:** config knobs still carry pre-rewrite names: `WEBRTC_PORT` (really
MediaMTX's WHEP/HTTP port), `WEBRTC_SCALE_LADDER` (see
`service/desktop_config.py:34-37`, `:80`). `desktop_config.py:84-86` tracks
legacy `GST_WEBRTC_*` / `WEBRTC_*_BITRATE` vars solely to warn that they're
ignored. Compat is reasonable; having no stated end-state is not — the old
vocabulary fossilizes.

**Suggested fix (behavior-affecting — flag in PR description):** introduce the
target names (suggest `MEDIAMTX_WEBRTC_PORT` or `WHEP_PORT`, and
`LIVE_SCALE_LADDER`) as the primary documented names; accept the old names as
aliases with a deprecation warning at startup; document the deprecation in
README's configuration reference. Do not remove the old names yet.

**Acceptance:** new names work; old names still work but warn once at startup;
docs reference only the new names (with an aliases note); tests updated to
cover the alias path.

## 5. Split `service/web/index.html` (808 lines) into page / stylesheet / script

**Problem:** ~110 lines of CSS and ~650 lines of JS (WHEP client, tier
auto-switch, stats overlay) are inlined in one file. Diffs are unreviewable and
the WHEP client logic can't be referenced or tested independently.

**Suggested fix:** split into `index.html` + `style.css` + `app.js` in
`service/web/`, served by the existing static handler — **no build step, no
bundler**. Verify `web_server.py`'s static file serving covers the new files
(it uses `SimpleHTTPRequestHandler.translate_path`, see
`service/web_server.py:291`).

**Acceptance:** page loads and streams as before (manual or functional-test
verification); no inline `<script>`/`<style>` blocks beyond a small bootstrap
if needed; total served content byte-equivalent in behavior.

## 6. Thin out `service/web_server.py`: extract archive-export logic, consolidate time parsing

**Problem:** the HTTP router module (381 lines) mixes routing with business
logic: segment staging (`stage_segments`, `_copy_active_to_stage`), zipping
(`zip_segments`), and transcode orchestration live beside the request handler.
It also defines its own `parse_duration` (`:90`) and `parse_timestamp` (`:101`)
while sibling time-parsing lives in `service/archive_times.py`.

**Suggested fix:** move staging/zip/export logic into a new module (suggest
`service/archive_export.py`); move `parse_duration`/`parse_timestamp` into
`archive_times.py` (they parse user-facing archive time inputs — same domain).
`web_server.py` keeps only the `Router` class and request/response glue.

**Acceptance:** `web_server.py` contains no non-HTTP business logic;
`tests/test_archive_endpoint.py` and `tests/test_video_timeline.py` pass
(import-path updates only).

## 7. Add static checks and a self-documenting dev workflow

**Problem:** CI (`.github/workflows/container-build.yml`) builds containers and
runs pytest, but there is no lint/format/type gate. The codebase's core job is
assembling ffmpeg argv strings, where a typo fails only at runtime. The
`Makefile` has no `test`/`lint` targets, and `tests/requirements.txt` uses
loose `>=` pins, so CI resolves different versions over time.

**Suggested fix:**

- Add `ruff` (lint + format check) and optionally `mypy` (cheap on a
  stdlib-only codebase) as a CI job and as dev-tool requirements. Fix or
  explicitly ignore existing findings — do not mass-reformat in the same PR as
  functional changes.
- Add `make test` (unit tests), `make lint` targets mirroring CI.
- Pin test deps (a `requirements.lock` / `pip-compile` output or exact `==`
  pins).

**Acceptance:** CI fails on lint errors; `make lint && make test` works
locally; test dependency versions are exact.

## 8. Separate X11 introspection from pure config math in `desktop_config.py`

**Problem:** `service/desktop_config.py` (444 lines) interleaves X11 queries
(`_open_xdisplay:180`, `_query_x_screen:196`, `_query_x_monitors:215`) with
pure computation (tier ladders `_compute_tiers:142`, crops
`_crops_from_region:298`, region naming `_name_regions:270`). The pure math is
the part most worth unit-testing, and it's entangled with code that needs a
live X server.

**Suggested fix:** move the three X11 functions into `service/x11.py`;
`desktop_config.py` imports them. Pure functions stay put. This is the
lowest-priority item — do it only if already touching the file, or as a
standalone mechanical PR.

**Acceptance:** `tests/test_desktop_config.py` passes; no X11/`subprocess`
usage remains in `desktop_config.py`.

---

## Suggested sequencing

1. Item 1 decision first (human input required), implementation can trail.
2. Items 2, 6, 8 are mechanical refactors — small, independent PRs.
3. Item 3 (docs) any time; item 5 (web split) any time.
4. Item 4 is the only behavior-affecting change — its own PR with clear notes.
5. Item 7 last or first, but keep the "enable checks" commit separate from any
   reformatting it triggers.

## Explicitly out of scope

- Rewriting the service in another language (evaluated separately; the rewrite
  makes it *possible* — argv strings and config files are the only interface —
  but it is not part of this cleanup).
- Changing ffmpeg encode settings, tier ladders, archive quality modes, or the
  MediaMTX version/config.
- Any change to the hub (`hub/` is 33 lines and fine).
