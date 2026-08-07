"""
Open-question processing for the Product Requirements Analysis Agent.

Between spec review and asking the user, the raw open questions pass through a
pipeline that parses LLM output into typed :class:`OpenQuestion` models, filters out
duplicates of already-answered questions and organizational/process questions,
consolidates semantically-equivalent questions, checks question/option coherence,
and attaches a recommended option. The LLM-backed steps take an explicit Strands
``model`` and fall back to the unmodified list on any failure; the rest are pure.

Question and option text is logged in full (no character truncation of those
fields). ``MAX_ISSUES``, ``MAX_GAPS``, and ``MAX_OPEN_QUESTIONS`` are intentional
item-count UX caps so a single spec review stays digestible; they are not
character limits on text fields.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Sequence

from strands.models.model import Model

from software_engineering_team.shared.deduplication import dedupe_strings as _dedupe_items

from .llm_io import call_llm_json
from .models import AnsweredQuestion, OpenQuestion, QuestionOption, SpecReviewResult
from .prompts import (
    CONSOLIDATE_QUESTIONS_PROMPT,
    GENERATE_QUESTION_RECOMMENDATIONS_PROMPT,
    REVIEW_QUESTIONS_ALIGNMENT_PROMPT,
)
from .qa_history import content_words

logger = logging.getLogger(__name__)

# Item-count UX caps (not character limits on text fields). Chosen to keep a
# single spec review digestible in one sitting rather than tuned against
# measured user drop-off; revisit with product input if that changes.
MAX_ISSUES = 10
MAX_GAPS = 10
MAX_OPEN_QUESTIONS = 10
# Empirical SequenceMatcher.ratio() cutoff for "same answer, different wording"
# (shared with software_engineering_team.shared.deduplication's dedupe threshold).
ANSWER_SIMILARITY_THRESHOLD = 0.85


def cap_open_questions(
    questions: List[OpenQuestion],
    *,
    limit: int = MAX_OPEN_QUESTIONS,
) -> List[OpenQuestion]:
    """Return at most ``limit`` open questions, preserving order.

    Preconditions: ``questions`` is a list of :class:`OpenQuestion`; ``limit`` >= 0.
    Postconditions: returns a shallow copy of ``questions`` when
        ``len(questions) <= limit``, otherwise the first ``limit`` items.
        Violating preconditions raises ``AssertionError`` (Design by Contract);
        otherwise does not raise.
    """
    assert isinstance(questions, list), "questions must be a list"
    assert limit >= 0, f"limit must be >= 0, got {limit}"
    if len(questions) <= limit:
        return list(questions)
    logger.info("Truncated open questions: %d->%d", len(questions), limit)
    return questions[:limit]


# Substring matches (see filter_organizational_questions), so keep entries short
# and specific to org/process/approval topics to avoid false-positiving on
# legitimate technical questions. Extend by adding another lowercase phrase.
ORGANIZATIONAL_PHRASES = [
    "decision process",
    "approval process",
    "who makes",
    "final decision",
    "consensus",
    "product manager",
    "stakeholder approval",
    "organizational structure",
    "who approves",
    "sign-off",
    "sign off",
    "hierarchy",
    "reporting",
]


def _clean_token(w: str) -> str:
    """Strip punctuation so tokens like 'store?' match their bare form.

    Preconditions: ``w`` is a string.
    Postconditions: returns the alphanumeric core of ``w``, lowercased; never
        raises. Lowercasing happens before the alphanumeric filter so uppercase
        letters are normalized rather than stripped (``Store?`` → ``store``).
    """
    return re.sub(r"[^a-z0-9]", "", w.strip().lower())


_LEXICAL_LL = frozenset(
    {
        "appall",
        "distill",
        "enrol",
        "enroll",
        "forestall",
        "fulfil",
        "fulfill",
        "install",
        "instill",
        "misspell",
        "quell",
        "recall",
        "reinstall",
        "scroll",
        "thrill",
        "uninstall",
    }
)
_SHORT_LL_CORES = frozenset(
    {
        "ball",
        "bill",
        "call",
        "chill",
        "drill",
        "fall",
        "fill",
        "kill",
        "pull",
        "roll",
        "sell",
        "shell",
        "small",
        "spell",
        "spill",
        "stall",
        "swell",
        "tell",
        "wall",
        "will",
    }
)
_LL_PREFIXES = frozenset(
    {
        "back",
        "down",
        "mis",
        "out",
        "over",
        "pre",
        "re",
        "un",
        "under",
        "up",
    }
)
_LEXICAL_TT = frozenset(
    {
        "batt",
        "boycott",
        "butt",
        "mitt",
        "putt",
        "watt",
    }
)
_COMPLETE_SES_BASES = frozenset({"bus", "gas", "bias", "lens", "corps"})
# Complete singulars ending in -s that take -es plurals (lenses→lens, buses→bus).
# Latinate -us/-os bases are handled separately in _stem_info.
_INSERT_K_STEMS = frozenset(
    {
        "frolick",
        "mimick",
        "panick",
        "picnick",
        "traffick",
    }
)
# Silent-e -oes stubs (shoe/canoe/oboe); other -oes forms are complete -o nouns.
_SILENT_E_OES_BASES = frozenset({"sho", "cano", "obo"})
# Silent-e -ches stubs that do not match the vowel-immediately-before-ch pattern.
_SILENT_E_CH_BASES = frozenset({"quich", "pastich"})
# Complete vowel+ch singulars that must not take silent-e restoration.
_COMPLETE_VOWEL_CH = frozenset(
    {
        "beach",
        "leech",
        "mooch",
        "peach",
        "pooch",
        "reach",
        "speech",
        "teach",
    }
)
# Complete -anch singulars that must not take silent-e restoration (unlike avalanche/tranche).
_COMPLETE_ANCH_BASES = frozenset({"branch", "ranch"})


def _undouble_inflectional(base: str) -> str:
    """Undouble only when -ed/-ing spelling doubled a final consonant.

    Preconditions: ``base`` is a lowercase stem after suffix strip.
    Postconditions: returns the undoubled stem when inflectional, else ``base``.
        Lexical ``ll``/``tt``/``fsz`` doubles are preserved. Never raises.
    """
    vowels = set("aeiou")
    if len(base) < 4 or base[-1] != base[-2] or base[-1] in vowels:
        return base
    doubled = base[-1]
    if doubled in "fsz":
        return base
    if doubled == "l":
        # Keep lexical cores/denylist/prefixes; default keep unknown ll.
        if base in _SHORT_LL_CORES or base in _LEXICAL_LL:
            return base
        for core in _SHORT_LL_CORES:
            if base.endswith(core) and base[: -len(core)] in _LL_PREFIXES:
                return base
        undoubled = base[:-1]
        # Inflectional British -l doubling, including short bases (fuel/dial/duel).
        if len(base) >= 5 and undoubled.endswith(("el", "ol", "al")) and len(undoubled) >= 3:
            return undoubled
        return base
    if doubled == "t":
        if base in _LEXICAL_TT or base.endswith("cott"):
            return base
        return base[:-1]
    return base[:-1]


def _strip_inserted_ck(base: str) -> str:
    """Remove spelling-only k after known -c verbs only (mimick→mimic).

    Preconditions: ``base`` is a lowercase stem after suffix strip / undoubling.
    Postconditions: returns ``base`` without inserted ``k`` for allowlisted stems;
        lexical ``-ick``/``-pick``/``-kick``/``-click`` forms are unchanged. Never raises.
    """
    if base.endswith(("pick", "kick", "click")):
        return base
    if base in _INSERT_K_STEMS or any(base.endswith(s) for s in _INSERT_K_STEMS):
        return base[:-1]
    return base


def _stem_info(w: str) -> tuple[str, bool, bool, bool]:
    """Normalize word for matching; flag silent-e / y-ie stubs / exact eligibility.

    Preconditions: ``w`` is a cleaned token (lowercase).
    Postconditions: returns
        ``(stem, silent_e_candidate, y_or_ie_stub, exact_ok)``. Never raises.
        ``exact_ok`` is False for restoration-only stubs (``ies``/``ied``, short
        silent-e ``-ing`` stubs, and silent-e plural ``-es`` forms) so they
        cannot exact-match unrelated raw tokens like ``spec``/``cas``/``cod``.
        Lexical doubles and true ``-c`` verb ``k`` insertion are preserved;
        default for unknown ``ll`` is keep. Plural ``settings``/``mappings``
        recurse through ``-ing`` normalization.
    """
    w = w.strip()
    vowels = set("aeiou")

    if w in {"uses", "used"}:
        # Third-person / past of use (len 5) must not fall through short-token guard.
        return "use", False, False, True

    if w.endswith("ied") and len(w) >= 4:
        stub = w[:-3]
        if stub:
            return stub, False, True, False

    if len(w) <= 4:
        return w, False, False, True

    if w.endswith("ed"):
        if len(w) >= 5:
            raw = w[:-2]
            base = _strip_inserted_ck(_undouble_inflectional(raw))
            if len(base) >= 3:
                silent_e = base == raw
                return base, silent_e, False, True

    if w.endswith("ing") and len(w) > 5:
        raw = w[:-3]
        base = _strip_inserted_ck(_undouble_inflectional(raw))
        if len(base) >= 3:
            # Non-doubled -ing is often silent-e (coding→cod), but -x verbs
            # (fixing→fix) never restore e.
            silent_e = base == raw and not base.endswith("x")
            # Short silent-e stubs (coding→cod, making→mak) are restoration-only.
            exact_ok = not (silent_e and len(base) <= 3)
            return base, silent_e, False, exact_ok

    if w.endswith("ies") and len(w) > 4 and w[-4] not in vowels:
        return w[:-3], False, True, False
    if w.endswith("sses") and len(w) > 4:
        return w[:-2], False, False, True
    if w.endswith("xes") and len(w) > 4:
        return w[:-2], False, False, True
    if w.endswith("zes") and len(w) > 4:
        base = w[:-2]
        if base.endswith("zz"):
            # quizzes→quizz→quiz; buzzes→buzz stays lexical.
            if base.endswith("izz") and len(base) <= 5:
                return base[:-1], False, False, True
            return base, False, False, True
        return base, True, False, False
    if w.endswith("ches") and len(w) > 4:
        base = w[:-2]
        if base.endswith("tch") or base in _COMPLETE_VOWEL_CH or base in _COMPLETE_ANCH_BASES:
            return base, False, False, True
        # Silent-e: *ache/*anche/*iche and short vowel+ch (cache); else exact (arch).
        if (
            (base.endswith("ach") and not base.endswith(("beach", "peach")))
            or base.endswith("anch")
            or base in _SILENT_E_CH_BASES
            or (len(base) == 4 and base[-3] in vowels)
        ):
            return base, True, False, False
        return base, False, False, True
    if w.endswith("shes") and len(w) > 4:
        return w[:-2], False, False, True
    if w.endswith("ses") and len(w) > 4:
        base = w[:-2]
        # Exact for Latinate -us/-os singulars long enough that the base is the
        # complete word (status/focus/cactus: len >= 5). Shorter -us bases like
        # hous/abus from houses/abuses must stay silent-e so they restore to
        # house/abuse; do not drop the length guard. Short complete -s singulars
        # (bus/gas/bias/lens/corps) are handled via _COMPLETE_SES_BASES.
        if (base.endswith(("us", "os")) and len(base) >= 5) or base in _COMPLETE_SES_BASES:
            return base, False, False, True
        return base, True, False, False
    if w.endswith("oes") and len(w) > 4 and w[-4] not in vowels:
        base = w[:-2]
        # Silent-e allowlist (shoe/canoe/oboe); other -oes are complete -o nouns
        # (echo/mango/cargo/domino/buffalo/...).
        if base in _SILENT_E_OES_BASES:
            return base, True, False, False
        return base, False, False, True
    if w.endswith("s") and len(w) > 4 and not w.endswith(("ss", "us", "is")):
        singular = w[:-1]
        if singular.endswith("ing") and len(singular) > 5:
            return _stem_info(singular)
        return singular, False, False, True

    return w, False, False, True


def _stems_match(
    stem: str,
    silent_e_candidate: bool,
    y_or_ie_stub: bool,
    exact_ok: bool,
    qa_exact_stems: set[str],
    qa_silent_e_stubs: set[str],
    qa_y_or_ie_stubs: set[str],
) -> bool:
    """Return True when ``stem`` matches a qa stem.

    Preconditions: ``stem`` is a non-empty stemmed token; qa sets come from
        history stemming.
    Postconditions: exact membership applies only when ``exact_ok``.
        Restoration stubs match via ``+e`` / ``+y`` / ``+ie`` or stub-to-stub,
        never against unrelated raw tokens. Never raises.
    """
    if exact_ok and stem in qa_exact_stems:
        return True
    # Stub-to-stub so species↔species and caches↔caches still match.
    if y_or_ie_stub and stem in qa_y_or_ie_stubs:
        return True
    if silent_e_candidate and stem in qa_silent_e_stubs:
        return True
    if silent_e_candidate and (stem + "e") in qa_exact_stems:
        return True
    if stem.endswith("e") and stem[:-1] in qa_silent_e_stubs:
        return True
    if y_or_ie_stub and ((stem + "y") in qa_exact_stems or (stem + "ie") in qa_exact_stems):
        return True
    if stem.endswith("y") and stem[:-1] in qa_y_or_ie_stubs:
        return True
    if stem.endswith("ie") and stem[:-2] in qa_y_or_ie_stubs:
        return True
    return False


def filter_duplicate_questions(
    new_questions: List[OpenQuestion],
    qa_history: str,
) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
    """Filter out questions that appear to be duplicates of answered ones.

    Filters out questions whose keyword stems (plus inflectional variants such as
    plurals, past tense, and silent-e forms) match stemmed history tokens via
    :func:`_stem_info` / :func:`_stems_match` — not verbatim raw-token equality.
    A question is considered a duplicate when at least 90% of its keyword stems
    are found in the history. Below-90% coverage is kept for possible
    consolidation elsewhere. This is keyword coverage, not a similarity ratio
    between the question and history.

    Uses :func:`content_words` (stopword-based, not length-based) for the same
    keyword-admission rule as :func:`qa_history.extract_answer_from_qa_history`,
    so a short-keyword question (e.g. one about an acronym) that the extractor
    can now match isn't excluded from ``duplicates`` here first — otherwise it
    would never reach the extractor at all and would be re-asked regardless of
    the extractor's own behavior.

    Returns:
        Tuple of (filtered_questions, duplicate_questions).
        - filtered_questions: Questions that are NOT duplicates (should be asked)
        - duplicate_questions: Questions that ARE duplicates (already answered)

    Preconditions: ``new_questions`` is a list of :class:`OpenQuestion`;
        ``qa_history`` is a string.
    Postconditions: the two returned lists partition ``new_questions`` (order
        preserved within each). Never raises for well-typed inputs
        (``List[OpenQuestion]``, ``str``); ``_stem_info`` / ``_clean_token`` are
        total over cleaned lowercase tokens. Precondition violations raise
        ``AssertionError`` (raised explicitly so the guard still holds under
        ``python -O``).
    """
    if not isinstance(new_questions, list):
        raise AssertionError("new_questions must be a list")
    if not isinstance(qa_history, str):
        raise AssertionError("qa_history must be a string")
    qa_history_lower = qa_history.lower()
    filtered = []
    duplicates = []

    qa_stem_infos = [
        _stem_info(tok) for tok in re.findall(r"[a-z0-9]+", qa_history_lower) if len(tok) >= 2
    ]
    # Exact set excludes restoration-only stubs so species/cases cannot hit spec/cas.
    qa_exact_stems = {stem for stem, _, _, exact_ok in qa_stem_infos if exact_ok}
    # Silent-e stubs include both restoration-only and exact-eligible forms
    # (e.g. moved→mov) so store↔stored matching still works.
    qa_silent_e_stubs = {stem for stem, silent_e, _, _ in qa_stem_infos if silent_e}
    qa_y_or_ie_stubs = {stem for stem, _, y_or_ie, _ in qa_stem_infos if y_or_ie}

    for q in new_questions:
        q_text_lower = (q.question_text or "").lower()
        # Same keyword admission as qa_history.extract_answer_from_qa_history
        # (stopword-based via content_words, not a length>3 gate).
        key_stem_infos = [_stem_info(_clean_token(w)) for w in content_words(q_text_lower)]
        key_by_stem: dict[str, tuple[bool, bool, bool]] = {}
        for stem, silent_e, y_or_ie, exact_ok in key_stem_infos:
            if not stem:
                continue
            prev_s, prev_y, prev_x = key_by_stem.get(stem, (False, False, False))
            key_by_stem[stem] = (
                prev_s or silent_e,
                prev_y or y_or_ie,
                prev_x or exact_ok,
            )
        if not key_by_stem:
            filtered.append(q)
            continue

        matches = sum(
            1
            for stem, (silent_e, y_or_ie, exact_ok) in key_by_stem.items()
            if _stems_match(
                stem,
                silent_e,
                y_or_ie,
                exact_ok,
                qa_exact_stems,
                qa_silent_e_stubs,
                qa_y_or_ie_stubs,
            )
        )
        match_ratio = matches / len(key_by_stem)
        if match_ratio >= 0.90:
            logger.info(
                "Filtering duplicate question (%.0f%% match): %s",
                match_ratio * 100,
                q.question_text,
            )
            duplicates.append(q)
            continue
        filtered.append(q)

    if duplicates:
        logger.info(
            "Filtered %d duplicate questions based on qa_history",
            len(duplicates),
        )

    return filtered, duplicates


def filter_organizational_questions(questions: List[OpenQuestion]) -> List[OpenQuestion]:
    """Remove questions about organizational structure, approval processes, or decision hierarchy.

    The client/user is the source of truth; we do not ask who approves, how decisions
    are made, or about org structure. A question is considered organizational if any
    of the configured phrases appear in question_text or (if present) context.

    Preconditions: ``questions`` is a list of :class:`OpenQuestion`.
    Postconditions: returns the sublist that is not organizational, order preserved.
        Precondition violations raise ``AssertionError`` (raised explicitly so the
        guard still holds under ``python -O``).
    """
    if not isinstance(questions, list):
        raise AssertionError("questions must be a list")
    kept: List[OpenQuestion] = []
    for q in questions:
        text_norm = (q.question_text or "").lower().strip()
        context_norm = (q.context or "").lower().strip() if q.context else ""
        is_org = False
        for phrase in ORGANIZATIONAL_PHRASES:
            if phrase in text_norm or (context_norm and phrase in context_norm):
                is_org = True
                break
        if not is_org:
            kept.append(q)
    removed = len(questions) - len(kept)
    if removed:
        logger.info(
            "Filtered %d organizational/process question(s)",
            removed,
        )
    return kept


def parse_spec_review_response(raw: Any) -> SpecReviewResult:
    """Parse LLM response into SpecReviewResult.

    Applies deduplication and enforces max limits on issues/gaps to prevent runaway
    repetitive output from the LLM.

    Preconditions: ``raw`` is the decoded LLM output (any type).
    Postconditions: returns a valid :class:`SpecReviewResult`; issues/gaps are
        deduped and capped at ``MAX_ISSUES``/``MAX_GAPS``; open questions are
        parsed but not capped here (the agent workflow applies
        ``MAX_OPEN_QUESTIONS`` after semantic consolidation and
        answer-similarity deduplication so near-duplicates do not crowd out
        distinct topics). Malformed open-question items are skipped and logged;
        non-dict top-level ``raw`` and non-list ``issues``/``gaps``/
        ``open_questions`` are logged and treated as empty; this function never
        raises to callers.
    """
    if not isinstance(raw, dict):
        preview = repr(raw)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        logger.warning(
            "Spec review response is not a JSON object (%s): %s",
            type(raw).__name__,
            preview,
        )
        return SpecReviewResult(summary="Spec review completed (no structured output)")

    raw_issues = raw.get("issues", [])
    raw_gaps = raw.get("gaps", [])
    raw_questions = raw.get("open_questions", [])

    # Keep only string issues/gaps; non-string LLM elements are dropped.
    if isinstance(raw_issues, list):
        issues = [i for i in raw_issues if isinstance(i, str)]
    else:
        logger.warning(
            "Expected list for 'issues', got %s: %r",
            type(raw_issues).__name__,
            raw_issues,
        )
        issues = []
    if isinstance(raw_gaps, list):
        gaps = [g for g in raw_gaps if isinstance(g, str)]
    else:
        logger.warning(
            "Expected list for 'gaps', got %s: %r",
            type(raw_gaps).__name__,
            raw_gaps,
        )
        gaps = []

    original_issue_count = len(issues)
    original_gap_count = len(gaps)

    try:
        issues = _dedupe_items(issues)[:MAX_ISSUES]
    except Exception as exc:
        logger.warning("Deduplication failed for issues, using raw capped list: %s", exc)
        issues = issues[:MAX_ISSUES]
    try:
        gaps = _dedupe_items(gaps)[:MAX_GAPS]
    except Exception as exc:
        logger.warning("Deduplication failed for gaps, using raw capped list: %s", exc)
        gaps = gaps[:MAX_GAPS]

    if len(issues) < original_issue_count or len(gaps) < original_gap_count:
        logger.info(
            "Deduplicated spec review results: issues %d->%d, gaps %d->%d",
            original_issue_count,
            len(issues),
            original_gap_count,
            len(gaps),
        )

    open_questions = []
    if isinstance(raw_questions, list):
        for i, q in enumerate(raw_questions):
            try:
                open_questions.append(parse_open_question(q, i))
            except Exception as exc:
                logger.warning(
                    "Skipping malformed open question at index %d (%s): %s; raw=%r",
                    i,
                    type(exc).__name__,
                    exc,
                    q,
                )
    else:
        logger.warning(
            "Expected list for 'open_questions', got %s: %r",
            type(raw_questions).__name__,
            raw_questions,
        )

    return SpecReviewResult(
        issues=issues,
        gaps=gaps,
        open_questions=open_questions,
        summary=_str_or_default(raw.get("summary"), "Spec review complete"),
    )


def _str_or_default(value: Any, default: str = "") -> str:
    """Return an LLM-provided string field, or ``default`` when absent/wrong-typed.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns ``value`` when it is already a ``str``; otherwise
        returns ``default``. Does not coerce via ``str()`` — non-string values
        (including numbers and bools) yield ``default``.
    """
    return default if value is None or not isinstance(value, str) else value


def _require_string_field(data: dict, key: str, default: str) -> str:
    """Accept a string field; raise on present null/non-string; default if missing.

    Preconditions: ``data`` is a mapping; ``default`` is the fallback for an absent key.
    Postconditions: returns ``default`` when ``key`` is absent; returns the value when
        it is a ``str``; raises ``ValueError`` (including key, type, and value repr)
        when the key is present with ``null`` or any non-string type so callers
        cannot silently remap explicit null IDs/text onto generated defaults
        (e.g. ``q0``) or blank content.
    """
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, str):
        return value
    raise ValueError(f"expected string for {key!r}, got {type(value).__name__}: {value!r}")


def _safe_constraint_layer(value: Any) -> int:
    """Coerce LLM-provided constraint_layer output to int, defaulting to 0.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns a non-negative int. ``None``, non-numeric input, and
        bools (treated as malformed, not as 0/1) yield 0, matching the
        "not a constraint question" default in :class:`OpenQuestion`. Float
        values (including numeric strings like ``"2.9"``) are truncated toward
        zero via ``int(...)``.
    """
    try:
        if value is None:
            return 0
        # bool is a subclass of int; treat it as malformed.
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            # NaN/inf -> malformed
            if value != value or value in (float("inf"), float("-inf")):
                return 0
            return max(0, int(value))
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return 0
            return max(0, int(float(s)))
        return 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _safe_bool(value: Any, default: bool) -> bool:
    """Coerce LLM-provided boolean-ish or numeric values to bool.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns ``default`` when ``value`` is ``None`` or unrecognized;
        returns a bool when ``value`` is already a bool, numeric 0/1, or a common
        boolean string (``true``/``false``/``yes``/``no``/…).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    # Numeric booleans (0/1); reject other numbers as malformed.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        logger.warning(
            "Unexpected numeric boolean value %r, using default %r",
            value,
            default,
        )
        return default

    if isinstance(value, str):
        v = value.strip().lower()
        if v == "":
            return default
        if v in {"true", "t", "yes", "y", "1"}:
            return True
        if v in {"false", "f", "no", "n", "0"}:
            return False
        return default

    return default


