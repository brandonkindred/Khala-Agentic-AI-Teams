# SE Review Gate Finding Inventory

## Purpose

A golden-set evaluation harness for the SE review gates needs a corpus whose
labels use a closed defect-class vocabulary. That vocabulary is only
trustworthy if it is justified against what the gates actually emit — not
against a plausible taxonomy invented for the corpus. This document is the
factual baseline that vocabulary is built from.

It catalogues the finding shapes produced today by four LLM
finding-producing gates: the code-review coordinator, the QA agent, the
security agent, and `false_positive_filter`. Every field, severity value,
and defect category below is drawn from the current model definitions,
prompts, and test fixtures — not inferred from prompts alone. No production
code changes accompany this document.

**Out of scope:** the linting gate (`linting_tool_agent`) is a fifth
finding-producing gate not inventoried here. It runs on the frontend
code-review gate via `run_microtask_review` *and* on the `run_review` path
(`shared/v2_review.py:1130-1136`, `1303-1314`) — it is deliberately not part
of the shared code-review phase impl, per
`docs/GATE_DEPENDENCY_GRAPH.md:34-36`. It emits its own structured shape, `LintIssue`
(`linting_tool_agent/models.py:12-23`: `file_path: str`, `line: int`,
`column: int`, `rule: str`, `message: str`,
`severity: Literal["error","warning","info"]`) — a second gate whose
findings are structurally matchable by file path + numeric line, which the
corpus and matching-rule work should account for alongside code review. The
`devops_team`'s `change_review_agent` also emits review findings within this
team package. It runs the code-review engine internally (`devops_maintainability`
profile) but is not a passthrough: `agent.py::_to_finding` translates each
engine `CodeReviewIssue` into a distinct `ReviewFinding` shape
(`devops_team/models.py:160-169`: `finding_id: str`, `severity:
Literal["critical","high","medium","low","minor","nit"]`, `area: str`,
`file_ref: str`, `issue: str`, `rationale: str`, `recommended_fix: str`,
`blocking: bool`, `exploitability: str`), remapping `category`→`area`,
`file_path`→`file_ref`, `description`→`issue`, `suggestion`→`recommended_fix`,
computing `blocking` from `is_blocking(severity)`, and remapping the engine's
`info` severity to `low` (`change_review_agent/agent.py:38-99`) since
`ReviewFinding`'s severity literal has no `info` member. This translated
shape is a sixth finding shape not catalogued in depth here, alongside the
lint gate above — both are out of scope for the four gates inventoried below
but should be accounted for when the corpus and matching rule are built.

## 1. Code Review Coordinator (`code_review_agent/`)

### Finding models

**`CodeReviewIssue`** — `code_review_agent/models.py:385-482` — the canonical,
persisted finding record returned in `CodeReviewOutput.issues`.

| field | type | default |
|---|---|---|
| `severity` | `str` | `"high"` |
| `category` | `str` | `"general"` |
| `file_path` | `str` | `""` |
| `line` | `Optional[int]` | `None` |
| `start_line` | `Optional[int]` | `None` |
| `title` | `str` | `""` |
| `description` | `str` | `""` |
| `suggestion` | `str` | `""` |
| `pre_existing` | `bool` | `True` |
| `omission` | `bool` | `False` |

A model validator (`_omission_implies_in_scope`, models.py:465) rejects
`omission=True` combined with `pre_existing=True`.

**`ChunkReviewIssueLLM`** — `code_review_agent/models.py:523-640` — the
strictly-typed schema the LLM must emit per chunk, coerced into
`CodeReviewIssue` downstream. Same field set as above, but:
- `severity: CodeReviewIssueSeverity` — `Literal["critical", "high", "medium", "low", "info"]`
- `category: _ChunkReviewIssueCategory` — a closed `Literal` of 13 values (below)
- `pre_existing` / `omission` are `StrictBool`

