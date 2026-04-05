---
title: OpenTriage — Intelligent Agent Failure Response System
version: 1.0
status: draft
domain: agent observability / orchestration
created: 2026-04-04
source: Discord #watcher-v2 design session, WATCHER_SPEC.md, OPENLOG_SPEC.md
depends-on: openlog-agent (pip package)
---

# OpenTriage — Intelligent Agent Failure Response System

## Phase 0: Grounding

**Scope test (one sentence, no "and"):** OpenTriage is an LLM-powered system that reads OpenLog event data, classifies failures by severity using tiered models, auto-remediates known patterns within budget constraints, escalates novel failures to humans, and degrades gracefully via a circuit breaker when its own accuracy drops.

**What problem it solves:**
- OpenLog records what happened. Nobody acts on it automatically.
- At 20+ agent runs/day, manual diagnosis of every failure is 60-90 min/day of human time.
- Known failures with known remedies (add backoff, split imports, retry with context) are repeated manually every time — wasting tokens and time.
- Novel failures go undetected for hours if they happen outside interactive sessions.
- **Quantified:** ~15% agent error rate × 20 runs/day × 20 min avg diagnosis = 60 min/day. OpenTriage reduces this to ~5 min/week (monthly review only) for known patterns.

**Root cause:** OpenLog is infrastructure without an operator. It captures, classifies, and injects — but it never decides "this should be retried" or "this is new and the human needs to know." The feedback loop has a gap between classification and action.

**What already exists:**
- `openlog-agent` (pip package) — event capture, fingerprint registry, fuzzy indexer, context injector. OpenTriage reads its `.openlog/` data directory.
- `WATCHER_SPEC.md` — prior design for an autonomous monitoring agent. Good ideas (circuit breaker, tiered models, budget system) but coupled to a specific workspace and Anthropic models. OpenTriage carries forward the design, not the implementation.
- `OBSERVATION_SPEC.md` — event schema and correlation pipeline. OpenTriage uses OpenLog's simpler schema instead.

**Explicitly out of scope:**
1. **Event capture.** OpenLog handles this. OpenTriage reads events; it never writes to `.openlog/events/`.
2. **Fingerprint matching.** OpenLog's indexer handles substring/trigram classification. OpenTriage adds LLM-based classification on top for ambiguous cases and novel patterns.
3. **The recording layer.** If OpenLog breaks, OpenTriage has nothing to read. Fixing OpenLog is not OpenTriage's job — OpenLog's heartbeat hook handles that.

---

## Problem Statement

Agent systems produce structured failure events (via OpenLog). These events are classified into fingerprints and injected into future sessions. But between classification and the next session, nothing happens. Known patterns sit in the registry waiting for an agent to encounter them again. Novel patterns sit undetected until a human reviews the monthly report.

**Quantified impact:**
- **Remediation delay:** Known-pattern failures that could be auto-retried in seconds wait hours for a human session.
- **Novel pattern lag:** Unknown failures during overnight or parallel runs go uncaptured for 24+ hours.
- **Human cost without OpenTriage:** ~60-90 min/day at 20 runs/day, 15% error rate.
- **Human cost with OpenTriage:** ~5 min/week for monthly report review + occasional escalation.
- **Token waste:** Every failed agent run that could have been retried with context is wasted inference cost.

**Root cause:** The observation system has no operator. OpenTriage is the operator.

---

## Design Principles