def _coerce_list(value: Any, *, allow_str: bool = False, allow_dict: bool = False) -> list:
    """Coerce LLM-provided list-valued output to a list.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns a new list; does not mutate ``value`` itself.
        - None -> []
        - list/tuple -> list(value) (new outer list; nested objects are
          shallow-referenced, so mutating ``result[i]`` mutates the original
          nested object)
        - string -> [value] when ``allow_str`` is True
        - dict -> [value] when ``allow_dict`` is True (the dict is the single
          top-level element ``result[0]``; mutating that dict mutates the
          original)
        - any other scalar -> []
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if allow_str and isinstance(value, str):
        return [value]
    if allow_dict and isinstance(value, dict):
        return [value]
    return []


def parse_open_question(q_data: Any, index: int) -> OpenQuestion:
    """Parse a single open question from LLM output.

    Preconditions: ``index`` is a non-negative int; ``q_data`` is a ``dict``.
    Postconditions: returns a valid :class:`OpenQuestion` when parsing succeeds.
        - ``options`` is coerced to a list (``None`` becomes ``[]``); dict and
          string entries are kept and parsed, while other types are dropped.
        - ``section_impact`` and ``asked_via`` are coerced to lists
          (``None`` becomes ``[]``); elements are kept only when already ``str``.
        - Option ``confidence`` values are normalized to ``[0.0, 1.0]``,
          defaulting to ``0.5`` when missing or malformed.
        - When ``q_data`` has options but no default, the highest-confidence
          option is marked default.
        - Malformed option entries are skipped individually (logged); they do
          not discard the surrounding question.
        - Missing ``id`` / ``question_text`` fall back to ``q{index}`` / ``""``.
    Raises:
        ValueError: when ``q_data`` is not a ``dict``; when ``id`` or
            ``question_text`` is present but not a ``str``; or on other
            unanticipated malformed shapes this helper does not coerce.
            This function has no top-level try/except. Callers handle failures
            differently: ``parse_spec_review_response``, ``consolidate_open_questions``,
            and ``review_question_answer_alignment`` catch per item;
            ``run_context_constraints_discovery`` catches per item and falls back
            to the fixed list when none parse.
    """
    if not isinstance(q_data, dict):
        raise ValueError(f"parse_open_question expects dict input, got {type(q_data).__name__}")

    raw_options = _coerce_list(q_data.get("options", []), allow_str=True, allow_dict=True)
    options = []
    for i, opt in enumerate(raw_options):
        try:
            options.append(parse_question_option(opt, i))
        except ValueError as exc:
            logger.warning("Skipping malformed question option at index %d: %s", i, exc)

    if options and not any(opt.is_default for opt in options):
        best = max(options, key=lambda opt: opt.confidence)
        default_idx = options.index(best)
        options[default_idx] = best.model_copy(update={"is_default": True})

    raw_depends = q_data.get("depends_on")
    if isinstance(raw_depends, (list, tuple)):
        if len(raw_depends) > 1:
            logger.warning(
                "depends_on list truncated to first element for question %s (got %d entries)",
                q_data.get("id", f"q{index}"),
                len(raw_depends),
            )
        depends_on = raw_depends[0] if raw_depends and isinstance(raw_depends[0], str) else None
    elif isinstance(raw_depends, str):
        depends_on = raw_depends
    else:
        depends_on = None

    raw_section_impact = _coerce_list(q_data.get("section_impact", []), allow_str=True)
    section_impact = [v for v in raw_section_impact if isinstance(v, str)]

    raw_asked_via = _coerce_list(q_data.get("asked_via", []), allow_str=True)
    asked_via = [v for v in raw_asked_via if isinstance(v, str)]

    return OpenQuestion(
        id=_require_string_field(q_data, "id", f"q{index}"),
        question_text=_require_string_field(q_data, "question_text", ""),
        context=_str_or_default(q_data.get("context")),
        recommendation=_str_or_default(q_data.get("recommendation")),
        options=options,
        allow_multiple=_safe_bool(q_data.get("allow_multiple", False), default=False),
        source=_str_or_default(q_data.get("source"), "spec_review"),
        category=_str_or_default(q_data.get("category"), "general"),
        priority=_str_or_default(q_data.get("priority"), "medium"),
        constraint_domain=_str_or_default(q_data.get("constraint_domain")),
        constraint_layer=_safe_constraint_layer(q_data.get("constraint_layer")),
        depends_on=depends_on,
        blocking=_safe_bool(q_data.get("blocking", True), default=True),
        owner=_str_or_default(q_data.get("owner"), "user"),
        section_impact=section_impact,
        due_date=_str_or_default(q_data.get("due_date")),
        status=_str_or_default(q_data.get("status"), "open"),
        asked_via=asked_via,
    )


def _safe_confidence(value: Any) -> float:
    """Coerce LLM-provided confidence output to a valid [0.0, 1.0] float, defaulting to 0.5.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns a float clamped to [0.0, 1.0]; non-numeric or missing input
        yields 0.5, matching the "no machine-supplied score" default.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.5
    if result != result:  # NaN check without importing math
        return 0.5
    return max(0.0, min(1.0, result))


