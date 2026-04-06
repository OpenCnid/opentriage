# Active Remediation — Self-Healing Agent Loop

## Problem Statement

When the Rocky Idle bot runs unattended, it encounters runtime failures — selector drift, auth expiry, strategy bugs, page crashes — at a rate of 5-15 errors per hour (observed from OpenLog data: 49 errors across ~2 hours of Ralph building). Currently, OpenLog captures these errors and OpenTriage classifies them, but the pipeline terminates at alerting. No automated fix is attempted.

**Quantified impact:** Every unrecoverable bot failure means downtime until a human reads the alert in #harness-alerts, diagnoses the issue, writes a fix, and restarts. For a bot designed to run 24/7, even 1 failure per hour that requires manual intervention means the bot is effectively unusable without babysitting.

**Root cause:** OpenTriage's remediation engine has the classification data, the fingerprint context, and the error details, but no mechanism to translate "known failure pattern with documented remedy" into "spawn a coding agent, apply the fix, verify it works, restart the bot."

**Affected systems:** Rocky Idle Harness (primary consumer), but the architecture is generic — any OpenLog-instrumented project could use this.

## Design Principles

| ID | Principle | Rationale |
|----|-----------|-----------|
| DP-01 | Evidence-first diagnosis | Every remediation attempt must include the error event, screenshot (if available), relevant source file paths, and recent session context. No "it seems like" reasoning. |
| DP-02 | Verify before claiming fixed | The fix agent must run tests after applying changes. OpenTriage must verify the error pattern doesn't recur within N cycles. A fix that passes tests but recurs is not a fix. |
| DP-03 | Budget-bounded autonomy | Each remediation attempt has a cost ceiling ($2 default), retry limit (3 default), and time limit (5 min default). Exceeding any limit escalates to human. |
| DP-04 | Never modify test expectations | A remediation agent must fix the code, not weaken the tests. If the test itself is wrong, escalate to human. (F020 defense) |
| DP-05 | Append-only audit trail | Every remediation attempt — success or failure — is logged immutably. No rewriting history. |
| DP-06 | Screenshot-aware diagnosis | When a screenshot exists alongside an error event, the remediation agent must analyze it. DOM selector failures are often diagnosable from screenshots alone. |
| DP-07 | Graceful degradation | If the remediation system itself fails, the bot continues running (or restarts cleanly). The fix loop must never make things worse. |

## Definitions

- **Error event:** An OpenLog JSONL line with `kind: "error"` and a non-empty `f_raw` field.
- **Screenshot:** A PNG file in `~/.rocky-idle-harness/screenshots/` timestamped within 30 seconds of the error event's `ts` field.
- **Fingerprint:** A confirmed pattern in `~/.openlog/fingerprints.json` with a `remedy` field describing the fix approach.
- **Remediation attempt:** A single cycle of: diagnose → generate fix → apply → test → verify.
- **Fix agent:** A Claude Code instance spawned via `sessions_spawn` or `omc team` with the error context, tasked with producing a code fix.
- **Recurrence window:** 6 hours after a fix is applied. If the same fingerprint fires again within this window, the fix is considered failed.
- **Cost ceiling:** Maximum USD spend per remediation attempt ($2 default, configurable in `.opentriage/config.toml`).
- **Remediation budget:** Maximum USD spend per day across all remediations ($10 default).
- **Evidence bundle:** A JSON object containing: error event, screenshot path (if exists), relevant source file paths (from fingerprint or LLM analysis), recent session events (last 20), git diff of recent changes.

## Architecture

### Data Flow

```
Bot runs → error occurs → OpenLog adapter writes JSONL event
    ↓
OpenTriage cron (every 2h) → triage cycle → classifies error
    ↓
If classification matches fingerprint WITH remedy:
    → Remediation engine checks circuit breaker + budget
    → Assembles evidence bundle (error + screenshot + source context)
    → Spawns fix agent (Claude Code via sessions_spawn)
    → Fix agent: diagnose → edit files → run tests → commit
    → OpenTriage verifies: error doesn't recur in next cycle
    → If recurs: mark fix as failed, escalate
    ↓
If classification is novel (no fingerprint):
    → LLM drafts fingerprint + remedy hypothesis
    → Stores as draft (auto-approved if severity < fatal)
    → Next occurrence triggers remediation with the new remedy
```

