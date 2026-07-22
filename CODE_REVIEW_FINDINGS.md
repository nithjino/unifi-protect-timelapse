# First-pass code review findings

Reviewed: 2026-07-21
Revision: `604040e`
Scope: Python CLI, web application, Qt desktop application, macOS SwiftUI application, Windows WPF application, packaging, CI, and tests.

## Executive summary

| Severity | Count |
| --- | ---: |
| Critical | 0 |
| High | 4 |
| Medium | 7 |
| Low | 6 |

Resolution status (2026-07-22): all HIGH, MEDIUM, and LOW findings are handled. Their headings are crossed out below.

The most consequential risks are in the web application's trust boundary and in unattended export workflows. In particular, an alternate ASGI launch can expose the application without authentication, Windows cancellation can leave very large partial files behind, colliding camera names can overwrite or suppress exports, and all three desktop schedulers can permanently skip failed daily exports.

This was a source review, not a penetration test of a live Protect appliance. Findings whose impact depends on deployment or upstream behavior say so explicitly.

## Critical

No critical findings were identified.

## High

### ~~HIGH-1: The web authentication boundary depends on the launch command and trusts attacker-controlled host data~~ — Resolved

**Affected code:** `timelapse/web.py:169-217`, `timelapse/web.py:284-343`, `timelapse/web.py:589-604`

The application disables authentication whenever no web password is configured. The non-loopback safety check is performed only in `main()`, while the module also exports a ready-to-run global ASGI application with `app = create_app()`. Launching that object through a standard ASGI command such as `uvicorn timelapse.web:app --host 0.0.0.0` bypasses the non-loopback check and exposes camera metadata, thumbnails, export creation, schedules, and generated files without authentication.

Even in the intended loopback-only mode, same-origin validation derives its expected origin from `Host`, or from forwarded host/protocol headers when proxy trust is enabled. There is no trusted-host allowlist. A request with matching attacker-controlled `Host` and `Origin` values passes `_is_same_origin`. This is a DNS-rebinding risk for the passwordless local service, subject to the browser's current private-network protections.

**Impact:** A deployment mistake can become unauthenticated remote access to recordings and export controls. A local passwordless deployment may also be reachable through DNS rebinding in browser environments that permit the request.

**Recommendation:** Enforce the non-loopback/password invariant inside application startup or middleware, not only in the console entry point. Add a strict trusted-host/origin allowlist, do not derive authorization decisions from unvalidated forwarded headers, and consider a random local session token even for loopback mode. Add tests that import the ASGI object directly and send hostile `Host`, `Origin`, and forwarded-header combinations.

### ~~HIGH-2: Windows cancellation can strand multi-gigabyte partial exports~~ — Resolved

**Affected code:** `native-windows/BackendProcess.cs:118-125`, `native-windows/MainWindow.xaml.cs:790-797`, `native-windows/MainWindow.xaml.cs:873-899`, `timelapse/download.py:137-199`, `timelapse/config.py:23-24`

The Windows client cancels an export by immediately killing the Python helper process tree. The helper creates a hidden `.part` file and removes it only from a Python `finally` block. Forceful process termination prevents that cleanup from running. No startup or periodic stale-part cleanup was found.

The UI tells the user that the partial file is removed, but a single abandoned part can approach the default 10 GiB export limit. Repeated cancellations can therefore consume the destination disk.

**Impact:** Routine cancellation or application shutdown can leak large files and eventually cause disk exhaustion. Later exports and unrelated applications may then fail.

**Recommendation:** First request cooperative cancellation and wait for a bounded grace period before killing the helper. Independently clean stale `.part` files at startup and before choosing an output path. The helper protocol should acknowledge cleanup before the UI reports cancellation complete.

### ~~HIGH-3: Camera-name filename collisions can silently omit scheduled recordings~~ — Resolved

**Affected code:** `timelapse/download.py:49-55`, `timelapse/web_state.py:306-327`, `timelapse/web_state.py:413-447`

Output names contain a sanitized, 48-character-truncated camera display name but no stable camera identifier. Two cameras with the same name—or different names that sanitize or truncate to the same value—produce the same output path for the same range and speed. The web job manager launches all selected cameras concurrently without reserving output paths.

