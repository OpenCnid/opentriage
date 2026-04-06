---
title: OpenTriage — Model-Agnostic Failure Response Engine
version: 1.0
status: draft
domain: agent orchestration / observability
created: 2026-04-04
depends-on: openlog-agent (pip package), .openlog/ data format
---

# OpenTriage — Model-Agnostic Failure Response Engine

## Phase 0: Grounding

**Scope test (one sentence, no "and"):** OpenTriage is a pip-installable failure-response engine that transforms raw OpenLog events into classifications, remediations, or human escalations through a self-monitoring pipeline.

*Note: Classification, remediation, escalation, self-monitoring, and novel pattern synthesis are sequential phases of a single response pipeline. Each failure event enters the pipeline once and exits through exactly one of three gates: resolved (remediation succeeded), escalated (human needed), or deferred (observed, no action taken). These are phases, not independent concerns — one spec.*

**What problem it solves:**
- OpenLog captures structured failure data into `.openlog/events/`. Nobody acts on it autonomously. Events accumulate until a human reads the monthly report or checks manually. At 20+ agent runs/day with a 15% error rate, that's 3+ failures/day sitting unprocessed.
- Known failure patterns with documented remedies (in `fingerprints.json`) could be auto-fixed in seconds. Instead, they wait 1-24 hours for human attention.
- Novel failure patterns that recur go unrecognized because OpenLog's indexer uses string matching only — it cannot synthesize *why* a new pattern matters or draft a meaningful remedy.
- **Quantified impact:** 45-90 min/day of human diagnosis time at current run rates. Known-pattern remediation delay: 1-24 hours. Novel pattern catalog growth: manual-only, ~1-2 entries/month vs. potential 5-10/month with automated drafting.

**Root cause:** OpenLog is a data layer with no intelligence layer. It captures and classifies via string matching, but it cannot reason about failures, apply fixes, or decide when to involve a human. The feedback loop between "failure detected" and "failure addressed" is entirely human-mediated. OpenTriage closes that loop.

**What already exists:**
- `openlog-agent` (pip package) — in-agent event logger (`log_event()`), post-session indexer (fingerprint matching), pre-session context injector. Creates and maintains `.openlog/`. **OpenTriage depends on this package.**
- `.openlog/fingerprints.json` — OpenLog's fingerprint registry. Contains known failure patterns with slugs, phrasings, counts, severities, and optional remedies. **This is OpenTriage's failure catalog.**
- `.openlog/events/*.jsonl` — structured session event files (JSONL, one per agent run). **This is OpenTriage's primary data source.**
- `.openlog/config.json` — OpenLog's configuration (thresholds, injection caps). **Read by OpenTriage for context.**

**Explicitly out of scope:**
1. **Event capture.** OpenTriage does not log events. That is openlog-agent's `log_event()` function. OpenTriage reads events after they are written.
2. **Fingerprint matching algorithm changes.** OpenLog's substring/trigram matching is owned by openlog-agent's indexer. OpenTriage reuses the same algorithm as a fast path but does not modify or replace it.
3. **Agent spawning or lifecycle management.** OpenTriage does not spawn agents. Remediation is executed via configurable command templates or callback interfaces. The host system (orchestrator, CI pipeline, cron) controls agent lifecycle.

---

## Problem Statement

OpenLog captures structured failure data from agent runs into `.openlog/events/*.jsonl` and classifies them into a fingerprint registry (`fingerprints.json`) via fuzzy string matching. This is Layer 1-2 of observability: capture and classification.

No system acts on this data autonomously. Known-pattern failures with documented remedies wait for human attention. Novel patterns go unrecognized beyond basic string similarity. The gap between "failure detected" and "failure addressed" is entirely human-mediated.

**Quantified impact:**
- **Diagnosis lag:** 1-24 hours for failures outside active human sessions. Some failures are never diagnosed.
- **Remediation delay:** Known-pattern failures that could be auto-fixed in seconds wait 1-24 hours. At 3 known-pattern failures/day, that is 3-72 hours of cumulative delay/day.
- **Catalog growth stall:** OpenLog's indexer creates provisional fingerprints via string matching. Without LLM synthesis, provisionals lack root cause analysis, descriptions, and remedies. Promotion requires human effort that does not happen consistently.
- **Cost without OpenTriage:** ~60-90 min/day of human diagnosis time at 20 runs/day (15% error rate, ~20 min average diagnosis).
- **Projected cost with OpenTriage:** ~$2-8/week for tiered LLM inference + ~5 min/week human review for circuit breaker promotions and draft approvals.

**Root cause (unified):** OpenLog is infrastructure without an operator. OpenTriage is the operator.

**Who is affected:** Every project using openlog-agent. The human operator reviewing agent outcomes. The agents themselves — who could receive faster remediation of known failures.

---

## Design Principles

| ID | Principle | Rationale |
|----|-----------|-----------|
| OT1 | **Model-agnostic by default.** | OpenTriage works with any LLM backend via a provider protocol. No vendor lock-in. Anthropic, OpenAI, Ollama, or a custom endpoint — the triage logic is identical. Provider choice is configuration, not architecture. |
| OT2 | **Tiered intelligence, tiered cost.** | Match model capability to task difficulty. Cheap models ($0.01-0.05/call) for triage. Standard models ($0.05-0.20/call) for root cause confirmation. Expensive models ($0.15-0.50/call) for novel synthesis. Never use an expensive model where a cheap one suffices. |
| OT3 | **String matching before LLM inference.** | OpenLog's fingerprint matching (substring + trigram) is free, instant, and deterministic. Use it first. Only invoke an LLM when string matching fails or returns low confidence. Per-event cost is near zero for known patterns. |
| OT4 | **Demotions are automatic; promotions are human-approved.** | When accuracy drops, OpenTriage restricts its own authority immediately (circuit breaker demotion). Restoring authority requires human approval. The cost of a false demotion (temporary reduced autonomy) is far lower than unchecked bad decisions. |
| OT5 | **Budget-capped everything.** | Every remediation has a per-event retry limit, a per-event cost cap, a daily cost cap, and a weekly cost cap. No single failure causes a cost runaway. No accumulation of small costs exceeds the weekly budget. |
| OT6 | **Escalation is always safe.** | When uncertain, escalate to a human. A false escalation costs one notification. A missed failure costs broken builds, wasted compute, or corrupted state. The asymmetry always favors escalating. |
| OT7 | **Stateless between runs.** | Each `opentriage` invocation reads state from files (`.opentriage/state.json`, `.openlog/fingerprints.json`) and writes updated state back. No in-memory state persists between CLI invocations. Restartable, debuggable, immune to context drift. |
| OT8 | **OpenTriage cannot modify its own configuration.** | Config files (`.opentriage/config.toml`) are read-only to OpenTriage. Threshold changes, budget adjustments, and provider switches require human edits. Self-modification creates alignment drift that is impossible to audit. |

---

## Definitions

