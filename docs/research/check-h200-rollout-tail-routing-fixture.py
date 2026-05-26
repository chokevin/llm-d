#!/usr/bin/env python3

import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_SCHEMA_VERSION = "h200-rollout-tail-routing-fixture.v0"
EXPECTED_CASE_IDS = [
    "h200-long-4096",
    "h200-capped-2048",
    "h200-short-700",
    "h200-straggler-2048",
]
EXPECTED_TWO_LANE_POLICY = "proxyguard8-two-lane"
LONG_LANE = "predicted-long"
SHORT_LANE = "short-normal"

REQUEST_FIELDS = {
    "request_id",
    "arrival_ms",
    "model",
    "prompt_tokens",
    "requested_output_cap",
    "predicted_output_tokens_bucket",
    "shape_hint",
    "slo_class",
}
POLICY_FIELDS = {
    "name",
    "version",
    "scorer_weights",
    "fallback_policy",
    "fallback_reason",
}
CANDIDATE_FIELDS = {
    "endpoint_id",
    "rank_id",
    "metrics_age_ms",
    "running_requests",
    "waiting_requests",
    "kv_cache_utilization",
    "decode_tps",
    "completed_rps",
    "recent_request_wall_p95_ms",
    "projected_remaining_decode_tokens",
    "score",
    "filtered_reason",
}
DECISION_FIELDS = {
    "selected_endpoint_id",
    "selected_rank_id",
    "selected_score",
    "baseline_least_inflight_endpoint_id",
    "rejected_near_tie_endpoint_ids",
    "reason",
}
OUTCOME_FIELDS = {
    "actual_output_tokens",
    "cap_saturated",
    "queue_ms",
    "decode_ms",
    "request_wall_ms",
    "success",
    "quality_or_truncation_signal",
}
TWO_LANE_ITEM_FIELDS = {
    "group_id",
    "shape_hint",
    "predicted_output_tokens",
    "lane",
    "assigned_endpoint_id",
    "assigned_rank_id",
    "projected_load_before",
    "projected_load_after",
    "score_components",
    "expected_decision",
}
TWO_LANE_SCORE_FIELDS = {
    "projected_load",
    "predicted_load",
    "throughput_penalty",
    "proxy_guardrail_penalty",
}
PROMOTION_BUDGET_FIELDS = {
    "max_tail_wall_improvement_pct_min",
    "runtime_improvement_pct_min",
    "completed_rps_regression_pct_max",
    "short_p95_regression_pct_max",
}
SUMMARY_METRIC_FIELDS = {
    "tail_wall_improvement_pct",
    "runtime_improvement_pct",
    "completed_rps_regression_pct",
    "short_p95_regression_pct",
}
EXPECTED_LONG_DECISIONS = {
    "select-predicted-long-lane-and-update-projected-load",
    "spread-predicted-long-to-different-rank",
    "spread-predicted-long-after-short-lane-isolation",
}
EXPECTED_SHORT_DECISIONS = {
    "keep-short-normal-on-throughput-lane",
}
EXPECTED_TWO_LANE_OUTCOME = "promote-two-lane-policy-for-predicted-long-traffic-only"


