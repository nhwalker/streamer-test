# Maintainability Improvement Plan

**Baseline:** `main` at `957ff1c` (ffmpeg + MediaMTX rewrite merged via PR #52,
plus three CI-hardening commits). Companion document:
[MAINTAINABILITY_REVIEW.md](MAINTAINABILITY_REVIEW.md) — the underlying
review; this plan sequences it into implementable phases. Where the two
disagree, this plan wins (the review predates the CI-fix commits, so some of
its line numbers have shifted; re-locate by symbol name, not line).

**Decisions (locked by repo owner — do not re-litigate):**

| # | Question | Decision |
|---|----------|----------|
| 1 | Integration-test stack | **Consolidate on the Java `functional-tests/` suite.** Port unique pytest container/browser scenarios into it; `tests/` keeps pure-Python unit tests only. |
| 2 | Legacy env-var naming | **Introduce new names, keep old ones as aliases** with a one-time startup deprecation warning. Docs switch to new names. |

**Standing constraints (apply to every phase):**

- `service/*.py` stays **stdlib-only** — no third-party runtime imports.
- No changes to ffmpeg command semantics, MediaMTX config, or HTTP API except
  where a phase explicitly says otherwise (Phase 4 only).
- Every phase is a separate PR (or small PR series) that leaves CI green.
- Refactor commits must not mix with reformat commits.

---

## Phase 0 — Guardrails (do first; everything else benefits)

*Review item 7. Small, no behavior change.*

1. Add `ruff` config (lint + format check). Run it; fix or explicitly
   ignore existing findings. Two commits: (a) enable + config, (b) any
   mechanical autofixes.
2. Optional but cheap: `mypy` in non-strict mode over `service/` (stdlib-only
   code type-checks almost for free).
3. Pin `tests/requirements.txt` to exact versions (`pip-compile` output or
   hand-pinned `==`).
4. `Makefile`: add `test` (unit tests only — see Phase 5 note), `lint`
   targets mirroring CI.
5. CI: add a lint job to `.github/workflows/container-build.yml` that fails
   on ruff errors.

**Done when:** `make lint && make test` passes locally; CI has a required
lint step; test deps are exact-pinned.

## Phase 1 — Mechanical refactors (independent; can run in parallel)

*Review items 2, 6, 8. No behavior change; import-path-only test updates.*

**PR 1a — single encoder module (review item 2).**
Create `service/encoders.py` owning the one-frame NVENC probe (currently
duplicated as `_nvenc_works()` in both `pipeline.py` and
`video_transcode.py`) plus the per-use-case encoder-arg builders (or
re-exports of `archive_encoder.archive_encoder_args` and the transcode
builder). Keep each call site's *current* arguments — unify the probe, not
the encoder settings. Preserve `pipeline.py`'s hard-exit-when-ffmpeg-missing
behavior.

**PR 1b — thin the web server (review item 6).**
Move segment staging (`stage_segments`, `_copy_active_to_stage`), zipping
(`zip_segments`), and export orchestration out of `service/web_server.py`
into `service/archive_export.py`. Move `parse_duration`/`parse_timestamp`
into `service/archive_times.py`. `web_server.py` keeps the `Router` class
and request/response glue only. **Note:** the CI-hardening commits added
~50 lines to `web_server.py` (active-segment serve hardening) after the
review was written — read the current file first and keep that logic with
whichever module now owns it.

**PR 1c — X11 split (review item 8, lowest priority).**
Move `_open_xdisplay`, `_query_x_screen`, `_query_x_monitors` from
`service/desktop_config.py` into `service/x11.py`. Pure tier/crop/naming
math stays behind. Skip this PR if time-constrained.

**Done when:** exactly one NVENC probe exists; `web_server.py` contains no
non-HTTP business logic; existing `tests/` pass with import-path updates only.

## Phase 2 — Documentation cleanup

*Review item 3. Docs only.*

1. Delete `REWRITE_PLAN.md` — the rewrite is merged; the plan is historical
   and already contradicts the implementation (stream-path naming:
   `desktop`/`desktop_half` in the plan vs `full_t0`/`full_t1` shipped).
