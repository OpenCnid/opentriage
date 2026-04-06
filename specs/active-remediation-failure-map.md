# Active Remediation — Failure Map

```yaml
lifecycle: created pre-implementation → consult during → update post-first-run → archive when stable
spec: active-remediation.md
catalog-version: F001–F032 (26 entries)
created: 2026-04-05
```

---

## Section 1 — Domain Tables

### Topological Failures (broken connections between components)

| ID | Pattern | Severity | Existing? | Earliest Catch |
|----|---------|----------|-----------|----------------|
| T1 | **Evidence-to-Agent Schema Drift.** F-AR03 produces `evidence.json` and F-AR04 reads it to build the fix prompt. No shared schema or validation between them — if the evidence bundle format evolves (new field, renamed key, nested structure change), the spawner builds a broken prompt silently. | 🟡 | F005 variant (stale handoff documents) | **Design:** Define `EvidenceBundle` as a typed dataclass/TypedDict shared between `evidence.py` and `agent_handler.py`. Add `validate_evidence(bundle)` call before prompt generation. |
| T2 | **Screenshot Path Three-Hop Resolution.** F-AR01 writes `data.screenshot` path into JSONL events. F-AR03 reads it into the evidence bundle. F-AR04 tells the fix agent to analyze it. Three components pass file paths — any hop could reference a moved, deleted, or permission-denied screenshot. The 30-second mtime window in F-AR01 is fragile if system clocks drift or screenshots are written asynchronously. | 🟡 | New | **Implementation:** F-AR03 should `os.path.exists()` + `os.access(path, os.R_OK)` on screenshot paths at assembly time. Set to `null` with a warning annotation if inaccessible. Don't pass stale paths downstream. |
| T3 | **Fingerprint Registry Shared Mutable State.** `~/.openlog/fingerprints.json` is read by: triage engine, evidence assembler, remedy format parser, recurrence verifier. Written by: draft system, auto-approve, remedy migration (F-AR02). No file locking. Concurrent read/write during a triage cycle + remediation cycle = possible corruption or stale reads. | 🔴 | New | **Design:** Either (a) copy-on-read with atomic writes (`write temp → rename`) or (b) add a `LockedRegistry` context manager. The triage cycle should snapshot the registry at start and operate on the snapshot. |
| T4 | **Fix Commit → Bot Restart Signal Gap.** F-AR04 commits a fix. The spec claims `loop.sh` "already handles this" (exits iteration → restarts). But there's no defined signal from "fix committed" to "loop.sh detects it should restart." If the bot is mid-session and not in a restart-friendly state, the fix sits uncommitted until the next natural loop cycle — which could be hours. | 🟡 | New | **Design decision needed:** Either (a) touch a sentinel file that `loop.sh` polls, (b) send SIGUSR1 to the bot process, or (c) accept that fixes only take effect on next natural restart. Document the chosen mechanism in F-AR05. |
| T5 | **Recurrence Window Active-Time Tracking.** F-AR06 says "extend window until bot has run for at least 1 hour of active time post-fix." But no mechanism exists to measure "active time." OpenLog events could approximate this (count events post-fix), but the spec doesn't wire this. Without it, the window extension is unimplementable. | 🟡 | New | **Implementation:** Track `active_minutes_since_fix` by counting OpenLog event timestamps post-fix. Define "active" = at least 1 event per 5-minute window. Store in `state.json` alongside `pending_verifications`. |

### Contradiction Failures (conflicting rules within the spec)