def load_fixture(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        with path.open(encoding="utf-8") as fixture_file:
            data = json.load(fixture_file)
    except FileNotFoundError:
        return None, [f"{path}: fixture file does not exist"]
    except PermissionError:
        return None, [f"{path}: fixture file is not readable"]
    except json.JSONDecodeError as exc:
        return None, [f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}"]

    if not isinstance(data, dict):
        return None, [f"{path}: top-level JSON value must be an object"]
    return data, []


def require_fields(
    value: dict[str, Any],
    required_fields: set[str],
    label: str,
    errors: list[str],
) -> None:
    missing = sorted(required_fields - value.keys())
    if missing:
        errors.append(f"{label}: missing fields: {', '.join(missing)}")


def require_object(value: Any, label: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label}: must be an object")
        return None
    return value


def require_number(value: Any, label: str, errors: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label}: must be a number")
        return None
    return float(value)


def validate_record(record: Any, index: int, errors: list[str]) -> None:
    label = f"records[{index}]"
    record_obj = require_object(record, label, errors)
    if record_obj is None:
        return

    require_fields(
        record_obj,
        {"case_id", "request", "policy", "candidates", "decision", "outcome", "replay_assertion"},
        label,
        errors,
    )
    case_id = record_obj.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        errors.append(f"{label}.case_id: must be a non-empty string")

    request = require_object(record_obj.get("request"), f"{label}.request", errors)
    policy = require_object(record_obj.get("policy"), f"{label}.policy", errors)
    decision = require_object(record_obj.get("decision"), f"{label}.decision", errors)
    outcome = require_object(record_obj.get("outcome"), f"{label}.outcome", errors)

    if request is not None:
        require_fields(request, REQUEST_FIELDS, f"{label}.request", errors)
    if policy is not None:
        require_fields(policy, POLICY_FIELDS, f"{label}.policy", errors)
        if not isinstance(policy.get("scorer_weights"), dict):
            errors.append(f"{label}.policy.scorer_weights: must be an object")
    if decision is not None:
        require_fields(decision, DECISION_FIELDS, f"{label}.decision", errors)
    if outcome is not None:
        require_fields(outcome, OUTCOME_FIELDS, f"{label}.outcome", errors)

    candidates = record_obj.get("candidates")
    if not isinstance(candidates, list) or len(candidates) < 2:
        errors.append(f"{label}.candidates: must contain at least two candidate endpoints")
        return

    candidate_by_endpoint: dict[str, dict[str, Any]] = {}
    for candidate_index, candidate in enumerate(candidates):
        candidate_label = f"{label}.candidates[{candidate_index}]"
        candidate_obj = require_object(candidate, candidate_label, errors)
        if candidate_obj is None:
            continue
        require_fields(candidate_obj, CANDIDATE_FIELDS, candidate_label, errors)
        endpoint_id = candidate_obj.get("endpoint_id")
        if isinstance(endpoint_id, str) and endpoint_id:
            candidate_by_endpoint[endpoint_id] = candidate_obj
        require_number(candidate_obj.get("running_requests"), f"{candidate_label}.running_requests", errors)
        require_number(candidate_obj.get("score"), f"{candidate_label}.score", errors)

    if decision is None or request is None or outcome is None:
        return

    selected_endpoint = decision.get("selected_endpoint_id")
    baseline_endpoint = decision.get("baseline_least_inflight_endpoint_id")
    if selected_endpoint not in candidate_by_endpoint:
        errors.append(f"{label}.decision.selected_endpoint_id: must reference a candidate endpoint")
    if baseline_endpoint not in candidate_by_endpoint:
        errors.append(f"{label}.decision.baseline_least_inflight_endpoint_id: must reference a candidate endpoint")

    selected_candidate = candidate_by_endpoint.get(selected_endpoint)
    if selected_candidate is not None:
        selected_score = require_number(decision.get("selected_score"), f"{label}.decision.selected_score", errors)
        candidate_score = require_number(selected_candidate.get("score"), f"{label}.selected_candidate.score", errors)
        if selected_score is not None and candidate_score is not None and selected_score != candidate_score:
            errors.append(f"{label}.decision.selected_score: must match selected candidate score")

    running_by_endpoint = {
        endpoint: candidate["running_requests"]
        for endpoint, candidate in candidate_by_endpoint.items()
        if isinstance(candidate.get("running_requests"), (int, float)) and not isinstance(candidate.get("running_requests"), bool)
    }
    if running_by_endpoint and baseline_endpoint in running_by_endpoint:
        least_running = min(running_by_endpoint.values())
        if running_by_endpoint[baseline_endpoint] != least_running:
            errors.append(f"{label}.decision.baseline_least_inflight_endpoint_id: must reference a least-running candidate")

    actual_output_tokens = require_number(outcome.get("actual_output_tokens"), f"{label}.outcome.actual_output_tokens", errors)
    requested_output_cap = require_number(request.get("requested_output_cap"), f"{label}.request.requested_output_cap", errors)
    cap_saturated = outcome.get("cap_saturated")
    if actual_output_tokens is not None and requested_output_cap is not None:
        if actual_output_tokens > requested_output_cap:
            errors.append(f"{label}.outcome.actual_output_tokens: must not exceed requested_output_cap")
        if isinstance(cap_saturated, bool) and cap_saturated != (actual_output_tokens >= requested_output_cap):
            errors.append(f"{label}.outcome.cap_saturated: must match actual_output_tokens >= requested_output_cap")
        elif not isinstance(cap_saturated, bool):
            errors.append(f"{label}.outcome.cap_saturated: must be a boolean")

    replay_assertion = record_obj.get("replay_assertion")
    if not isinstance(replay_assertion, str) or not replay_assertion.strip():
        errors.append(f"{label}.replay_assertion: must be a non-empty string")


def target_key(item: dict[str, Any]) -> str | None:
    endpoint_id = item.get("assigned_endpoint_id")
    rank_id = item.get("assigned_rank_id")
    if not isinstance(endpoint_id, str) or not endpoint_id:
        return None
    if not isinstance(rank_id, str) or not rank_id:
        return None
    return f"{endpoint_id}|{rank_id}"


def validate_two_lane_acceptance(value: Any, errors: list[str]) -> None:
    acceptance = require_object(value, "two_lane_acceptance", errors)
    if acceptance is None:
        return

    require_fields(
        acceptance,
        {
            "policy_name",
            "source",
            "promotion_budget",
            "rank_identity_required",
            "batch",
            "summary_metrics",
            "expected_outcome",
        },
        "two_lane_acceptance",
        errors,
    )
    if acceptance.get("policy_name") != EXPECTED_TWO_LANE_POLICY:
        errors.append(f"two_lane_acceptance.policy_name: expected {EXPECTED_TWO_LANE_POLICY!r}")
    source = acceptance.get("source")
    if not isinstance(source, str) or "not a benchmark" not in source.lower():
        errors.append("two_lane_acceptance.source: must state that the fixture is not a benchmark result")
    if acceptance.get("rank_identity_required") is not True:
        errors.append("two_lane_acceptance.rank_identity_required: must be true")
    if acceptance.get("expected_outcome") != EXPECTED_TWO_LANE_OUTCOME:
        errors.append(f"two_lane_acceptance.expected_outcome: expected {EXPECTED_TWO_LANE_OUTCOME!r}")

    promotion_budget = require_object(
        acceptance.get("promotion_budget"),
        "two_lane_acceptance.promotion_budget",
        errors,
    )
    summary_metrics = require_object(
        acceptance.get("summary_metrics"),
        "two_lane_acceptance.summary_metrics",
        errors,
    )
    if promotion_budget is not None:
        require_fields(
            promotion_budget,
            PROMOTION_BUDGET_FIELDS,
            "two_lane_acceptance.promotion_budget",
            errors,
        )
    if summary_metrics is not None:
        require_fields(
            summary_metrics,
            SUMMARY_METRIC_FIELDS,
            "two_lane_acceptance.summary_metrics",
            errors,
        )

    batch = acceptance.get("batch")
    if not isinstance(batch, list) or len(batch) < 3:
        errors.append("two_lane_acceptance.batch: must contain at least three assignments")
        return

    long_targets: list[str] = []
    saw_short_lane = False
    previous_after: dict[str, Any] | None = None
    for index, item in enumerate(batch):
        label = f"two_lane_acceptance.batch[{index}]"
        item_obj = require_object(item, label, errors)
        if item_obj is None:
            continue
        require_fields(item_obj, TWO_LANE_ITEM_FIELDS, label, errors)

        predicted_output_tokens = require_number(
            item_obj.get("predicted_output_tokens"),
            f"{label}.predicted_output_tokens",
            errors,
        )
        before = require_object(item_obj.get("projected_load_before"), f"{label}.projected_load_before", errors)
        after = require_object(item_obj.get("projected_load_after"), f"{label}.projected_load_after", errors)
        score_components = require_object(item_obj.get("score_components"), f"{label}.score_components", errors)
        if score_components is not None:
            require_fields(score_components, TWO_LANE_SCORE_FIELDS, f"{label}.score_components", errors)
            predicted_load = require_number(
                score_components.get("predicted_load"),
                f"{label}.score_components.predicted_load",
                errors,
            )
            if predicted_load is not None and predicted_output_tokens is not None and predicted_load != predicted_output_tokens:
                errors.append(f"{label}.score_components.predicted_load: must match predicted_output_tokens")

        if previous_after is not None and before is not None and before != previous_after:
            errors.append(f"{label}.projected_load_before: must equal previous assignment projected_load_after")
        if after is not None:
            previous_after = after

        lane = item_obj.get("lane")
        expected_decision = item_obj.get("expected_decision")
        if not isinstance(expected_decision, str) or not expected_decision:
            errors.append(f"{label}.expected_decision: must be a non-empty string")
        key = target_key(item_obj)
        if lane == LONG_LANE:
            if expected_decision not in EXPECTED_LONG_DECISIONS:
                errors.append(f"{label}.expected_decision: must describe a predicted-long lane decision")
            if key is not None:
                long_targets.append(key)
            if before is not None and after is not None and key is not None and predicted_output_tokens is not None:
                before_value = require_number(before.get(key), f"{label}.projected_load_before[{key!r}]", errors)
                after_value = require_number(after.get(key), f"{label}.projected_load_after[{key!r}]", errors)
                if before_value is not None and after_value is not None:
                    expected_after = before_value + predicted_output_tokens
                    if after_value != expected_after:
                        errors.append(f"{label}.projected_load_after[{key!r}]: expected {expected_after:g}")
        elif lane == SHORT_LANE:
            if expected_decision not in EXPECTED_SHORT_DECISIONS:
                errors.append(f"{label}.expected_decision: must describe a short-normal lane decision")
            saw_short_lane = True
            if before is not None and after is not None and before != after:
                errors.append(f"{label}: short-normal lane must not consume guarded projected long-load budget")
        else:
            errors.append(f"{label}.lane: must be {LONG_LANE!r} or {SHORT_LANE!r}")

    if len(set(long_targets)) < 2:
        errors.append("two_lane_acceptance.batch: predicted-long assignments must spread across at least two endpoint/rank targets")
    if not saw_short_lane:
        errors.append("two_lane_acceptance.batch: must include at least one short-normal assignment")

    if promotion_budget is not None and summary_metrics is not None:
        tail_improvement = require_number(
            summary_metrics.get("tail_wall_improvement_pct"),
            "two_lane_acceptance.summary_metrics.tail_wall_improvement_pct",
            errors,
        )
        runtime_improvement = require_number(
            summary_metrics.get("runtime_improvement_pct"),
            "two_lane_acceptance.summary_metrics.runtime_improvement_pct",
            errors,
        )
        completed_rps_regression = require_number(
            summary_metrics.get("completed_rps_regression_pct"),
            "two_lane_acceptance.summary_metrics.completed_rps_regression_pct",
            errors,
        )
        short_p95_regression = require_number(
            summary_metrics.get("short_p95_regression_pct"),
            "two_lane_acceptance.summary_metrics.short_p95_regression_pct",
            errors,
        )
        tail_min = require_number(
            promotion_budget.get("max_tail_wall_improvement_pct_min"),
            "two_lane_acceptance.promotion_budget.max_tail_wall_improvement_pct_min",
            errors,
        )
        runtime_min = require_number(
            promotion_budget.get("runtime_improvement_pct_min"),
            "two_lane_acceptance.promotion_budget.runtime_improvement_pct_min",
            errors,
        )
        rps_regression_max = require_number(
            promotion_budget.get("completed_rps_regression_pct_max"),
            "two_lane_acceptance.promotion_budget.completed_rps_regression_pct_max",
            errors,
        )
        short_regression_max = require_number(
            promotion_budget.get("short_p95_regression_pct_max"),
            "two_lane_acceptance.promotion_budget.short_p95_regression_pct_max",
            errors,
        )
        if tail_improvement is not None and tail_min is not None and tail_improvement < tail_min:
            errors.append("two_lane_acceptance.summary_metrics.tail_wall_improvement_pct: below promotion budget")
        if runtime_improvement is not None and runtime_min is not None and runtime_improvement < runtime_min:
            errors.append("two_lane_acceptance.summary_metrics.runtime_improvement_pct: below promotion budget")
        if completed_rps_regression is not None and rps_regression_max is not None and completed_rps_regression > rps_regression_max:
            errors.append("two_lane_acceptance.summary_metrics.completed_rps_regression_pct: exceeds promotion budget")
        if short_p95_regression is not None and short_regression_max is not None and short_p95_regression > short_regression_max:
            errors.append("two_lane_acceptance.summary_metrics.short_p95_regression_pct: exceeds promotion budget")


def validate_fixture(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        errors.append(f"schema_version: expected {EXPECTED_SCHEMA_VERSION!r}")

    validate_two_lane_acceptance(data.get("two_lane_acceptance"), errors)

    source = data.get("source")
    if not isinstance(source, str) or "not a benchmark" not in source.lower():
        errors.append("source: must state that the fixture is not a benchmark result")

    records = data.get("records")
    if not isinstance(records, list):
        errors.append("records: must be a list")
        return errors

    actual_case_ids = [record.get("case_id") for record in records if isinstance(record, dict)]
    if actual_case_ids != EXPECTED_CASE_IDS:
        errors.append(f"records: expected case order {EXPECTED_CASE_IDS!r}, got {actual_case_ids!r}")

    for index, record in enumerate(records):
        validate_record(record, index, errors)

    return errors


def main() -> int:
    default_path = Path(__file__).with_name("h200-rollout-tail-routing-fixture.json")
    fixture_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    data, load_errors = load_fixture(fixture_path)
    if load_errors:
        for error in load_errors:
            print(error, file=sys.stderr)
        return 1

    assert data is not None
    errors = validate_fixture(data)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"{fixture_path}: fixture contract validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
