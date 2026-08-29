"""Unit coverage for :func:`diff_or_full` and :func:`diff_spec_or_full`.

Covers the three acceptance-criteria cases from the diff-formatting-utility
issue for both the code-string diff (``diff_or_full``) and the spec-dict
diff (``diff_spec_or_full``): no previous round (full text/JSON), a small
incremental change (diff), and a near-total-rewrite (falls back to full
text/JSON).
"""

from __future__ import annotations

import json

from investment_team.strategy_lab.agents._diff_format import diff_or_full, diff_spec_or_full


def test_no_previous_round_returns_full_text():
    current = "def strategy():\n    return 1\n"

    result = diff_or_full(None, current)

    assert result == current


def test_small_incremental_change_returns_diff():
    previous = "\n".join(f"line_{i} = {i}" for i in range(50)) + "\n"
    current = previous.replace("line_10 = 10", "line_10 = 999")

    result = diff_or_full(previous, current)

    assert result != current
    assert "line_10" in result
    assert "999" in result
    assert len(result) < len(current)


def test_near_total_rewrite_falls_back_to_full_text():
    previous = "\n".join(f"old_line_{i}" for i in range(30)) + "\n"
    current = "\n".join(f"totally_different_content_{i}_xyz" for i in range(30)) + "\n"

    result = diff_or_full(previous, current)

    assert result == current


def test_identical_code_returns_diff_not_full_text():
    code = "def f():\n    return 42\n"

    result = diff_or_full(code, code)

    assert result != code
    assert len(result) < len(code)


def test_empty_previous_code_is_not_none_and_diffs():
    previous = ""
    current = "x = 1\n"

    result = diff_or_full(previous, current)

    assert result == current or "x = 1" in result


def test_no_trailing_newline_change_does_not_concatenate_lines():
    previous = "\n".join(f"line_{i} = {i}" for i in range(50))
    current = previous.replace("line_49 = 49", "line_49 = 999")

    result = diff_or_full(previous, current)

    assert "line_49 = 49\n" in result
    assert "999" in result
    assert "49999" not in result
    assert "49+line_49" not in result


def test_spec_no_previous_round_returns_full_json():
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"stop_loss": 0.1}}

    result = diff_spec_or_full(None, current_spec)

    assert result == json.dumps(current_spec, indent=2, sort_keys=True)


def test_spec_small_incremental_change_returns_diff():
    previous_spec = {
        "entry_rules": {"threshold": 0.5, "lookback": 20},
        "exit_rules": {"stop_loss": 0.1},
        "universe": [f"symbol_{i}" for i in range(50)],
    }
    current_spec = {
        "entry_rules": {"threshold": 0.7, "lookback": 20},
        "exit_rules": {"stop_loss": 0.1},
        "universe": [f"symbol_{i}" for i in range(50)],
    }

    result = diff_spec_or_full(previous_spec, current_spec)
    full_json = json.dumps(current_spec, indent=2, sort_keys=True)

    assert result != full_json
    assert len(result) < len(full_json)
    assert "changed: entry_rules.threshold" in result
    assert "0.5" in result
    assert "0.7" in result
    assert "stop_loss" not in result


def test_spec_added_and_removed_keys_are_reported():
    previous_spec = {"legacy_field": True, "entry_rules": {"threshold": 0.5}}
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "removed: legacy_field" in result
    assert "added: exit_rules" in result


def test_spec_added_key_includes_its_value():
    previous_spec = {"entry_rules": {"threshold": 0.5}}
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "added: exit_rules.trailing_stop: 0.05" in result


def test_spec_removed_key_includes_its_value():
    previous_spec = {"entry_rules": {"threshold": 0.5}, "legacy_field": "old_value"}
    current_spec = {"entry_rules": {"threshold": 0.5}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "removed: legacy_field: 'old_value'" in result


def test_spec_added_nested_dict_reports_every_leaf_value():
    previous_spec = {"entry_rules": {"threshold": 0.5}}
    current_spec = {
        "entry_rules": {"threshold": 0.5},
        "exit_rules": {"trailing_stop": 0.05, "hard_stop": {"pct": 0.1}},
    }

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "added: exit_rules.trailing_stop: 0.05" in result
    assert "added: exit_rules.hard_stop.pct: 0.1" in result


def test_spec_added_empty_dict_reports_a_single_line():
    previous_spec = {"entry_rules": {"threshold": 0.5}}
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "added: exit_rules: {}" in result


def test_spec_key_wholly_removed_differs_from_key_emptied_to_dict():
    previous_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}
    current_wholly_removed = {"entry_rules": {"threshold": 0.5}}
    current_emptied = {"entry_rules": {"threshold": 0.5}, "exit_rules": {}}

    result_wholly_removed = diff_spec_or_full(previous_spec, current_wholly_removed)
    result_emptied = diff_spec_or_full(previous_spec, current_emptied)

    assert result_wholly_removed != result_emptied
    assert "removed: exit_rules.trailing_stop: 0.05" in result_wholly_removed
    assert "changed: exit_rules:" in result_emptied
    assert "{}" in result_emptied