| Term | Meaning |
|------|---------|
| **OpenTriage** | The pip-installable Python package defined by this spec. Provides the `opentriage` CLI and a Python API for failure triage, remediation, and escalation. |
| **OpenLog** | The data layer (`openlog-agent` pip package) that captures agent events and maintains the fingerprint registry. OpenTriage reads OpenLog's outputs; it does not write to OpenLog's files. |
| **Event** | A single JSONL record in `.openlog/events/{date}-{session}.jsonl`, written by openlog-agent's `log_event()`. Contains `ts`, `kind`, `ref`, `parent`, `f_raw`, `stderr`, `exit_code`. |
| **Fingerprint** | A canonical failure class in `.openlog/fingerprints.json`. Contains `slug`, `patterns` (list of known phrasings), `count`, `status` (provisional/confirmed), `last_seen`, `severity` (null/recoverable/fatal), `remedy` (null or string). |
| **Correlation** | A triage record mapping one event to a classification. Written to `.opentriage/correlations/{date}.jsonl`. Contains event reference, matched fingerprint (or "novel"/"transient"), confidence level, and the tier that produced the classification. |
| **Triage cycle** | One execution of `opentriage triage`: scan events, classify, remediate, escalate, update metrics. |
| **Triage window** | The time period scanned for uncorrelated events. Default: 2 hours. Configurable via `triage.scan_window_hours` in `.opentriage/config.toml`. On-demand runs with `--all` scan full history. |
| **Circuit breaker state** | One of: `full-autonomy`, `classify-only`, `observe-only`, `suspended`. Stored in `.opentriage/state.json`. Controls which pipeline phases are authorized. See F-OT03 for transition rules. |
| **Model tier** | One of `cheap`, `standard`, `expensive`. Maps to a provider-specific model via `[provider]` config. `cheap` handles triage classification. `standard` handles root cause confirmation. `expensive` handles novel pattern synthesis. |
| **Provider** | An implementation of the `LLMProvider` protocol that sends prompts to a specific LLM backend and returns structured responses. Configured in `.opentriage/config.toml`. |
| **Fast path** | Fingerprint matching via substring + trigram similarity (no LLM). Events classified by the fast path do not enter the slow path. |
| **Slow path** | LLM-powered classification for events the fast path could not classify (similarity < 0.7 to all fingerprints, or similarity between 0.4 and 0.7 needing confirmation). Sequential fallback after the fast path, not a parallel system. |
| **Remediation** | An action taken for a known-pattern classification: execute a configured command template with the remedy injected as context. Produces a record in `.opentriage/remediations/{date}.jsonl`. |
| **Remediation budget** | Per-event limits: `max_retries_per_event` (default 2), `max_cost_per_event_usd` (default $5.00). Global limits: `max_daily_cost_usd` (default $20.00), `max_weekly_cost_usd` (default $50.00). Checked before every remediation. At 10 events/day with 30% needing LLM, daily LLM cost is ~$0.50-2.00. Remediation subprocess costs dominate. $20/day allows ~10-40 remediations depending on remedy cost. |
| **Remediation outcome** | The result of a remediation, determined on the next triage cycle: `success` (subsequent events from the same context show no recurrence of the matched pattern), `failure` (same pattern recurred), `pending` (no subsequent events yet), `no_result` (no events after 24 hours — treated as `failure` for metrics). |
| **Remediation handler** | A configurable mechanism for executing remedies. Built-in handlers: `subprocess` (runs a command template with `shell=False`), `callback` (calls a registered Python function), `noop` (logs only, used in classify-only/observe-only states). |
| **Escalation** | A structured alert sent to a human via a configured channel. Contains: what failed (f_raw), what pattern matched (slug or "novel"), what was tried (remediation attempts), what action is needed. |
| **Escalation channel** | A pluggable output target for escalation messages. Built-in: `stdout`, `webhook`, `discord`, `slack`. Custom channels implement the `EscalationChannel` protocol. |
| **Draft fingerprint** | A proposed new fingerprint entry generated by the novel pattern synthesizer. Saved to `.opentriage/drafts/{slug}.json`. Status is always `proposed`. Requires human action to be promoted into `.openlog/fingerprints.json`. |
| **Override** | When the standard-tier LLM changes the cheap-tier LLM's classification. Tracked as a metric. An override rate above 30% over the rolling accuracy window indicates the cheap-tier model or triage prompt needs tuning. |
| **Net remediation effect** | `(successful_remediations - failed_remediations) / total_resolved_remediations` over the rolling accuracy window. A negative value means remediations are making things worse on average. |
| **Rolling accuracy window** | The evaluation period for circuit breaker metrics. Default: 7 days. Configurable via `circuit_breaker.evaluation_window_days`. Used for computing remediation success rate, override rate, and net remediation effect. |

---

## Architecture

### Data Flow

```
TRIAGE CYCLE (opentriage triage)

  1. Read .opentriage/state.json → check circuit breaker
     → suspended: log "triage skipped: suspended", exit 0
     → observe-only: classify only, skip remediation + escalation
     → classify-only: classify + escalate, skip remediation
     → full-autonomy: all phases enabled

  2. Scan .openlog/events/*.jsonl for triage window (default: last 2h)
     Filter to: kind = "error" with non-empty f_raw
     Exclude: events with existing correlation in .opentriage/correlations/

  3. FAST PATH (no LLM, per OT3):
     For each uncorrelated event:
       Match f_raw against .openlog/fingerprints.json (confirmed only)
       Substring match → assign fingerprint (high confidence)
       Trigram similarity ≥ 0.7 → assign fingerprint (high confidence)
       Similarity 0.4–0.7 → flag needs_llm with top candidate
       Similarity < 0.4 → flag needs_llm, no candidate
     Events classified here do NOT enter step 4.

  4. SLOW PATH (cheap-tier LLM):
     For each needs_llm event:
       Classify: known-pattern | novel | transient
       high confidence → write correlation
       medium confidence or novel → route to step 5
       transient → write correlation, track for recurrence

  5. CONFIRMATION PATH (standard-tier LLM):
     Confirm or override cheap-tier classification
     Write final correlation (with override flag if changed)

  6. REMEDIATION (if circuit breaker = full-autonomy):
     For known-pattern with non-null remedy in fingerprints.json:
       Check budget → if exceeded, escalate
       Execute via configured handler → write remediation record

  7. ESCALATION:
     For: novel confirmed, failed remedy, budget exceeded, circuit breaker change
       Send structured alert via configured channels

  8. NOVEL SYNTHESIS (expensive-tier LLM):
     For confirmed novel patterns:
       Draft new fingerprint → save to .opentriage/drafts/

  9. SELF-MONITORING:
     Track outcomes of previous remediations
     Update rolling metrics in .opentriage/state.json
     Evaluate circuit breaker transitions (F-OT03)

HEALTH CYCLE (opentriage health)
  Read correlations + remediations + outcomes for period
  Compute metrics → .opentriage/metrics/{date}.json
  Detect trends → escalate if thresholds breached

WATCH MODE (opentriage watch --interval 120)
  Run triage cycle every 120 seconds
  Run health cycle once daily
```

### Canonical Source Map

| Data | Location | Written By | Read By |
|------|----------|------------|---------|
| Agent session events | `.openlog/events/*.jsonl` | openlog-agent (`log_event()`) | OpenTriage triage engine |
| Fingerprint registry | `.openlog/fingerprints.json` | openlog-agent indexer | OpenTriage triage engine, novel synthesizer |
| OpenLog config | `.openlog/config.json` | Human | OpenTriage (reference only) |
| OpenTriage config | `.opentriage/config.toml` | Human | All OpenTriage components (read-only) |
| Circuit breaker state | `.opentriage/state.json` | OpenTriage (self-monitoring) | All OpenTriage components |
| Correlation records | `.opentriage/correlations/{date}.jsonl` | Triage engine | Remediation engine, health monitor, self-monitor |
| Remediation records | `.opentriage/remediations/{date}.jsonl` | Remediation engine | Self-monitor, health monitor |
| Draft fingerprints | `.opentriage/drafts/{slug}.json` | Novel synthesizer | Human (review), health monitor (count) |
| Health metrics | `.opentriage/metrics/{date}.json` | Health monitor | Human, self-monitor |
| Escalation log | `.opentriage/escalations.jsonl` | Escalation system | Health monitor, human review |

### Integration Points

| System | Integration | Details |
|--------|-------------|---------|
| openlog-agent | Pip dependency; reads `.openlog/` | Imports `openlog.EventReader`, `openlog.FingerprintRegistry` or falls back to direct file reads if API unavailable |
| LLM providers | Via `LLMProvider` protocol | Configured in `config.toml`. Provider sends prompts, receives text (parsed as JSON by OpenTriage) |
| Remediation targets | Via `RemediationHandler` protocol | Default: subprocess with command template (`shell=False`). Custom: Python callback |
| Escalation targets | Via `EscalationChannel` protocol | Built-in: stdout, webhook, Discord, Slack. Custom: implement `send(alert)` |
| Cron / scheduler | External; runs `opentriage triage` | Or use `opentriage watch` for built-in continuous mode |
| CI/CD pipelines | `opentriage triage --window 24h` | Reports failures, exit code 2 if critical issues found (CI gate) |

### Package Structure

```
opentriage/                    # pip-installable package
├── __init__.py
├── __main__.py                # python -m opentriage
├── cli.py                     # CLI entry point (opentriage command)
├── config.py                  # Config loading (.opentriage/config.toml)
├── provider/
│   ├── __init__.py
│   ├── protocol.py            # LLMProvider Protocol definition
│   ├── anthropic.py           # Anthropic provider
│   ├── openai.py              # OpenAI provider
│   └── ollama.py              # Ollama/local model provider
├── triage/
│   ├── __init__.py
│   ├── engine.py              # Main triage loop (F-OT02)
│   ├── matcher.py             # Fast-path fingerprint matching
│   └── classifier.py          # LLM-powered classification
├── circuit_breaker.py         # State machine (F-OT03)
├── remediation/
│   ├── __init__.py
│   ├── engine.py              # Remediation logic (F-OT04)
│   ├── budget.py              # Budget tracking
│   └── handlers.py            # Subprocess, callback, noop handlers
├── escalation/
│   ├── __init__.py
│   ├── router.py              # Channel routing (F-OT05)
│   └── channels.py            # stdout, webhook, Discord, Slack
├── synthesis/
│   ├── __init__.py
│   └── drafter.py             # Novel pattern drafting (F-OT06)
├── health/
│   ├── __init__.py
│   ├── monitor.py             # Metrics computation (F-OT07)
│   └── trends.py              # Trend detection
└── io/
    ├── __init__.py
    ├── reader.py               # OpenLog file readers
    └── writer.py               # OpenTriage file writers
```

### State Directory Structure