### Integration Points

- **OpenLog adapter** (`rocky-idle-harness/src/lib/openlog-adapter.ts`): Needs to attach screenshot paths to error events.
- **OpenTriage triage engine** (`opentriage/src/opentriage/triage/engine.py`): Already classifies; needs to trigger remediation for matched fingerprints with remedies.
- **OpenTriage remediation engine** (`opentriage/src/opentriage/remediation/engine.py`): Currently stub. Needs "agent" handler type.
- **OpenClaw sessions_spawn**: Used to spawn isolated fix agents with the evidence bundle.
- **Bot restart mechanism**: After a fix is committed, the bot process needs to restart. `loop.sh` already handles this (exits iteration → restarts).
- **Fingerprint registry** (`~/.openlog/fingerprints.json`): `remedy` field becomes structured (not just a string) — includes fix strategy, relevant files, test commands.

### Canonical Source Map

| Knowledge | Location |
|-----------|----------|
| Error events | `~/.openlog/events/*.jsonl` |
| Screenshots | `~/.rocky-idle-harness/screenshots/` |
| Fingerprints + remedies | `~/.openlog/fingerprints.json` |
| Triage correlations | `.opentriage/correlations/*.jsonl` |
| Remediation log | `.opentriage/remediations/*.jsonl` |
| Config (budgets, limits) | `.opentriage/config.toml` [remediation] section |
| Fix agent output | `.opentriage/remediations/{attempt-id}/` (diagnosis, diff, test output) |

## Features

### F-AR01: Screenshot-Enriched Error Events

**Goal:** Attach the nearest screenshot path to error events so downstream consumers can analyze visual state.

**One-time vs. ongoing:** One-time code change, ongoing effect.

**Procedure:**
1. In `openlog-adapter.ts`, modify `logError()` to accept an optional `screenshotPath` parameter.
2. In `bot.ts`, when logging an error via `openlog?.logError()`, check if a screenshot was taken within the last 30 seconds (compare `Date.now()` against file mtime in `~/.rocky-idle-harness/screenshots/`).
3. If found, include `data.screenshot: "/absolute/path/to/screenshot.png"` in the event.
4. For page crash events, take a screenshot immediately before logging (if page is still accessible — try/catch).

**Edge cases:**
- Page is crashed/closed, screenshot impossible → log event without screenshot, set `data.screenshot: null`.
- Multiple screenshots within 30s → use the most recent one.
- Screenshots directory doesn't exist → skip silently.

**Delegation safety:** Fully delegatable. Mechanical verification (test that events contain screenshot field).

**Success criteria:**
- ✅ **Immediate:** Error events in `~/.openlog/events/*.jsonl` contain `data.screenshot` field (string path or null).
- ⚙️ **Mechanical:** `grep -l "screenshot" ~/.openlog/events/*.jsonl` returns non-empty after a bot session with errors.

### F-AR02: Structured Remedy Format

**Goal:** Upgrade fingerprint `remedy` field from plain text to structured remediation instructions.

**One-time vs. ongoing:** One-time migration + ongoing format for new fingerprints.

**Procedure:**
1. Define the structured remedy schema:
   ```json
   {
     "strategy": "code-fix" | "config-change" | "restart" | "escalate",
     "description": "Human-readable remedy text",
     "relevant_files": ["src/skills/combat-automation.ts"],
     "test_command": "npx vitest run src/skills/__tests__/combat-automation.test.ts",
     "fix_prompt": "The DOM selector for the Fight button changed. Update startCombat() to use the new selector pattern visible in the attached screenshot.",
     "max_cost_usd": 2.0,
     "requires_screenshot": true
   }
   ```
2. Update `load_fingerprints()` in `opentriage/io/reader.py` to handle both string and dict `remedy` fields (backward compatible).
3. Migrate existing fingerprints: convert plain-text `remedy` strings to structured format. Keep `description` as the original text, infer `strategy` and `relevant_files` from the fingerprint slug and patterns.
4. Update `seedFingerprints()` in rocky-idle's `openlog-adapter.ts` to use the structured format.

**Edge cases:**
- Old fingerprints with string remedy → treat as `{"strategy": "escalate", "description": "<original text>"}`.
- Fingerprint with no remedy → skip remediation entirely (classify only).
- Remedy with `requires_screenshot: true` but no screenshot available → downgrade to escalate.