**`ArchitectureConsistencyFindingLLM`** — `code_review_agent/models.py:734-819`
(architecture-consistency pass) and **`SideEffectImpactFindingLLM`** —
`code_review_agent/models.py:822-911` (side-effect/blast-radius pass) are
**target-only schemas, not the shape actually validated at runtime today.**
Both classes' own docstrings say so, and the merged in-process pass
(`merged_architecture_side_effect_pass.py::_parse_batch_reply`, lines
211-239) confirms it: each half of the LLM reply is parsed via the
standalone passes' own `parse_findings`/`_coerce_finding` helpers, not by
Pydantic-validating against these classes. They document the *intended*
field set — the same as `CodeReviewIssue` minus `title` and `start_line`:
`severity`, `category` (restricted to 2 values each, see below), `file_path`,
`line`, `description`, `suggestion`, `pre_existing` (`StrictBool`), `omission`
(`StrictBool`, with the same `_omission_implies_in_scope` invariant as
`CodeReviewIssue`) — and each pass's own `_coerce_finding` does propagate
`description`, `suggestion`, `pre_existing`, and `omission` onto the
resulting `CodeReviewIssue`, so no field is lost in practice. But the
*active* coercion contract is looser than the `StrictBool` schema implies:
`_coerce_finding` routes `pre_existing`/`omission` through
`chunking._coerce_scope_tags` → `_coerce_bool` (chunking.py:453-475), which
accepts only the bool `True` or a recognized truthy *string* token
(`"true"`/`"yes"`/`"1"`, case-insensitive) and silently returns `False` for
everything else — **including any number**, so a raw `"pre_existing": 1`
becomes `False` (flipping the finding to in-scope) rather than being
accepted or rejected. That path also repairs a conflicting
`omission`+`pre_existing` pairing (omission wins, `pre_existing` forced
`False`) instead of raising, where `ChunkReviewIssueLLM`'s `StrictBool`
fields would reject a non-bool token outright and drive a corrective retry.
A corpus schema should treat these two classes as documentation of
the target field set, not proof that malformed booleans are rejected at this
boundary today.

**`CodeReviewOutput`** — `code_review_agent/models.py:1106-1134` — top-level
output: `approved: bool`, `issues: List[CodeReviewIssue]`,
`not_reviewed_ranges: List[str]`, `summary: str`, `spec_compliance_notes: str`.

### Severity values

`Literal["critical", "high", "medium", "low", "info"]`
(`CodeReviewIssueSeverity`, models.py:489; mirrored by
`_VALID_SEVERITIES` in `chunking.py:58`). `critical` and `high` are the only
severities that can force `approved=False`, but severity alone is not
sufficient: `ChunkReviewLLMResponse._require_approval_consistent_with_issues`
(models.py:711-726) keys on an **actionable** critical/high issue — one whose
`description` is non-blank *and* whose `suggestion` is not a no-op phrasing
(`is_no_op_suggestion`). A critical finding with a blank description or a
"No changes needed." suggestion does not block. The check binds in both
directions: `approved=True` is rejected when an actionable critical/high
issue is present, and `approved=False` is rejected when none is.

### Defect categories actually named

Closed 13-value enum (`_ChunkReviewIssueCategory`, models.py:506-520;
mirrored by `_VALID_CATEGORIES`, chunking.py:62-78):

`naming`, `structure`, `logic`, `spec-compliance`, `standards`,
`integration`, `testing`, `architecture`, `refactor`, `maintainability`,
`side-effects`, `documentation`, `general`.

The architecture-consistency pass is restricted to `architecture` (a stated
architecture boundary/pattern/decision the change contradicts) or `refactor`
(a capability re-implemented that already exists elsewhere) —
`prompts.py:154-167`. The side-effect pass is restricted to `side-effects`
(a real caller-breaking side effect) or `documentation` (a docstring/comment
that no longer matches the implementation) — `models.py:850-853`.

The coordinator's own review prompt is not a literal in `prompts.py` — it is
built by profile (`CODE_REVIEW_PROMPT = build_review_system_prompt(
ReviewProfile.CODE_REVIEW)`, `prompts.py:21`), so its defect checklist lives
in `code_review_agent/profiles.py`, not in the list quoted below.

For contrast, the defect claims enumerated at `prompts.py:42-47` belong to
`FALSE_POSITIVE_VERIFY_BODY` (`prompts.py:24-55`), not to the code-review
prompt, and their polarity is **inverted**: they are the verifier's list of
claims that are *commonly false positives* once the whole codebase is
visible — "X is undefined / never defined / not imported / not registered",
"no tests for X", "missing error handling / validation / null check",
"duplicate / unused / dead code", "file/module Y must be created / does not
exist", "this relative import is unclear / unresolved". They describe what
`false_positive_filter` is primed to *drop*, and must not be read as defect
kinds the code-review gate names (see §4).