2. Assign topic ownership: `README.md` = overview, quick start, running/ops,
   configuration reference; `PIPELINE.md` = internals (ffmpeg graph, MediaMTX,
   archive lifecycle, HTTP API). Remove duplicated sections from the non-owner
   doc, leaving a one-line link.
3. Spot-check every env var, port, stream path, and endpoint mentioned in the
   docs against `service/desktop_config.py`, `service/stream_command.py`, and
   `service/web_server.py`. Fix drift.

**Done when:** no doc contradicts the code; each topic is explained in exactly
one place; `REWRITE_PLAN.md` is gone.

## Phase 3 — Split the web client

*Review item 5. No behavior change, no build step.*

Split `service/web/index.html` (~800 lines) into `index.html` +
`style.css` + `app.js`, served by the existing static handler. Verify
`Router.translate_path` serves the new files. Confirm via the functional
suite (page loads, stream plays, tier switch works).

**Done when:** no inline `<script>`/`<style>` beyond a minimal bootstrap;
functional tests pass; page behavior unchanged.

## Phase 4 — Env-var renaming with aliases (only behavior-affecting phase)

*Review item 4. Decision: new names + aliases.*

1. New primary names (old names become aliases, still honored):
   - `WEBRTC_PORT` → `WHEP_PORT`
   - `WEBRTC_SCALE_LADDER` → `LIVE_SCALE_LADDER`
2. Resolution order: new name wins if both are set; setting only the old name
   works but logs one startup warning naming the replacement.
3. Legacy ignored vars (`GST_WEBRTC_*`, `WEBRTC_*_BITRATE`) keep their
   existing "set but ignored" warning; add the pointer to the new docs
   section.
4. Docs (README configuration reference) switch to new names, with an
   "aliases / migration" note. Tests cover both the new-name path and the
   alias-with-warning path.
5. Do **not** remove the old names in this phase; removal is a future
   decision after deployments migrate.

**Done when:** new names work; old names work with a single deprecation
warning; docs lead with new names; both paths tested.

## Phase 5 — Test-stack consolidation (largest; land last)

*Review item 1. Decision: consolidate on the Java `functional-tests/` suite.*

1. **Inventory** the pytest integration layer: which tests in
   `tests/test_container.py` (17 tests), `tests/test_archive_endpoint.py`
   (46 tests — note some may be unit-level; classify each), `tests/test_hub.py`,
   and which `conftest.py` fixtures exist only to support container/browser
   runs. Produce the port list before writing any Java.
2. **Port** scenarios not already covered by `functional-tests/` into the
   Java suite, reusing its harness (`ServiceStack`, recorder/evidence
   extensions). Unit-level tests misfiled as endpoint tests stay in `tests/`
   rewritten against the modules directly (Phase 1b's `archive_export.py`
   split makes this easy).
3. **CI:** add a Gradle functional-test job to the workflow (containers +
   browser in the runner; mirror what the pytest job needed). Keep the pytest
   job for unit tests — it should get *faster* (no containers).
4. **Delete** the ported pytest tests, the container/browser fixtures from
   `conftest.py` (expect it to shrink from ~600 lines to well under 200), and
   drop `testcontainers`/`selenium`/`webdriver-manager` from
   `tests/requirements.txt`.
5. `make test` = unit tests; add `make functional` = Gradle suite.

**Done when:** one browser/container harness exists (Java); every scenario
from the inventory is either ported or deliberately retired (list retirements
in the PR description); CI runs both suites green; pytest deps no longer
include browser/container packages.

---

## Sequencing summary

```
Phase 0 (guardrails)
   └─► Phase 1a / 1b / 1c   (parallel, mechanical)
            └─► Phase 2 (docs)      — any time after 0
            └─► Phase 3 (web split) — any time after 0
            └─► Phase 4 (env names) — its own PR, clearly flagged
                     └─► Phase 5 (test consolidation) — last; benefits from 1b
```

Rough sizing: Phases 0–3 are each a small PR (hours). Phase 4 is small but
needs careful docs/tests. Phase 5 is the large one (days) — do not start it
before Phase 1b lands.

## Out of scope (unchanged from the review)

- Rewriting the service in another language.
- Changing ffmpeg encode settings, tier ladders, archive quality modes, or
  the MediaMTX version/config.
- The hub (`hub/` is ~33 lines and fine).