**Delegation safety:** Delegatable with guardrail: sub-agent must preserve all existing fingerprint data during migration (test by comparing pre/post fingerprint counts).

**Success criteria:**
- ✅ **Immediate:** `~/.openlog/fingerprints.json` contains at least 5 fingerprints with structured remedy objects.
- ⚙️ **Mechanical:** `python3 -c "import json; fps=json.load(open('~/.openlog/fingerprints.json'))['fingerprints']; structured=[f for f in fps.values() if isinstance(fps[f].get('remedy'), dict)]; print(len(structured))"` returns ≥ 5.

### F-AR03: Evidence Bundle Assembler

**Goal:** Collect all diagnostic context for a classified error into a single structured bundle that a fix agent can consume.

**One-time vs. ongoing:** One-time implementation, ongoing use.

**Procedure:**
1. Create `opentriage/src/opentriage/remediation/evidence.py`.
2. `assemble_evidence(correlation, openlog_dir, opentriage_dir) -> EvidenceBundle`:
   - `error_event`: The original JSONL event dict.
   - `screenshot_path`: From `event.data.screenshot` if exists and file is accessible.
   - `fingerprint`: The matched fingerprint entry with structured remedy.
   - `session_events`: Last 20 events from the same session (from OpenLog).
   - `recent_correlations`: Last 10 correlations for the same fingerprint slug (pattern history).
   - `git_context`: Output of `git log --oneline -5` and `git diff HEAD~1 --stat` from the project directory.
   - `relevant_files`: From the structured remedy, or inferred from `ref` field (e.g., `tool:exec` → `bot.ts`).
3. Serialize to JSON, write to `.opentriage/remediations/{attempt-id}/evidence.json`.
4. Total evidence bundle must be under 50KB (truncate session events if needed).

**Edge cases:**
- Screenshot file was deleted since the error → set `screenshot_path: null`.
- Git not initialized in project dir → omit `git_context`.
- Session has 500+ events → take last 20 only.
- No fingerprint match (novel error) → include LLM classification reasoning instead.

**Delegation safety:** Fully delegatable. No writes to external systems. Output is a read-only artifact.

**Success criteria:**
- ✅ **Immediate:** `evidence.py` produces valid JSON bundle for a test error event.
- ⚙️ **Mechanical:** Unit test creates mock event + fingerprint, calls `assemble_evidence()`, asserts all fields present and serializable.

### F-AR04: Fix Agent Spawner

**Goal:** Spawn an isolated coding agent with the evidence bundle to diagnose and fix the error.

**One-time vs. ongoing:** One-time implementation, ongoing use.

**Procedure:**
1. Create `opentriage/src/opentriage/remediation/agent_handler.py`.
2. `spawn_fix_agent(evidence: EvidenceBundle, config: RemediationConfig) -> RemediationResult`:
   - Build a prompt from the evidence bundle: error description, screenshot analysis instruction, relevant file paths, test command.
   - Write the prompt to `.opentriage/remediations/{attempt-id}/prompt.md`.
   - Execute: `claude --print --permission-mode bypassPermissions -p "$(cat prompt.md)"` in the project directory.
   - Capture stdout/stderr + exit code.
   - If exit code 0 and tests pass: `status = "fixed"`.
   - If exit code non-zero: `status = "failed"`, capture diagnostics.
3. The prompt template includes:
   - The error event and classification
   - Screenshot path (instruct agent to analyze it with image tool)
   - Relevant source files to read
   - Test command to run after fixing
   - Explicit constraint: "Do NOT modify test expectations" (DP-04)
   - Budget: single-shot, no loops inside the agent
4. Timeout: 5 minutes (configurable). Kill agent on timeout.
5. Write result to `.opentriage/remediations/{attempt-id}/result.json`.

**Edge cases:**
- Claude Code not installed or not in PATH → return `status: "agent_unavailable"`, escalate.
- Agent runs but doesn't commit → check `git diff` — if changes exist but uncommitted, auto-commit with `[opentriage-autofix]` prefix.
- Agent modifies files outside the project directory → git checkout reverts (sandbox by project dir).
- Cost ceiling exceeded mid-agent → impossible to enforce in real-time with `--print` mode; enforce at budget-check level before spawning.

