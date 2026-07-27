from software_engineering_team.shared.phases.review_cycle import (
    cr_call_is_grounding_bad,
    grounding_rejection_ratio,
)
from software_engineering_team.shared.v2_models import BaseMicrotaskReviewConfig


def test_config_defaults_are_conservative():
    c = BaseMicrotaskReviewConfig()
    assert c.grounding_failure_cycle_limit == 3
    assert c.grounding_failure_ratio_threshold == 0.75


def test_grounding_rejection_ratio():
    assert grounding_rejection_ratio(4, 1) == 0.75
    assert grounding_rejection_ratio(None, 0) is None
    assert grounding_rejection_ratio(0, 0) is None


def test_cr_call_is_grounding_bad():
    assert cr_call_is_grounding_bad(
        passed=False, raw_issue_count=4, kept_count=1, ratio_threshold=0.75
    )
    assert not cr_call_is_grounding_bad(
        passed=True, raw_issue_count=4, kept_count=0, ratio_threshold=0.75
    )
    assert not cr_call_is_grounding_bad(
        passed=False, raw_issue_count=None, kept_count=1, ratio_threshold=0.75
    )