| ID | Pattern | Severity | Existing? | Earliest Catch |
|----|---------|----------|-----------|----------------|
| C1 | **Per-Attempt Cost Ceiling Unenforceable.** F-AR04 edge cases section says: "Cost ceiling exceeded mid-agent → impossible to enforce in real-time with --print mode; enforce at budget-check level before spawning." This means the $2/attempt ceiling (DP-03) is a *hope*, not a *guard*. A complex fix could cost $5-8. The daily budget ($10) is the only real gate, but a single runaway agent could blow it. | 🔴 | F024 variant (autonomous cost runaway) | **Design:** Either (a) use `sessions_spawn` with `runTimeoutSeconds` as a cost proxy (time ≈ cost for LLMs), (b) add a token-count monitor that kills the process at a threshold, or (c) accept the risk and set the daily budget as the hard limit. Amend DP-03 to state the real enforcement mechanism. |
| C2 | **Claude CLI vs. sessions_spawn Execution Path.** Architecture says "Claude Code via sessions_spawn." F-AR04 procedure says `claude --print --permission-mode bypassPermissions`. These are different: `sessions_spawn` uses OpenClaw's agent runtime with built-in timeout/budget controls; the CLI runs a raw subprocess. The spec must pick one. | 🔴 | New | **Spec amendment required.** Recommend `sessions_spawn` — it provides timeout enforcement, cost tracking, and sandboxing that raw CLI doesn't. Update F-AR04 procedure accordingly. |
| C3 | **Fingerprint Modification Contradiction.** Anti-pattern: "Do NOT modify fingerprints.json during remediation." F-AR05 procedure step 4: "After successful remediation, update the fingerprint's count and last_remediated timestamp." These directly contradict. | 🟡 | New | **Spec amendment required.** Clarify: the anti-pattern means "fix agents don't touch fingerprints.json." The orchestrator (F-AR05) may update metadata fields (count, timestamps) after the fix agent exits. Reword both sections. |
| C4 | **Novel Error Auto-Remediation Path Undefined.** Data flow says novel errors get LLM-drafted remedy → auto-approved if severity < fatal → "next occurrence triggers remediation." But F-AR05 only triggers for "matched fingerprints with remedies." The transition from "auto-approved draft" to "matched fingerprint in the main registry" isn't defined. Does auto-approve copy to main registry? Is an LLM-generated `fix_prompt` trusted enough for unsupervised code-fix? | 🟡 | F002 variant (confabulation — LLM drafts untested remedy) | **Design decision needed:** Either (a) LLM-drafted remedies always start as `strategy: "escalate"` until a human promotes them to `code-fix`, or (b) LLM-drafted remedies get one supervised attempt before auto mode. Document in Decisions Log. |

### Coverage Gaps (unguarded failure surfaces)