def parse_question_option(opt_data: Any, index: int) -> QuestionOption:
    """Parse a single question option from LLM output.

    Preconditions: ``index`` is a non-negative int; ``opt_data`` is the decoded item.
    Postconditions: returns a valid :class:`QuestionOption` for a dict or a string
        label; a non-numeric, ``None``, out-of-range, or overflowing
        ``confidence`` value defaults to 0.5 (or is clamped to ``[0.0, 1.0]``)
        instead of raising. For a string label, the returned option uses
        ``id=f"opt{index}"``, treats the first element (``index == 0``) as the
        default, sets ``rationale`` to an empty string, and assigns
        ``confidence`` 0.5. Raises ``ValueError`` for unsupported non-dict,
        non-string entries (``null``, numbers, …) and for present ``null`` or
        non-string ``id``/``label`` values so callers can drop them rather than
        materializing blank default options.
    """
    if isinstance(opt_data, dict):
        return QuestionOption(
            id=_require_string_field(opt_data, "id", f"opt{index}"),
            label=_require_string_field(opt_data, "label", ""),
            is_default=_safe_bool(opt_data.get("is_default", False), default=False),
            rationale=_str_or_default(opt_data.get("rationale")),
            confidence=_safe_confidence(opt_data.get("confidence", 0.5)),
        )

    if isinstance(opt_data, str):
        return QuestionOption(
            id=f"opt{index}",
            label=opt_data,
            is_default=index == 0,
            rationale="",
            confidence=0.5,
        )

    raise ValueError(f"unsupported option type {type(opt_data).__name__!r} at index {index}")


