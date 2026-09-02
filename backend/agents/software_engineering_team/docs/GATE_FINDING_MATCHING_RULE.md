# SE Review Gate Finding-to-Label Matching Rule

## Purpose

A gate finding and a label agree when they name the same defect, but a
model's wording will never match a label's wording exactly. The matching
rule must therefore key on something stable rather than on prose — and it
must be precise enough that the runner implements it without re-litigating
what counts as a match. This document specifies that rule, building directly
on:

- [`GATE_FINDING_INVENTORY.md`](GATE_FINDING_INVENTORY.md) — the factual
  catalogue of what the code-review, QA, and security gates and
  `false_positive_filter` actually emit.
- [`CORPUS_CASE_FORMAT.md`](CORPUS_CASE_FORMAT.md) — the case format, the
  finding-label schema, and the 31-value closed defect-class vocabulary. That
  document explicitly defers this exact question: "Mapping the corpus's
  clear fields onto each gate's own native fields ... is the matching rule's
  job, not this schema's."

It specifies, in order: the gate scope filter and how a raw finding's native
fields resolve to a location and a defect-class token (§1–§3); the match
test itself (§4); the line tolerance and its justification (§5); how
ambiguous many-to-many candidates are resolved deterministically (§6); what
the rule deliberately cannot distinguish (§7); and a worked walkthrough
against the two examples already in `CORPUS_CASE_FORMAT.md` (§8).

**Out of scope for this document:** the false-positive-resistance expression
and its worked examples (a later specification) — this document only
restates that the match test is polarity-symmetric, in §4; implementing this
rule in the runner; any change to gate prompts, models, or logic. No
production code accompanies this document.

## 1. Scope filter

A finding only ever competes for a label when `finding.gate == label.gate`.
`false_positive_filter` has no `gate` value in the label schema and is not
matched at the per-label level by this rule at all — per
`CORPUS_CASE_FORMAT.md` §4, its drop-precision metric is a runner-level
computation over `code_review` labels plus the filter's own keep/drop
decision on the raw code-review output, not a distinct matching pass.

## 2. Location resolution

Each raw finding is resolved to a normalized `(file_path, line, line_end)`
triple, or to `unresolved`. This is the only step that touches a gate's
native fields for location purposes; everything after this section operates
on the normalized triple.

### 2.1 Code review

Already structured — no parsing. `file_path` is used as-is. When
`start_line` is present the finding's range is `(start_line, line)` (`line`
acts as the span's end, per `GATE_FINDING_INVENTORY.md` §1, "Location
fields"); when absent, the range is the point `(line, line)`. `line: None`
resolves to a file-wide finding: `file_path` known, no line.

### 2.2 QA

- **`fix_build` mode**: `file_path` is structured and used as-is.
  `line_or_section` is parsed as an integer only when it matches `^\d+$` (it
  may instead hold a function name such as `"def health"`, per
  `GATE_FINDING_INVENTORY.md` §2) — a non-numeric value resolves to
  file-known, line-unresolved.
- **Every other mode** (default review, `write_tests`): no structured
  `file_path` exists at all. One fixed regex is attempted against the
  free-text `location` string (see §2.3). No other parsing is attempted.
- **`acceptance_evidence` mode**: produces no `bugs_found` at all
  (`GATE_FINDING_INVENTORY.md` §2) — nothing to resolve; a case cannot target
  this mode with `expected_findings`.

### 2.3 Security, and QA outside `fix_build`

Both gates carry only a free-text `location` string with no structured
`file_path` or numeric line (`GATE_FINDING_INVENTORY.md` §2, §3). Exactly one
regex is attempted against that string, applied as a **leftmost, unanchored
search** — not a full-string match:

```
[\w./\\-]+\.[A-Za-z0-9]+(?::(\d+))?
```

— a path segment containing a `.`-extension, optionally followed by `:` and
digits. The leftmost match in the string wins; any further match later in
the same string is not considered. A match resolves `file_path` to the
matched text with the optional `:digits` suffix stripped, and resolves the
line to that suffix's digits when present, or to file-known/line-unresolved
when absent. No match anywhere in the string resolves to fully `unresolved`.