A direct reproduction with two distinct camera IDs and the same display name produced identical paths. For daily schedules, a collided output can later be classified as `skipped`, and the scheduler treats `skipped` as successful. The day can be marked complete with only one camera's recording present.

**Impact:** Multi-camera exports can contend for one file, fail unpredictably, or silently leave an incomplete daily archive.

**Recommendation:** Include a stable, filesystem-safe camera ID or short hash in every filename. Reserve destinations atomically before launching work, reject duplicate paths within a batch, and mark a scheduled day complete only after every expected camera has a distinct validated artifact.

### ~~HIGH-4: Desktop daily schedulers advance their checkpoint before exports succeed~~ — Resolved

**Affected code:** `timelapse/gui.py:1804-1825`, `timelapse/gui.py:1856-1863`, `native-macos/Sources/TimeLapseNative/AppModel.swift:432-460`, `native-macos/Sources/TimeLapseNative/AppModel.swift:805-893`, `native-windows/MainWindow.xaml.cs:499-522`, `native-windows/MainWindow.xaml.cs:697-770`, `native-windows/MainWindow.xaml.cs:914-933`

The Qt, macOS, and Windows schedulers update `last_run_day` when work is merely queued or launched. They do not roll the checkpoint back when an export fails or is cancelled, and no persisted per-camera completion state exists. On the next timer tick, the scheduler moves on rather than retrying the missed day.

The desktop implementations also choose a suffixed filename when the expected output already exists. Restarting or re-enabling a schedule can therefore duplicate successful artifacts while still failing to repair an unsuccessful camera/day.

**Impact:** A transient network, authentication, storage, or cancellation event can create a permanent gap in an unattended archive without a later automatic retry.

**Recommendation:** Persist completion per schedule, day, and camera. Advance the day checkpoint only when every required artifact has completed and passed validation. Retry only failed/missing cameras with bounded backoff, and make the expected daily output idempotent instead of automatically suffixing it.

## Medium

### ~~MEDIUM-1: Web export work has no global concurrency, queue, or storage bound~~ — Resolved

**Affected code:** `timelapse/web_state.py:287-327`, `timelapse/web_state.py:454-462`, `timelapse/web.py:453-521`

Every accepted request creates one asynchronous task per selected camera. There is no semaphore, active-job limit, per-session quota, duration cap, or aggregate disk budget. `_trim_jobs` removes only terminal jobs, so it cannot control active work. A targeted check created 105 simultaneously active jobs without rejection.

**Impact:** Repeated clicks, a buggy client, or an authenticated LAN user can exhaust Protect connections, sockets, memory, bandwidth, and disk. The default per-export size limit does not cap aggregate usage.

**Recommendation:** Put exports through a bounded queue, cap active and queued jobs globally and per session, limit requested duration, reserve disk space, and enforce an aggregate storage quota. Return `429` or `503` when capacity is exhausted.

### ~~MEDIUM-2: Any HTTP 2xx response, including an empty body, is accepted as a completed video~~ — Resolved

**Affected code:** `timelapse/download.py:118-183`

The downloader checks the status code and maximum byte count, but it does not require a non-empty response or validate the media type/container. A direct reproduction with an empty HTTP 200 response produced a zero-byte final `.mp4` and no error.

**Impact:** Upstream/proxy failures that return an empty or non-video 2xx body are reported as successful exports. Schedulers can then consider a recording complete even though it is unusable.

**Recommendation:** Reject empty downloads, validate an expected content type where reliable, and perform a lightweight MP4/container sanity check before the atomic rename. Persist and display a validation failure instead of success.

### ~~MEDIUM-3: A syntactically valid but malformed schedule state file can prevent web startup~~ — Resolved

**Affected code:** `timelapse/web_state.py:605-634`, `timelapse/web_state.py:235-242`

Schedule loading assumes the decoded JSON root is a mapping and immediately calls `.get`. A valid JSON array therefore raises `AttributeError`; this was reproduced with `[]`. Nested schedule values also receive only partial structural validation. Because schedule loading runs during startup, one malformed state file can keep the web application offline.

**Impact:** A partial/manual edit, incompatible old state, or unexpected serialization bug becomes a persistent denial of service until an operator finds and repairs the file.

