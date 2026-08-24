# Collapse daily automation policy

## Summary

Replace five scheduling implementations with two shared policy modules and one concrete persistence module:

- `DailyAutomationEngine` owns catch-up, retry, pause, resume, stop, and remove transitions.
- `ExportJobCoordinator` owns manual and daily job transitions, concurrency, cancellation, output reservations, and rate-limit queueing.
- `AutomationRegistry` owns durable schema, exclusive ownership, atomic persistence, migration, and recovery.

CLI, web, and Qt use them in process. macOS and Windows use them through one long-lived backend supervisor per app.

## Placement

- Deepen `timelapse/schedule.py` into the `DailyAutomationEngine`; keep calendar bounds, catch-up, batch completion, and lifecycle transitions together.
- Add `timelapse/jobs.py` for `ExportJobCoordinator`; move shared job state, reservations, cancellation, capacity, and rate-limit eligibility out of UI models.
- Add `timelapse/automation_registry.py` for the concrete registry implementation and migrations.
- Turn `timelapse/native_backend.py` into the session supervisor while retaining one-shot health mode. Keep CLI, `WebState`, and Qt as in-process adapters, and make Swift and C# models protocol clients and UI projections.

## Domain and interfaces

- Add `CONTEXT.md` defining:
  - **Daily Automation**: durable intent to export every completed calendar day.
  - **Export Batch**: all camera exports for one automation and calendar day.
  - **Export Job**: one camera export within a batch.
- Record an ADR for per-entrypoint registries and the native session supervisor.
- Persist each automation with:
  - Immutable ID; unique display name; camera IDs and display names; connection reference; speed; output directory; IANA timezone; and creation time.
  - Status: `active`, `paused`, `stopped`, or `removing`.
  - Next unprocessed day, consecutive failure count, next retry time, and last error.
- Trim and Unicode-normalize display names, enforce uniqueness with case folding, and resolve selectors by immutable ID before normalized name. Reject names that parse as automation IDs so CLI selectors cannot be ambiguous.
- Add one concrete, deep `AutomationRegistry` module that owns schema validation, the lifetime owner lock, atomic replacement, migration, and recovery. Use real registry files in temporary directories for tests; do not introduce a public storage interface until a second production backend exists. Never persist credentials.
- Store a tagged, non-secret connection reference: Python profile ID or name, absolute dotenv path, Web environment, or native profile UUID. Resolve it through the owning entrypoint before every batch. Preserve stable IDs across profile renames where the credential store supports them; deleting or failing to resolve a reference pauses dependent automations immediately.
- Capture and validate an IANA timezone when creating each Daily Automation. Add `tzlocal` and `tzdata`: CLI and Qt default to `tzlocal.get_localzone_name()`, macOS uses `TimeZone.current.identifier`, Windows converts `TimeZoneInfo.Local.Id` with `TryConvertWindowsIdToIanaId`, and Web uses its configured name. Validate every result with Python `ZoneInfo`; if discovery or conversion fails, require an explicit IANA name.
- Add CLI commands:
  - `automation add`, `edit`, `list`, `run`, `resume`, `stop`, `remove`, and `re-export`.
  - Accept an automation's unique display name or immutable ID for management commands.
  - Keep `--daily`; it imports or finds a matching automation, reactivates it only when stopped, and runs only that automation. It never clears a safety pause or implicitly starts other active CLI automations.
  - Add an automation-day re-export command that schedules missing or damaged artifacts without rewinding later Processed Days.
- Define `automation add` with a required unique name, repeatable camera selector, speed, absolute output directory, IANA timezone, and exactly one named profile or absolute dotenv reference. Resolve selectors to stable camera IDs at creation. Provide scriptable `list --json`; make `run` the long-lived runner; accept name or ID for edit, stop, resume, remove, and re-export.
- Use CLI exit code 0 for success, 1 for an operational failure, 2 for invalid syntax or input, and 3 when another process owns the registry. List, stop, resume, and remove must not require Protect availability or secrets.
- Implement CLI owner control with a user-scoped Unix socket on macOS/Linux and a current-user-restricted named pipe on Windows. Record the endpoint and a random session nonce in owner-lock metadata; management commands verify both. Rely on OS-released locks for safe stale-owner recovery after crashes.
- Allow editing a Daily Automation's display name, connection reference, and camera set. Removing a camera changes the current and future batches; adding or replacing one starts at the current unprocessed day, with explicit re-export required for older Processed Days. Keep output directory, speed, and timezone immutable; changing artifact identity or calendar history requires a new automation.