def _norm_answer_text(t: Any) -> str:
    """Normalize answer/option text for similarity comparison.

    Preconditions: none; ``t`` may be any value.
    Postconditions: returns a lowercased, whitespace-collapsed string; never raises.
    """
    return " ".join(str(t or "").lower().split()).strip()


def dedupe_questions_by_answer_similarity(
    open_questions: List[OpenQuestion],
    answered_questions: List[AnsweredQuestion],
) -> List[OpenQuestion]:
    """Drop open questions whose answer we already have.

    Compares answers (selected_answer and other_text from answered_questions) to the
    option labels of each open question. If any option of an open question is
    semantically the same as an answer we already have, we do not ask that question
    again. Preserves order of open_questions.

    Complexity note: existing answers and option labels are normalized once.
    Exact normalized matches short-circuit via a set; fuzzy matches use
    ``SequenceMatcher.ratio`` with a pair cache. Worst case remains
    O(open_questions × option_labels × existing_answers × L²) in string length L,
    which is acceptable while batches stay capped near ``MAX_OPEN_QUESTIONS``.

    Preconditions: both arguments are lists of the respective models.
    Postconditions: returns a sublist of ``open_questions`` (order preserved);
        questions with no options/labels are always kept; never raises.
    """
    if not open_questions:
        return list(open_questions)

    # Build set of existing answers (normalized) we already have.
    # Keep a list for stable iteration order during SequenceMatcher checks.
    existing_answers: List[str] = []
    existing_answers_set: set[str] = set()
    for aq in answered_questions:
        s = _norm_answer_text(aq.selected_answer)
        if s:
            if s not in existing_answers_set:
                existing_answers.append(s)
                existing_answers_set.add(s)
        other = getattr(aq, "other_text", None)
        if other is not None and str(other).strip():
            o = _norm_answer_text(other)
            if o and o not in existing_answers_set:
                existing_answers.append(o)
                existing_answers_set.add(o)

    if not existing_answers:
        return list(open_questions)

    # Same threshold as shared deduplication for "same meaning"
    SIMILARITY_THRESHOLD = ANSWER_SIMILARITY_THRESHOLD
    kept: List[OpenQuestion] = []
    ratio_cache: dict[tuple[str, str], float] = {}

    def _cached_ratio(a: str, b: str) -> float:
        key = (a, b) if a <= b else (b, a)
        cached = ratio_cache.get(key)
        if cached is not None:
            return cached
        ratio = SequenceMatcher(None, a, b).ratio()
        ratio_cache[key] = ratio
        return ratio

    for q in open_questions:
        if not q.options:
            # No options: we cannot know what answer this would get; keep it
            kept.append(q)
            continue
        option_labels = [_norm_answer_text(opt.label) for opt in q.options if opt.label]
        option_labels = [label for label in option_labels if label]
        if not option_labels:
            kept.append(q)
            continue
        # If any option is the same as an answer we already have, skip this question
        already_covered = False
        for opt_label in option_labels:
            if opt_label in existing_answers_set:
                logger.info(
                    "Skipping open question (answer already have): question_id=%s option=%r ~ existing=%r",
                    q.id,
                    opt_label,
                    opt_label,
                )
                already_covered = True
                break
            for existing in existing_answers:
                if _cached_ratio(opt_label, existing) >= SIMILARITY_THRESHOLD:
                    logger.info(
                        "Skipping open question (answer already have): question_id=%s option=%r ~ existing=%r",
                        q.id,
                        opt_label,
                        existing,
                    )
                    already_covered = True
                    break
            if already_covered:
                break
        if not already_covered:
            kept.append(q)

    return kept