The search being unanchored and leftmost, rather than a full-string match, is
a deliberate, stated choice: `location: "see src/app.py:12 handler"` resolves
to `("src/app.py", 12, 12)` — the surrounding prose is not required to be
absent, only the pattern itself is required to appear. A location naming two
path-shaped tokens resolves to the leftmost one only; the second is not
considered, consistent with "exactly one regex, one match." The pattern is
also purely syntactic: a token that only incidentally matches the
path-with-extension shape — a version string such as `1.2`, or `v1.2` — is
still accepted as a resolved location if it is the leftmost match. This is
the same trade-off as the rest of this section: refining the pattern to
exclude version-like tokens is exactly the kind of accumulating heuristic
this rule exists to refuse, so an occasional false positive here is an
accepted cost of keeping the boundary at one fixed pattern.

This is deliberately the *only* parsing attempt specified. `location:
"run:3"` — the real security example in `GATE_FINDING_INVENTORY.md` §3, a
function name and a line but no file extension — contains no path-shaped
token anywhere in the string for a leftmost search to find, so it resolves
to `unresolved`, not to a guessed file. A rule that keeps adding parses to
rescue more findings is a rule re-admitting prose judgment through the back
door; one fixed pattern, applied one fixed way, keeps the boundary bright.

### 2.4 Unresolved findings

A fully unresolved finding (no file at all) never matches any label, whether
that label is line-specific or file-wide. There is no single-file case
inference, no "the fixture only has one file so assume it's that one," and
no other rescue heuristic. See §7 item 3.

## 3. Defect-class resolution

Each raw finding is resolved to a vocabulary token (§3 of
`CORPUS_CASE_FORMAT.md`), or to `unclassifiable`.

### 3.1 Code review

`category` maps 1:1 onto Group A of the vocabulary — it is drawn from the
same closed 13-value enum the vocabulary itself is built from
(`_ChunkReviewIssueCategory`). `category: "general"` is deliberately excluded
from the vocabulary (`CORPUS_CASE_FORMAT.md` §4) and therefore resolves to
`unclassifiable` — a finding correctly locating a real defect but tagged
`general` can never match a label, by design, not by oversight.

### 3.2 Security

Exact, case-insensitive string equality between the raw `category` field and
a Group B token only: `injection`, `xss`, `csrf`, `auth`, `crypto`,
`insecure-deserialization`, `ssrf`. No synonym table and no aliasing —
`"authz bypass"` does not normalize to `auth`, `"cross-site scripting"` does
not normalize to `xss`. Any other string resolves to `unclassifiable`. This
is the same trade-off as §3.1: it deliberately undercounts real detections
phrased outside the seven canonical tokens rather than reintroduce the prose
similarity judgment the matching rule exists to avoid.

### 3.3 QA

**No defect-class resolution exists.** `BugReport` has no category field at
any layer — not structured, not free-text (`GATE_FINDING_INVENTORY.md` §2).
There is nothing to resolve. Consequently, `gate: qa` labels are never
checked against `defect_class` by the match test in §4 below — a QA finding
matches a QA label on location alone. This is a forced consequence of the
constraint that this work makes no gate prompt or logic change, not a
convenience choice: giving QA a defect-class check would require QA to emit
one, which is out of scope here.

## 4. The match test

Finding F counts as matching label L exactly when all of the following hold:

1. **Gate identity**: `F.gate == L.gate` (§1).
2. **Location**:
   - If `L.line` is `null` (a file-wide/structural label): match requires
     only `F.file_path == L.file_path` (compared as a normalized relative
     POSIX-style path, no leading `./`). `F`'s line, if it has one, is
     irrelevant.
   - If `L.line` is not `null`: match requires `F` to be resolved (§2) with
     `F.file_path == L.file_path`, **and** `F` to carry at least one numeric
     line (not file-known/line-unresolved), **and** `F`'s resolved range
     `[F.line, F.line_end]` to **overlap** the tolerance-expanded label range
     `[L.line - 3, L.line_end + 3]` (tolerance justified in §5) — that is,
     `F.line <= L.line_end + 3` and `F.line_end >= L.line - 3`. For a
     single-line finding (every §2.2/§2.3 resolution, which always produces
     `F.line == F.line_end`), this reduces to the point-in-range test: `L.line
     - 3 <= F.line <= L.line_end + 3`. For a spanned code-review finding
     (§2.1's `(start_line, line)` range, where `F.line != F.line_end`), any
     line the span touches within the tolerance-expanded range is enough —
     the finding does not need to anchor to one specific line of its own
     span, consistent with §5's own justification that a model anchoring to
     any line of a multi-line defect should still count as locating it. A
     finding that only knows its file, not any line, does **not** satisfy a
     line-specific label — a vague "somewhere in this file" hit is not
     credited as hitting a stated line. This asymmetry is deliberate: the
     reverse direction (file-wide label, any location in file) is intended
     to accept exactly this vagueness, because that is what a file-wide
     *label* itself claims; a *finding's* vagueness is not given the same
     benefit.
3. **Defect class**: `F`'s resolved class (§3) equals `L.defect_class`
   exactly — except when `L.gate == qa`, where this check is skipped
   entirely per §3.3.

The test is symmetric with respect to label polarity: a `must_find` label is
**satisfied** when some finding matches it under this test; a `must_not_find`
label is **violated** when some finding matches it. How a satisfied or
violated label feeds precision, recall, or a false-positive metric is a
runner concern; elaborating false-positive-resistance patterns beyond this
restatement is a separate, later specification, per this document's stated
scope.

## 5. Line tolerance: 3 lines

The tolerance is 3 lines, applied symmetrically to expand the label's
`[line, line_end]` range on both ends before the overlap test in §4.

No empirical distribution of finding-line-vs-true-line deltas exists yet —
neither the corpus (built by a later story) nor the runner that would
produce real deltas by running gates against it exist at the time of this
specification. The sanity check below is therefore against the concrete
field semantics and worked examples already on hand in the two prior
documents, not against a statistical sample, and the value is stated as a
starting point rather than a final calibration:

- Code review's own model supports multi-line spans (`start_line` + `line`)
  specifically because a single logical defect routinely anchors to more
  than one physical line — for example, a query or function call built
  across several lines. The worked SQL-injection example in
  `CORPUS_CASE_FORMAT.md` §5 (`CASE-0002`) happens to be a single-line
  statement, but a structurally identical defect written across, say, three
  lines of call arguments is exactly the shape the `start_line`/`line` span
  fields exist to describe — a model anchoring its report to any one of
  those lines should still count as locating the same defect.
- A model that reports a line number relative to a diff hunk rather than
  absolute to the file — a plausible failure mode, since gates review
  chunked diffs — drifts by roughly the hunk's own size. 3 lines fully
  absorbs that class of drift for a hunk up to 3 lines, like `CASE-0002`'s;
  for a hunk one line larger, like `CASE-0001`'s 4-line addition, worst-case
  hunk-relative drift is ~4 lines, one past this tolerance — an accepted
  residual at this calibration rather than a claim that 3 lines absorbs
  every hunk this size, and revisited per the final bullet below. Either way
  it does not absorb drift from a hunk an order of magnitude larger.
- 3 lines stays tighter than the typical vertical gap between distinct,
  blank-line-separated logical blocks in reasonably formatted code, so it
  does not casually credit a match against an unrelated adjacent statement.
  §7 item 4 states the corresponding failure mode directly: dense, unspaced
  code can still bring two distinct defects within tolerance of each other.
- This number should be revisited once the runner (a later story) produces
  real finding-vs-label deltas over the built corpus; keeping it on
  structural reasoning alone past that point would repeat the mistake this
  section is trying to avoid.

## 6. Resolving many-to-many candidate matches

Within one case and one gate, build the set of all `(finding, label)` pairs
that pass the match test in §4. A finding may pass against more than one
label (e.g. two labels 2 lines apart, both within tolerance of the same
finding); a label may pass against more than one finding. Resolution must be
deterministic — never dependent on set or dict iteration order — so two runs
over the same gate output produce the same assignment.

**Rule: greedy nearest-line-distance assignment over a fully specified total
order.**

1. For each candidate pair, compute a distance: for a line-specific label,
   the gap between the finding's resolved range `[F.line, F.line_end]` and
   the label's **unexpanded** range `[L.line, L.line_end]` — `0` when the two
   ranges overlap directly, otherwise `max(L.line - F.line_end, F.line -
   L.line_end)` (the gap from the nearer span end to the nearer range end;
   for a single-line finding, `F.line == F.line_end`, so this reduces to the
   distance from that one line to the nearer end of `[L.line, L.line_end]`).
   This is a **stricter** overlap test than §4's candidate-admission test,
   which uses the ±3-expanded range — every pair reaching this step already
   passed §4, but only pairs overlapping the unexpanded label range get
   distance `0`; a pair admitted purely through §4's tolerance (e.g. a
   single-line finding at line 10 against a label at `[12, 12]`: admitted
   since `10 >= 12 - 3`, but the raw ranges don't overlap) carries distance
   `1`–`3`, not `0`. This is what makes the sort in step 2 prefer an exact
   overlap over a tolerance-only hit instead of treating every admitted
   candidate as equally close. For a file-wide label, distance is defined as
   `0` (there is only ever one file-wide "slot" per file per label to fill,
   so line distance does not disambiguate it).
2. Sort all candidate pairs by, in order: (a) ascending distance; (b) the
   label's `label_id`, lexicographically; (c) the finding's position (index)
   in its gate's own output list, ascending.
3. Walk the sorted list in order. Assign a pair only when neither its finding
   nor its label has already been assigned by an earlier pair in the walk.
4. Each label ends the walk satisfied by at most one finding; each finding
   ends the walk assigned to at most one label. A finding left unassigned
   still counts toward that gate's total output for precision purposes — it
   is simply not credited as satisfying any label a second time, nor is an
   already-assigned finding double-counted against a second label it also
   happened to pass against.

This is explicitly a **greedy** assignment, not a global optimum (e.g. the
Hungarian algorithm for maximum bipartite matching). A greedy walk can, in
principle, leave a pair unassigned that an optimal algorithm would have
paired by reassigning an earlier, lower-priority match — see §7 item 5. The
simplification is justified by the corpus being small and hand-curated with
deliberately separated defects (per the case-authoring convention in
`CORPUS_CASE_FORMAT.md`), not because the difference is assumed never to
matter; it is a stated, revisitable choice.

## 7. What this rule deliberately cannot distinguish

1. **Whether a QA finding names the right kind of bug at a matched
   location** — only that QA flagged something there. QA has no
   defect-class to check (§3.3), so a QA "match" is a location match only.
2. **A real security detection phrased outside the seven canonical
   tokens** — recall is undercounted for those cases rather than risking a
   synonym table that reintroduces prose-similarity judgment (§3.2).
3. **Any finding whose location fails the one fixed regex in §2.3**,
   however unambiguous that location would be to a human reading the prose.
   An unresolved finding never matches, full stop (§2.4).
4. **Diffuse defect classes from pinpointable ones.** A flat, uniform 3-line
   tolerance applies equally to a diffuse `architecture` finding and an
   exactly-pinpointable `null-deref` finding; it cannot express that the
   right tolerance for one is not the right tolerance for the other until
   real data justifies splitting them.
5. **A pathological cluster of overlapping candidate matches at the same
   location**, where an optimal bipartite match would legitimately produce a
   different, better assignment than the greedy walk in §6. Declared
   low-probability given curated corpus cases, not eliminated.
6. **A correctly-tagged-`general` or otherwise-unclassifiable finding from a
   genuine miss** — both resolve to `unclassifiable` and both score as a
   miss under this rule, even when the former actually located the right
   defect (§3.1).
7. **The "same" real-world defect across gates.** A security finding that
   exactly nails an injection at the exact right line never satisfies a
   `code_review` label for what a human would call the same underlying bug —
   matching is always gate-scoped (§1), by the corpus schema's own
   one-label-per-gate design (`CORPUS_CASE_FORMAT.md` §2).
8. **A resolved location that is a real but partial identifier of the right
   file.** §4's file-path comparison is a literal equality, not a
   suffix/basename match: a free-text location that resolves (§2.3) to the
   basename `user_repo.py` never equals a label's fuller relative path
   `app/repositories/user_repo.py`, even though a human reader would
   immediately recognize the same file. This is distinct from item 3 above —
   the location here is not unresolved, it resolved successfully to a
   different string than the label's.

## 8. Worked walkthrough

Both cases below are the worked examples already defined in
`CORPUS_CASE_FORMAT.md` §5. This section does not introduce new corpus
content — it applies §1–§7 to hypothetical real gate output against those
existing labels, to make the rule concrete.

### 8.1 `CASE-0001` — single label, code review

Label `L1`: `gate: code_review`, `defect_class: naming`, `file_path:
app/utils/json.py`, `line: 1`, `line_end: 1`.

Suppose the code-review gate emits:

```json
{"severity": "low", "category": "naming", "file_path": "app/utils/json.py",
 "line": 1, "description": "helper module shadows stdlib json"}
```

- Location (§2.1): resolved directly — `("app/utils/json.py", 1, 1)`.
- Defect class (§3.1): `"naming"` maps directly onto the vocabulary token
  `naming`.
- Match test (§4): gate matches; file matches; `1` falls within
  `[1 - 3, 1 + 3] = [-2, 4]`; class matches. **L1 is satisfied.**

Now suppose the same gate instead emits `category: "general"` with the same
location and description. Location and gate still match, but §3.1 resolves
`"general"` to `unclassifiable`, which cannot equal `naming`. **L1 is not
satisfied** — a real, correctly-located finding scores as a miss because of
§7 item 6, exactly as documented.

### 8.2 `CASE-0002` — three labels, one per gate, same real defect

Labels `L1` (`code_review`/`logic`), `L2` (`security`/`injection`), `L3`
(`qa`/`unvalidated-input`), all at `app/repositories/user_repo.py:27`.

- **Code review** emits `category: "logic"`, `file_path:
  app/repositories/user_repo.py`, `line: 27`. Resolves directly; class
  `logic` matches `L1`'s `defect_class`. **L1 satisfied.**
- **Security** emits `category: "injection"`, `location: "user_repo.py:27"`.
  §2.3's leftmost-search regex matches (`user_repo.py` has a `.`-extension,
  `:27` present), resolving to `("user_repo.py", 27, 27)`. Note this resolves
  to the *basename* the free-text string actually contained, not the label's
  full relative path `app/repositories/user_repo.py` — §4's file-path
  comparison is a literal equality, and a security finding whose free-text
  location never contains the full relative path fails the file match even
  though a human would recognize the file. §2.3 does not attempt to
  reconcile a basename against a full path; this is §7 item 8, not item 3 —
  the location here resolved successfully, it just resolved to a different
  string than the label's. Had the location instead been
  `"app/repositories/user_repo.py:27"`, the match would succeed: gate
  matches, file matches, `27` overlaps `[24, 30]`, class `injection` matches
  `L2`. **L2 satisfied only when the free-text location includes the full
  relative path.**