| ID | Pattern | Severity | Existing? | Earliest Catch |
|----|---------|----------|-----------|----------------|
| G1 | **Fix Agent Sandbox Is Illusory.** F-AR04 says "agent modifies files outside project → git checkout reverts." But `git checkout` only reverts *tracked* files. New files created outside the repo persist. `--print` with `bypassPermissions` gives full filesystem access. The fix agent could write to `~/.openlog/`, `~/.opentriage/`, system configs, or other projects. | 🔴 | New | **Implementation:** Use `sessions_spawn` with `sandbox: "require"` if available, or at minimum: (a) capture `git status --porcelain` pre and post agent, (b) revert any untracked files outside the project tree, (c) use `--cwd` to restrict the agent's working directory. |
| G2 | **Concurrent Fix Agent Race Condition.** F-AR05 deduplicates same-fingerprint errors within a cycle, but two *different* fingerprints could both target the same source file. Two fix agents running concurrently produce conflicting edits → merge conflicts → both "succeed" but the committed state is garbage. | 🟡 | F018 variant (cross-task interference) | **Implementation:** Serial execution — queue fix agents and run one at a time. The 5-minute timeout makes parallelism unnecessary (a triage cycle with 3 fixes = 15 minutes max serial vs. race condition risk parallel). |
| G3 | **Fix Agent Prompt Injection via Error Content.** The evidence bundle includes error messages and session context from OpenLog. If the bot processes user-facing content (game text, chat messages) and that content appears in error messages, it's injected verbatim into the fix agent's prompt. A pathological error message could manipulate the fix agent. | 🟡 | New | **Implementation:** Sanitize error messages in the evidence bundle — truncate to 500 chars, strip control characters, wrap in a clearly delimited `<error_content>` block with instructions that it's untrusted data. |
| G4 | **No Rollback for Cascading Failures.** F-AR06 checks if the *same* fingerprint recurs. But a fix could introduce a *new* failure pattern. The fix passes tests, the original error stops, but a different error starts. No mechanism detects "this fix caused a new problem." | 🔴 | New | **Design:** After a fix is applied, track not just the target fingerprint but the overall error rate. If the error rate *increases* in the recurrence window (even for different fingerprints), flag the fix as potentially harmful and `git revert` the fix commit. Requires: fix commits are atomic (single commit with known SHA). |
| G5 | **Test Suite as Sole Oracle.** F-AR04 judges fix success by "tests pass." But the test suite may not cover the specific failure pattern. A fix agent is incentivized to make tests green, not to fix the actual bug. It could make a semantically meaningless change that happens to satisfy the test assertions. | 🟡 | F001 variant + F002 variant | **Implementation:** Add a secondary verification: after "tests pass," check that the git diff actually touches files mentioned in the fingerprint's `relevant_files`. If the diff is empty or only touches unrelated files, the fix is suspicious — escalate. |
| G6 | **antml:thinking Skip-List Undefined.** Anti-patterns say "add to a skip-list" for `antml:thinking` errors but never define the skip-list format, storage location, or wiring. This will be ignored during implementation because it's not actionable. | 🟢 | New | **Spec amendment:** Define `skip_patterns` list in `.opentriage/config.toml` under `[remediation]` section. Each entry is a regex matched against the error's `f_raw` field. Pre-seed with `antml:thinking`. Wire into F-AR05 before the budget check. |
| G7 | **Circuit Breaker State Fragmentation.** F-AR05 mentions circuit breaker (3 consecutive failures → 24h suspend) but doesn't specify where state lives. F-AR06 defines `state.json` with `pending_verifications` but doesn't mention circuit breaker state. Two stateful mechanisms, no shared state file definition. | 🟡 | New | **Spec amendment:** Define `.opentriage/state.json` schema in a new Definitions section entry. Include both `pending_verifications[]` and `circuit_breakers{}` (keyed by fingerprint slug, containing `consecutive_failures`, `suspended_until`, `last_attempt_ts`). |
| G8 | **DP-04 Enforcement Is Prompt-Only.** "Never modify test expectations" (DP-04) is stated in the fix agent prompt but has no mechanical enforcement. The fix agent has full write access to test files. This is F020 (Silent Test Deletion) wearing a new mask. | 🟡 | F020 variant | **Implementation:** After fix agent exits, run `git diff --name-only` and check for modified test files (match `*test*`, `*spec*`, `__tests__/*`). If any test files were modified: parse the diff to check if assertions were weakened/removed. If test count dropped: hard reject the fix. |

---

## Section 2 — Existing Catalog Manifestation Map