_OPTION_FULL_FIELDS: Sequence[str] = ("id", "label", "is_default", "rationale", "confidence")
_OPTION_RECOMMEND_FIELDS: Sequence[str] = ("id", "label", "rationale")
_CONSOLIDATE_QUESTION_FIELDS: Sequence[str] = (
    "id",
    "question_text",
    "context",
    "recommendation",
    "source",
    "category",
    "priority",
    "allow_multiple",
    "constraint_domain",
    "constraint_layer",
    "depends_on",
    "blocking",
    "owner",
    "section_impact",
    "due_date",
    "status",
    "asked_via",
    "options",
)
_ALIGN_QUESTION_FIELDS: Sequence[str] = (
    "id",
    "question_text",
    "context",
    "category",
    "priority",
    "allow_multiple",
    "constraint_domain",
    "constraint_layer",
    "depends_on",
    "blocking",
    "owner",
    "section_impact",
    "due_date",
    "status",
    "asked_via",
    "options",
)
_RECOMMEND_QUESTION_FIELDS: Sequence[str] = ("id", "question_text", "context", "options")


def _open_question_to_dict(
    q: OpenQuestion,
    fields: Iterable[str],
    *,
    option_fields: Sequence[str] = _OPTION_FULL_FIELDS,
) -> dict[str, Any]:
    """Serialize an :class:`OpenQuestion` for LLM prompt payloads.

    Preconditions: ``q`` is an :class:`OpenQuestion`; ``fields`` names attributes
        on that model (``options`` is expanded via ``option_fields``).
    Postconditions: returns a new dict with the requested fields; never mutates
        ``q``. Nested option dicts only include ``option_fields``.
    """
    payload: dict[str, Any] = {}
    for field in fields:
        if field == "options":
            payload["options"] = [
                {name: getattr(opt, name) for name in option_fields} for opt in q.options
            ]
        else:
            payload[field] = getattr(q, field)
    return payload