```
.opentriage/                   # per-project state (created by opentriage init)
├── config.toml                # human-edited configuration
├── state.json                 # circuit breaker state + rolling metrics
├── correlations/              # triage classification records (JSONL)
│   └── 2026-04-04.jsonl
├── remediations/              # remediation attempt records (JSONL)
│   └── 2026-04-04.jsonl
├── drafts/                    # proposed new fingerprints (JSON)
│   └── silent-dep-failure.json
├── metrics/                   # health metrics (JSON)
│   └── 2026-04-04.json
└── escalations.jsonl          # all escalation history (append-only)
```

### Default Configuration

`.opentriage/config.toml` created by `opentriage init`:

```toml
[provider]
backend = "anthropic"                         # "anthropic", "openai", "ollama"
cheap_model = "claude-haiku-4-5-20251001"     # triage classification
standard_model = "claude-sonnet-4-6"          # root cause confirmation
expensive_model = "claude-opus-4-6"           # novel pattern synthesis
api_key_env = "ANTHROPIC_API_KEY"             # env var containing API key
base_url = ""                                 # custom endpoint (optional)
timeout_seconds = 60                          # per-call timeout

[budget]
max_retries_per_event = 2
max_cost_per_event_usd = 5.0
max_daily_cost_usd = 20.0
max_weekly_cost_usd = 50.0

[circuit_breaker]
classification_accuracy_floor = 0.70          # demote below this
recovery_threshold = 0.80                     # promote above this
evaluation_window_days = 7                    # rolling window size
min_resolved_for_evaluation = 5               # need ≥5 outcomes before evaluating

[triage]
scan_window_hours = 2
max_events_per_cycle = 50
fast_path_similarity_threshold = 0.7          # ≥ this → known-pattern (no LLM)
needs_llm_similarity_floor = 0.4              # between floor and threshold → LLM with candidate
transient_recurrence_threshold = 3            # N transients with similarity → reclassify as novel
transient_recurrence_window_hours = 24

[escalation]
channels = ["stdout"]                         # active channels
discord_webhook_url = ""
slack_webhook_url = ""
webhook_url = ""
fallback_channel = "stdout"                   # if primary channels fail

[remediation]
handler = "subprocess"                        # "subprocess", "callback", "noop"
command_template = ""                         # e.g., "agent retry --context {remedy_file}"
timeout_seconds = 300                         # per-remediation subprocess timeout

[health]
trend_pattern_spike_threshold = 3             # 3+ today vs 0-1 avg → alert
trend_remediation_failure_rate = 0.50         # >50% for any fingerprint → alert
trend_novel_rate = 0.40                       # >40% of errors are novel → alert
trend_override_rate = 0.30                    # >30% cheap→standard overrides → alert
trend_daily_cost_warning_usd = 10.0           # 50% of max_daily budget → alert
trend_pending_drafts_max = 5                  # >5 unreviewed drafts → alert
```

---

## Features

### F-OT01: LLM Provider Interface

**Goal:** Abstract LLM access behind a tiered protocol so OpenTriage works with any model backend without code changes.

**One-time vs. ongoing:** One-time implementation of the protocol and built-in providers. Ongoing: new providers added by implementing the protocol.

**Procedure:**

1. Define the `LLMProvider` protocol in `opentriage/provider/protocol.py`:
   ```python
   from typing import Protocol, runtime_checkable

   @runtime_checkable
   class LLMProvider(Protocol):
       def complete(self, messages: list[dict], tier: str = "cheap") -> str:
           """Send messages to the model at the given tier. Return response text.

           Args:
               messages: list of {"role": "user"|"system", "content": "..."} dicts.
               tier: "cheap", "standard", or "expensive".

           Returns:
               Model response text (caller parses as JSON where needed).
           """
           ...

       def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
           """Estimate cost in USD. Return 0.0 if not supported (e.g., local models)."""
           ...
   ```

2. Implement built-in providers:
   - `AnthropicProvider`: maps cheap → claude-haiku-4-5-20251001, standard → claude-sonnet-4-6, expensive → claude-opus-4-6. Uses `anthropic` Python SDK.
   - `OpenAIProvider`: maps cheap → gpt-4o-mini, standard → gpt-4o, expensive → o3. Uses `openai` Python SDK.
   - `OllamaProvider`: maps cheap → llama3.1:8b, standard → llama3.1:70b, expensive → llama3.1:405b. Uses HTTP API to local Ollama server.
   All tier-to-model mappings are overridable via `[provider]` config fields (`cheap_model`, `standard_model`, `expensive_model`).

3. Provider instantiation at CLI startup: read `[provider]` section from config, dynamically import the matching module, construct provider with model overrides and API key from the env var named in `api_key_env`.

4. If the configured provider's SDK is not installed, raise: `"Provider 'anthropic' requires the 'anthropic' package. Install: pip install opentriage[anthropic]"`. Use `extras_require` in `pyproject.toml` for optional provider dependencies.

5. Provider errors are surfaced as structured exceptions (`ProviderError`, `ProviderTimeoutError`, `ProviderAuthError`) that the triage engine handles per its edge-case rules.

**Edge cases:**
- Provider API returns an error (rate limit, auth failure): retry once after 2 seconds. If still failing, classify the event as `classification: "deferred", reason: "provider_error"` and escalate. Do not block the entire triage cycle for one failed call.
- Provider returns non-JSON text when JSON was expected: retry once with an appended instruction `"Respond with valid JSON only."` If still malformed, defer and route to standard-tier for re-attempt.
- Local model (Ollama) responds slowly (>30s per call): timeout is configurable via `provider.timeout_seconds` (default 60). On timeout, treat as provider error (retry once, then defer).

**Delegation safety:** Fully delegatable. Pure library code. Provider implementations are thin wrappers around SDK calls. No project-specific knowledge required.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** `from opentriage.provider.protocol import LLMProvider` imports without error. Each built-in provider passes `isinstance(provider, LLMProvider)` check.
- ✅ **Immediate / ⚙️ Mechanical:** `AnthropicProvider.complete(messages, tier="cheap")` with a valid API key returns a non-empty string within `timeout_seconds`.
- ✅ **Immediate / ⚙️ Mechanical:** Calling with an invalid API key raises `ProviderAuthError` within 10 seconds.
- 📏 **Trailing / ⚙️ Mechanical:** After 50 triage cycles, cost estimates from `estimate_cost()` are within 20% of actual billed amounts.

---

### F-OT02: Triage Engine

**Goal:** Classify uncorrelated OpenLog error events against the fingerprint registry using string matching (fast path) or tiered LLM inference (slow path).

**One-time vs. ongoing:** One-time implementation. Ongoing: runs every triage cycle via `opentriage triage` or `opentriage watch`.

**Procedure:**

1. Entry point: `opentriage triage [--window HOURS] [--all] [--dry-run]`. Default window: 2 hours.

2. Read `.opentriage/state.json` → check circuit breaker state:
   - `suspended`: log `"triage skipped: circuit breaker suspended"`, exit code 0.
   - `observe-only`: execute steps 3-5 only (classify, write correlations). Skip remediation, escalation, synthesis.
   - `classify-only`: execute steps 3-7 (classify + escalate). Skip remediation.
   - `full-autonomy`: execute all steps.

3. Scan `.openlog/events/*.jsonl` files overlapping the triage window. Filter to events where `kind = "error"` and `f_raw` is non-empty.

4. Exclude already-correlated events: for each candidate, check `.opentriage/correlations/` for a record matching `ts` + `ref` + `session_id`. Skip matches.

5. **Fast path (no LLM, per OT3).** For each uncorrelated event:
   a. Load `.openlog/fingerprints.json`. Consider only fingerprints with `status: "confirmed"`.
   b. Check `f_raw` against each fingerprint's `patterns` list:
      - Substring match (case-insensitive): any pattern is a substring of `f_raw` → match.
      - If no substring match: compute trigram Jaccard similarity between `f_raw` and each pattern. Take the maximum across all fingerprints.
   c. If max similarity ≥ `triage.fast_path_similarity_threshold` (default 0.7) → classify as `known-pattern`, confidence `high`, assign matched fingerprint slug. Write correlation record. **This event does not enter step 6.**
   d. If max similarity between `triage.needs_llm_similarity_floor` (default 0.4) and the threshold → flag `needs_llm: true`, record the top candidate fingerprint slug.
   e. If max similarity < 0.4 → flag `needs_llm: true`, no candidate.

6. **Slow path (cheap-tier LLM).** For each `needs_llm` event:
   a. Build triage prompt:
      ```
      You are a failure classifier. Classify this agent error event.

      EVENT:
      f_raw: {f_raw}
      stderr: {first 500 chars of stderr}
      exit_code: {exit_code}
      ref: {ref}

      KNOWN FAILURE PATTERNS:
      {For each confirmed fingerprint: slug, first pattern, severity, remedy (first 50 chars)}
      {If a candidate from fast path: "Closest match: {slug} (similarity: {score})"}

      Respond with JSON only:
      {"classification":"known-pattern"|"novel"|"transient","matched_fingerprint":"slug or null","confidence":"high"|"medium"|"low","reasoning":"1-2 sentences"}
      ```
   b. Send to cheap-tier model via provider.
   c. Parse response JSON. On parse failure: retry once with `"Respond with valid JSON only"` appended. On second failure: classify as `deferred`, route to standard-tier.
   d. If `confidence = "high"` and `classification = "known-pattern"` → write correlation record.
   e. If `confidence = "medium"` or `classification = "novel"` → route to confirmation (step 7).
   f. If `classification = "transient"` → write correlation record. Track for recurrence (step 9).