### Location fields

`CodeReviewIssue` / `ChunkReviewIssueLLM`: `file_path: str` (always present,
may be blank for file-wide findings) + `line: Optional[int]` +
`start_line: Optional[int]` (set only for multi-line spans; `line` then acts
as the end line). The architecture/side-effect passes have `file_path` +
`line` only — no `start_line`. All numeric line fields are optional; `None`
denotes a structural/file-wide finding with no single anchor line.

### Example findings (from tests)

- `{"severity": "critical", "category": "logic", "file_path": "app/main.py", "description": "SQL injection risk", "suggestion": "Use parameterized queries"}` — `tests/test_code_review_coordinator.py:511-518`
- `{"severity": "high", "category": "logic", "file_path": "app/main.py", "line": 10, "description": "duplicate string literal", "suggestion": "extract a constant"}` — `tests/test_code_review_coordinator.py:557-564`

## 2. QA Agent (`qa_agent/`)

### Finding model

**`BugReport`** — `qa_agent/models.py:10-37` — the gate's only finding shape.
It is requested in the default review, `fix_build`, and `write_tests` modes,
but **not** in `acceptance_evidence` mode: that mode's prompt is built from
`ACCEPTANCE_EVIDENCE_FIELD_NAMES` (`qa_agent/models.py:151-157` — `approved`,
`quality_gates`, `acceptance_trace`, `validation_evidence`, `summary`) via
`AcceptanceEvidenceModel` (`agent.py:368-388`), which omits `bugs_found`
entirely. So an `acceptance_evidence` run produces evidence records, not
findings — a corpus case cannot expect findings from that mode.

| field | type | default |
|---|---|---|
| `severity` | `str` | required, no default |
| `description` | `str` | required |
| `location` | `str` | `""` |
| `file_path` | `str` | `""` |
| `line_or_section` | `str` | `""` |
| `steps_to_reproduce` | `str` | `""` |
| `expected_vs_actual` | `str` | `""` |
| `recommendation` | `str` | `""` |

A model validator (`_collapse_location`, models.py:30-37) auto-populates
`location` from `file_path`/`line_or_section` when `location` is blank.

`QAOutput` (models.py:81-149) carries `bugs_found: List[BugReport]` plus
mode-specific fields (`quality_gates`, `acceptance_trace`,
`validation_evidence`) that are populated only in `acceptance_evidence` mode
and are not finding shapes.

### Severity values

Plain `str`, **not enum-enforced**. Documented set (comment at models.py:13,
prompt at prompts.py:71): `critical, high, medium, low`. A test fixture uses
`"info"` (`tests/test_qa_agent.py:53`), confirming there is no runtime
validation of this set — any string is accepted.

### Defect categories actually named

**No `category`/taxonomy field exists on `BugReport` at all.** The QA prompt
names bug *patterns* in prose only (`qa_agent/prompts.py:39-48`): off-by-one
errors, race conditions, resource leaks, null/None dereferencing, integer
overflow/type coercion, SQL injection via string formatting, unvalidated
external input, missing I/O error handling, inconsistent state after partial
failure. In `fix_build` mode, root causes named are: missing import, wrong
path, type error, syntax error (`prompts.py:100`).

### Location fields — gap flagged