def _fetch_llm_list(
    model: Model,
    prompt: str,
    response_key: str,
    operation_name: str,
    allow_empty: bool = False,
) -> List[Any] | None:
    """Call the LLM, parse JSON, and extract a named list field.

    Shared seam for the "call LLM -> validate response shape -> fall back to
    caller's original list" pattern common to the consolidate/align/recommend
    steps below. Per-item parsing and reconciliation stay with each caller.

    Preconditions: ``model`` is a Strands ``Model``; ``prompt`` is a non-empty
        string; ``response_key``/``operation_name`` are non-empty strings.
    Postconditions: returns the list found under ``response_key`` when the LLM
        call succeeds and yields a list that is non-empty, or empty when
        ``allow_empty`` is True; returns ``None`` on any failure (LLM
        exception, non-dict response, a missing/non-list key, or an empty
        list when ``allow_empty`` is False) — callers fall back to their
        original list on ``None``. Never raises.
    """
    try:
        raw = call_llm_json(model, prompt)
    except Exception as e:
        logger.warning("%s failed, using original list: %s", operation_name, str(e))
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get(response_key)
    if not isinstance(items, list):
        return None
    if not items and not allow_empty:
        return None
    return items


def consolidate_open_questions(
    model: Model, open_questions: List[OpenQuestion]
) -> List[OpenQuestion]:
    """Merge duplicate or semantically equivalent questions before sending to user.

    Uses a single LLM call to identify questions that ask the same thing (e.g. OAuth
    provider asked multiple ways) and consolidate them into one question per distinct
    decision, with merged options.

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list.
    Postconditions: returns the consolidated list, or the unmodified list on <=1
        input or any failure (payload serialization, prompt formatting, LLM call,
        or a full-batch parse failure). Items that individually fail to parse are
        skipped and logged rather than discarding the whole batch. Duplicate ids
        within the LLM batch are skipped (first wins). When the LLM echoes a known
        id, metadata is merged by starting from the original question and overlaying
        only fields the LLM actually supplied (so omitted owner/due_date/status/
        asked_via/section_impact/etc. are preserved). If the LLM supplies an empty
        or ``spec_review`` source, the original source is retained; blank
        recommendations likewise fall back to the original. Never raises to callers;
        precondition violations raise ``AssertionError``.
    """
    assert isinstance(model, Model), "model must be a Strands Model"
    assert isinstance(open_questions, list), "open_questions must be a list"
    if len(open_questions) <= 1:
        return list(open_questions)

    try:
        # Batch size is capped upstream (MAX_OPEN_QUESTIONS); building the full
        # payload in memory is intentional and cheap at that scale.
        questions_json = json.dumps(
            [_open_question_to_dict(q, _CONSOLIDATE_QUESTION_FIELDS) for q in open_questions],
            indent=2,
            default=str,
        )
        prompt = CONSOLIDATE_QUESTIONS_PROMPT.format(questions_json=questions_json)
        consolidated = _fetch_llm_list(
            model, prompt, "consolidated_questions", "Question consolidation"
        )
        if consolidated is None:
            return list(open_questions)
        original_by_id = {q.id: q for q in open_questions}
        result = []
        seen_ids: set[str] = set()
        for i, q_data in enumerate(consolidated):
            try:
                parsed = parse_open_question(q_data, i)
                if parsed.id in seen_ids:
                    logger.warning(
                        "Duplicate consolidated question id %r at index %d; skipping",
                        parsed.id,
                        i,
                    )
                    continue
                seen_ids.add(parsed.id)
                orig = original_by_id.get(parsed.id)
                if orig is not None and isinstance(q_data, dict):
                    # Start from the original; overlay only keys the LLM supplied
                    # so omitted metadata (owner, due_date, status, …) is kept.
                    updates = {
                        field: getattr(parsed, field)
                        for field in q_data
                        if field in OpenQuestion.model_fields and field != "id"
                    }
                    if (
                        "source" in updates
                        and updates["source"] in ("", "spec_review")
                        and orig.source
                    ):
                        updates["source"] = orig.source
                    if "recommendation" in updates and not updates["recommendation"]:
                        updates["recommendation"] = orig.recommendation
                    parsed = orig.model_copy(update=updates)
                result.append(parsed)
            except Exception as e:
                logger.warning("Failed to parse consolidated question %d: %s", i, e)
        return result if result else list(open_questions)
    except Exception as e:
        logger.warning(
            "Question consolidation failed, using original list: %s",
            str(e),
            exc_info=True,
        )
        return list(open_questions)