7. **Confirmation path (standard-tier LLM).** For qualifying events:
   a. Build richer prompt including:
      - Full event JSON.
      - Cheap-tier classification + reasoning.
      - Full fingerprint entry for matched pattern (all patterns, remedy, severity).
      - For novel events: the 3 closest fingerprints by similarity score.
      - All events from the same session (full context chain).
   b. Send to standard-tier model.
   c. Parse response. If standard-tier overrides cheap-tier: write correlation with `overridden_by: "standard"`.
   d. Write final correlation record.

8. Trigger downstream phases based on correlations and circuit breaker state:
   - Known-pattern with non-null remedy → F-OT04 (remediation), if `full-autonomy`.
   - Novel (confirmed) → F-OT06 (synthesis) + F-OT05 (escalation).
   - Budget exceeded or failed remedy → F-OT05 (escalation).

9. **Transient recurrence detection:** Scan correlations from the last `triage.transient_recurrence_window_hours` (default 24) hours. If `triage.transient_recurrence_threshold` (default 3) or more events classified as `transient` have pairwise trigram similarity ≥ 0.6 → reclassify the most recent as `novel`, route to synthesis (F-OT06).

10. **Batch limits:** Process at most `triage.max_events_per_cycle` (default 50) events per cycle. If more are pending, process the 50 with the most recent timestamps. Log `"triage backlog: {N} events deferred to next cycle"`. If backlog exceeds 100, escalate via F-OT05.

**Edge cases:**
- `.openlog/events/` directory does not exist: exit with message `"No OpenLog events found. Is openlog-agent installed?"` Exit code 0 (fresh install, not an error).
- `fingerprints.json` is missing or empty: all events go to the slow path. Log warning `"No fingerprints loaded — all events will use LLM classification."`.
- Event has `f_raw` but no `stderr` or `exit_code`: classify based on `f_raw` alone. Note missing fields in the correlation record.
- A single cycle produces 50+ LLM calls (all events need slow path): the budget system (F-OT04) tracks cumulative cost. If daily limit is reached mid-cycle, remaining events are deferred to next cycle.

**Delegation safety:** Fully delegatable. Reads files and calls the LLM provider. Does not modify OpenLog data, spawn agents, or take actions beyond writing correlation records to `.opentriage/correlations/`.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** Given a test event with `f_raw: "circular import between auth and user"` and a registry containing `circular-import` with pattern `"circular import"`, the fast path matches at `confidence: "high"`. No LLM call made.
- ✅ **Immediate / ⚙️ Mechanical:** Given a test event with `f_raw: "widget factory explosion"` and no matching fingerprints, the slow path invokes cheap-tier and writes a correlation.
- ✅ **Immediate / ⚙️ Mechanical:** Given 3 transient correlations within 24 hours with `f_raw` similarity ≥ 0.6, the recurrence detector reclassifies the latest as `novel`.
- 📏 **Trailing / ⚙️ Mechanical:** After 20 triage cycles on production data, fast-path classification handles ≥60% of events without LLM.
- 📏 **Trailing / ⚙️ Mechanical:** Standard-tier override rate stays below 30% after 50 events.

---

### F-OT03: Circuit Breaker State Machine

**Goal:** Degrade OpenTriage's authority automatically when its remediation accuracy drops below configured thresholds.

**One-time vs. ongoing:** One-time implementation. Ongoing: evaluated at the end of every triage cycle (Data Flow step 9).

**Procedure:**

1. Initialize `.opentriage/state.json` on first run (`opentriage init` or first `opentriage triage`):
   ```json
   {
     "circuit_breaker": "full-autonomy",
     "last_triage_run": null,
     "last_health_run": null,
     "demotion_history": [],
     "rolling_remediation_success_rate": null,
     "rolling_override_rate": null,
     "net_remediation_effect": null,
     "total_remediations": 0,
     "total_escalations": 0,
     "consecutive_provider_errors": 0,
     "human_approved_promotion": false,
     "version": "1.0"
   }
   ```

2. **Automatic demotions** (evaluated at the end of every triage cycle):
   - `full-autonomy` → `classify-only`: `rolling_remediation_success_rate` < `circuit_breaker.classification_accuracy_floor` (default 0.70) over the rolling accuracy window.
   - `full-autonomy` → `observe-only`: `net_remediation_effect` < 0 (remediations cause more failures than they fix) over the rolling accuracy window.
   - Any state → `suspended`: `consecutive_provider_errors` ≥ 3 (LLM backend unreachable for 3 consecutive triage cycles). Or: human sets `"circuit_breaker": "suspended"` directly in state file.

3. **Human-approved promotions:**
   - `classify-only` → `full-autonomy`: `rolling_remediation_success_rate` > `circuit_breaker.recovery_threshold` (default 0.80) AND `human_approved_promotion = true`. After promotion, reset `human_approved_promotion` to `false`.
   - `observe-only` → `classify-only`: `human_approved_promotion = true`.
   - `suspended` → `observe-only`: `human_approved_promotion = true`.

4. **Permissions per state:**

   | State | Classify | Remediate | Escalate | Draft fingerprints |
   |-------|----------|-----------|----------|--------------------|
   | `full-autonomy` | Yes | Yes | Yes | Yes |
   | `classify-only` | Yes | No | Yes | Yes |
   | `observe-only` | Yes | No | critical-only | No |
   | `suspended` | No | No | No | No |

5. Every state transition:
   - Append to `demotion_history`: `{"from": "...", "to": "...", "reason": "...", "ts": <unix_timestamp>}`.
   - Send escalation alert (F-OT05) with old state, new state, and reason.

6. **Metric computation** (runs at end of each triage cycle):
   - `rolling_remediation_success_rate`: remediations with `outcome: "success"` / total remediations with resolved outcomes (not `pending`) in the rolling accuracy window.
   - `rolling_override_rate`: standard-tier overrides / total standard-tier calls in the rolling accuracy window.
   - `net_remediation_effect`: `(successes - failures) / total_resolved_remediations` in the rolling accuracy window.
   - **Minimum data requirement:** at least `circuit_breaker.min_resolved_for_evaluation` (default 5) resolved remediations in the window. If fewer, all metrics remain `null` and no transitions fire.

7. CLI: `opentriage promote` — sets `human_approved_promotion = true`. Prints current state and what promotion would occur if metrics qualify.

8. CLI: `opentriage status` — prints circuit breaker state, all rolling metrics, demotion history (last 10 entries), and promotion eligibility.

**Edge cases:**
- State file corrupted or missing: default to `suspended`. Create fresh state file. Escalate immediately with `reason: "state_file_corrupted"`.
- Multiple demotion conditions fire simultaneously: apply the most restrictive. `suspended` > `observe-only` > `classify-only` > `full-autonomy`.
- Metrics are `null` (insufficient data): no transitions fire. State remains unchanged. Prevents oscillation during first days of operation.
- Human edits `circuit_breaker` field directly in state file: accepted as manual override.

**Delegation safety:** Fully delegatable. Pure state machine logic on JSON files. No LLM calls (except escalation alert via F-OT05 on transition).

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** Setting `rolling_remediation_success_rate` to 0.50 in state.json and running a triage cycle causes `full-autonomy` → `classify-only` transition.
- ✅ **Immediate / ⚙️ Mechanical:** Setting `human_approved_promotion = true` with `rolling_remediation_success_rate` at 0.85 causes `classify-only` → `full-autonomy` transition.
- ✅ **Immediate / ⚙️ Mechanical:** Corrupting state.json (invalid JSON) causes default to `suspended` and creates a fresh state file.
- 📏 **Trailing / 👁️ Process:** Over 14 days, `demotion_history` accurately records all transitions with correct timestamps and reasons.

---

### F-OT04: Auto-Remediation Engine

**Goal:** Apply known remedies for classified failures by executing a configurable command template within budget constraints.

**One-time vs. ongoing:** One-time implementation. Ongoing: triggered by the triage engine for known-pattern classifications when circuit breaker is `full-autonomy`.

**Procedure:**

1. The triage engine (F-OT02 step 8) passes correlated events. Only events meeting ALL criteria enter remediation:
   - Classification: `known-pattern` with confidence `high` or `medium` (confirmed by standard-tier).
   - Matched fingerprint has a non-null, non-empty `remedy` in `fingerprints.json`.
   - Circuit breaker: `full-autonomy`.

