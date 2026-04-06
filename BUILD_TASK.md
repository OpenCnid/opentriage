# BUILD TASK: Active Remediation — Self-Healing Agent Loop

## Context

OpenTriage is a pip-installable failure-response engine. Core features (F-OT01 through F-OT07) are built with 155 tests. The remediation engine (`src/opentriage/remediation/`) has basic subprocess/callback/noop handlers and budget tracking already implemented.

**What's needed:** Upgrade the remediation engine from "run a subprocess" to "assemble evidence, spawn a coding agent, verify the fix, track recurrence." This is the self-healing loop that closes the gap between "error classified" and "error fixed."

**Spec:** `specs/active-remediation.md` (6 features: F-AR01 through F-AR06)
**Failure map:** `specs/active-remediation-failure-map.md` (20 failure patterns identified, 9 amendments applied to spec)

## Implementation Sequence

Build in this order — each feature depends on the previous:

### Phase 1 (parallel): F-AR01 + F-AR02

**F-AR01: Screenshot-Enriched Error Events**
- This feature is for the rocky-idle-harness project, NOT this codebase
- Skip F-AR01 for now — it requires changes to a different repo
- Mark as DEFERRED in your scorecard

**F-AR02: Structured Remedy Format**
- Upgrade `remedy` field in fingerprints from plain string to structured dict
- Schema: `{strategy, description, relevant_files, test_command, fix_prompt, max_cost_usd, requires_screenshot}`
- Update `io/reader.py` `load_fingerprints()` to handle both string and dict remedy fields (backward compatible)
- Update `io/writer.py` if needed for writing structured remedies
- Add migration logic: string remedies → `{"strategy": "escalate", "description": "<original>"}`
- Tests: verify both string and dict remedies load correctly, verify migration

### Phase 2: F-AR03 Evidence Bundle Assembler

- Create `src/opentriage/remediation/evidence.py`
- `assemble_evidence(correlation, fingerprint, openlog_dir, opentriage_dir) -> dict`
- Include: error event, screenshot path (validate exists), fingerprint + structured remedy, last 20 session events, last 10 correlations for same slug, git context, relevant files
- Sanitize error content: truncate f_raw to 500 chars, strip control chars
- Total bundle < 50KB (truncate session events if needed)
- Write to `.opentriage/remediations/{attempt-id}/evidence.json`
- Tests: mock event + fingerprint → assemble → assert all fields present

### Phase 3: F-AR04 Fix Agent Spawner

- Create `src/opentriage/remediation/agent_handler.py`
- `spawn_fix_agent(evidence: dict, config, project_dir) -> dict`
- Build prompt from evidence bundle, write to `.opentriage/remediations/{attempt-id}/prompt.md`
- Execute via subprocess: `claude --print --permission-mode bypassPermissions -p "$(cat prompt.md)"` in project dir
- Timeout: 300s (configurable)
- Post-fix verification checklist (ALL must pass for status="fixed"):
  1. Git diff is non-empty
  2. Changed files overlap with fingerprint's relevant_files
  3. No test files modified with reduced assertion count
  4. Total files changed ≤ 5 and lines changed ≤ 200
  5. Tests ran and produced parseable pass/fail output
- If any check fails: status="suspicious", escalate
- Fix prompt must include: "Fix ONLY the error described. Do NOT modify test expectations. One bug, one fix."
- Write result to `.opentriage/remediations/{attempt-id}/result.json`
- Tests: mock the subprocess call, verify prompt generation, verify post-fix checks

### Phase 4: F-AR05 Remediation Orchestrator

- Upgrade `remediation/engine.py` `run_remediation()` to handle structured remedies:
  - `strategy: "code-fix"` → assemble evidence → spawn fix agent
  - `strategy: "restart"` → touch restart sentinel file
  - `strategy: "config-change"` → apply config described in remedy
  - `strategy: "escalate"` → route to escalation channel
- Add circuit breaker: 3 consecutive failures for same fingerprint → 24h suspend
- Store circuit breaker state in `.opentriage/state.json` under `circuit_breakers{}`
- Add skip patterns: check `f_raw` against configurable regex list, skip matching errors
- Serial execution: queue fix agents by severity, run one at a time
- Deduplicate: same fingerprint in same cycle → remediate only the first
- Daily spend tracking in state.json
- Tests: verify routing by strategy, circuit breaker behavior, deduplication

### Phase 5: F-AR06 Recurrence Verification

- When remediation succeeds: record `{fingerprint_slug, fixed_at_ts, attempt_id, commit_sha, recurrence_window_hours: 6}` in `state.json` `pending_verifications[]`
- Each triage cycle: check pending verifications against new correlations
- Same fingerprint recurs → status="recurred", increment circuit breaker failure count
- Window expires with no recurrence → status="verified"
- If failure count < 3 → re-attempt with updated context
- If failure count ≥ 3 → suspend fingerprint, escalate
- Tests: simulate fix → no recurrence → verify; simulate fix → recurrence → verify failure

## Critical Constraints

- **DO NOT modify test expectations to make tests pass** (F020 defense)
- **DO NOT break existing tests** — current test count is the baseline
- **Backward compatible** — string remedies must still work everywhere
- **Config-driven** — all thresholds go in `config.toml` `[remediation]` section
- **State file schema:** `.opentriage/state.json` must include both `pending_verifications[]` and `circuit_breakers{}`
- **Atomic file writes** — use write-to-temp + rename for `fingerprints.json` and `state.json`

## Testing Requirements

- Run tests with: `python3 -m pytest tests/ -v`
- Every new module needs corresponding test file in `tests/`
- Mock external calls (subprocess, file I/O where needed)
- Test count must not decrease

## Files You'll Touch

**New files:**
- `src/opentriage/remediation/evidence.py`
- `src/opentriage/remediation/agent_handler.py`
- `tests/test_evidence.py`
- `tests/test_agent_handler.py`
- `tests/test_recurrence.py`

**Modified files:**
- `src/opentriage/remediation/engine.py` (major upgrade)
- `src/opentriage/remediation/handlers.py` (add agent handler routing)
- `src/opentriage/io/reader.py` (structured remedy support)
- `src/opentriage/config.py` (remediation config section)
- `tests/test_remediation.py` (update for new engine behavior)
