# SE Review Gate Corpus False-Positive-Resistance Expression and Worked Examples

## Purpose

Step 4 of 4 for the gate-evaluation corpus and finding-label specification
(#7586, closing issue #7677), closing out the design work the evaluation
harness's runner and corpus stories (#7578) implement against. This
document specifies the two gaps the prior three steps deliberately left
open, and closes them without changing the case format, the label schema,
or the matching rule:

- [`GATE_FINDING_INVENTORY.md`](GATE_FINDING_INVENTORY.md) — the factual
  catalogue of what the code-review, QA, security, and
  `false_positive_filter` gates actually emit.
- [`CORPUS_CASE_FORMAT.md`](CORPUS_CASE_FORMAT.md) — the case format and
  finding-label schema, including the `polarity: must_find | must_not_find`
  field this document builds directly on. Its own Purpose section states
  its scope stops at "elaborating false-positive-resistance patterns beyond
  the `must_not_find` polarity defined below" — that elaboration is this
  document.
- [`GATE_FINDING_MATCHING_RULE.md`](GATE_FINDING_MATCHING_RULE.md) — the
  finding-to-label match test (§4), which it states is "polarity-symmetric"
  but explicitly defers "the false-positive-resistance expression and its
  worked examples" to "a later specification" — again, this document.

This document specifies, in order: how a case expresses false-positive
resistance as a positive declaration rather than an absence (§1); how a
case scopes that declaration to a region, up to and including its whole
diff, and how that is distinguished from a case carrying no labels at all
(§2); precisely how the §4 match test in `GATE_FINDING_MATCHING_RULE.md`
treats a finding landing inside a must-not-find region (§3); and two new
worked example cases — one `must_find`, one exercising `must_not_find` —
each demonstrated against hypothetical gate output rather than merely
asserted (§4).

**Out of scope for this document:** building the corpus itself (a later
story); implementing the runner or its metrics; CI wiring; any change to
gate prompts, models, or logic; any change to the case format, label
schema, or matching rule already specified in the three documents above.
No production code accompanies this document.

## 1. False-positive resistance is a positive declaration, never an absence

A gate listed in `gates` with zero corresponding labels is not the same
thing as a gate never listed in `gates` at all, and this section is about
the former. `CORPUS_CASE_FORMAT.md` §1 already rules on exactly that case,
and its ruling stands unchanged here: such a gate is "expected to report
nothing on this fixture (a clean-fixture / false-positive-resistance case
for that gate), not that the gate is unscored." That intent is real, and
this document does not dispute or override it.

What this document adds is a second, complementary mechanism for the same
underlying goal, needed precisely where the whole-gate shortcut above can't
reach: `GATE_FINDING_MATCHING_RULE.md` §4's match test only ever runs
against an actual label, comparing a finding's resolved gate, location, and
class to a label's — it has no way to test a finding against a gate-wide
"expect nothing" intent that isn't written down as a label, because nothing
in `GATE_FINDING_MATCHING_RULE.md` specifies how a runner would operationalize
that whole-gate intent without one. Operationalizing it is out of this
document's scope — that would be a new, gate-wide matching mechanism, not
"elaborating the `must_not_find` label already defined" (this document's
stated scope). So a case that wants a checkable, per-region or per-class
guarantee today — or one that wants `must_not_find` resistance in a region
of a gate that also carries `must_find` labels elsewhere, where the
zero-labels shortcut isn't available at all — needs the label-based
mechanism below regardless of what `CORPUS_CASE_FORMAT.md` §1 already says
about the zero-label case.

False-positive resistance for a gate over a region is therefore expressed
**only** by one or more `must_not_find` labels naming that region, reusing
the fields `CORPUS_CASE_FORMAT.md` §2 already defines and requires no
schema change to use:

```yaml
label_id: L2
gate: security
defect_class: injection        # required — the class a false positive here would be filed under
polarity: must_not_find        # severity is omitted entirely, not null
file_path: app/services/backup_service.py
line: 13                       # or null — see §2
line_end: 13
description: >
  Why this region is a deliberate decoy, not a fixture bug.
```

Two schema details already carry the weight of this requirement, restated
here because they are precisely what makes the polarity positive rather
than a negative inference:

- `defect_class` is **required** on a `must_not_find` label, exactly as on
  `must_find` — the label names the specific class a false positive in this
  region would be filed under (`CORPUS_CASE_FORMAT.md` §2). A
  `must_not_find` label with no `defect_class` would not be expressible
  under the schema at all; there is no way to write "nothing of any kind
  should be found here" as a single label. §2 below addresses what a case
  does when it wants that broader guarantee.
- `severity` is **omitted**, not merely optional or `null`, because there is
  no finding to grade. This is a structural signal, readable straight off
  the label without consulting `polarity`: a label carrying `severity` is a
  `must_find`; a label without it is a `must_not_find`. (`polarity` is still
  the field the matching rule actually branches on — this is a reading aid
  for a human labeler, not a substitute for checking `polarity`.)

## 2. Region scope: file-wide, bounded, and whole-diff

A `must_not_find` label's region is stated exactly the way a `must_find`
label's location is (`CORPUS_CASE_FORMAT.md` §2, `file_path` + `line` +
`line_end`):