| Failure | Manifestation in Active Remediation | Risk | Mitigation |
|---------|-------------------------------------|------|------------|
| **F001** (Exit Code Lies) | Fix agent CLI (`claude --print`) returns exit 0 even when it didn't fix anything, or ran but didn't execute tests. The spawner sees "success" and logs `status: "fixed"` for a non-fix. | 🔴 | Don't trust exit code alone. Verify: (a) `git diff` is non-empty, (b) tests actually ran (parse test runner output for pass/fail counts), (c) the changed files overlap with `relevant_files`. |
| **F002** (Agent Confabulation) | Fix agent claims "updated the selector" but edited the wrong file, made a no-op change, or "fixed" something that wasn't broken. Output looks plausible. | 🔴 | Evidence bundle includes expected fix location (`relevant_files`). Post-fix verification must check that the diff touches those files. If it doesn't, escalate — the agent confabulated. |
| **F004** (Git Boundary Invisible Docs) | Fix agent is spawned in the project directory but can't see OpenTriage docs, fingerprint documentation, or the spec. It operates blind on what the fingerprint means. | 🟡 | Include fingerprint `description` and `fix_prompt` in the evidence bundle prompt — don't rely on the agent reading external docs. The prompt IS the documentation. |
| **F012** (Duplicate Implementation) | Fix agent re-implements a function that already exists elsewhere, creating duplication. Particularly likely for utility functions in the bot. | 🟡 | Include `git log --oneline -5` in evidence bundle so the agent sees recent changes. Add to fix prompt: "Check if the fix already exists in a recent commit before writing new code." |
| **F013** (Plan Vandalism) | Fix agent modifies files outside its scope — changes the bot's core loop, rewrites strategy modules, or "improves" unrelated code while fixing the target bug. | 🟡 | Post-fix `git diff --stat`: if >3 files changed or >100 lines changed for a single bug fix, flag as suspicious. Single-bug fixes should be surgical. |
| **F014** (Multi-Task Violation) | Fix agent tries to fix multiple errors at once if the evidence bundle contains context about other recent errors. "While I'm here, let me also fix..." | 🟡 | Fix prompt must explicitly state: "Fix ONLY the error described. Do not address other issues you notice. One bug, one fix." |
| **F020** (Silent Test Deletion) | Fix agent deletes or weakens failing tests to make validation pass. DP-04 says don't, but enforcement is prompt-only. | 🔴 | Mechanical: compare test count pre/post fix agent. If count drops, hard reject. If test files are modified, parse diff for removed assertions. (See G8 above.) |
| **F024** (Autonomous Cost Runaway) | Triage cron every 2h × multiple fingerprints per cycle × $2-8 per fix attempt. 12 cycles/day × 3 fixes/cycle = $72-288/day potential. The $10/day budget is the only gate, and it's checked pre-spawn (a $5 agent that started under budget still overruns). | 🔴 | (a) Daily budget is hard-enforced (no new spawns if budget exceeded), (b) per-agent timeout as cost proxy (5 min = ~$2-3 for Sonnet), (c) add a "first week" conservative budget ($5/day) with manual review of all remediations before increasing. |
| **F032** (Imprecise Logging) | Remediation log records `status: "fixed"` based on "tests pass" — the same imprecise logging pattern. "Tests pass" ≠ "bug actually fixed." The log claims a business outcome (fixed) from a mechanical signal (exit code + test runner). | 🟡 | Remediation log should include: git diff summary, files changed, test output snippet, and whether the changed files match `relevant_files`. Status should distinguish: `"tests_pass"` (mechanical) vs. `"verified"` (recurrence window passed). |

---

## Section 3 — No-Catch-Point Failures