2. Budget check (all four limits, in order):
   a. Count previous remediation attempts for this `ref` + `session_id`: must be < `budget.max_retries_per_event` (default 2).
   b. Sum cost of previous remediations for this event: must be < `budget.max_cost_per_event_usd` (default $5.00).
   c. Sum all remediation costs today (UTC midnight to now): must be < `budget.max_daily_cost_usd` (default $20.00).
   d. Sum all remediation costs this week (Monday 00:00 UTC to now): must be < `budget.max_weekly_cost_usd` (default $50.00).
   e. If ANY limit exceeded: skip remediation. Escalate via F-OT05 with `type: "budget_exceeded"`, specifying which limit was hit, current value, and maximum.

3. Execute remedy via configured handler:
   - **`subprocess` handler:** Write remedy context to a temp file containing: the `remedy` text from `fingerprints.json`, the original `f_raw`, the `stderr` (first 500 chars), the fingerprint `slug`, and one-line instruction `"Previous run failed with the above pattern. Apply the documented remedy."`. Fill command template variables (`{event_id}`, `{session_id}`, `{remedy_file}`, `{fingerprint_slug}`). Execute via `subprocess.run(args_list, shell=False, timeout=remediation.timeout_seconds)`. **Never use `shell=True`** — remedy context is written to a file and the file path is passed as an argument, preventing command injection.
   - **`callback` handler:** Call registered Python function with `(event_dict, fingerprint_dict, remedy_context_str)`.
   - **`noop` handler:** Log the remediation that would execute. Used automatically when circuit breaker is not `full-autonomy`.

4. Write remediation record to `.opentriage/remediations/{date}.jsonl`:
   ```json
   {
     "ts": 1720000000,
     "event_ref": "task-3",
     "session_id": "abc1",
     "fingerprint_slug": "circular-import",
     "action": "subprocess",
     "attempt_number": 1,
     "estimated_cost_usd": 0.15,
     "remedy_applied": "Check barrel files, split shared types into types.ts",
     "outcome": "pending",
     "handler_exit_code": 0
   }
   ```

5. **Outcome tracking** (runs at start of next triage cycle, Data Flow step 9):
   - For each remediation with `outcome: "pending"`:
     - Scan `.openlog/events/*.jsonl` for events with `ts` after the remediation's `ts` from the same `ref` or `session_id`.
     - If a `complete` event exists with no subsequent `error` matching the same fingerprint → `outcome: "success"`.
     - If an `error` event matching the same fingerprint slug recurred → `outcome: "failure"`.
     - If no subsequent events and <24 hours elapsed → `outcome` remains `"pending"`.
     - If no subsequent events and ≥24 hours elapsed → `outcome: "no_result"`. Treated as `failure` for circuit breaker metrics.
   - Update the remediation record's `outcome` field.
   - Feed resolved outcomes into circuit breaker metrics (F-OT03 step 6).

**Edge cases:**
- Subprocess command fails to launch (command not found, permission denied): record `outcome: "spawn_failed"`, `handler_exit_code: -1`. Escalate. Do not retry — the infrastructure is the problem.
- Subprocess times out (exceeds `remediation.timeout_seconds`): kill process, record `outcome: "timeout"`. Escalate.
- Same event triggers remediation twice in one cycle (batch race): deduplicate by `event_ref` + `session_id` before processing.
- Fingerprint `remedy` field is empty string (non-null but empty): treat as null. No remediation. Escalate with `reason: "empty_remedy"`.

**Delegation safety:** Fully delegatable. Executes user-configured command templates. Does not invent commands. Template is human-authored in config. Variables are filled from event data and written to a temp file (no shell interpolation).

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** Given a known-pattern correlation with a remedy-bearing fingerprint, the engine writes a remediation record with the correct command template expansion and `handler_exit_code` from subprocess.
- ✅ **Immediate / ⚙️ Mechanical:** Given a 3rd remediation attempt for the same event (exceeding `max_retries_per_event = 2`), the engine escalates with `"budget_exceeded"` instead of retrying.
- ✅ **Immediate / ⚙️ Mechanical:** With `handler = "noop"`, the engine logs without executing.
- 📏 **Trailing / ⚙️ Mechanical:** After 14 days, >60% of remediations have `outcome: "success"`.

---

### F-OT05: Escalation System

**Goal:** Deliver structured alert messages to humans via configurable channels when failures require human judgment.

**One-time vs. ongoing:** One-time implementation of routing and built-in channels. Ongoing: triggered by triage engine, remediation engine, circuit breaker, and health monitor.

**Procedure:**

1. Define the `EscalationChannel` protocol in `opentriage/escalation/channels.py`:
   ```python
   @runtime_checkable
   class EscalationChannel(Protocol):
       def send(self, alert: dict) -> bool:
           """Send an alert dict. Return True if delivered."""
           ...
   ```

2. Built-in channels:
   - `StdoutChannel`: prints formatted, indented text to stdout. Always available.
   - `WebhookChannel`: POSTs JSON body to `escalation.webhook_url`. Expects HTTP 2xx.
   - `DiscordChannel`: POSTs embed to `escalation.discord_webhook_url`. Truncates body to 2000 chars, appends `"...[truncated, full alert in .opentriage/escalations.jsonl]"` if exceeded.
   - `SlackChannel`: POSTs block to `escalation.slack_webhook_url`. Truncates body to 3000 chars with same truncation notice.

3. Alert format (structured dict; stdout prints indented text, webhooks send raw JSON, Discord/Slack render as embeds):
   ```json
   {
     "severity": "critical",
     "type": "escalation",
     "title": "F002 Confabulation — run_abc123",
     "body": "Agent claimed completion but modified 0 files.",
     "context": {
       "event_ref": "task-3",
       "session_id": "abc1",
       "fingerprint_slug": "confabulation",
       "remediation_attempts": 2,
       "cost_spent_usd": 0.28
     },
     "action_needed": "Review task prompt for ambiguous success criteria.",
     "ts": 1720000000
   }
   ```

4. Escalation triggers:
   - **Triage engine:** novel pattern confirmed → `type: "novel_pattern"`.
   - **Remediation engine:** budget exceeded → `type: "budget_exceeded"`, specifying which limit.
   - **Remediation engine:** remedy failed (outcome tracking) → `type: "remedy_failed"`.
   - **Circuit breaker:** state transition → `type: "circuit_breaker_change"`, with old/new state and reason.
   - **Health monitor:** trend alert → `type: "trend_alert"`, with breached threshold name and value.
   - **Triage engine:** backlog >100 events → `type: "triage_backlog"`, with pending count.

5. Delivery: iterate configured channels in order. Log each delivery attempt. If a channel returns `False` or raises, try the next channel.

6. Every escalation is appended to `.opentriage/escalations.jsonl` with `delivery_status` per channel, regardless of delivery success.

7. If all configured channels fail: write to `fallback_channel` (default: stdout). If stdout fails (daemon with closed fd): the `escalations.jsonl` log is the last resort.

**Edge cases:**
- Webhook returns non-2xx: retry once after 3 seconds. If still failing, mark `delivery_status: "failed"` for that channel and try next.
- Discord/Slack rate limit (HTTP 429): respect `Retry-After` header, retry once. If still blocked, fall back.
- No channels configured: default to `["stdout"]`.
- Flood protection: max 20 escalations per triage cycle. After that, batch remaining into a single summary escalation.

**Delegation safety:** Fully delegatable. Channels are configured by humans (webhook URLs in config). The system cannot send to unconfigured destinations.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** `StdoutChannel.send(test_alert)` prints formatted output and returns `True`.
- ✅ **Immediate / ⚙️ Mechanical:** `WebhookChannel.send(test_alert)` with a valid test URL returns `True` and the endpoint receives JSON.
- ✅ **Immediate / ⚙️ Mechanical:** With all channels failing, the alert still appears in `.opentriage/escalations.jsonl`.
- 📏 **Trailing / 👁️ Process:** After 7 days, every escalation in `escalations.jsonl` has `delivery_status: "delivered"` for at least one channel.

---

### F-OT06: Novel Pattern Synthesis

**Goal:** Draft new fingerprint entries for the OpenLog registry when the expensive-tier LLM confirms a failure pattern is genuinely novel.

**One-time vs. ongoing:** One-time implementation. Ongoing: triggered by the triage engine when a novel classification is confirmed by the standard-tier LLM.

**Procedure:**

1. Trigger: triage engine (F-OT02 step 8) produces a correlation with `classification: "novel"` and `confidence: "high"` (confirmed by standard-tier).

2. Gather context:
   - The error event (full JSONL record).
   - All events from the same session (`.openlog/events/{session}.jsonl`).
   - Cheap-tier and standard-tier classification reasoning.
   - Full fingerprint catalog summary (all slugs + first pattern, for dedup check).