- **Bounded region**: `line` and `line_end` set an inclusive line range
  within `file_path` — the decoy is confined to those lines.
- **File-wide region**: `line: null` and `line_end: null` — the guarantee
  covers the entire file, mirroring the same file-wide semantics
  `CORPUS_CASE_FORMAT.md` §2 already documents for a `must_find` label's
  structural finding, and exactly the branch `GATE_FINDING_MATCHING_RULE.md`
  §4 already handles by comparing only `file_path`.

Neither region shape is new — both already exist in the schema. What is new
here is the **whole-diff** shape, which must be distinguishable in
particular from a gate that's listed in `gates` but carries zero labels —
the case §1 already covers, where `CORPUS_CASE_FORMAT.md` §1's intent
stands but nothing enforces it. There is no dedicated schema field for "the
entire diff" — no
wildcard `file_path`, no case-level flag — because a label is always
anchored to one file, and introducing one would be a schema change this
document is out of scope to make. Instead, whole-diff resistance is a
**usage convention**, built from the existing fields:

> To assert that a gate must not report a given defect class anywhere in a
> case's diff, add one file-wide (`line: null`, `line_end: null`)
> `must_not_find` label with that `defect_class`, for every file path the
> diff touches.

This is sufficient to make that distinction concrete and checkable — and,
laid out against the two other shapes a case's relationship to a gate can
take, it clarifies rather than disputes what `CORPUS_CASE_FORMAT.md` §1
already rules on:

| | `expected_findings` for the gate | What is asserted |
|---|---|---|
| Gate not listed in `gates` | N/A — the gate isn't named by the case at all | Nothing; genuinely out of scope. `gates` is the case's own declaration of which gates it scores (`CORPUS_CASE_FORMAT.md` §1), so a gate absent from it is not scored, full stop — no tension with §1 here. |
| Gate listed in `gates`, zero labels | `[]` for that gate | Per `CORPUS_CASE_FORMAT.md` §1: the gate **is** scored, and is expected to report nothing — "not that the gate is unscored." That ruling stands. But no label exists for §4's match test to check a finding against, so nothing specified in `GATE_FINDING_MATCHING_RULE.md` enforces it: a hallucination here is invisible to the label-based mechanism this document specifies, even though the intent says it shouldn't be there. |
| Whole-diff `must_not_find` resistance | One file-wide `must_not_find` label per file in the diff, per guarded class | Positively and checkably asserts, per class: no finding of that class exists anywhere in each named file — enforced by the existing §4 match test today, unlike the row above. |