| ID | Principle | Rationale |
|----|-----------|-----------|
| OT1 | **OpenLog is the source of truth.** | OpenTriage reads `.openlog/` data. It never writes to `.openlog/events/`. It writes its own state to `.opentriage/`. Two systems, two directories, no conflicts. |
| OT2 | **Tiered intelligence, tiered cost.** | Cheap model for triage (classify known vs. novel). Mid-cost model for root cause analysis. Expensive model only for novel pattern synthesis. Never use Opus-tier where Haiku-tier suffices. |
| OT3 | **The circuit breaker is law.** | If OpenTriage's accuracy drops, it demotes itself. If it violates authority, it suspends itself. Demotions are automatic and immediate. Promotions require human approval. The system cannot override its own constraints. |
| OT4 | **Remediation has a budget.** | Every retry costs tokens and time. Per-event limits: 2 retry attempts, configurable cost cap. Per-day limits: configurable total cost. Exceeding budget → escalate instead of retry. |
| OT5 | **Escalation is always safe.** | A false escalation costs a notification. A missed failure costs wasted tokens, broken output, or corrupt repos. The asymmetry always favors escalating. |
| OT6 | **Model-agnostic by default.** | Works with any LLM via a provider interface. Ships with OpenAI and Anthropic support. Users can add custom providers. No vendor lock-in. |
| OT7 | **Stateless between runs.** | Each triage invocation reads from `.openlog/` and `.opentriage/state.json`. No in-memory state persists between runs. The system is restartable at any time. |
| OT8 | **The package is the product.** | `pip install opentriage`. CLI works immediately. Configuration via `opentriage init`. No workspace-specific setup beyond config. |

---

## Definitions

| Term | Meaning |
|------|---------|
| **Triage** | The act of reading uncorrelated OpenLog error events, classifying them by severity and pattern, and deciding on an action (remediate, escalate, or observe). |
| **Correlation** | Matching an OpenLog error event to an OpenLog fingerprint (existing) or identifying it as novel. Produces a correlation record in `.opentriage/correlations/`. |
| **Remediation** | An automated action taken in response to a correlated failure: retry with injected context, kill a runaway agent, or adjust spawn parameters. |
| **Escalation** | Sending a structured alert to a human via a configured channel (Discord webhook, Slack webhook, stdout, or file). |
| **Novel pattern** | An error event that doesn't match any existing OpenLog fingerprint at ≥0.5 LLM confidence. Triggers draft fingerprint creation. |
| **Circuit breaker** | A state machine with four states: `full-autonomy`, `classify-only`, `observe-only`, `suspended`. Controls what OpenTriage is allowed to do. |
| **Provider** | An LLM API interface. Ships with `openai` and `anthropic` providers. Users can register custom providers via a Python interface. |
| **Triage window** | The time period scanned for uncorrelated events. Default: last 2 hours. Configurable. |
| **Budget** | Per-event and per-day limits on remediation cost. Measured in retry count and estimated token cost. |
| **Draft fingerprint** | A proposed new entry for OpenLog's `fingerprints.json`, generated by OpenTriage when a novel pattern recurs ≥2 times. Saved to `.opentriage/drafts/` for human review. |
| **Outcome tracking** | After a remediation retry, OpenTriage checks whether the retried agent succeeded. This feeds the circuit breaker's accuracy metric. |

---

## Architecture

### Layer Model

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: SYNTHESIS (expensive model, novel patterns only)  │
│  Novel + recurrent → draft fingerprint for OpenLog registry │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: RESPONSE (budget-constrained)                     │
│  Known + remedy → auto-retry with context injected          │
│  Known + no remedy → escalate with classification context   │
│  Novel → escalate with full event context                   │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: TRIAGE (cheap model)                              │
│  Event → classify: known-pattern | novel | transient        │
│  Severity: critical | high | medium | low                   │
├─────────────────────────────────────────────────────────────┤
│  Layer 0: HEALTH (no LLM, mechanical)                       │
│  Circuit breaker evaluation                                  │
│  Outcome tracking (did remediations work?)                   │
│  Budget enforcement                                          │
│  Self-monitoring (accuracy, cost, staleness)                 │
└─────────────────────────────────────────────────────────────┘
        │                    │
        ▼                    ▼
   .openlog/            .opentriage/
   (read-only)          (read-write)
