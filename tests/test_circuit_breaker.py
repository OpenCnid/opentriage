"""Tests for circuit breaker state machine (F-OT03)."""

import json
import time
from pathlib import Path

from opentriage.circuit_breaker import (
    DEFAULT_STATE,
    PERMISSIONS,
    can,
    evaluate_demotions,
    evaluate_promotions,
    load_state,
    run_circuit_breaker,
    transition,
    update_metrics,
)
from tests.conftest import write_state


def test_default_state_is_full_autonomy():
    state = dict(DEFAULT_STATE)
    assert state["circuit_breaker"] == "full-autonomy"


def test_permissions():
    state = {"circuit_breaker": "full-autonomy"}
    assert can(state, "classify") is True
    assert can(state, "remediate") is True
    assert can(state, "escalate") is True
    assert can(state, "draft") is True

    state = {"circuit_breaker": "classify-only"}
    assert can(state, "classify") is True
    assert can(state, "remediate") is False
    assert can(state, "escalate") is True

    state = {"circuit_breaker": "observe-only"}
    assert can(state, "classify") is True
    assert can(state, "remediate") is False
    assert can(state, "escalate") is True  # critical-only
    assert can(state, "draft") is False

    state = {"circuit_breaker": "suspended"}
    assert can(state, "classify") is False
    assert can(state, "remediate") is False
    assert can(state, "escalate") is False


def test_demotion_low_success_rate(tmp_dirs):
    ot_dir, _ = tmp_dirs
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "full-autonomy"
    state["rolling_remediation_success_rate"] = 0.50
    state["total_remediations"] = 10

    config_cb = {"classification_accuracy_floor": 0.70, "min_resolved_for_evaluation": 5}
    new = evaluate_demotions(state, config_cb)
    assert new == "classify-only"


def test_demotion_negative_net_effect(tmp_dirs):
    ot_dir, _ = tmp_dirs
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "full-autonomy"
    state["net_remediation_effect"] = -0.3
    state["total_remediations"] = 10

    config_cb = {"classification_accuracy_floor": 0.70, "min_resolved_for_evaluation": 5}
    new = evaluate_demotions(state, config_cb)
    assert new == "observe-only"


def test_demotion_provider_errors():
    state = dict(DEFAULT_STATE)
    state["consecutive_provider_errors"] = 3
    config_cb = {"min_resolved_for_evaluation": 5}
    new = evaluate_demotions(state, config_cb)
    assert new == "suspended"


def test_no_demotion_insufficient_data():
    state = dict(DEFAULT_STATE)
    state["rolling_remediation_success_rate"] = 0.50
    state["total_remediations"] = 2  # Below min_resolved

    config_cb = {"classification_accuracy_floor": 0.70, "min_resolved_for_evaluation": 5}
    new = evaluate_demotions(state, config_cb)
    assert new is None


def test_promotion_classify_to_full(tmp_dirs):
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "classify-only"
    state["human_approved_promotion"] = True
    state["rolling_remediation_success_rate"] = 0.85

    config_cb = {"recovery_threshold": 0.80}
    new = evaluate_promotions(state, config_cb)
    assert new == "full-autonomy"


def test_promotion_without_human_approval():
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "classify-only"
    state["rolling_remediation_success_rate"] = 0.85

    config_cb = {"recovery_threshold": 0.80}
    new = evaluate_promotions(state, config_cb)
    assert new is None  # No human approval


def test_promotion_suspended_to_observe():
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "suspended"
    state["human_approved_promotion"] = True

    config_cb = {"recovery_threshold": 0.80}
    new = evaluate_promotions(state, config_cb)
    assert new == "observe-only"


def test_transition_records_history():
    state = dict(DEFAULT_STATE)
    state = transition(state, "classify-only", "low success rate")
    assert state["circuit_breaker"] == "classify-only"
    assert len(state["demotion_history"]) == 1
    assert state["demotion_history"][0]["from"] == "full-autonomy"
    assert state["demotion_history"][0]["to"] == "classify-only"
    assert state["human_approved_promotion"] is False


def test_load_corrupted_state(tmp_dirs):
    ot_dir, _ = tmp_dirs
    (ot_dir / "state.json").write_text("not json!!")
    state = load_state(ot_dir)
    assert state["circuit_breaker"] == "suspended"


def test_load_missing_state(tmp_dirs):
    ot_dir, _ = tmp_dirs
    state = load_state(ot_dir)
    assert state["circuit_breaker"] == "suspended"


def test_update_metrics():
    state = dict(DEFAULT_STATE)
    outcomes = ["success", "success", "failure", "success"]
    state = update_metrics(state, outcomes)
    assert state["rolling_remediation_success_rate"] == 0.75
    assert state["net_remediation_effect"] == 0.5


def test_run_circuit_breaker_demotion(tmp_dirs):
    ot_dir, _ = tmp_dirs
    state = dict(DEFAULT_STATE)
    state["rolling_remediation_success_rate"] = 0.50
    state["total_remediations"] = 10
    write_state(ot_dir, state)

    config_cb = {
        "classification_accuracy_floor": 0.70,
        "recovery_threshold": 0.80,
        "min_resolved_for_evaluation": 5,
    }
    state, alerts = run_circuit_breaker(state, config_cb, ot_dir)
    assert state["circuit_breaker"] == "classify-only"
    assert len(alerts) == 1
    assert alerts[0]["type"] == "circuit_breaker_change"


def test_simultaneous_demotions_most_restrictive():
    """When both low success rate AND negative net effect fire, pick most restrictive."""
    state = dict(DEFAULT_STATE)
    state["circuit_breaker"] = "full-autonomy"
    state["rolling_remediation_success_rate"] = 0.50
    state["net_remediation_effect"] = -0.2
    state["total_remediations"] = 10

    config_cb = {"classification_accuracy_floor": 0.70, "min_resolved_for_evaluation": 5}
    new = evaluate_demotions(state, config_cb)
    assert new == "observe-only"  # More restrictive than classify-only