def review_question_answer_alignment(
    model: Model, open_questions: List[OpenQuestion]
) -> List[OpenQuestion]:
    """Ensure each question and its options make sense together (e.g. no Yes/No for open-ended questions).

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list.
    Postconditions: returns the aligned list, or the unmodified list (in its
        original order) on empty input or when no item in the batch parses
        successfully. Never raises to callers; precondition violations raise
        ``AssertionError``. Serialization, prompt formatting, LLM, and parse
        failures fall back to the unmodified list. This is a per-question review
        (ids are preserved): an item that individually fails to parse or that
        repeats an id already placed in the result (a duplicate) falls back to
        its original (unaligned) question by id, when that original id is not
        already in the result; an item carrying an id not present in
        ``open_questions`` (a hallucinated/unrecognized id) has no original to
        fall back to and is dropped outright. Any original question whose id
        never appears in the result (whether dropped as a duplicate/
        hallucination or simply omitted by the LLM) is appended at the end.
        If no item in the batch parses successfully, the LLM-provided order
        carries no meaning, so the original list is returned unchanged rather
        than in fallback (LLM-provided) order. The result therefore contains
        exactly one entry per original id: no question is ever dropped,
        added, or duplicated.
    """
    assert isinstance(model, Model), "model must be a Strands Model"
    assert isinstance(open_questions, list), "open_questions must be a list"
    if len(open_questions) == 0:
        return []
    original_by_id = {q.id: q for q in open_questions}

    try:
        # Batch size is capped upstream (MAX_OPEN_QUESTIONS); building the full
        # payload in memory is intentional and cheap at that scale.
        questions_json = json.dumps(
            [_open_question_to_dict(q, _ALIGN_QUESTION_FIELDS) for q in open_questions],
            indent=2,
            default=str,
        )
        prompt = REVIEW_QUESTIONS_ALIGNMENT_PROMPT.format(questions_json=questions_json)
        aligned = _fetch_llm_list(
            model, prompt, "aligned_questions", "Question-answer alignment review"
        )
        if aligned is None:
            return list(open_questions)
        result = []
        seen_ids = set()
        any_parsed = False
        for i, q_data in enumerate(aligned):
            try:
                parsed = parse_open_question(q_data, i)
                if parsed.id not in original_by_id:
                    raise ValueError(
                        f"aligned question id {parsed.id!r} does not match any original question"
                    )
                if parsed.id in seen_ids:
                    raise ValueError(f"aligned question id {parsed.id!r} is a duplicate")
                result.append(parsed)
                seen_ids.add(parsed.id)
                any_parsed = True
            except Exception as e:
                logger.warning("Failed to parse aligned question %d: %s", i, e)
                fallback_id = q_data.get("id") if isinstance(q_data, dict) else None
                original = original_by_id.get(fallback_id) if isinstance(fallback_id, str) else None
                if original is not None and original.id not in seen_ids:
                    result.append(original)
                    seen_ids.add(original.id)
        if not any_parsed:
            # Nothing in the batch was genuinely realigned, so the LLM-provided
            # order (which any fallbacks above were assembled in) carries no
            # meaning — return the original list in its original order instead.
            return list(open_questions)
        for q in open_questions:
            if q.id not in seen_ids:
                result.append(q)
        return result
    except Exception as e:
        logger.warning(
            "Question-answer alignment review failed, using original list: %s",
            str(e),
            exc_info=True,
        )
        return list(open_questions)