**Recommendation:** Validate a versioned schema before use. On invalid state, log a precise warning, move the bad file aside, and start with an empty state rather than crashing. Add corruption and migration tests.

### ~~MEDIUM-4: The CLI daily scheduler exits permanently on the first transient export error~~ — Resolved

**Affected code:** `timelapse/cli.py:118-141`

The continuous daily loop has no per-day/per-camera recovery boundary. An exception from one export escapes the loop and terminates the process with an error. It has no bounded retry, backoff, or persisted checkpoint from which a supervisor can safely resume.

**Impact:** A short Protect outage or temporary disk problem stops all future daily exports until a person or external supervisor notices and restarts the command.

**Recommendation:** Retry transient failures with exponential backoff and jitter, retain the current day until all cameras succeed, expose a clear health state, and document whether an external service manager is required.

### ~~MEDIUM-5: Persistent web schedule failures retry forever at a fixed one-minute interval~~ — Resolved

**Affected code:** `timelapse/web_state.py:413-447`

The web scheduler retries any failed/cancelled daily batch every 60 seconds indefinitely. There is no exponential backoff, maximum attempt count, pause state, or distinction between permanent configuration errors and transient failures. Each attempt also creates new job records.

**Impact:** Bad credentials, an unwritable destination, or an invalid request can hammer the Protect service and local storage/logging indefinitely while obscuring the original failure in repeated job history.

**Recommendation:** Classify failures, use capped exponential backoff with jitter, pause after a bounded number of attempts, and surface an operator-visible “needs attention” state with an explicit retry action.

### ~~MEDIUM-6: Web state changes and background task creation are not transactional~~ — Resolved

**Affected code:** `timelapse/web_state.py:306-365`, `timelapse/web_state.py:535-634`

Export tasks are started before job-state persistence completes. If the state write fails, the request can report an error while untracked exports continue. Conversely, schedule data is inserted and persisted before its background task is started; a persistence or task-start failure can leave memory, disk, and runtime behavior disagreeing. Background persistence errors have little diagnostic context.

**Impact:** Disk-full, permission, or filesystem failures can create ghost exports, inactive in-memory schedules, duplicate retries, or lost status after restart.

**Recommendation:** Define a clear state machine and commit durable state before side effects. Roll back in-memory mutations on persistence failure, start tasks only from committed records, and reconcile non-terminal records at startup. Use structured logging for every persistence failure.

### ~~MEDIUM-7: CI does not compile or behavior-test either native desktop client~~ — Resolved

**Affected code:** `.github/workflows/ci.yml:42-95`, `tests/test_native_windows_project.py`

The CI workflow runs on Ubuntu and validates Python, JavaScript syntax, shell syntax, and the Docker build. It does not run `swift test`/a macOS build or a Windows `dotnet build`. The Windows project tests are source-string assertions rather than execution tests. Native scheduling and cancellation behavior—the areas containing several findings above—has no meaningful automated coverage.

Both native projects compiled successfully during this review, and the existing Swift tests passed; the gap is regression protection, not a current build failure.

**Impact:** Platform-specific compile failures and behavioral regressions can reach a release despite a green CI run.

**Recommendation:** Add pinned macOS and Windows jobs that restore/build the native clients and run their tests. Extract scheduler, filename, process-cancellation, and checkpoint logic into testable units and add failure-path tests.

## Low

### ~~LOW-1: The configured request timeout is not an end-to-end operation deadline~~ — Resolved

**Affected code:** `timelapse/download.py:91-99`, `timelapse/service.py:37-161`

The configured timeout is passed to the export request, but upstream authentication/retry behavior can consume additional timeout windows. Camera discovery and thumbnail calls do not consistently apply the same configured deadline. An operation can therefore take substantially longer than the setting's documented meaning suggests.

**Impact:** Cancellation and failure detection can feel hung, particularly during Protect outages.

**Recommendation:** Wrap the complete logical operation in one monotonic deadline, pass the remaining budget to each subrequest, and document distinct connect/read/whole-operation timeouts if all three are needed.

### ~~LOW-2: Cleanup exceptions can mask the original operational error~~ — Resolved