## Implementation

- Process every missed day oldest-first. Advance the stored day only after every selected camera has a validated MP4.
- Initialize a new Daily Automation at yesterday in its captured timezone. Apply the same rule when a migrated Web automation has no prior progress; preserve an imported CLI checkpoint's existing `next_day`.
- Retry only missing or invalid camera exports after partial batch success.
- Give each automation-day five Export Batch attempts before pausing. Retain successful camera artifacts between attempts. Delays are 60, 120, 240, and 480 seconds plus up to 20% jitter.
- Identify a normal Export Batch by automation ID, local calendar day, and generation zero. A re-export that schedules work increments the generation; a valid-artifact no-op does not. Identify each Export Job by its batch ID and camera ID; retrying updates its attempt count rather than creating duplicate jobs or history rows.
- Treat only a regular, non-empty MP4 with an `ftyp` marker in its first 4 KiB as complete. Check pre-existing paths before job submission; an unreadable or invalid collision pauses the automation without deleting the path.
- Require an output directory to exist at automation creation. Store its absolute real path and compare existing directories with filesystem identity so symlinks and case aliases cannot evade overlap checks. If the directory later disappears or becomes inaccessible, pause before submitting work.
- Reject a Daily Automation whose canonical output directory, camera ID, and speed overlap any retained automation, regardless of timezone: daily filenames do not contain the timezone, so different calendars can otherwise claim one path for different footage. Stopping does not release ownership; removal does.
- Stopping prevents future Export Batches but lets every running or coordinator-queued Export Job in the submitted batch finish. A successful current batch still marks its day processed, but the next day does not start.
- Removing a Daily Automation first stops future batches, lets its active jobs finish, and removes the definition only after those jobs terminate. Existing Export Artifacts remain, and artifact ownership stays reserved until removal completes.
- Deleting an Export Artifact removes only that MP4 and its job entry. It does not stop its Daily Automation or rewind the automation's Processed Day. Re-exporting that day requires the explicit re-export action.
- Delete only a tracked, exact canonical regular file within its automation's output directory; refuse symlinks, directories, and untracked paths. Persist deletion intent before unlinking and remove the job row only after success. On restart, reconcile a persisted deletion against an already absent file. A filesystem error leaves the row and Processed Day intact and reports an actionable error.
- For explicit re-export, an absent canonical artifact becomes normal work. A valid artifact is a no-op. Move a tracked but invalid artifact to a timestamped quarantine path before exporting; never silently delete it. An untracked collision pauses for human review.
- Permit explicit re-export while an automation is `active` or `stopped` without changing its status or next unprocessed day. Reject re-export while `paused` until the safety issue is resolved and the automation is resumed; reject it while `removing`.
- Resolve the stored connection reference before every Export Batch, so edits to a referenced profile affect future work. Pause with an actionable error if the profile or environment configuration is unavailable.
- Resolve every selected camera before submitting the batch. If any camera is missing, pause immediately without consuming an attempt or silently dropping it; after the user removes or replaces the camera, Resume continues the same day.
- Require a named credential profile or an absolute `.env` path for CLI Daily Automations. Continue accepting bare credential flags for manual exports, but reject them for `--daily` and `automation add` because they cannot be resolved after restart.
- Standardize job-capacity defaults at four active and twenty queued exports per entrypoint runtime while retaining existing storage safeguards. Keep the Web environment settings and expose equivalent validated runtime configuration elsewhere.
- Persist every Export Job in an oversized batch before execution, then lazily admit only enough work to fill the configured active and queued capacities. As slots open, admit the remaining jobs. Stop and Remove still let the entire persisted current batch drain, including jobs not yet admitted, but never begin a later day.
- Count a job as active when its execution coroutine starts, including time spent waiting for Protect's existing two-slot machine-level private-operation lock. Count coordinator-admitted waiting jobs against the queued limit; durable oversized-batch jobs awaiting lazy admission do not consume queue capacity.
- Make manual-export collision policy an explicit entrypoint choice at submission: Qt, macOS, and Windows retain suffix generation; CLI and Web retain reject-on-collision. Daily Automation paths remain canonical: a valid artifact skips its job and an invalid collision pauses the automation.
- Treat a structured `Retry-After` as a hard minimum before a rate-limited job may retry. Without `Retry-After`, another job terminating or the bounded fallback timer may make one queued job eligible.
- Carry rate limits as `ProtectRateLimitError(retry_not_before: datetime | None)` using a UTC instant. A present value is a hard floor. Without one, a sibling terminal event wakes exactly one queued job; a 60-second fallback wakes one when no sibling completes, so a lone job cannot wait forever.
- Keep at most four low-level HTTP attempts inside the Protect client and add no independent coordinator retry budget. One Export Batch attempt submits each missing camera once; an exhausted HTTP or rate-limit failure ends that round. The next batch attempt waits for both its batch backoff and every applicable `Retry-After` deadline. Five batch attempts therefore permit at most twenty HTTP attempts for one camera and day.
- Give manual and daily exports one runtime-wide coordinator so they share rate-limit and cancellation policy within their owning process or app session. Cross-process Protect requests continue to share the existing machine-level operation locks.
- Persist registries atomically after every meaningful transition.
- Hold an exclusive registry-owner lock for the automation runner's lifetime. Route management commands to a running CLI owner through its local control channel. When no runner exists, a management command acquires the lock, rereads the registry, applies its transition, and atomically replaces the file. A second owner must refuse to start.
- Persist the Export Batch and every Export Job intent before admitting work to the coordinator. A persistence failure prevents the corresponding enqueue or launch.
- Finalize each Export Artifact with an atomic rename, persist the job's terminal state, and advance the Processed Day only after every expected artifact validates. On restart, reconcile durable intents against artifacts before launching only the missing work.