**Delegation safety:** NOT delegatable to a sub-agent (spawns an external process). Must be called from the main OpenTriage remediation engine.

**Success criteria:**
- ✅ **Immediate:** `spawn_fix_agent()` with a test evidence bundle produces a result JSON with `status` field.
- ⚙️ **Mechanical:** Integration test: create a deliberately broken file, assemble evidence, spawn agent, verify file is fixed and tests pass.
- 📏 **Trailing:** Over 5 remediation attempts, at least 2 produce `status: "fixed"`.

### F-AR05: Remediation Orchestrator

**Goal:** Wire F-AR03 and F-AR04 into the triage cycle so remediations happen automatically after classification.

**One-time vs. ongoing:** One-time wiring, ongoing automatic execution.

**Procedure:**
1. In `opentriage/src/opentriage/remediation/engine.py`, replace the NOOP handler with:
   ```python
   if fingerprint.remedy.strategy == "code-fix":
       evidence = assemble_evidence(correlation, openlog_dir, opentriage_dir)
       result = spawn_fix_agent(evidence, config)
       write_remediation_log(opentriage_dir, result)
   elif fingerprint.remedy.strategy == "restart":
       # Send restart signal (touch a restart sentinel file)
       result = trigger_restart(project_dir)
   elif fingerprint.remedy.strategy == "config-change":
       # Apply config change described in remedy
       result = apply_config_fix(evidence, config)
   else:  # "escalate" or unknown
       result = escalate(correlation, config)
   ```
2. Add budget check before each remediation: query daily spend from `remediations/*.jsonl`, compare against `config.remediation.max_daily_cost_usd`.
3. Add circuit breaker check: if 3 consecutive remediations for the same fingerprint fail, suspend remediation for that fingerprint (24h cooldown).
4. After successful remediation, update the fingerprint's `count` and `last_remediated` timestamp.

**Edge cases:**
- Two errors for the same fingerprint arrive in the same triage cycle → remediate only the first, skip duplicates.
- Budget exhausted mid-cycle → skip remaining remediations, log as deferred.
- Fix agent succeeds but same error recurs next cycle → increment failure counter, try once more, then suspend + escalate.

**Delegation safety:** Core orchestration — not delegatable. Runs in the OpenTriage process.

**Success criteria:**
- ✅ **Immediate:** `opentriage triage` with a matched fingerprint that has a `code-fix` remedy spawns a fix agent and produces a remediation log entry.
- 📏 **Trailing:** After 7 days of operation, `remediations/*.jsonl` contains at least 3 entries with `status: "fixed"`.
- ⚙️ **Mechanical:** `grep -c '"status": "fixed"' .opentriage/remediations/*.jsonl` returns > 0 after first successful fix.

### F-AR06: Recurrence Verification

**Goal:** After a fix is applied, verify the error doesn't recur within the recurrence window (6 hours).

**One-time vs. ongoing:** One-time implementation, ongoing automatic verification.

**Procedure:**
1. When a remediation succeeds (`status: "fixed"`), record `{fingerprint_slug, fixed_at_ts, recurrence_window_hours: 6}` in `.opentriage/state.json` under `pending_verifications[]`.
2. Each triage cycle checks `pending_verifications`: if the same fingerprint appears in new correlations within the window → mark fix as `"recurred"`, increment failure counter.
3. If window expires with no recurrence → mark as `"verified"`, update fingerprint with `last_verified_fix` timestamp.
4. If recurrence detected and failure count < 3 → re-attempt remediation with updated context ("Previous fix attempt failed because the error recurred. Previous diff: ...").
5. If failure count ≥ 3 → suspend fingerprint remediation, escalate to human via #harness-alerts.

**Edge cases:**
- Bot not running during recurrence window → extend window until bot has run for at least 1 hour of active time post-fix.
- Multiple errors of same type in one cycle → count as single recurrence.
- Fix was applied but git reverted by another agent (Ralph loop) → detect via git log, don't count as recurrence.

**Delegation safety:** Fully delegatable (pure data comparison).

**Success criteria:**
- ✅ **Immediate:** `state.json` contains `pending_verifications` array after a successful remediation.
- ⚙️ **Mechanical:** Unit test: simulate fix → wait → no recurrence → verify `status: "verified"`.
- 📏 **Trailing:** After 14 days, at least 1 fix has `status: "verified"` (no recurrence).