**Affected code:** `timelapse/service.py:201-221`, `timelapse/download.py:194-199`

Client-session cleanup catches only a narrow timeout case, and temporary-file unlinking is performed from `finally` without preserving an already active exception. A cleanup `OSError` or unexpected close failure can replace the more useful authentication, export, or cancellation error.

**Impact:** Logs and UI error messages may point to cleanup rather than the actual failure, making diagnosis harder.

**Recommendation:** Preserve the primary exception, log cleanup failures separately, and make cleanup best-effort unless resource integrity requires escalation.

### ~~LOW-3: Web action failures are commonly returned as HTTP 200~~ — Resolved

**Affected code:** `timelapse/web.py:453-544`

Several form/action routes catch broad exceptions and render an error fragment without an appropriate non-success status. Internal failures, validation failures, and capacity failures are therefore indistinguishable from successful requests to clients, proxies, and monitoring.

**Impact:** Automation and observability can record failed operations as successful, and broad exception handling can hide programmer defects.

**Recommendation:** Catch expected domain exceptions narrowly and map them to `4xx`/`5xx` responses. Let unexpected exceptions reach centralized logging/error middleware with a correlation ID.

### ~~LOW-4: The Windows export action does not handle destination-directory creation failures~~ — Resolved

**Affected code:** `native-windows/MainWindow.xaml.cs:644-670`

The regular export path calls `Directory.CreateDirectory` outside an error boundary, while the daily path explicitly handles the same kind of failure. Permission, invalid-path, or I/O errors can escape the UI event handler.

**Impact:** Selecting an unwritable or unavailable destination may trigger an unhandled UI exception instead of an actionable message.

**Recommendation:** Validate/create the directory inside the existing error path, preserve the UI state, and show a specific remediation message. Share this logic with the daily scheduler.

### ~~LOW-5: Container base images are tag-pinned but not digest-pinned~~ — Resolved

**Affected code:** `Dockerfile:1-3`

The Python and `uv` build images are selected by mutable tags. The application dependency lock is strong, but rebuilding the same commit later can still consume different base-image bytes.

**Impact:** Builds are less reproducible and inherit a small supply-chain drift window.

**Recommendation:** Pin production and build images by digest, update them through an audited dependency bot, and retain the readable tag alongside the digest.

### ~~LOW-6: The test suite relies on a deprecated Starlette/httpx test-client path~~ — Resolved

**Affected code:** `pyproject.toml`, `tests/test_web.py`

The full test run passes but emits a Starlette deprecation warning for the current `httpx` integration. Leaving the warning unaddressed makes a future dependency upgrade more likely to turn it into a test failure at an inconvenient time.

**Impact:** Test maintenance debt and an avoidable future upgrade break.

**Recommendation:** Follow the supported Starlette/FastAPI transport path for the locked versions, then make deprecation warnings visible or fatal in CI so new ones are handled deliberately.

## Validation performed

- `ruff check .`: passed.
- Ruff formatting check: passed for all 25 Python files.
- Pyright: 0 errors.
- Python tests: 91 passed; one deprecation warning described in LOW-6.
- JavaScript syntax check for `timelapse/static/app.js`: passed.
- Shell syntax checks: passed.
- macOS package build and `swift test`: passed, 11 tests.
- Windows Release `dotnet build`: passed with 0 warnings and 0 errors.
- Locked runtime dependency audit with `pip-audit 2.10.1`: no known vulnerabilities reported as of the review date.
- Repository secret-pattern and history checks found no tracked `.env` file or obvious committed credential. The local ignored `.env` contents were not inspected.
- Targeted reproductions confirmed duplicate-camera path collisions, successful zero-byte downloads, startup failure on a JSON-array schedule file, acceptance of a matching attacker-supplied `Host`/`Origin`, and lack of an active web-job cap.

## Review limits

- No live Protect appliance or hostile network was used, so upstream response variations and appliance-specific authorization behavior were assessed from call sites and tests.
- macOS and Windows were compiled/tested, but their full graphical workflows were not exercised interactively.
- The dependency audit reflects public advisories and the exact locked runtime versions on 2026-07-21; it is not a guarantee that no undisclosed vulnerability exists.