def add_recommendations(
    model: Model, open_questions: List[OpenQuestion], spec_content: str
) -> List[OpenQuestion]:
    """Add a short recommendation (which option and why) to each question.

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list;
        ``spec_content`` is a string or ``None``.
    Postconditions: returns the list with ``recommendation`` populated where the LLM
        supplied one, or the unmodified list on empty input or any failure
        (payload serialization, prompt formatting, LLM call, or apply-step
        errors). Never raises to callers; precondition violations raise
        ``AssertionError``.
    """
    assert isinstance(model, Model), "model must be a Strands Model"
    assert isinstance(open_questions, list), "open_questions must be a list"
    assert isinstance(spec_content, (str, type(None))), "spec_content must be a string or None"
    if len(open_questions) == 0:
        return list(open_questions)

    try:
        # Batch size is capped upstream (MAX_OPEN_QUESTIONS); building the full
        # payload in memory is intentional and cheap at that scale.
        questions_json = json.dumps(
            [
                _open_question_to_dict(
                    q, _RECOMMEND_QUESTION_FIELDS, option_fields=_OPTION_RECOMMEND_FIELDS
                )
                for q in open_questions
            ],
            indent=2,
            default=str,
        )
        spec_content_str = spec_content or ""
        prompt = GENERATE_QUESTION_RECOMMENDATIONS_PROMPT.format(
            spec_content=spec_content_str,
            questions_json=questions_json,
        )
        recs = _fetch_llm_list(
            model, prompt, "recommendations", "Recommendation generation", allow_empty=True
        )
        if recs is None:
            return list(open_questions)
        rec_by_id = {
            r.get("id"): r.get("recommendation")
            for r in recs
            if (
                isinstance(r, dict)
                and isinstance(r.get("id"), str)
                and isinstance(r.get("recommendation"), str)
                and r.get("recommendation").strip() != ""
            )
        }
        result = []
        for q in open_questions:
            rec = rec_by_id.get(q.id)
            result.append(q.model_copy(update={"recommendation": rec}) if rec else q)
        return result
    except Exception as e:
        logger.warning(
            "Recommendation generation failed, returning original questions unchanged: %s",
            str(e),
            exc_info=True,
        )
        return list(open_questions)