3. Build synthesis prompt:
   ```
   You are a failure pattern analyst. A novel failure has been confirmed.
   Draft a new fingerprint entry for the failure registry.

   ERROR EVENT:
   {full event JSON}

   SESSION CONTEXT (all events from this session):
   {events, chronologically}

   CLASSIFICATION CHAIN:
   Cheap-tier: {classification, reasoning}
   Standard-tier: {confirmed classification, reasoning}

   EXISTING PATTERNS (for deduplication):
   {slug: first_pattern for each confirmed fingerprint}

   Respond with JSON:
   {
     "slug": "lowercase-hyphenated-max-40-chars",
     "description": "1-2 sentence description of the failure class",
     "patterns": ["the f_raw from this event", "1-2 alternative phrasings"],
     "severity": "fatal" | "recoverable" | null,
     "remedy": "suggested fix in 1-3 sentences, or null if unknown",
     "root_cause_hypothesis": "HYPOTHESIS — 1-3 sentences, not verified",
     "dedup_check": "This is NOT a variant of {closest_slug} because..."
   }
   ```

4. Send to expensive-tier model via provider.

5. Parse response. Save to `.opentriage/drafts/{slug}.json`:
   ```json
   {
     "slug": "silent-dependency-failure",
     "description": "Package dependency silently fails to install...",
     "patterns": ["dependency silently failed", "silent install failure"],
     "severity": "fatal",
     "remedy": "Pin dependency versions in requirements.txt...",
     "root_cause_hypothesis": "HYPOTHESIS — Package resolver...",
     "dedup_check": "Not a variant of circular-import because...",
     "source_event": {"session_id": "abc1", "ref": "task-3", "ts": 1720000000},
     "status": "proposed",
     "created": "2026-04-04",
     "recurrence_count": 1
   }
   ```

6. If a draft with the same slug already exists: increment `recurrence_count`, update `last_seen`. Do not overwrite other fields. If `recurrence_count` reaches 3, escalate with `type: "recurring_novel"`, severity `"high"`.

7. Escalate with `type: "novel_pattern"` so the human knows a new draft is available.

8. **Promotion path** (human-initiated): human reviews drafts in `.opentriage/drafts/`, edits fields, and adds the fingerprint to `.openlog/fingerprints.json` manually or via openlog-agent tooling. OpenTriage never promotes drafts automatically.

**Edge cases:**
- Expensive-tier returns slug colliding with existing fingerprint: append `-draft` to slug. Human resolves.
- Expensive-tier unavailable (provider error): save minimal draft with `f_raw` and `source_event` only; `severity: null`, `remedy: null`, `status: "incomplete"`. Escalate.
- 5+ novel patterns in a single cycle: process all. Escalate with `type: "novel_burst"` indicating the failure landscape may have shifted (dependency update, infrastructure change, or prompt regression).
- Dedup check says "this IS a variant of {existing}": save draft with `status: "likely_variant"`. Human decides whether to merge.

**Delegation safety:** Fully delegatable. Writes to isolated `.opentriage/drafts/`. Cannot modify `.openlog/fingerprints.json`. Cannot promote its own drafts.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** Given a synthetic novel event, the synthesizer produces a draft JSON with all required fields.
- ✅ **Immediate / ⚙️ Mechanical:** Draft `root_cause_hypothesis` contains the string `"HYPOTHESIS"`.
- ✅ **Immediate / ⚙️ Mechanical:** Submitting the same novel pattern 3 times results in `recurrence_count: 3` and a `"recurring_novel"` escalation.
- 📏 **Trailing / 👁️ Process:** After 30 days, >50% of drafts are promoted to `fingerprints.json` (with or without modification). Below 50% triggers synthesis prompt revision.

---

### F-OT07: Health Monitor

**Goal:** Compute periodic metrics tracking triage accuracy, remediation effectiveness, cost, and system trends.

**One-time vs. ongoing:** One-time implementation. Ongoing: runs via `opentriage health` or daily in watch mode.

**Procedure:**

1. Entry point: `opentriage health [--days N] [--today]`. Default: last 7 days.

2. Read all correlation records, remediation records, and escalation logs for the requested period.

3. Compute daily metrics for each day:
   ```json
   {
     "date": "2026-04-04",
     "events": {
       "total_scanned": 47,
       "errors_found": 8,
       "correlated": 8,
       "uncorrelated_remaining": 0
     },
     "classifications": {
       "known_pattern_fast_path": 5,
       "known_pattern_llm": 1,
       "novel": 1,
       "transient": 1,
       "deferred": 0,
       "override_count": 1,
       "override_rate": 0.50
     },
     "remediations": {
       "attempted": 4,
       "succeeded": 3,
       "failed": 1,
       "no_result": 0,
       "escalated_budget": 0,
       "success_rate": 0.75
     },
     "cost": {
       "cheap_tier_usd": 0.08,
       "standard_tier_usd": 0.22,
       "expensive_tier_usd": 0.35,
       "remediation_subprocess_usd": 1.20,
       "total_usd": 1.85
     },
     "system": {
       "circuit_breaker_state": "full-autonomy",
       "state_transitions": 0,
       "pending_drafts": 2,
       "triage_cycles_run": 12,
       "escalations_sent": 3
     }
   }
   ```

4. Write to `.opentriage/metrics/{date}.json` for each day computed.

5. **Trend detection** (compares current period against previous equivalent period):
   - Pattern frequency spike: any fingerprint slug appeared `trend_pattern_spike_threshold` (default 3) or more times today vs. 0-1 daily average historically → escalate `"trend_alert"`.
   - Remediation failure rate >`trend_remediation_failure_rate` (default 50%) for any single fingerprint → escalate.
   - Novel rate >`trend_novel_rate` (default 40%) of total errors → escalate (fingerprint catalog may be stale).
   - Override rate >`trend_override_rate` (default 30%) → escalate (cheap-tier model or prompt needs tuning).
   - Daily cost >`trend_daily_cost_warning_usd` (default $10, which is 50% of `max_daily_cost_usd`) → escalate.
   - Pending drafts >`trend_pending_drafts_max` (default 5) → escalate (human review backlog growing).

6. Output to stdout: human-readable summary (5-10 lines) of the period plus any trend alerts.

7. Weekly rollup: when `--days 7` or automatically on Sundays in watch mode, aggregate daily metrics into `.opentriage/metrics/weekly-{YYYY}-W{NN}.json`.

**Edge cases:**
- No data for requested period: output `"No triage activity in the requested period."` Write metrics file with all values 0.
- Metrics files from prior days missing (gap): compute from available data. Note gap in summary output. Do not interpolate.
- First 3 days (no historical baseline for trends): skip trend detection. Note `"trend detection requires ≥3 days of history"` in output.

**Delegation safety:** Fully delegatable. Read-only analysis producing JSON and optional escalations.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** Given synthetic correlation/remediation records, `opentriage health --today` produces a metrics JSON with all required fields and values that sum correctly.
- ✅ **Immediate / ⚙️ Mechanical:** Injecting a synthetic pattern spike (slug appearing 5 times vs. 0 historical) triggers `"trend_alert"`.
- 📏 **Trailing / ⚙️ Mechanical:** After 7 days, weekly rollup sums match daily metrics within $0.01 rounding tolerance.
- 📏 **Trailing / 👁️ Process:** Health reports reviewed weekly; at least 1 trend alert acted upon in the first month.

---

### F-OT08: CLI Interface

**Goal:** Expose all OpenTriage operations as an `opentriage` command-line tool installable via pip.

**One-time vs. ongoing:** One-time implementation. Ongoing: CLI is the primary user interface for all OpenTriage operations.

**Procedure:**

1. Package entry point in `pyproject.toml`:
   ```toml
   [project.scripts]
   opentriage = "opentriage.cli:main"
   ```

2. Commands:

   **`opentriage init`** — Create `.opentriage/` directory with default `config.toml`, empty `state.json`, and subdirectories (`correlations/`, `remediations/`, `drafts/`, `metrics/`). If `.opentriage/` exists: print `"Already initialized."` and exit. Use `--force` to reinitialize (preserves data, resets config to defaults). If `.openlog/` does not exist: warn `"OpenLog directory not found. Install openlog-agent first."`.

   **`opentriage triage [--window HOURS] [--all] [--dry-run]`** — Run one triage cycle (F-OT02). `--dry-run` classifies without remediating or escalating. Output: one summary line per classified event + totals.

   **`opentriage remediate --event EVENT_REF --session SESSION_ID`** — Manually trigger remediation for a specific event. Requires the event to be already correlated. Respects budget limits.

   **`opentriage status`** — Print circuit breaker state, rolling metrics, pending drafts count, last triage/health timestamps, promotion eligibility. Formatted table to stdout.

   **`opentriage health [--days N] [--today]`** — Run health monitor (F-OT07). Print summary, write metrics file.

   **`opentriage watch [--interval SECONDS]`** — Continuous mode: run triage every `--interval` seconds (default 120), health once daily. Signal handlers (SIGTERM, SIGINT) flush state and exit cleanly.

   **`opentriage promote`** — Set `human_approved_promotion = true` in state.json. Print current state and describe which promotion would occur if metrics qualify.

   **`opentriage config [KEY] [VALUE]`** — View or set config. `opentriage config` prints all. `opentriage config provider.backend` prints one. `opentriage config provider.backend openai` sets one.