`BugReport` has **no numeric line field**. Location is carried by up to
three overlapping string fields:
- `location: str` — free text ("file path, function name, or line
  reference"), the field populated directly in default/general-review mode.
- `file_path: str` — populated only in `fix_build` mode.
- `line_or_section: str` — a **string**, not an int; may hold a line number
  as text (`"42"`) or a function name (`"def health"`), also `fix_build`-mode
  only.

**Constraint on matching:** outside `fix_build` mode, QA findings carry only
the free-text `location` string with no structured file path or numeric
line — they cannot be matched by file+line without parsing free text, and
even `fix_build` mode's `line_or_section` may not be a line number at all.

### Example findings (from tests)

- `BugReport(severity="high", description="missing import", file_path="app/main.py", line_or_section="42")` → `location` collapses to `"app/main.py:42"` — `tests/test_qa_agent.py:23-29`
- `{"severity": "critical", "description": "NPE in /auth"}` (no location at all) — `tests/test_qa_agent_cache.py:162`

## 3. Security Agent (`security_agent/`)

### Finding model

**`SecurityVulnerability`** — `security_agent/models.py:10-17`.

| field | type | default |
|---|---|---|
| `severity` | `str` | required, no default |
| `category` | `str` | required, no default |
| `description` | `str` | required |
| `location` | `str` | `""` |
| `recommendation` | `str` | `""` |

`SecurityLLMResponse` (models.py:49-79) is the schema actually validated
against the LLM reply: `vulnerabilities: List[SecurityVulnerability]`,
`summary: str`, `remediations: List[dict]` (all required). It has no
`approved` field — `CybersecurityExpertAgent.run` always re-derives
`approved` via `derive_approved` (`security_agent/agent.py:227`).

### Severity values

Plain `str`, not enum-enforced. Documented set (models.py:13,
prompts.py:63): `critical, high, medium, low, info`. Blocking rule is shared
platform-wide: `BLOCKING_SEVERITIES = frozenset({"critical", "high"})`
(`shared/security_service.py:42`), applied case-insensitively via
`is_blocking`/`any_blocking`/`derive_approved` (security_service.py:136-208).

### Defect categories actually named

`category` is a **free-text string, not a closed enum** — the prompt gives
only examples: `"category": string (e.g. injection, xss, auth, crypto)"`
(prompts.py:64). No CWE or OWASP-ID field exists.

The prompt names defect kinds in two disjoint places. Its **"Your
expertise"** block (`security_agent/prompts.py:17-22`) lists the vulnerability
classes: OWASP Top 10, injection (SQL/NoSQL/command), XSS, CSRF,
authentication/authorization flaws, cryptographic issues (weak algorithms,
hardcoded secrets), insecure deserialization, and SSRF. Its separate
**"Methodology"** block (`prompts.py:29-35`) instead enumerates attack
surfaces to walk, not defect classes: entry points (HTTP/WebSocket/CLI/file
upload/env var), data flow and injection points, authentication boundaries,
authorization gaps, secrets management, and dependency CVEs.

### Location fields — gap flagged

`SecurityVulnerability` has **only `location: str = ""`** — free text ("file
path, function name, or line reference", prompts.py:66). **No structured
`file_path` field and no numeric line/line-range field exist at all.**

**Constraint on matching:** security findings cannot be matched by file+line
without parsing free text, and that text is not guaranteed to contain a
parseable line number.

### Example findings (from tests)

- `{"severity": "critical", "category": "injection", "description": "Command injection in run()", "location": "run:3", "recommendation": "Use subprocess with shell=False"}` — `tests/test_security_agent.py:66-73`
- `{"severity": "low", "category": "style", "description": "nitpick", "recommendation": "rename var"}` (location omitted entirely) — `tests/test_security_agent.py:74-79`

## 4. `false_positive_filter` (`code_review_agent/false_positive_filter.py`)

### Behavior: removal-only, never a transform

`false_positive_filter` does not define its own finding shape — it filters
`CodeReviewIssue` records (the code-review coordinator's shape, above) and
never relabels or modifies a surviving finding. It returns the input list
minus zero or more entries, in original order
(`filter_false_positives`, false_positive_filter.py:2214-2232;
`_verify_and_filter`, 2253-2422).

Internally it uses a `_Verdict` dataclass (false_positive_filter.py:1721-1741,
`is_false_positive: bool`, `confidence: str` (`high`/`medium`/`low`),
`reasoning: str`) that is **never persisted to the output** — it exists only
to drive the drop decision.

### Decision rule (allowlist, not denylist)

A finding is dropped only when all of the following hold:
- the verifier agent returns `is_real_issue: false` for that finding, and
- confidence is `high` or `medium` (never `low`) — enforced both in
  `_Verdict.__post_init__` (raises on `is_false_positive=True` with `low`
  confidence) and in verdict coercion:
  `is_false_positive = is_real is False and confidence in ("high", "medium")`
  (false_positive_filter.py:1766), and
- the verifier agent is confirmed to have performed a **successful full
  `read_file`** of the cited file before that verdict is honored
  (`_agent_read_the_cited_file`, false_positive_filter.py:1932ff); otherwise
  any false-positive verdicts from that batch are discarded and a warning is
  logged, keeping the findings (false_positive_filter.py:2175-2187).

Any ambiguity — no file path, an unresolved path, an unparsable verdict, a
verifier error/timeout, or an ungrounded read — keeps the finding. This is a
deliberate fail-safe design (module docstring, false_positive_filter.py:16-47).

The filter is severity-agnostic: it never inspects `severity` to decide
drop/keep (the only use of `severity` in the removal path is in a log line,
false_positive_filter.py:2405-2412).

### What it emits when it drops a finding

**No first-class suppressed-finding output.** A dropped finding disappears
entirely from the returned `List[CodeReviewIssue]` — there is no
suppressed-record object, no separate "dropped findings" output field, and
no marker left on any surviving finding. The immediate trace at the drop
site is a `logger.info` call (false_positive_filter.py:2405-2412) recording
severity, `file:line`, a truncated description, and truncated reasoning,
plus a summary count log line.

Separately, when a job is running with a bound `job_id` and Postgres
enabled, `_verify_group` also calls `record_reasoning_transcript_turns` and
`record_formatting_transcript_turns` (false_positive_filter.py:2154-2174),
which persist the verifier's LLM turns — including the formatted JSON
verdict per finding index (`is_real_issue`, `confidence`, `reasoning`) — as
durable `false_positive_filter`-stage transcript entries
(`code_review_agent/transcript.py:325`). This is a durable but unstructured
record (raw prompt/response text keyed by stage and file, not a queryable
per-finding suppression record) and is a no-op when no `job_id` is bound
(most tests and any caller that never opened an `llm_attribution` block) or
Postgres is unavailable — it should not be conflated with a first-class
suppressed-finding output, but it may be a usable source for the evaluation
harness. The filter can be disabled entirely via the
`CODE_REVIEW_FALSE_POSITIVE_FILTER` environment variable (a default-on
toggle read through `shared.env.env_flag_enabled`, which treats
`false`/`0`/`no`/`off` — case-insensitive, whitespace-tolerant — as disabled
and any other value, blank, or unset as enabled)
(`_FILTER_ENV`, false_positive_filter.py:99-102), in which case the input
list is returned unchanged.

### Example finding it evaluates (from tests)

```python
CodeReviewIssue(
    severity="high",
    category="logic",
    file_path="app/main.py",
    line=1,
    description="foo is never defined",
    suggestion="define foo",
)
```
— `tests/test_false_positive_filter.py:54-71`

## 5. Cross-gate comparison

| Gate | Finding model | Severity typing | Category typing | Numeric line? | Structured `file_path`? |
|---|---|---|---|---|---|
| Code review | `CodeReviewIssue` / `ChunkReviewIssueLLM` | Closed `Literal` (5 values) at the LLM boundary; plain `str` on the persisted record | Closed 13-value enum at the LLM boundary (2-value subsets for the architecture/side-effect passes); plain `str` defaulting to `"general"` on the persisted record | Yes — `line` + `start_line` | Yes — always present, may be blank |
| QA | `BugReport` | Plain `str`, unenforced (test fixture uses `"info"`, outside the documented set) | **None** — no category field | No (`fix_build` mode adds only a structured `file_path`, not a numeric line field — `line_or_section` stays a string that may hold non-numeric text in every mode) | Only in `fix_build` mode |
| Security | `SecurityVulnerability` | Plain `str`, unenforced | Free-text `str`, not a closed set | No — no line field of any kind | No — `location` is a single free-text field |
| `false_positive_filter` | N/A — filters `CodeReviewIssue`; never emits its own shape | N/A (severity-agnostic) | N/A | N/A | N/A |

Among the four gates inventoried here, code review is the only one whose
findings are structurally matchable by file path + numeric line without
free-text parsing (the out-of-scope linting gate's `LintIssue` is a second
such gate — see the Purpose section). QA and security both depend on a
free-text location string, and QA has no defect-category field at all —
both are direct constraints on what a label schema and a file+line matching
rule can cover for those two gates.