The middle and bottom rows are the pair this document is actually about:
same underlying intent (this gate shouldn't report certain things here),
but only the bottom row is something the matching rule as specified can
check. The three rows are also structurally distinguishable YAML for a given
gate — a gate absent from `gates`, no labels for that gate in
`expected_findings` (the flat, per-label `gate` field means this is a
per-gate condition, not a whole-document `expected_findings: []`, in a
multi-gate case), or N `must_not_find` labels for it — so a labeler, a
schema validator, and the matching rule all see which case they're looking
at directly; nothing has to be inferred from what's missing.

**What this convention deliberately does not claim.** It is per-class, not
"this gate finds nothing of any kind." A case asserting whole-diff
resistance to every one of the 31 vocabulary classes across every file
would need `31 × (file count)` labels — technically expressible, never
practical, and not what a real decoy case needs. A decoy is written because
some specific pattern in the fixture plausibly trips a specific class of
false positive (as in the worked `CASE-0003` below); the case only needs
`must_not_find` labels for the classes actually at risk. This is a scope
choice named on purpose, the same way `CORPUS_CASE_FORMAT.md` §4 names
inventory content it deliberately left out of the vocabulary rather than
silently omitting it.

## 3. How the matching rule treats a finding inside a must-not-find region

`GATE_FINDING_MATCHING_RULE.md` §4 already states the governing rule in one
sentence: "a `must_not_find` label is **violated** when some finding
matches it under this test." That test is the same three-part test used for
`must_find` — gate identity, location, defect class (§4, points 1–3) — run
unchanged; nothing about `must_not_find` relaxes or extends any of the
three conditions. This document adds the two consequences that follow from
running that same test against a `must_not_find` label, neither of which
the matching-rule document draws out:

- **For `code_review` and `security` labels, a finding in the region but of
  a *different* class does not violate the label.** Point 3 of the match
  test requires `F`'s resolved class to equal `L.defect_class` exactly.
  A `must_not_find` label with `defect_class: injection` is violated by an
  `injection`-tagged finding landing in its region and by nothing else — a
  finding in the same region tagged `auth` or `logic` fails point 3 and
  simply does not compete for that label. It may or may not be a real,
  useful finding; this document takes no position on that — it is just not
  what this label is guarding against. A case wanting resistance to more
  than one class in the same region states that with more than one label,
  one per class (the same one-fact-per-label design `CORPUS_CASE_FORMAT.md`
  §2 already uses for `must_find`, e.g. its `CASE-0002` one-label-per-gate
  example).
- **For `qa` labels, this scoping does not exist — any QA finding in the
  region violates the label.** `GATE_FINDING_MATCHING_RULE.md` §3.3 states
  QA's defect-class check is skipped entirely at point 3, for every QA
  label regardless of polarity, because `BugReport` has no category field
  to check at all. A `qa`-gated `must_not_find` label is therefore
  unavoidably "no QA finding of *any* kind should land here" — not because
  the schema or this document chooses that scope, but because QA gives the
  matching rule nothing to narrow it with. A corpus author writing a QA
  decoy case should read the label that way: one `qa` `must_not_find` label
  already covers every QA bug pattern in its region, where the equivalent
  `code_review`/`security` guarantee would need one label per class.

Point 2 (location) applies exactly as `GATE_FINDING_MATCHING_RULE.md` §4
already specifies for either polarity — the ±3-line tolerance for a bounded
region, file-only comparison for a file-wide one, and the same asymmetry
that a finding carrying only a file (no resolved line) never passes the
match test against a bounded label — so it neither satisfies a `must_find`
bounded label nor violates a `must_not_find` one. A `must_not_find` label
gets no different, more forgiving, or stricter tolerance than a `must_find`
label at the same location; the polarity only changes what a passing match
*means* (satisfied vs. violated), never what counts as passing.

§6's deterministic many-to-many assignment does not apply here. §6 exists to
decide, for `must_find`, which single finding among several candidates gets
*credited* to which label. A `must_not_find` label has no such contest to
resolve: it is violated the moment at least one finding passes the match
test against it, however many findings pass. Whether one hallucinated
finding or five land in a guarded region, the label is violated once — §6's
one-finding-per-label bookkeeping is a `must_find`-only concern.

## 4. Worked examples

Both examples below are new cases (`CASE-0003`, `CASE-0004`), continuing the
sequential case-ID numbering `CORPUS_CASE_FORMAT.md` §5 started with
`CASE-0001`/`CASE-0002` — this document does not repeat or renumber those.
Each is walked through against hypothetical gate output the same way
`GATE_FINDING_MATCHING_RULE.md` §8 walks through its two cases, so the
scoring follows from applying the rule as written rather than being merely
asserted.

### 4.1 `CASE-0003` — one `must_find` and one `must_not_find` in the same case

A `security`-gate case: a real command-injection defect, and an f-string
decoy in the same file that looks similar to a pattern-matching heuristic
but builds a log message, not a command.

```yaml
case_id: CASE-0003
title: Command injection via shell interpolation, with an f-string log-line decoy
language: python
stack: fastapi
gates: [security]
mode: diff
expected_findings:
  - label_id: L1
    gate: security
    defect_class: injection
    severity: critical
    polarity: must_find
    file_path: app/services/backup_service.py
    line: 8
    line_end: 9
    description: >
      target_dir is interpolated unsanitized into a shell command string,
      then executed via subprocess.run(..., shell=True); command injection.

  - label_id: L2
    gate: security
    defect_class: injection
    polarity: must_not_find
    file_path: app/services/backup_service.py
    line: 13
    line_end: 13
    description: >
      Interpolates the same untrusted target_dir, but only into a log
      message passed to logger.info — never executed or used to build a
      command. A heuristic keying on "f-string near external input" would
      misflag this line as injection; it is not.
```

`diff.patch` for `CASE-0003`:

```diff
--- /dev/null
+++ b/app/services/backup_service.py
@@ -0,0 +1,13 @@
+import logging
+import subprocess
+
+logger = logging.getLogger(__name__)
+
+
+def run_backup(target_dir: str) -> None:
+    cmd = f"tar -czf /backups/out.tar.gz {target_dir}"
+    subprocess.run(cmd, shell=True)
+
+
+def log_backup_start(target_dir: str) -> None:
+    logger.info(f"Starting backup of {target_dir}")
```

Line 8 is the added `cmd = f"tar -czf ...` line, line 9 the `subprocess.run`
call it feeds — L1's span. Line 13 is the added `logger.info(f"Starting
backup of {target_dir}")` line — L2's region.

**Walkthrough A — a well-behaved security gate:**

```json
[{"category": "injection",
  "location": "app/services/backup_service.py:9",
  "description": "Unsanitized target_dir passed to subprocess.run with shell=True enables command injection."}]
```

- Location (`GATE_FINDING_MATCHING_RULE.md` §2.3): the leftmost-search regex
  matches the whole string, extension `py`, digit group `9` → resolves to
  `("app/services/backup_service.py", 9, 9)`.
- Defect class (§3.2): `"injection"` is an exact match on the Group B token
  `injection`.
- Against `L1` (§4): gate matches; `L1`'s unexpanded range is `[8, 9]`,
  tolerance-expanded to `[5, 12]`; the finding's `[9, 9]` overlaps it; class
  matches. **L1 is satisfied.**
- Against `L2`: `L2`'s tolerance-expanded range is `[10, 16]`; the finding's
  `[9, 9]` does not overlap it (`9 < 10`). No candidacy — this finding was
  never eligible to violate `L2` in the first place.
- No finding was emitted anywhere near line 13. **L2 is not violated.** The
  case scores cleanly: the real defect is caught, the decoy is not flagged.
- `L1`'s expanded range (`[5, 12]`) and `L2`'s (`[10, 16]`) overlap on lines
  10–12. That is not a defect in this case — a hallucinated `injection`
  finding anywhere in that shared span would satisfy `L1` *and* violate `L2`
  at once, which is exactly what the rule as stated in §3 predicts: nothing
  narrows a finding's candidacy to only its "nearest" label. This walkthrough
  doesn't exercise that zone (the finding lands at line 9), but a corpus
  author should expect it, not be surprised by it.

**Walkthrough B — the same gate additionally hallucinates on the decoy:**

Add a second finding to the same output:

```json
{"category": "injection",
 "location": "app/services/backup_service.py:13",
 "description": "target_dir interpolated into a string without sanitization."}
```

- Location: resolves to `("app/services/backup_service.py", 13, 13)` by the
  same §2.3 regex. Defect class: `"injection"` → `injection`.
- Against `L2` (§3 of this document, first bullet): gate matches; `L2`'s
  tolerance-expanded range is `[10, 16]`; `[13, 13]` overlaps it; class
  `injection` equals `L2.defect_class`. This finding **matches** `L2`, and
  per `GATE_FINDING_MATCHING_RULE.md` §4's polarity-symmetric statement, a
  `must_not_find` label matched by a finding is **violated**. **L2 is
  violated** — correctly scored as a false positive.
- This finding is not a candidate against `L1` at all — `[13, 13]` falls
  outside `L1`'s expanded range `[5, 12]` — so it neither helps nor
  interferes with `L1`'s own (still-satisfied) status.

**Variant — the same hallucinated finding, differently classed:** suppose
the gate instead emitted `category: "auth"` at the same location
(`app/services/backup_service.py:13`). Location still resolves inside
`L2`'s region, but §3.2 defect-class resolution gives `auth`, which is not
`L2.defect_class` (`injection`). Point 3 of the match test fails, so this
finding does not match `L2` — **L2 is not violated** — demonstrating §3's
first bullet directly: a `must_not_find` label guards its one named class,
not the region against every possible finding.

### 4.2 `CASE-0004` — whole-diff resistance via one file-wide label per file

A `code_review`-gate case: a purely mechanical, behavior-preserving
extract-helper refactor across two files, with no real defects. It declares
whole-diff resistance (per §2's convention) to the two classes such a
refactor most plausibly trips: `naming` and `logic`.

```yaml
case_id: CASE-0004
title: Pure extract-helper refactor across two files, no behavior change
language: python
stack: fastapi
gates: [code_review]
mode: diff
expected_findings:
  - label_id: L1
    gate: code_review
    defect_class: naming
    polarity: must_not_find
    file_path: app/utils/formatting.py
    line: null
    line_end: null
    description: >
      Extracted helper _format_amount reuses the parent function's own
      parameter names and formatting expression verbatim.

  - label_id: L2
    gate: code_review
    defect_class: logic
    polarity: must_not_find
    file_path: app/utils/formatting.py
    line: null
    line_end: null
    description: >
      The extraction is behavior-preserving — same expression, same return
      value.

  - label_id: L3
    gate: code_review
    defect_class: naming
    polarity: must_not_find
    file_path: app/utils/validation.py
    line: null
    line_end: null
    description: >
      Extracted helpers _has_at_symbol / _has_domain_dot name exactly what
      each one checks.

  - label_id: L4
    gate: code_review
    defect_class: logic
    polarity: must_not_find
    file_path: app/utils/validation.py
    line: null
    line_end: null
    description: >
      Splitting the boolean expression into two named helpers preserves the
      original short-circuit evaluation and return value.
```

`diff.patch` for `CASE-0004`:

```diff
--- a/app/utils/formatting.py
+++ b/app/utils/formatting.py
@@ -10,1 +10,5 @@ def format_currency(amount: float, currency: str = "USD") -> str:
-    return f"{currency} {amount:,.2f}"
+    return _format_amount(amount, currency)
+
+
+def _format_amount(amount: float, currency: str) -> str:
+    return f"{currency} {amount:,.2f}"
--- a/app/utils/validation.py
+++ b/app/utils/validation.py
@@ -5,1 +5,9 @@ def is_valid_email(value: str) -> bool:
-    return "@" in value and "." in value.split("@")[-1]
+    return _has_at_symbol(value) and _has_domain_dot(value)
+
+
+def _has_at_symbol(value: str) -> bool:
+    return "@" in value
+
+
+def _has_domain_dot(value: str) -> bool:
+    return "." in value.split("@")[-1]
```

**Walkthrough A — a clean code-review run:** the gate emits no findings at
all for this diff. The match test (§4 of `GATE_FINDING_MATCHING_RULE.md`)
requires *some* finding to match a label before that label can be violated;
with zero findings, there is nothing to test against any of `L1`–`L4`. All
four remain unviolated. **The case passes.**

**Walkthrough B — the gate hallucinates a naming finding:**

```json
{"category": "naming",
 "file_path": "app/utils/formatting.py",
 "line": 14,
 "description": "Helper name _format_amount is redundant with the parent function name."}
```

- Location (§2.1): already structured, resolves directly to
  `("app/utils/formatting.py", 14, 14)`.
- Defect class (§3.1): `"naming"` maps 1:1 to the vocabulary token `naming`.
- Against `L1` (file-wide, `line: null`): per §4's file-wide branch, only
  `F.file_path == L.file_path` is required for the location leg —
  `app/utils/formatting.py` matches on both sides regardless of `F`'s line.
  Class `naming` equals `L1.defect_class`. **L1 is violated** — a false
  positive, correctly caught precisely because a positive label existed to
  catch it against.
- Against `L2` (`logic`, same file): class `naming` does not equal
  `L2.defect_class` (`logic`); point 3 fails. **L2 is not violated** — this
  finding only ever competed for the one label whose class it actually
  named.
- `L3`/`L4` (`app/utils/validation.py`) never entered the location leg at
  all — different `file_path`. Unaffected.

**Contrast with no labels at all:** had this case instead shipped with
`expected_findings: []` for `code_review`, `CORPUS_CASE_FORMAT.md` §1 would
still call that a clean-fixture expectation for the gate — but per §1 of
this document, there would be no label for the exact same hallucinated
`naming` finding on `app/utils/formatting.py` to violate, so the metric
would show a clean run, not a false positive, on the identical gate output.
This is the concrete form of the distinction §2 draws between an unlabeled
case and one declaring whole-diff resistance: the four `must_not_find`
labels are what make this specific hallucination checkable by the matching
rule; their absence leaves the same underlying intent unenforced, not
disproven.
