"""Observed outcomes and availability drive all three services equally."""

import pytest

from chef.workers.routing import RUNTIMES, select


def candidates():
    return [{"runtime": runtime, "available": True, "roles": ["implement", "review"],
             "tools": ["read", "write"], "model": "demo-" + runtime, "effort": "high"}
            for runtime in RUNTIMES]


def test_equal_candidates_rotate_without_claude_penalty():
    assert [select(candidates(), role="implement", cursor=i)["runtime"]
            for i in range(3)] == list(RUNTIMES)


def test_active_load_breaks_ties_but_never_caps_admission():
    jobs = [{"status": "running", "route": {"runtime": runtime}}
            for runtime in RUNTIMES for _ in range(20)]
    jobs.append({"status": "reserved", "route": {"runtime": "claude"}})
    assert select(candidates(), role="implement", jobs=jobs)["runtime"] == "codex"
    assert select(candidates()[:1], role="implement", jobs=jobs)["runtime"] == "claude"


def test_tools_and_available_roles_are_hard_admission_constraints():
    values = candidates()
    values[0]["available"] = False
    values[1]["tools"] = ["read"]
    assert select(values, role="implement", required_tools=["write"])["runtime"] == "grok"
    with pytest.raises(ValueError, match="no available worker"):
        select(values, role="verify")


def test_continuation_prefers_original_worker_but_independent_review_is_fresh():
    previous = {"runtime": "grok", "session": "old-context"}
    assert select(candidates(), role="implement", previous=previous) == {
        "runtime": "grok", "reason": "continue the eligible original worker", "fresh": False}
    assert select(candidates(), role="review", previous=previous, independent=True)["fresh"] is True
    values = candidates()
    values[-1]["available"] = False
    assert select(values, role="implement", previous=previous)["runtime"] == "claude"


def quality(samples=5):
    return [{"runtime": runtime, "role": "dev-medium", "condition": "small-module-v1",
             "model": "demo-" + runtime, "effort": "high",
             "samples": samples, "successes": samples if runtime == "grok" else samples - 1,
             "rework": 0} for runtime in RUNTIMES]


def test_quality_requires_matching_sufficient_comparable_samples():
    values = quality()
    assert select(candidates(), role="implement", quality=values,
                  condition="small-module-v1")["runtime"] == "grok"
    assert select(candidates(), role="implement", quality=values,
                  condition="changed-policy")["runtime"] == "claude"
    assert select(candidates(), role="implement", quality=quality(4),
                  condition="small-module-v1")["runtime"] == "claude"
    assert select(candidates(), role="implement", quality=values[:2],
                  condition="small-module-v1")["runtime"] == "claude"


def test_tiny_observed_difference_does_not_override_load():
    values = quality(100)
    assert select(candidates(), role="implement", quality=values,
                  condition="small-module-v1")["runtime"] == "claude"


def test_changed_model_policy_invalidates_previous_quality():
    values = candidates()
    values[-1]["policy"] = "new-model-or-role-digest"
    assert select(values, role="implement", quality=quality(),
                  condition="small-module-v1")["runtime"] == "claude"


@pytest.mark.parametrize("field,value", [("model", None), ("model", ""), ("model", "inherit"),
                                        ("model", "unknown"), ("effort", None), ("effort", "unknown")])
def test_unknown_preferences_cannot_receive_quality_based_routing(field, value):
    values = candidates()
    values[-1][field] = value
    assert select(values, role="implement", quality=quality(),
                  condition="small-module-v1")["runtime"] == "claude"


def test_changed_model_or_effort_cannot_reuse_old_quality():
    for field, value in (("model", "demo-other"), ("effort", "low")):
        values = candidates()
        values[-1][field] = value
        assert select(values, role="implement", quality=quality(),
                      condition="small-module-v1")["runtime"] == "claude"


@pytest.mark.parametrize("role", [None, "invented", ""])
def test_invalid_roles_fail_before_selection(role):
    with pytest.raises(ValueError):
        select(candidates(), role=role)