### Lifecycle

- `active` may start the oldest due Export Batch. `paused` preserves work after a safety or configuration failure and requires explicit Resume. `stopped` preserves the definition but starts no new batch. `removing` starts no new batch, waits for the submitted batch to terminate, and then deletes the definition.
- Resume accepts `paused` or `stopped` and resets the batch-failure counter. Legacy `--daily` may reactivate a stopped matching automation, but it never clears a safety pause.
- Once Stop wins the serialized transition, it remains `stopped` while the submitted batch drains. Batch success may advance the Processed Day; failure records its error and attempt count but cannot change the status to `paused`. Remove similarly remains `removing` and deletes the definition after the batch terminates, regardless of its outcome.
- Runtime shutdown and supervisor interruption leave the current batch recoverable and do not consume an Export Batch attempt. An explicit user cancellation is a failed attempt. Configuration and invalid-artifact collisions pause immediately without consuming the five-attempt budget.
- A fully successful batch resets the consecutive failure count. Partial success retains valid artifacts, increments the batch failure count once, and retries only the missing or invalid jobs.

### Entrypoint adapters

- Web: replace scheduling logic in `WebState` with engine calls while preserving SSE updates, quotas, and job history. Rename schedule routes and views to Daily Automations with distinct Stop, Resume, and Remove actions; reserve Delete for Export Artifacts and remove the ambiguous schedule-delete action rather than retaining a 2.0 alias.
- Qt: replace the single daily checkbox with a multi-automation list and Add, Edit, Stop, Resume, and Remove actions.
- CLI: run all active CLI-registry automations in one process; manual export syntax remains unchanged.
- macOS and Windows:
  - Start a version-2 backend session with persistent stdin and multiplexed stdout events.
  - Complete a version handshake before accepting credentials or work. Give every command a request ID, serialize protocol writes, reject duplicate IDs within a session, and emit exactly one terminal acknowledgement for every accepted command. Isolate malformed commands, and acknowledge cancellation and graceful shutdown.
  - Reject a protocol-version mismatch before starting work. Keep one-shot health mode for packaging checks; use stable entity IDs to make durable create commands idempotent across sessions.
  - Support camera, thumbnail, manual export, automation CRUD, job cancellation, state snapshot, and shutdown messages.
  - Treat the Python supervisor as authoritative for durable automation, batch, job intent, and terminal state. Native models provide credentials and project supervisor snapshots into UI state; manual commands use stable IDs so artifact reconciliation can recover a completion whose event was lost.
  - After an unexpected exit, restart once after one second, rehydrate referenced credentials, reconcile durable state and artifacts, and resume only missing work. If the restarted supervisor exits again within five minutes, start a quiescent recovery session that pauses active automations, marks manual jobs interrupted, reconciles state, and launches no work until explicit Resume. Reset this crash budget after five continuous healthy minutes.
  - Before each Export Batch, hydrate only the referenced profile's credentials from Keychain or Credential Manager into supervisor memory. Repeat hydration after a profile edit or supervisor restart; never write secrets to the registry.
  - Present the same Add, Edit, Stop, Resume, and Remove vocabulary as Web and Qt; reserve Delete for an Export Artifact.