| Pattern | Why No Catch Point | Recommendation | Status |
|---------|--------------------|----------------|--------|
| **G4** (Cascading failures from fix) | The fix introduces a *new* failure type, not a recurrence. No existing mechanism correlates "fix applied at time T" with "new error pattern started at time T." Requires causal inference across fingerprints. | Track overall error rate in recurrence window, not just target fingerprint. If error rate increases post-fix, auto-revert. Requires atomic fix commits. | → spec amendment |
| **C4** (LLM-drafted remedy quality) | An LLM generates a remedy hypothesis for a novel error. No ground truth exists to validate whether the remedy is correct. The first remediation attempt IS the test — and it has write access to production code. | Conservative default: LLM-drafted remedies start as `strategy: "escalate"` until validated. Human can promote to `code-fix` after reviewing the first occurrence. Accept slower self-healing for safer startup. | → design decision |
| **G3** (Prompt injection via error content) | Error messages are user-facing content passed into an agent prompt. The content is fundamentally untrusted but structurally trusted (it's part of the evidence bundle). No static analysis can distinguish "safe error" from "adversarial error." | Sanitization + structural separation (XML-delimited untrusted blocks). Reduces risk but can't eliminate it. Acceptable given the bot processes game content, not arbitrary user input. | → accept with mitigation |

---

## Section 4 — Priority Ordering

### Before implementing:

1. **C2** — Decide: `sessions_spawn` or CLI? This affects F-AR04's entire procedure, sandboxing (G1), cost enforcement (C1), and timeout handling. (design decision, 30 min)
2. **C3** — Resolve fingerprint modification contradiction. Reword anti-pattern + F-AR05. (spec edit, 10 min)
3. **C4** — Decide: LLM-drafted remedy trust level. Affects the novel error data flow path. (design decision, 20 min)
4. **T3** — Define fingerprint registry concurrency strategy. Affects all components that touch `fingerprints.json`. (design decision, 20 min)
5. **G7** — Define `state.json` schema covering both circuit breaker and recurrence verification. (spec amendment, 15 min)
6. **G6** — Define skip-list format and wiring. (spec amendment, 10 min)

### During implementation:

7. **T1** — Shared `EvidenceBundle` type between F-AR03 and F-AR04. (bake into F-AR03 implementation)
8. **T2** — Screenshot path validation in evidence assembler. (bake into F-AR03)
9. **G2** — Serial fix agent execution queue. (bake into F-AR05)
10. **G3** — Error content sanitization in evidence bundle. (bake into F-AR03)
11. **G8** — Test file modification detection post-fix. (bake into F-AR04)
12. **G5** — Diff-vs-relevant-files check post-fix. (bake into F-AR04)

### After first live run:

13. **G4** — Overall error rate tracking in recurrence window. Needs baseline data. (add to F-AR06 after observing real remediation patterns)
14. **T4** — Fix-to-restart signal mechanism. Depends on how `loop.sh` actually behaves in practice. (design after observation)
15. **T5** — Active-time tracking for recurrence window. Needs real OpenLog event patterns. (refine F-AR06 after data)
16. **C1** — Per-attempt cost calibration. Observe real fix agent costs, then tune timeout/budget. (tune after 5+ remediations)

---

## Section 5 — Spec Amendments

### Amendment 1: Execution Path Decision (C2)

**Add to Decisions Log:**
> **D-AR01:** Fix agents are spawned via `sessions_spawn` (OpenClaw native), not raw CLI. Rationale: sessions_spawn provides timeout enforcement (`runTimeoutSeconds`), sandboxing, and integration with OpenClaw's cost tracking. The fix prompt is written to a file and passed as the `task` parameter.

**Amend F-AR04 Procedure step 3:** Replace `claude --print --permission-mode bypassPermissions -p "$(cat prompt.md)"` with:
```
sessions_spawn(
  task: contents of prompt.md,
  mode: "run",
  model: "sonnet",
  cwd: project_directory,
  runTimeoutSeconds: 300,
  label: f"remediation-{attempt_id}"
)
```

### Amendment 2: Fingerprint Modification Clarification (C3)

**Reword Anti-Pattern:** "Do NOT modify fingerprints.json during remediation" → "Fix agents MUST NOT read or write fingerprints.json. The remediation orchestrator (F-AR05) may update fingerprint metadata (count, timestamps) after the fix agent has exited and its result is recorded."

**Reword F-AR05 step 4:** "After the fix agent exits and result is logged, the orchestrator updates the fingerprint's `count` and `last_remediated` timestamp. This happens outside the fix agent's lifecycle."

### Amendment 3: Novel Error Remedy Trust (C4)

**Add to Decisions Log:**
> **D-AR02:** LLM-drafted remedies for novel errors default to `strategy: "escalate"` until a human promotes them to `code-fix` via `opentriage approve --promote-strategy code-fix <slug>`. Rationale: an untested LLM hypothesis should not have unsupervised write access to production code. First-week safety > speed.

**Amend Data Flow section:** Replace "auto-approved if severity < fatal" with "auto-approved for classification purposes, but remedy strategy defaults to `escalate` until human promotes. This means novel errors get classified and tracked automatically, but remediation requires one human approval per new fingerprint."

### Amendment 4: State File Schema (G7)

**Add to Definitions:**
> **State file** (`state.json`): Persistent remediation state at `.opentriage/state.json`.
> ```json
> {
>   "pending_verifications": [
>     {
>       "fingerprint_slug": "selector-drift-fight-btn",
>       "fixed_at_ts": "2026-04-05T10:00:00Z",
>       "attempt_id": "rem-20260405-001",
>       "commit_sha": "abc1234",
>       "recurrence_window_hours": 6,
>       "active_minutes_post_fix": 0
>     }
>   ],
>   "circuit_breakers": {
>     "selector-drift-fight-btn": {
>       "consecutive_failures": 0,
>       "suspended_until": null,
>       "last_attempt_ts": null
>     }
>   },
>   "daily_spend_usd": 0.0,
>   "daily_spend_reset_date": "2026-04-05"
> }
> ```

### Amendment 5: Skip Pattern List (G6)

**Add to F-AR05 Procedure (before budget check):**
> Step 0: Check error against skip patterns in `config.toml` `[remediation] skip_patterns`. If `f_raw` matches any pattern, skip remediation and log as `status: "skipped"`.

**Add to Definitions:**
> **Skip patterns:** Regex list in `.opentriage/config.toml` under `[remediation]`. Pre-seeded: `["antml:thinking", "antml:.*artifact"]`. Matched against the error event's `f_raw` field.

### Amendment 6: Serial Execution Constraint (G2)

**Add to F-AR05 Procedure:**
> Fix agents execute serially within a triage cycle. If multiple fingerprints match, queue them by severity (highest first) and run one at a time. Each agent must complete (success, failure, or timeout) before the next spawns. Rationale: prevents concurrent edits to the same file and simplifies rollback.

### Amendment 7: Post-Fix Verification Hardening (G5, G8, F001, F002, F020)

**Add to F-AR04 Procedure (after step 3, before writing result):**
> Verification checklist (all must pass for `status: "fixed"`):
> 1. Git diff is non-empty (agent actually changed something)
> 2. Changed files overlap with fingerprint's `relevant_files` (agent fixed the right thing)
> 3. No test files modified with reduced assertion count (F020 defense)
> 4. Total files changed ≤ 5 and lines changed ≤ 200 (surgical fix guard)
> 5. Tests ran and produced parseable pass/fail output (not just exit code 0)
>
> If any check fails: `status: "suspicious"`, log the specific failure, escalate to human.

### Amendment 8: Fingerprint Registry Concurrency (T3)

**Add to Definitions:**
> **Registry access pattern:** All reads of `fingerprints.json` during a triage cycle operate on a snapshot taken at cycle start. Writes use atomic rename (`write to .tmp → os.rename`). The triage engine holds no long-lived file handles. This prevents corruption from concurrent remediation + triage cycles.

### Amendment 9: Evidence Bundle Hardening (T2, G3)

**Amend F-AR03 Procedure step 2 (`screenshot_path`):**
> Validate screenshot path: `os.path.exists()` and `os.access(path, os.R_OK)`. If inaccessible, set to `null` with annotation `"screenshot_note": "File missing or inaccessible at assembly time"`.

**Add to F-AR03 Procedure step 2 (`error_event`):**
> Sanitize `f_raw` and `data.*` string fields: truncate to 500 chars, strip control characters (keep printable ASCII + common Unicode). Wrap in the prompt as `<untrusted_error_content>` block.

---

*Failure map complete. 8 topological, 4 contradiction, 8 coverage gap patterns identified. 9 catalog manifestations mapped. 3 no-catch-point failures flagged. 9 spec amendments derived.*