```

### Data Flow

```
EVERY 2 HOURS (or on-demand via CLI):

  1. Layer 0: Read .opentriage/state.json → check circuit breaker
     → If suspended: log and exit
     → If observe-only: skip to health metrics
     
  2. Layer 1 (cheap model): Read .openlog/events/*.jsonl for triage window
     → Filter: error events without existing correlation
     → For each: classify against .openlog/fingerprints.json
     → Write correlations to .opentriage/correlations/{date}.jsonl
     
  3. Layer 2: For each correlated event:
     → Known + remedy + budget allows → remediate (retry command)
     → Known + no remedy → escalate
     → Novel → escalate with event context
     
  4. Layer 3 (expensive model, only if novel patterns detected):
     → Read all events for the novel pattern's session
     → Draft a fingerprint entry for .opentriage/drafts/
     → If ≥2 similar drafts exist → propose adding to OpenLog registry
     
  5. Layer 0: Track outcomes from previous remediations
     → Update accuracy metrics
     → Evaluate circuit breaker transitions
     → Write .opentriage/state.json
     → If daily: write .opentriage/metrics/{date}.json
```

### File Structure

```
.opentriage/
├── state.json               # Circuit breaker state + metrics
├── config.json              # Provider config, budget, channels
├── correlations/            # Triage results (JSONL per day)
│   └── 2026-04-04.jsonl
├── remediations/            # Remediation actions taken (JSONL per day)
│   └── 2026-04-04.jsonl
├── drafts/                  # Proposed new fingerprints for OpenLog
│   └── circular-dependency-in-barrel.json
├── metrics/                 # Daily health metrics
│   └── 2026-04-04.json
└── escalations/             # Escalation history
    └── 2026-04-04.jsonl
```

### Integration Points

| System | How | Direction |
|--------|-----|-----------|
| OpenLog events | `.openlog/events/*.jsonl` | Read-only |
| OpenLog fingerprints | `.openlog/fingerprints.json` | Read for classification. Draft writes go to `.opentriage/drafts/`, not directly to the registry. |
| OpenLog injector | `openlog inject` | OpenTriage can invoke the injector when retrying an agent |
| LLM providers | Configurable (OpenAI, Anthropic, custom) | Outbound API calls |
| Alert channels | Configurable (Discord webhook, Slack webhook, stdout, file) | Outbound |
| Agent spawning | Shell command execution (configurable) | Outbound (remediation retries) |

---

## Features

### F-OT01: Triage Engine

**Goal:** Read uncorrelated OpenLog error events, classify them against the fingerprint registry using LLM, and produce structured correlation records.

**One-time vs. ongoing:** One-time implementation. Ongoing: runs every triage cycle (default 2 hours, or on-demand).

**Procedure:**

1. Read `.openlog/events/*.jsonl` for the triage window (default: last 2 hours). Filter to `kind: "error"` events only.
2. Read `.opentriage/correlations/` to find events already correlated. Skip those.
3. For each uncorrelated error event:
   a. First: try OpenLog's fingerprint registry (`fingerprints.json`). If the event's `f_raw` matches a confirmed fingerprint with ≥0.7 trigram similarity, classify as `known-pattern` with that fingerprint's slug. No LLM needed.
   b. If no registry match: send `f_raw` + `stderr` (first 200 chars) to the cheap-tier LLM with this prompt:
      ```
      Classify this agent failure. Is it a known software pattern (import error, rate limit, timeout, permission denied, etc.) or genuinely novel?
      
      Failure: {f_raw}
      Error output: {stderr[:200]}
      
      Respond with JSON: {"classification": "known-pattern"|"novel"|"transient", "severity": "critical"|"high"|"medium"|"low", "pattern_name": "short-slug", "confidence": 0.0-1.0}
      ```
   c. Parse LLM response. If confidence < 0.5, classify as `novel` regardless of what the LLM said.
   d. Write correlation record to `.opentriage/correlations/{date}.jsonl`:
      ```json
      {"ts": 1720000000, "event_ts": 1719999900, "session_id": "abc123", "f_raw": "...", "classification": "known-pattern", "fingerprint": "circular-import", "severity": "high", "confidence": 0.85, "layer": 1, "model": "gpt-4o-mini"}
      ```
4. Return summary: `{total_events, correlated, known, novel, transient}`.

**Edge cases:**
- LLM API is down: fall back to OpenLog's registry-only matching. Log a `decision` event noting the fallback.
- LLM returns malformed JSON: classify as `novel` with confidence 0.0. Log the raw response for debugging.
- Triage window has 0 uncorrelated events: return summary with all zeros. Normal operation.
- Event file is malformed: skip bad lines (inherited from OpenLog's OL8 principle).

**Delegation safety:** Fully delegatable. Reads files + calls LLM + writes files. No agent spawning. No destructive actions.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: Running `opentriage triage` against test events produces correlation JSONL with valid schema.
- ✅ Immediate / ⚙️ Mechanical: Running with LLM unavailable falls back to registry-only matching without crash.
- 📏 Trailing / ⚙️ Mechanical: After 20 triage cycles, ≥80% of `known-pattern` classifications match an existing OpenLog fingerprint.

---

### F-OT02: Circuit Breaker State Machine

**Goal:** Manage OpenTriage's authority level so it degrades gracefully when its accuracy drops or violations occur.

**One-time vs. ongoing:** One-time implementation. Ongoing: evaluated every triage cycle as part of Layer 0.

**Procedure:**

1. Create `.opentriage/state.json` with initial state:
   ```json
   {
     "circuit_breaker": "full-autonomy",
     "last_triage_run": null,
     "last_health_run": null,
     "demotion_history": [],
     "rolling_accuracy_7d": null,
     "rolling_remediation_success_7d": null,
     "total_cost_today_usd": 0.0,
     "human_approved_promotion": false
   }
   ```
2. State transitions (evaluated every triage cycle):
   - `full-autonomy` → `classify-only`: Rolling 7-day remediation success rate < 50%
   - `full-autonomy` → `observe-only`: 3+ authority violations in 24 hours (attempted action beyond current state's permissions)
   - Any → `suspended`: State file corrupted OR triage hasn't run for >6 hours (2.5× normal interval)
   - `classify-only` → `full-autonomy`: Success rate recovers to >70% AND `human_approved_promotion: true`
   - `observe-only` → `classify-only`: Human sets `human_approved_promotion: true`
   - `suspended` → `observe-only`: Human sets `human_approved_promotion: true`
3. Permissions per state:

   | State | Classify | Remediate | Escalate | Draft fingerprints |
   |-------|----------|-----------|----------|--------------------|
   | `full-autonomy` | ✅ | ✅ | ✅ | ✅ |
   | `classify-only` | ✅ | ❌ | ✅ | ✅ |
   | `observe-only` | ❌ | ❌ | ✅ (critical only) | ❌ |
   | `suspended` | ❌ | ❌ | ❌ | ❌ |

4. Every state transition: log to `.opentriage/state.json` history AND escalate (alert the human with old state, new state, reason).
5. OpenTriage CANNOT promote itself. The `human_approved_promotion` flag must be set by a human editing state.json or via `opentriage promote`.

**Edge cases:**
- State file missing on first run: create with defaults (`full-autonomy`).
- State file corrupted: set to `suspended`, escalate immediately.
- Multiple demotion conditions fire simultaneously: apply the most restrictive state.

**Delegation safety:** Fully delegatable. Pure state machine logic. No LLM in this layer.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: Simulating 5 failed remediations causes automatic demotion from `full-autonomy` to `classify-only`.
- ✅ Immediate / ⚙️ Mechanical: Setting `human_approved_promotion: true` in state.json + running triage promotes from `classify-only` to `full-autonomy`.
- ✅ Immediate / ⚙️ Mechanical: Corrupted state.json results in `suspended` state.

---

### F-OT03: Auto-Remediation Engine

**Goal:** Automatically retry failed agent tasks when a known fingerprint has a documented remedy, within budget constraints.

**One-time vs. ongoing:** One-time implementation. Ongoing: runs as part of Layer 2 every triage cycle.

**Procedure:**

1. After Layer 1 triage, for each `known-pattern` correlation with `severity ≥ medium`:
   a. Look up the fingerprint in `.openlog/fingerprints.json`.
   b. Check if `remedy` is non-null.
   c. Check budget: per-event max 2 retries, daily cost cap (default $5/day configurable).
   d. If remedy exists and budget allows:
      - Construct retry command using `openlog inject` context + the remedy text.
      - Execute the retry via configured shell command (default: `openlog inject {task_type}` piped to agent).
      - Log remediation to `.opentriage/remediations/{date}.jsonl`:
        ```json
        {"ts": ..., "event_ts": ..., "fingerprint": "circular-import", "action": "retry", "command": "...", "attempt": 1, "budget_remaining_usd": 4.50}
        ```
   e. If no remedy: escalate with classification context (F-OT04).
   f. If budget exceeded: escalate with "budget exhausted" reason.
2. Outcome tracking: on the next triage cycle, check if the retried session produced a `complete` event with `exit_code: 0`. Update success/failure metrics in state.json.

**Edge cases:**
- Retry command fails immediately (exit code != 0): log failure, do not retry again (count against 2-attempt limit).
- The remedied agent produces a *different* error: log as a new event. OpenTriage will correlate it on the next cycle. It's not counted as a success for this remediation.
- Budget exhausted mid-cycle: stop all remediations for the rest of the day. Escalate a "budget exhausted" alert.
- Circuit breaker is `classify-only`: skip all remediation. Correlations still happen.

**Delegation safety:** This feature executes shell commands (agent retries). Bounded by: budget cap, retry cap, circuit breaker state. The shell command template is configured by the human, not generated by LLM.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: A known fingerprint with remedy triggers a retry command. The remediation is logged.
- ✅ Immediate / ⚙️ Mechanical: Exceeding 2 retries for the same event stops retries and escalates.
- 📏 Trailing / ⚙️ Mechanical: After 10 remediations, ≥60% produce a successful outcome event.

---

### F-OT04: Escalation System

**Goal:** Alert humans when OpenTriage encounters novel failures, failed remediations, budget exhaustion, or circuit breaker transitions.

**One-time vs. ongoing:** One-time implementation. Ongoing: fires on every escalation-worthy event.

**Procedure:**

1. Escalation channels configured in `.opentriage/config.json`:
   ```json
   {
     "escalation": {
       "channels": [
         {"type": "discord", "webhook_url": "https://discord.com/api/webhooks/..."},
         {"type": "stdout"},
         {"type": "file", "path": ".opentriage/escalations/{date}.jsonl"}
       ],
       "severity_filter": "medium"
     }
   }
   ```
2. Escalation triggers:
   - Novel failure classified (severity ≥ configured filter)
   - Remediation failed (2 attempts exhausted)
   - Budget exhausted for the day
   - Circuit breaker state transition
   - Draft fingerprint ready for review
3. Escalation payload:
   ```json
   {
     "ts": ...,
     "type": "novel-failure|remediation-failed|budget-exhausted|circuit-breaker|draft-ready",
     "severity": "critical|high|medium|low",
     "summary": "human-readable one-liner",
     "context": {"f_raw": "...", "stderr": "...", "fingerprint": "...", "attempts": 2},
     "action_required": "review draft|investigate|approve promotion|increase budget"
   }
   ```
4. Always write to `.opentriage/escalations/{date}.jsonl` regardless of channel config (local audit trail).
5. Channel failures (webhook down, etc.): log warning, continue. Never crash on escalation failure.

**Edge cases:**
- No channels configured: write to file + stdout only.
- Webhook returns non-2xx: retry once after 5 seconds. If still fails, log and continue.
- Flood protection: max 10 escalations per hour per channel. After that, batch remaining into a summary.

**Delegation safety:** Fully delegatable. Outbound webhooks only. No destructive actions.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: Escalation to stdout prints valid JSON.
- ✅ Immediate / ⚙️ Mechanical: Escalation to file produces valid JSONL.
- ✅ Immediate / ⚙️ Mechanical: Webhook failure doesn't crash the triage cycle.

---

### F-OT05: Novel Pattern Synthesis

**Goal:** When unknown failures recur, use an expensive-tier LLM to draft a new fingerprint entry for the OpenLog registry.

**One-time vs. ongoing:** One-time implementation. Ongoing: runs as part of Layer 3 when novel patterns are detected.

**Procedure:**

1. After Layer 1 triage, collect all `novel` correlations from the current cycle.
2. For each novel correlation, check `.opentriage/drafts/` for existing drafts with similar `f_raw` (trigram ≥ 0.6).
3. If ≥2 similar novel events exist (current + previous cycles):
   a. Send all related events to the expensive-tier LLM:
      ```
      These agent failures don't match any known pattern. Analyze them and propose a fingerprint entry.
      
      Events:
      {event_1_f_raw + stderr}
      {event_2_f_raw + stderr}
      
      Respond with JSON: {"slug": "short-name", "patterns": ["phrasing1", "phrasing2"], "remedy": "suggested fix or null", "severity": "fatal|recoverable"}
      ```
   b. Write draft to `.opentriage/drafts/{slug}.json`:
      ```json
      {"slug": "...", "patterns": [...], "remedy": "...", "severity": "...", "source_events": [...], "created": "2026-04-04", "status": "pending-review"}
      ```
   c. Escalate: "Draft fingerprint ready for review: {slug}"
4. Human reviews drafts via `opentriage drafts` CLI. Approved drafts are merged into `.openlog/fingerprints.json` via `opentriage approve {slug}`.

**Edge cases:**
- LLM proposes a slug that already exists in the registry: append `-2`, `-3` etc.
- LLM returns garbage: save raw response in draft for human inspection. Set `status: "needs-manual-review"`.
- Only 1 novel event (no recurrence): don't synthesize yet. Wait for recurrence.

**Delegation safety:** Delegatable. The synthesis only proposes — it never writes to OpenLog's registry directly. Human approval required.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: Two novel events with similar f_raw produce a draft in `.opentriage/drafts/`.
- ✅ Immediate / ⚙️ Mechanical: `opentriage approve {slug}` merges the draft into `fingerprints.json`.
- 📏 Trailing / 👁️ Process: After 1 month, ≥1 draft has been approved and the fingerprint is catching events.

---

### F-OT06: Health Monitor

**Goal:** Track daily metrics, detect trends, and report on OpenTriage's own performance.

**One-time vs. ongoing:** One-time implementation. Ongoing: runs daily (or on-demand).

**Procedure:**

1. `opentriage health` computes daily metrics:
   - Total events scanned, total correlated
   - Classification breakdown: known / novel / transient
   - Remediation count, success rate
   - Escalation count by type
   - Estimated LLM cost for the day
   - Circuit breaker state
2. Write to `.opentriage/metrics/{date}.json`.
3. Trend alerts (compare last 7 days):
   - Error rate spike: >2× the 7-day average
   - Novel rate >40% of total errors (the system isn't learning)
   - Remediation success rate <50% (remediations aren't helping)
   - Cost >2× the 7-day average
4. Trend alerts trigger escalation (F-OT04).

**Edge cases:**
- First day (no history): skip trend comparison. Report raw numbers only.
- Missing metrics files for some days: compute trends from available data.

**Delegation safety:** Fully delegatable. Read-only computations + file writes.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: `opentriage health` produces a valid metrics JSON file.
- 📏 Trailing / ⚙️ Mechanical: After 7 days of operation, trend comparison produces meaningful alerts.

---

### F-OT07: CLI + Configuration

**Goal:** Provide a complete CLI for operating OpenTriage and a simple configuration system.

**One-time vs. ongoing:** One-time implementation.

**Procedure:**

1. CLI commands (via `pyproject.toml` entrypoint `opentriage`):
   - `opentriage init` — create `.opentriage/config.json` with interactive prompts (provider, API key, budget, channels)
   - `opentriage triage` — run one triage cycle (Layer 0-3)
   - `opentriage triage --watch` — run continuously at configured interval
   - `opentriage status` — show circuit breaker state, today's metrics, pending drafts
   - `opentriage health` — compute and display daily health metrics
   - `opentriage drafts` — list pending draft fingerprints
   - `opentriage approve {slug}` — merge a draft into OpenLog's fingerprints.json
   - `opentriage reject {slug}` — delete a draft
   - `opentriage promote` — set `human_approved_promotion: true` in state.json
   - `opentriage reset` — reset circuit breaker to `full-autonomy` (requires --confirm)
   - `opentriage escalations` — show recent escalations
2. Configuration file `.opentriage/config.json`:
   ```json
   {
     "provider": {
       "name": "openai",
       "triage_model": "gpt-4o-mini",
       "synthesis_model": "gpt-4o",
       "api_key_env": "OPENAI_API_KEY"
     },
     "budget": {
       "max_retries_per_event": 2,
       "max_daily_cost_usd": 5.0,
       "max_escalations_per_hour": 10
     },
     "triage": {
       "window_hours": 2,
       "confidence_threshold": 0.5
     },
     "escalation": {
       "channels": [{"type": "stdout"}],
       "severity_filter": "medium"
     },
     "remediation": {
       "enabled": true,
       "command_template": "openlog inject {task_type} | {agent_command}"
     }
   }
   ```
3. Provider interface (Python):
   ```python
   class TriageProvider:
       def classify(self, f_raw: str, stderr: str) -> dict: ...
       def synthesize(self, events: list[dict]) -> dict: ...
   ```
   Ships with `OpenAIProvider` and `AnthropicProvider`. Users register custom providers.

**Edge cases:**
- Config file missing: `opentriage triage` prints "Run `opentriage init` first" and exits.
- API key not set: clear error message naming the expected env var.
- Provider not recognized: list available providers and exit.

**Delegation safety:** Fully delegatable. Configuration + CLI scaffolding.

**Success criteria:**
- ✅ Immediate / ⚙️ Mechanical: `opentriage init` creates a valid config.json.
- ✅ Immediate / ⚙️ Mechanical: `opentriage status` outputs current state without error.
- ✅ Immediate / ⚙️ Mechanical: Running with missing API key produces a clear error, not a crash.

---

## Implementation Sequence

| Step | Feature | Depends On | Effort | Parallelizable? |
|------|---------|------------|--------|----------------|
| 1 | F-OT07: CLI + Configuration | None | 3-4 hours | No (foundation) |
| 2 | F-OT02: Circuit Breaker | F-OT07 (reads config) | 3-4 hours | No |
| 3 | F-OT01: Triage Engine | F-OT07, F-OT02 | 4-6 hours | No |
| 4 | F-OT04: Escalation System | F-OT07 | 3-4 hours | Yes (parallel with F-OT01) |
| 5 | F-OT03: Auto-Remediation | F-OT01, F-OT02, F-OT04 | 4-6 hours | No |
| 6 | F-OT05: Novel Pattern Synthesis | F-OT01, F-OT04 | 3-4 hours | Yes (parallel with F-OT03) |
| 7 | F-OT06: Health Monitor | F-OT01, F-OT02 | 2-3 hours | Yes |

Ship after steps 1-4. Remediation and synthesis are high-value but can follow.

---

## Feature Tracker

| ID | Feature | Status | Depends On |
|----|---------|--------|------------|
| F-OT01 | Triage Engine | ❌ | F-OT07, F-OT02 |
| F-OT02 | Circuit Breaker State Machine | ❌ | F-OT07 |
| F-OT03 | Auto-Remediation Engine | ❌ | F-OT01, F-OT02, F-OT04 |
| F-OT04 | Escalation System | ❌ | F-OT07 |
| F-OT05 | Novel Pattern Synthesis | ❌ | F-OT01, F-OT04 |
| F-OT06 | Health Monitor | ❌ | F-OT01, F-OT02 |
| F-OT07 | CLI + Configuration | ❌ | — |

---

## Success Criteria (Spec-Level)

- ⚙️ Mechanical / ✅ Immediate: `pip install opentriage` succeeds and `opentriage --help` lists all commands.
- ⚙️ Mechanical / ✅ Immediate: `opentriage triage` against test fixtures produces valid correlation JSONL.
- ⚙️ Mechanical / 📏 Trailing: After 10 triage cycles with real data, circuit breaker remains in `full-autonomy` (accuracy stays above threshold).
- 👁️ Process / 📏 Trailing: After 1 month, ≥1 novel pattern has been synthesized, reviewed, and approved into OpenLog's registry.
- ⚙️ Mechanical / ✅ Immediate: Circuit breaker demotion triggers escalation to configured channel.
- ⚙️ Mechanical / ✅ Immediate: LLM API failure causes graceful fallback, not crash.

---

## Anti-Patterns

- **Do NOT write to `.openlog/events/`.** OpenTriage reads OpenLog data. It writes its own data to `.opentriage/`. Two systems, two directories.
- **Do NOT auto-approve draft fingerprints.** Novel pattern synthesis proposes. Humans approve. The `opentriage approve` command is the only path to modifying OpenLog's registry.
- **Do NOT retry without budget check.** Every remediation must check the budget first. A runaway retry loop is the most expensive failure mode.
- **Do NOT use an expensive model for triage.** Layer 1 runs on every error event. Use the cheapest model that can distinguish "known" from "novel." Reserve expensive models for Layer 3 synthesis only.
- **Do NOT self-promote.** The circuit breaker can demote automatically but NEVER promote automatically. Human sets the flag.

---

## Self-Review

### Pass 1: Structural
- ✅ All required sections present (Problem Statement through Anti-Patterns)
- ✅ All terms in success criteria defined in Definitions
- ✅ Architecture data flow traces through all features
- ✅ Implementation sequence has no circular dependencies
- ✅ Feature tracker matches implementation sequence

### Pass 2: Semantic
- ✅ No ambiguous language ("appropriate", "properly") — replaced with specific thresholds
- ✅ All success criteria have a verification method described
- ✅ Budget thresholds are explicit (2 retries, $5/day default)
- ✅ Circuit breaker transitions have exact conditions (50%, 70%, 3+ violations)
- ✅ Triage window is explicit (2 hours default, configurable)

### Pass 3: Adversarial
- **Confabulation (F002):** The triage engine's LLM can misclassify. Mitigation: confidence threshold (0.5), fallback to registry-only matching, circuit breaker demotion if accuracy drops.
- **Cost runaway (F024):** Remediation retries cost tokens. Mitigation: per-event cap (2), daily cap ($5), budget check before every retry.
- **Plan vandalism (F013):** OpenTriage could corrupt OpenLog's fingerprints.json. Mitigation: drafts go to `.opentriage/drafts/`, not directly to registry. Human approval required.
- **Self-promotion:** OpenTriage could override its circuit breaker. Mitigation: promotion requires `human_approved_promotion` flag. The code checks for this flag; it cannot set it.
- **LLM as single point of failure:** API outage kills triage. Mitigation: fallback to OpenLog's registry-only matching (no LLM needed). Circuit breaker doesn't penalize API failures.

---

## Remaining Risks

1. **LLM classification accuracy is unknown until real data flows.** The 0.5 confidence threshold is a guess. Needs calibration after 50+ triage cycles. Instrument all confidence scores.

2. **Remediation command template is powerful.** It executes shell commands. A bad template could do damage. Mitigation: template is human-configured, never LLM-generated. Document this clearly.

3. **Cost estimation is approximate.** Token counting varies by provider. The budget system uses estimates, not exact billing. Could under-count by 20-30%. Set budget conservatively.

4. **Draft fingerprint quality depends on LLM.** An expensive model that generates bad drafts wastes money and human review time. The `needs-manual-review` fallback catches garbage, but doesn't prevent the cost.