- Entrypoints cancel active jobs when shutting down. Automations remain active and resume on the next start.
- Store each entrypoint's registry separately. No machine-wide scheduler or background daemon.
- One runtime process or app session is the sole automation owner for its registry. A CLI runner exposes a local control channel for management commands; a second GUI instance refuses automation ownership instead of starting duplicate work. Manual exports in another process remain independent.
- Persist every nonterminal job until reconciliation. Apply terminal-history retention as entrypoint policy: Web keeps its latest 100 jobs, CLI removes terminal records after reporting, and desktop apps retain session-visible history but remove terminal records after clean shutdown. Durable automation progress and re-export generations do not depend on retained job history.

### Migration and delivery

- Migrate web schedule schema version 1 into the common registry schema.
- For each migrated Web version-1 automation, capture the server's configured IANA timezone and output directory, store a `web-environment` connection reference for future credential rotation, derive a unique display name, and convert `last_run_day` to the next unprocessed day. If `last_run_day` is null, initialize at yesterday. Fail migration safely if the timezone cannot be resolved.
- Import a legacy CLI checkpoint when `--daily` first creates or matches an automation.
- Match a legacy CLI checkpoint by canonical output directory, camera ID, and speed; timezone is captured configuration, not artifact identity. Back it up first, then atomically persist the automation with the checkpoint's `next_day`, source fingerprint, and import marker before retiring the checkpoint. Repeating migration after any crash is idempotent and cannot duplicate the automation or change its backlog.
- Back up legacy files before atomic migration and quarantine malformed state without overwriting it.
- Deliver as staged commits: core model, job coordinator, Python adapters, native supervisor, UI migrations, then deletion of copied policy.
- Ship all entrypoints together as version 2.0.0. Document the intentional compatibility changes: durable multi-automation desktops, distinct Web Stop and Remove behavior, named-profile or absolute-dotenv requirements for CLI Daily Automations, runtime capacity defaults, native shared storage reservations, and timed recovery for a lone rate-limited job.

## Test plan

- Deterministic engine tests covering multi-day catch-up, DST transitions, captured timezones, partial camera success, pause and resume, stop semantics, and missing connection references.
- Artifact tests for valid MP4s, empty files, directories, corrupt media, inaccessible paths, collisions, and interrupted downloads.
- Coordinator tests for mixed manual and daily jobs, concurrency limits, one-at-a-time rate-limit wakeups, lone-job timed retry, retry exhaustion, sibling-safe cancellation, and shutdown.
- Capacity tests for batches larger than active-plus-queued limits, lazy admission, and stopping while persisted jobs have not yet entered the coordinator queue.
- Collision-policy tests preserving desktop suffix behavior, CLI/Web rejection, and canonical Daily Automation validation.
- Persistence tests for atomic writes, schema round-trips, malformed records, web migration, CLI checkpoint import, and recoverable backups.
- Crash-cut tests after batch persistence, job admission, process launch, artifact rename, terminal-state persistence, and Processed Day advancement; every restart must converge without duplicate jobs or skipped days.
- Adapter tests for CLI commands, web routes, Qt multi-automation behavior, and executed Swift and C# supervisor sessions.
- Native integration tests covering interleaved job IDs, per-job cancellation, graceful shutdown, supervisor restart, and protocol-version mismatch.
- Native protocol tests covering handshake-before-work, serialized concurrent events, duplicate request IDs, malformed-message isolation, exactly-one terminal acknowledgements, quiescent recovery, and the five-minute crash-budget reset.
- Run `ruff check`, `ruff format --check`, `pyright`, `pytest`, `swift test`, and Windows build and test checks.
- Add a Windows xUnit project and extend Swift XCTest. Both launch a shared protocol-fixture executable through `TIMELAPSE_BACKEND_PATH` for interleaving, cancellation, crash, and mismatch cases. Python tests drive the real supervisor through in-memory streams with fake operation functions; packaging smoke tests continue to launch the real one-shot health executable.

## Assumptions

- Daily Automations run only while their owning CLI, web server, or desktop app is running.
- Registries are per entrypoint/runtime and are never shared implicitly.
- UI presentation and notifications remain platform-specific adapters.
- Existing manual naming behavior stays intact. Shared queue limits, timed rate-limit recovery, and native cross-job storage reservations are intentional 2.0 behavior changes.