## Implementation Sequence

| Order | Feature | Depends On | Effort | Parallelizable |
|-------|---------|------------|--------|----------------|
| 1 | F-AR01: Screenshot-Enriched Events | None | 1 iteration | Yes (with F-AR02) |
| 2 | F-AR02: Structured Remedy Format | None | 1 iteration | Yes (with F-AR01) |
| 3 | F-AR03: Evidence Bundle Assembler | F-AR01, F-AR02 | 1-2 iterations | No |
| 4 | F-AR04: Fix Agent Spawner | F-AR03 | 2-3 iterations | No |
| 5 | F-AR05: Remediation Orchestrator | F-AR03, F-AR04 | 1-2 iterations | No |
| 6 | F-AR06: Recurrence Verification | F-AR05 | 1 iteration | No |

**Total estimated effort:** 7-10 Ralph iterations.

## Feature Tracker

| ID | Feature | Status | Depends On |
|----|---------|--------|------------|
| F-AR01 | Screenshot-Enriched Error Events | ❌ | None |
| F-AR02 | Structured Remedy Format | ❌ | None |
| F-AR03 | Evidence Bundle Assembler | ❌ | F-AR01, F-AR02 |
| F-AR04 | Fix Agent Spawner | ❌ | F-AR03 |
| F-AR05 | Remediation Orchestrator | ❌ | F-AR03, F-AR04 |
| F-AR06 | Recurrence Verification | ❌ | F-AR05 |

## Success Criteria (Spec-Level)

- ⚙️ **Mechanical:** `opentriage triage` with a known fingerprint (having `strategy: "code-fix"`) produces a remediation log entry in `.opentriage/remediations/`.
- ⚙️ **Mechanical:** At least 1 fix agent invocation produces `status: "fixed"` with passing tests.
- 📏 **Trailing (7 days):** The ratio of `status: "fixed"` to total remediation attempts is ≥ 30%.
- 📏 **Trailing (14 days):** At least 1 fix has `status: "verified"` (no recurrence within window).
- 👁️ **Process:** Human review of remediation log confirms agents aren't modifying test expectations or making destructive changes.

## Anti-Patterns

- **Do NOT let the fix agent run in an unbounded loop.** Single-shot execution with timeout. If it can't fix in one pass, escalate.
- **Do NOT modify fingerprints.json during remediation.** The fix agent edits source code only. Fingerprint updates happen in the triage layer after verification.
- **Do NOT auto-remediate `fatal` severity fingerprints.** Fatal = likely data loss or security issue. Always escalate.
- **Do NOT retry a failed fix with the same prompt.** Each retry must include the previous attempt's diff and failure reason as additional context.
- **Do NOT spawn fix agents for `antml:thinking` errors.** These are internal Claude Code artifacts, not real code bugs. Add to a skip-list.

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-AR01 | Fix agents spawned via `sessions_spawn` (OpenClaw native), not raw CLI | Provides timeout enforcement (`runTimeoutSeconds`), sandboxing, cost tracking. CLI `--print` mode can't enforce per-attempt cost ceilings or sandbox filesystem access. |
| D-AR02 | LLM-drafted remedies for novel errors default to `strategy: "escalate"` until human promotes via `opentriage approve --promote-strategy code-fix <slug>` | Untested LLM hypothesis should not have unsupervised write access to production code. First-week safety > speed. |
| D-AR03 | Fix agents execute serially within a triage cycle, queued by severity (highest first) | Prevents concurrent edits to the same file and simplifies rollback. 5-min timeout makes parallelism unnecessary. |
| D-AR04 | Fingerprint registry reads use cycle-start snapshot; writes use atomic rename (`.tmp` → `os.rename`) | Prevents corruption from concurrent triage + remediation cycles. No long-lived file handles. |

## Out of Scope

1. **Live bot process management** — this spec covers code fixes. Restarting the bot process is handled by `loop.sh` or systemd, not OpenTriage.
2. **Multi-project remediation** — this spec targets rocky-idle-harness. Generalizing to arbitrary OpenLog-instrumented projects is a future concern.
3. **Cost tracking per-token** — we track budget at the attempt level ($2/attempt), not per-token. OpenAI/Anthropic billing handles the rest.