3. Global flags:
   - `--verbose` / `-v`: debug output including LLM prompts/responses.
   - `--quiet` / `-q`: suppress all except errors.
   - `--config PATH`: override config file (default: `.opentriage/config.toml`).
   - `--openlog-dir PATH`: override OpenLog data directory (default: `.openlog/`).

4. Exit codes:
   - `0`: success.
   - `1`: error (config missing, provider failure, etc.).
   - `2`: triage found critical issues (useful for CI gating).

**Edge cases:**
- Running any command before `opentriage init`: check for `.opentriage/`. If missing: `"Run 'opentriage init' first."`, exit code 1.
- `opentriage watch` in a terminal that closes: SIGTERM/SIGINT handlers flush pending state and exit cleanly.
- Two `opentriage triage` instances simultaneously: lockfile at `.opentriage/.triage.lock`. If locked: `"Another triage cycle is running (PID {pid}). Use --force to override stale lock."`. Lock expires after 1 hour (stale detection).

**Delegation safety:** Fully delegatable. CLI is a thin wrapper over feature modules. Each command maps to a feature entry point.

**Success criteria:**
- ✅ **Immediate / ⚙️ Mechanical:** `pip install opentriage` makes `opentriage` available. `opentriage --help` lists all commands.
- ✅ **Immediate / ⚙️ Mechanical:** `opentriage init` creates `.opentriage/` with `config.toml` and `state.json`.
- ✅ **Immediate / ⚙️ Mechanical:** `opentriage triage --dry-run` with test events produces classification output without modifying state.
- 📏 **Trailing / ⚙️ Mechanical:** After 7 days, `opentriage status` shows non-null `last_triage_run` and `last_health_run` timestamps and `total_remediations > 0`.

---

## Implementation Sequence

| Step | Feature | Depends On | Effort | Parallelizable |
|------|---------|------------|--------|----------------|
| 1 | F-OT01: LLM Provider Interface | — | 1 iteration | Yes (with 2, 3) |
| 2 | F-OT03: Circuit Breaker State Machine | — | 1 iteration | Yes (with 1, 3) |
| 3 | F-OT05: Escalation System | — | 1 iteration | Yes (with 1, 2) |
| 4 | F-OT02: Triage Engine | F-OT01 | 2-3 iterations | No |
| 5 | F-OT04: Auto-Remediation Engine | F-OT02, F-OT03, F-OT05 | 1-2 iterations | Yes (with 6) |
| 6 | F-OT06: Novel Pattern Synthesis | F-OT01, F-OT02 | 1 iteration | Yes (with 5) |
| 7 | F-OT07: Health Monitor | F-OT02, F-OT04 | 1 iteration | No |
| 8 | F-OT08: CLI Interface | All features | 1-2 iterations | No (built incrementally) |

**Steps 1, 2, 3 have no dependencies — start in parallel.**
**Steps 5 and 6 can run in parallel** (both depend on step 4 but not on each other).
**Step 8 (CLI) is built incrementally** — add subcommands as each feature completes. Skeleton with `init` and `--help` ships with step 1.

**External dependency:** `openlog-agent` must be installed and `.openlog/` must exist with events and fingerprints. OpenTriage can be installed first but has no data to process until openlog-agent is active.

---

## Feature Tracker

| ID | Feature | Status | Depends On |
|----|---------|--------|------------|
| F-OT01 | LLM Provider Interface | ❌ | — |
| F-OT02 | Triage Engine | ❌ | F-OT01 |
| F-OT03 | Circuit Breaker State Machine | ❌ | — |
| F-OT04 | Auto-Remediation Engine | ❌ | F-OT02, F-OT03, F-OT05 |
| F-OT05 | Escalation System | ❌ | — |
| F-OT06 | Novel Pattern Synthesis | ❌ | F-OT01, F-OT02 |
| F-OT07 | Health Monitor | ❌ | F-OT02, F-OT04 |
| F-OT08 | CLI Interface | ❌ | All |

---

## Success Criteria (Spec-Level)

| ID | Criterion | Type | Verification |
|----|-----------|------|-------------|
| SC1 | `pip install opentriage` succeeds; `opentriage --help` lists all commands | Immediate / Mechanical | Run install + help |
| SC2 | Triage engine classifies 5 synthetic events correctly (3 fast-path known, 1 LLM known, 1 novel) | Immediate / Mechanical | Run against test fixtures |
| SC3 | Circuit breaker transitions to `classify-only` when `rolling_remediation_success_rate` set to 0.50 | Immediate / Mechanical | Inject metric, run cycle |
| SC4 | Remediation engine respects all 4 budget limits (per-event retries, per-event cost, daily, weekly) | Immediate / Mechanical | Inject over-budget scenarios for each |
| SC5 | Escalation delivers to stdout, webhook, and at least one chat channel | Immediate / Mechanical | Send test alerts |
| SC6 | Novel synthesis draft contains `HYPOTHESIS` marker and all required fields | Immediate / Mechanical | Validate draft schema |
| SC7 | Health metrics JSON contains all required fields with sums matching component totals | Immediate / Mechanical | Schema validation + arithmetic |
| SC8 | Malformed JSONL event file does not crash any component | Immediate / Mechanical | Inject malformed lines |
| SC9 | Fast-path handles ≥60% of known-pattern events without LLM after 7 days | Trailing / Mechanical | Read from metrics |
| SC10 | Remediation success rate exceeds 60% after 14 days | Trailing / Mechanical | Read from remediation records |
| SC11 | >50% of novel pattern drafts promoted to fingerprints.json after 30 days | Trailing / Process | Count drafts vs promotions |
| SC12 | Weekly cost stays below `budget.max_weekly_cost_usd` ($50 default) | Trailing / Mechanical | Read from weekly metrics |

---

## Anti-Patterns

**Do NOT let OpenTriage write to `.openlog/fingerprints.json` directly.** The fingerprint registry is owned by openlog-agent's indexer. OpenTriage proposes drafts in `.opentriage/drafts/`. A human or the indexer promotes them. Two writers to the same JSON file create race conditions and make it impossible to audit who added what.

**Do NOT use the expensive-tier model for triage classification.** Triage processes every error event. At $0.15-0.50 per call, expensive-tier triage on 10 events costs $1.50-5.00. Cheap-tier costs $0.10-0.50 for the same 10. Reserve the expensive tier for novel pattern synthesis only (1-2 calls per cycle, not 10+).

**Do NOT skip string matching for known patterns.** The fast path is free and instant. Sending all events to the LLM "because it is smarter" wastes money and adds latency. String matching handles ≥60% of known patterns after the registry matures. LLM is the fallback, not the primary path.

**Do NOT auto-promote drafts to the fingerprint registry.** Novel pattern synthesis uses an LLM to hypothesize. Auto-promoting pollutes the registry with unverified patterns. Human review is the quality gate between "proposed" and "confirmed."

**Do NOT escalate without context.** Every escalation must include: what failed (`f_raw`), what pattern matched (slug or "novel"), what was tried (remediation attempts, cost), what is needed (specific action). `"Agent failed"` is noise. `"F002 Confabulation on run_abc123, 2 retries exhausted at $0.28, need prompt review"` is actionable.

**Do NOT trust remediation outcomes without verification.** `outcome: "success"` means the same fingerprint pattern did not recur in subsequent events. It does not mean the agent's task succeeded overall. OpenTriage tracks pattern recurrence, not task completion.

**Do NOT run all model tiers in sequence for every event.** The tiered system is a filter: cheap first (most events stop here), standard for uncertain cases (fewer), expensive for novel synthesis only (rare). Running all three for every event turns a $0.01 triage into a $0.70 triage.

**Do NOT use `shell=True` in the subprocess remediation handler.** Event data (`f_raw`, `stderr`) is untrusted input. Shell interpolation enables command injection. Write context to a temp file; pass the file path as an argument via `subprocess.run(args_list, shell=False)`.

---

## Self-Review

### Pass 1: Structural

Issues found and fixed:

- **P1-01:** `remediation handler` referenced in F-OT04 procedure but not in Definitions. **Fixed:** added "Remediation handler" to Definitions with three built-in types (subprocess, callback, noop).
- **P1-02:** `override` used in F-OT02 step 7 and F-OT07 metrics without definition. **Fixed:** added "Override" to Definitions with threshold (30%) and meaning.
- **P1-03:** `net_remediation_effect` referenced in F-OT03 transitions without explicit formula. **Fixed:** added to Definitions with formula `(successes - failures) / total_resolved`.
- **P1-04:** `rolling accuracy window` referenced in F-OT03 without definition. **Fixed:** added to Definitions with default (7 days) and config key.
- **P1-05:** Feature Tracker showed F-OT04 depending on F-OT05 but Implementation Sequence step 5 originally listed only F-OT02, F-OT03. **Fixed:** added F-OT05 to step 5 dependencies — remediation needs escalation for budget-exceeded fallback.
- **P1-06:** F-OT02 (Triage Engine) at 2-3 iterations: verified decomposition. Iteration 1: fast path + slow path (steps 3-6). Iteration 2: confirmation + recurrence detection (steps 7-9). Iteration 3: batch limits + integration (step 10). Acceptable.
- **P1-07:** Architecture Data Flow step numbers did not match F-OT02 procedure numbering. **Fixed:** aligned data flow to reference the same step logic as F-OT02.
- **P1-08:** `fast path` and `slow path` referenced throughout without Definitions entries. **Fixed:** added both to Definitions with explicit descriptions and sequential-fallback relationship.
- **P1-09:** `triage cycle` used in multiple features without definition. **Fixed:** added to Definitions.