- **QA** emits `description: "user_id from the request path reaches the
  query builder unvalidated"`, `file_path: ""`, `location: "user_repo.py, in
  find_by_id"` (default-mode QA never populates `file_path`, per
  `GATE_FINDING_INVENTORY.md` §2). §2.3's leftmost search matches
  `user_repo.py` (a `.`-extension present, leftmost path-shaped token in the
  string) with no digit group, resolving to file-known, line-unresolved. Per
  §4, a label with `line: 27` (not `null`) requires at least one numeric
  resolved line — file-known-only does not satisfy it. **L3 is not
  satisfied**, regardless of what `defect_class` QA's finding would have
  been checked against (§3.3 means it never would have been checked at all)
  — the finding fails on location before defect class is ever considered.
  Note also that a numeric line would not have been enough on its own: the
  resolved file here is the basename `user_repo.py`, not the label's
  `app/repositories/user_repo.py`, so even a hypothetical QA location of
  `"user_repo.py:27"` would still fail §4's file-path equality — the same
  §7 item 8 limitation the security bullet above demonstrates. Only a
  free-text location carrying QA's `.`-extension token as the full relative
  path (e.g. `"app/repositories/user_repo.py:27"`) could have satisfied
  `L3`'s location requirement at all.

This walkthrough shows the rule's bias directly: it would rather score a
real detection as a miss when its stated location doesn't literally resolve
to the label's normalized path than guess that a partial or missing location
means the same file, consistent with §2.4, §4, and §7 item 8.