def test_spec_key_wholly_added_differs_from_key_populated_from_empty():
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}
    previous_wholly_absent = {"entry_rules": {"threshold": 0.5}}
    previous_empty = {"entry_rules": {"threshold": 0.5}, "exit_rules": {}}

    result_wholly_absent = diff_spec_or_full(previous_wholly_absent, current_spec)
    result_from_empty = diff_spec_or_full(previous_empty, current_spec)

    assert result_wholly_absent != result_from_empty
    assert "added: exit_rules.trailing_stop: 0.05" in result_wholly_absent
    assert "changed: exit_rules:" in result_from_empty
    assert "{}" in result_from_empty


def test_spec_equal_nested_dicts_are_not_reported_as_changed():
    previous_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"trailing_stop": 0.05}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed:" not in result
    assert "added:" not in result
    assert "removed:" not in result


def test_spec_both_sides_empty_dict_is_not_reported_as_changed():
    previous_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {}}
    current_spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "exit_rules" not in result


def test_spec_bool_vs_int_type_change_is_reported_as_changed():
    previous_spec = {"entry_rules": {"active": True}}
    current_spec = {"entry_rules": {"active": 1}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.active" in result


def test_spec_int_vs_float_type_change_is_reported_as_changed():
    previous_spec = {"entry_rules": {"threshold": 1}}
    current_spec = {"entry_rules": {"threshold": 1.0}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.threshold" in result


def test_spec_bool_vs_int_type_change_inside_list_is_reported_as_changed():
    previous_spec = {"entry_rules": {"flags": [True, False]}}
    current_spec = {"entry_rules": {"flags": [1, False]}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.flags" in result


def test_spec_int_vs_float_type_change_inside_nested_dict_in_list_is_reported_as_changed():
    previous_spec = {"entry_rules": {"legs": [{"weight": 1}]}}
    current_spec = {"entry_rules": {"legs": [{"weight": 1.0}]}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.legs" in result


def test_spec_signed_zero_change_is_reported_as_changed():
    previous_spec = {"entry_rules": {"expected_return": -0.0}}
    current_spec = {"entry_rules": {"expected_return": 0.0}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.expected_return" in result


def test_spec_list_length_change_is_reported_as_changed():
    previous_spec = {"entry_rules": {"flags": [True]}}
    current_spec = {"entry_rules": {"flags": [True, False]}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.flags" in result


def test_spec_dict_key_set_change_inside_list_is_reported_as_changed():
    previous_spec = {"entry_rules": {"legs": [{"weight": 1}]}}
    current_spec = {"entry_rules": {"legs": [{"weight": 1, "side": "long"}]}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed: entry_rules.legs" in result


def test_spec_identical_lists_are_not_reported_as_changed():
    previous_spec = {"entry_rules": {"flags": [True, False], "legs": [{"weight": 1}]}}
    current_spec = {"entry_rules": {"flags": [True, False], "legs": [{"weight": 1}]}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "changed:" not in result


def test_spec_near_total_rewrite_falls_back_to_full_json():
    previous_spec = {f"field_{i}": f"old_value_{i}" for i in range(30)}
    current_spec = {f"field_{i}": f"totally_different_value_{i}_xyz" for i in range(30)}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert result == json.dumps(current_spec, indent=2, sort_keys=True)


def test_spec_identical_specs_returns_diff_not_full_json():
    spec = {"entry_rules": {"threshold": 0.5}, "exit_rules": {"stop_loss": 0.1}}

    result = diff_spec_or_full(spec, spec)
    full_json = json.dumps(spec, indent=2, sort_keys=True)

    assert result != full_json
    assert len(result) < len(full_json)


def test_spec_empty_previous_dict_is_not_none_and_diffs():
    previous_spec: dict = {}
    current_spec = {"entry_rules": {"threshold": 0.5}}

    result = diff_spec_or_full(previous_spec, current_spec)

    assert "added: entry_rules" in result