### Pass 2: Semantic

Issues found and fixed:

- **P2-01:** F-OT05 alert format description originally said "channels render as appropriate" — ambiguous. **Fixed:** replaced with explicit per-channel rendering: "stdout prints indented text, webhooks send raw JSON, Discord/Slack render as embeds."
- **P2-02:** F-OT06 step 2 originally said "relevant context" without specifics. **Fixed:** replaced with explicit list: "all events from the same session" and "full fingerprint catalog summary (all slugs + first pattern)."
- **P2-03:** Budget defaults ($20/day, $50/week) stated without derivation. **Fixed:** added note to Definitions under "Remediation budget": "At 10 events/day with 30% needing LLM, daily LLM cost is ~$0.50-2.00. Remediation subprocess costs dominate. $20/day allows ~10-40 remediations."
- **P2-04:** `provider.timeout_seconds` in F-OT01 edge cases had no default in config. **Fixed:** added `timeout_seconds = 60` to default config TOML.
- **P2-05:** F-OT08 trailing criterion originally said "all commands invoked at least once" — untestable without usage logs. **Fixed:** replaced with "`opentriage status` shows non-null `last_triage_run` and `last_health_run` and `total_remediations > 0`."
- **P2-06:** F-OT06 edge case originally said "something systemic may have changed" — vague. **Fixed:** replaced with "failure landscape may have shifted (dependency update, infrastructure change, or prompt regression)."
- **P2-07:** F-OT07 trend threshold "daily cost >$10" presented without derivation. **Fixed:** added "(50% of `max_daily_cost_usd`)" as inline explanation.
- **P2-08:** "One-time vs. ongoing" for F-OT08 (CLI): missing what happens after one-time. **Fixed:** added "Ongoing: CLI is the primary user interface for all OpenTriage operations."

### Pass 3: Adversarial

Vectors checked:

- **Confabulation (F002):** Where could a sub-agent claim completion without doing the work?
  - **F-OT02 (triage):** A build agent could hardcode all classifications as `"transient"`. **Mitigation:** SC2 requires correct classification of 5 specific synthetic events. SC9 trailing metric verifies real-world fast-path rate — 100% fast-path on diverse events would be anomalous and detectable via override rate.
  - **F-OT04 (remediation):** Agent could write remediation records without executing the command. **Mitigation:** Records include `handler_exit_code` (non-null proves subprocess ran). Outcome tracking independently verifies pattern recurrence.

- **Duplicate implementation (F012):** Where could a sub-agent create something that exists?
  - OpenLog's indexer does fingerprint matching. A build agent might reimplement it. **Mitigation:** F-OT02 step 5 says "substring + trigram matching (same algorithm as openlog-agent indexer)" and Architecture says to import from openlog or read files directly. No instruction to create a new matching algorithm.

- **Plan vandalism (F013):** Where could a build agent destructively overwrite shared state?
  - `.opentriage/state.json` is highest risk. **Mitigation:** F-OT03 step 1 specifies exact schema. `demotion_history` is append-only. `circuit_breaker` accepts exactly 4 values; any other value triggers default to `suspended`.
  - `.openlog/fingerprints.json` could be accidentally modified. **Mitigation:** Anti-Pattern #1 explicitly bans direct writes. F-OT06 writes only to `.opentriage/drafts/`.

- **Context loss:** Could a fresh sub-agent misinterpret this spec?
  - All terms defined, file paths explicit, JSON schemas provided, protocol code included.
  - **Potential gap fixed:** F-OT02 fast/slow path relationship could be misread as parallel systems. **Fixed:** added explicit note in F-OT02 step 5c ("This event does not enter step 6") and Definitions entries clarify "sequential fallback, not a parallel system."

- **Iteration boundaries:** What breaks with full context reset between iterations?
  - Each feature is self-contained. F-OT02 is largest at 2-3 iterations. Iteration 1 (fast + slow) testable independently. Iteration 2 (confirmation + recurrence) reads correlation files from iteration 1's output. No in-memory state crosses iterations (OT7).

- **Backpressure gaps:** What failures have no mechanical catch?
  - **Gap:** If LLM returns structurally valid but semantically wrong classifications (always "transient"), no real-time catch exists. **Mitigation:** Override rate (F-OT07) detects cheap-tier inaccuracy. Remediation success rate (F-OT03) detects overall quality. Both trailing with 7-day lag. **Accepted risk:** real-time semantic validation requires double the LLM calls, defeating cost optimization.
  - **Gap:** Misconfigured escalation channels cause silent delivery failure. **Mitigation:** F-OT05 logs all escalations to `escalations.jsonl` regardless. Health monitor tracks delivery. Failed channels fall back to stdout.

- **Scope creep:** Which features are closest to out-of-scope boundaries?
  - F-OT04 (remediation) is closest to "agent spawning" (out of scope). **Mitigation:** spec says remediation executes a *configured command template* — human-authored, not agent-invented.
  - F-OT06 (synthesis) is closest to "fingerprint matching improvements" (out of scope). **Mitigation:** drafts go to `.opentriage/drafts/`, not `fingerprints.json`. Synthesis is a proposal workflow, not a registry modification.

---

## Remaining Risks

1. **Cold start problem.** OpenTriage needs ≥5 resolved remediations (`min_resolved_for_evaluation`) to evaluate circuit breaker metrics. During the first 1-2 weeks, metrics are `null` and no self-monitoring transitions fire. If triage is misconfigured, it operates at full autonomy unchecked. *Mitigation: `max_retries_per_event = 2` limits blast radius. Document that manual monitoring is recommended during the first 2 weeks. Consider a `--cautious` flag in v1.1 that starts in `classify-only` until enough data accumulates.*

2. **LLM quality varies by provider.** A cheap local model (Llama 3.1 8B) may produce worse classifications than Haiku. The thresholds (0.7 similarity, 30% override rate) assume a minimum model quality baseline that is not enforced. *Mitigation: document minimum recommended model sizes per tier. Add `opentriage validate-provider` in v1.1 that runs a synthetic test suite and reports accuracy.*

3. **Remediation command template injection.** If event data were interpolated into shell commands, malicious `f_raw` could inject commands. *Mitigation: F-OT04 specifies `subprocess.run(shell=False)`. Remedy context is written to a file and the path is passed. Anti-Pattern #8 reinforces this. A test must verify `shell=False` is enforced.*

4. **Fingerprint registry read contention.** OpenLog's indexer and OpenTriage both read `fingerprints.json`. If the indexer writes while OpenTriage reads, a partial file could be read. *Mitigation: retry on JSON parse failure. OpenLog should use atomic write (write-to-temp + rename), but OpenTriage must be resilient regardless.*

5. **Escalation fatigue.** Frequent escalation (>5 alerts/day) causes humans to ignore alerts. *Mitigation: budget limits constrain total activity and thus alert volume. Health monitor tracks escalation frequency. F-OT05 includes per-cycle flood protection (max 20). v1.1 should add deduplication and digest mode.*

6. **No authentication on state files.** `.opentriage/state.json` and `config.toml` are plain files. Any process can modify them. *Out of scope for v1.0 — single-user, single-machine tool. Note for v2 if OpenTriage runs in shared infrastructure.*

---

## Changelog

- **v1.0** (2026-04-04): Initial spec following META_PROMPT v1 (Phase 0-3). 8 features, 12 spec-level success criteria.
  - **Pass 1 (Structural — 9 issues):** P1-01 through P1-09. Added 5 missing Definitions entries. Aligned data flow step numbers. Added missing dependency. Verified feature sizes.
  - **Pass 2 (Semantic — 8 issues):** P2-01 through P2-08. Replaced 2 ambiguous words. Added 3 threshold derivations. Fixed 2 untestable criteria. Clarified 1 vague phrase.
  - **Pass 3 (Adversarial — 7 vectors):** Confabulation mitigated by mechanical criteria + outcome tracking. Duplication mitigated by explicit reuse instructions. Vandalism mitigated by append-only history + schema constraints. Context loss mitigated by Definitions + explicit step references. 2 backpressure gaps accepted with trailing-metric mitigation. 2 scope-creep risks contained by boundary statements.
