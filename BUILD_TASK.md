# BUILD TASK: Missing CLI Management Commands

## Context
OpenTriage is a pip-installable failure-response engine. Core features (F-OT01 through F-OT07) are built and passing 94 tests. The CLI (`src/opentriage/cli.py`) has 8 working commands: `init`, `triage`, `remediate`, `status`, `health`, `watch`, `promote`, `config`.

What's missing: management commands for day-to-day human operation of the system.

## Commands to Implement

### 1. `opentriage drafts [--json]`
List pending draft fingerprints from `.opentriage/drafts/`. Show: slug, source event count, created date, confidence. `--json` outputs raw JSON array.

### 2. `opentriage approve <slug> [--comment TEXT]`
Approve a draft fingerprint: read from `.opentriage/drafts/{slug}.json`, validate it has required fields (pattern, severity, category), copy it into `.openlog/fingerprints.json` (append to the registry), then move the draft to `.opentriage/drafts/approved/{slug}.json`. Print confirmation. If slug doesn't exist, exit 1 with error.

### 3. `opentriage reject <slug> [--reason TEXT]`
Reject a draft fingerprint: move from `.opentriage/drafts/{slug}.json` to `.opentriage/drafts/rejected/{slug}.json`, adding `rejected_reason` and `rejected_at` fields. Print confirmation.

### 4. `opentriage escalations [--last N] [--json]`
Read `.opentriage/escalations.jsonl` and display recent escalations. Default last 20. Show: timestamp, severity, event ref, delivery status, channel. `--json` outputs raw JSONL.

### 5. `opentriage validate`
Validate the installation: check `.opentriage/` exists, `config.toml` is parseable, `.openlog/` exists and has event files, provider API key env var is set, state.json is valid. Print a checklist with ✅/❌ per check. Exit 0 if all pass, exit 1 if any fail.

### 6. `opentriage calibrate [--events N]`
Run a calibration check: take the last N events (default 10) that have both LLM classification AND a matching fingerprint, compare them, report agreement rate. This is a read-only diagnostic — no state changes. Helps users tune confidence thresholds.

### 7. `opentriage revert --remediation-id ID`
Mark a remediation as reverted in `.opentriage/remediations/`. Update the outcome to `reverted` and the circuit breaker's rolling success rate. Print what changed.

### 8. `opentriage cleanup [--older-than DAYS] [--dry-run]`
Clean up old data: remove correlation files, remediation records, and metric files older than N days (default 30). `--dry-run` lists what would be removed without deleting. Print summary of files removed/bytes freed.

## Technical Requirements
- All commands follow existing patterns in `cli.py` (argparse subparsers, same error handling style)
- All commands check for `.opentriage/` initialization (except `validate` which reports it)
- Add tests for each command in `tests/` following existing test patterns
- Use existing modules where possible (`io/reader.py`, `io/writer.py`, `circuit_breaker.py`)
- Exit codes: 0 success, 1 error, 2 critical finding (matching existing convention)
- Human-readable output by default, `--json` where specified

## Spec Reference
The full spec is in `OPENTRIAGE_SPEC.md`. Key sections:
- F-OT08 (CLI Interface): lines 845-900
- F-OT05 (Novel Pattern Synthesis / drafts): lines 700-760  
- F-OT04 (Escalation System): lines 600-670
- F-OT03 (Circuit Breaker): lines 460-530

## Existing Tests
Run with: `source .venv/bin/activate && python -m pytest tests/ -v`
94 tests currently passing. Don't break them.
