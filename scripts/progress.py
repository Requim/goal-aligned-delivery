"""创建、更新、检查和显式升级可恢复的 Markdown 进度记录。"""

import sys

sys.dont_write_bytecode = True

import argparse
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from delivery_io import (
    ConditionError,
    UsageError,
    atomic_create,
    atomic_write,
    file_hash,
    locked,
    read_json,
)


AUTHORITY_MARKER = "<!-- goal-delivery-progress:authoritative-json:v1 -->"
CHECK_STATUSES = {"passed", "failed", "blocked", "unverified"}
UNIT_STATUSES = {
    "pending", "in_progress", "awaiting_verification",
    "completed", "blocked", "cancelled",
}
OVERALL_STATUSES = {
    "in_progress", "awaiting_acceptance", "blocked",
    "paused", "completed", "cancelled",
}
OPERATION_STATUSES = {"prepared", "executing", "unknown", "succeeded", "failed"}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{label}: expected nonempty text")


def _fields(value, expected, label, optional=()):
    if not isinstance(value, dict):
        raise UsageError(f"{label}: expected object")
    expected = set(expected)
    optional = set(optional)
    if not expected.issubset(value) or set(value) - expected - optional:
        raise UsageError(f"{label}: unexpected or missing fields")


def _text_list(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise UsageError(f"{label}: expected {'nonempty ' if nonempty else ''}list")
    for index, item in enumerate(value):
        _text(item, f"{label}[{index}]")


def _enum(value, allowed, label):
    if not isinstance(value, str) or value not in allowed:
        raise UsageError(f"{label}: invalid value")


def _timestamp(value, label):
    _text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UsageError(f"{label}: expected ISO-8601 timestamp") from error
    if parsed.utcoffset() is None:
        raise UsageError(f"{label}: timezone is required")
    return parsed


def _sha256(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise UsageError(f"{label}: expected SHA-256")
    if any(char not in "0123456789abcdef" for char in value):
        raise UsageError(f"{label}: expected lowercase hexadecimal")


def _safe_artifact_path(value, root, label):
    _text(value, label)
    relative = Path(value)
    windows = PureWindowsPath(value)
    if relative.is_absolute() or windows.drive or windows.root:
        raise UsageError(f"{label}: path must be relative")
    if ".." in relative.parts or ".." in windows.parts or ":" in value:
        raise UsageError(f"{label}: traversal or stream is not allowed")
    root = Path(root).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise UsageError(f"{label}: path resolves outside record root")
    return resolved


def _observation(value, label, statuses):
    if value is None:
        return None
    _fields(
        value,
        {
            "attempt_id", "attempt_sequence", "status",
            "observed_at", "summary", "evidence",
        },
        label,
    )
    _text(value["attempt_id"], f"{label}.attempt_id")
    if type(value["attempt_sequence"]) is not int or value["attempt_sequence"] <= 0:
        raise UsageError(f"{label}.attempt_sequence: expected positive integer")
    _enum(value["status"], statuses, f"{label}.status")
    _timestamp(value["observed_at"], f"{label}.observed_at")
    _text(value["summary"], f"{label}.summary")
    _text_list(value["evidence"], f"{label}.evidence", nonempty=True)
    return value


def _attempt(value, label):
    if value is None:
        return None
    _fields(value, {"attempt_id", "sequence", "started_at"}, label)
    _text(value["attempt_id"], f"{label}.attempt_id")
    if type(value["sequence"]) is not int or value["sequence"] <= 0:
        raise UsageError(f"{label}.sequence: expected positive integer")
    _timestamp(value["started_at"], f"{label}.started_at")
    return value


def _validate_operation(operation, index):
    label = f"operations[{index}]"
    _fields(
        operation,
        {
            "operation_id", "target", "target_revision", "authorization",
            "idempotency_key", "status", "evidence", "query_observation",
            "authorization_still_valid", "attempt", "recovery_required",
        },
        label,
        optional={"result_observation"},
    )
    for field in ("operation_id", "target", "authorization", "idempotency_key"):
        _text(operation[field], f"{label}.{field}")
    target_revision = operation["target_revision"]
    if not ((isinstance(target_revision, str) and target_revision.strip()) or
            (type(target_revision) is int and target_revision > 0)):
        raise UsageError(f"{label}.target_revision: expected nonempty text or positive integer")
    _enum(operation["status"], OPERATION_STATUSES, f"{label}.status")
    _text_list(operation["evidence"], f"{label}.evidence")
    if type(operation["authorization_still_valid"]) is not bool:
        raise UsageError(f"{label}.authorization_still_valid: expected boolean")
    if type(operation["recovery_required"]) is not bool:
        raise UsageError(f"{label}.recovery_required: expected boolean")
    attempt = _attempt(operation["attempt"], f"{label}.attempt")
    query = _observation(
        operation["query_observation"],
        f"{label}.query_observation",
        {"applied", "not_applied", "query_failed"},
    )
    result = operation.get("result_observation")
    result = _observation(result, f"{label}.result_observation", {"succeeded", "failed"})
    status = operation["status"]
    if attempt is None:
        if status != "prepared" or query is not None or result is not None:
            raise UsageError(f"{label}: only initial prepared state may omit attempt")
    else:
        for observation, observation_label in (
            (query, "query_observation"),
            (result, "result_observation"),
        ):
            if observation is None:
                continue
            if (observation["attempt_id"] != attempt["attempt_id"] or
                    observation["attempt_sequence"] != attempt["sequence"]):
                raise UsageError(f"{label}.{observation_label}: attempt mismatch")
            if (_timestamp(observation["observed_at"], f"{label}.{observation_label}.observed_at") <
                    _timestamp(attempt["started_at"], f"{label}.attempt.started_at")):
                raise UsageError(f"{label}.{observation_label}: predates attempt")
    if status in {"executing", "unknown"} and not operation["evidence"]:
        raise UsageError(f"{label}: interrupted or executing operation needs evidence")
    if status == "prepared":
        if operation["recovery_required"]:
            raise UsageError(f"{label}: prepared operation cannot require recovery")
        if result is not None or (query is not None and query["status"] != "not_applied"):
            raise UsageError(f"{label}: prepared retry needs only not_applied query evidence")
        if attempt is None and query is not None:
            raise UsageError(f"{label}: initial prepared state cannot contain query evidence")
        if attempt is not None:
            if query is None or query["status"] != "not_applied":
                raise UsageError(f"{label}: prepared retry needs matching not_applied query")
            if not operation["authorization_still_valid"]:
                raise UsageError(f"{label}: prepared retry needs valid authorization")
    elif status == "executing":
        if attempt is None or query is not None or result is not None:
            raise UsageError(f"{label}: executing state cannot contain result observations")
    elif status == "unknown":
        if attempt is None or not operation["recovery_required"]:
            raise UsageError(f"{label}: unknown state must require recovery")
        if result is not None or (query is not None and query["status"] != "query_failed"):
            raise UsageError(f"{label}: unresolved query must remain query_failed")
    elif status == "succeeded":
        if attempt is None or operation["recovery_required"]:
            raise UsageError(f"{label}: succeeded state cannot require recovery")
        if query is not None and query["status"] != "applied":
            raise UsageError(f"{label}: succeeded cannot contain unresolved query evidence")
        direct = result is not None and result["status"] == "succeeded"
        recovered = query is not None and query["status"] == "applied"
        if not direct and not recovered:
            raise UsageError(f"{label}: succeeded requires direct result or applied query evidence")
    elif status == "failed":
        if attempt is None or operation["recovery_required"]:
            raise UsageError(f"{label}: failed state cannot require recovery")
        if result is None or result["status"] != "failed" or query is not None:
            raise UsageError(f"{label}: failed requires direct failure evidence")


def _dependency_cycle(units):
    graph = {unit["id"]: unit["depends_on"] for unit in units}
    visiting = set()
    visited = set()

    def visit(identifier):
        if identifier in visiting:
            return True
        if identifier in visited:
            return False
        visiting.add(identifier)
        if any(visit(dependency) for dependency in graph[identifier]):
            return True
        visiting.remove(identifier)
        visited.add(identifier)
        return False

    return any(visit(identifier) for identifier in graph)


def validate_state(state, root, *, verify_artifacts=True):
    """验证结构和可客观检查的完成条件，不认证业务语义正确。"""
    _fields(
        state,
        {
            "schema_version", "revision", "task_id", "title", "goal",
            "constraints", "baseline_inputs", "acceptance_criteria",
            "overall_status", "units", "blockers", "next_steps",
            "decisions", "operations", "updated_at",
        },
        "state",
    )
    if type(state["schema_version"]) is not int or state["schema_version"] != 1:
        raise UsageError("state.schema_version: unsupported version")
    if type(state["revision"]) is not int or state["revision"] <= 0:
        raise UsageError("state.revision: expected positive integer")
    for field in ("task_id", "title", "goal"):
        _text(state[field], f"state.{field}")
    _timestamp(state["updated_at"], "state.updated_at")
    _text_list(state["constraints"], "state.constraints")
    _text_list(state["next_steps"], "state.next_steps")
    _enum(state["overall_status"], OVERALL_STATUSES, "state.overall_status")

    if not isinstance(state["baseline_inputs"], list):
        raise UsageError("state.baseline_inputs: expected list")
    for index, item in enumerate(state["baseline_inputs"]):
        _fields(item, {"path", "summary"}, f"baseline_inputs[{index}]")
        _text(item["path"], f"baseline_inputs[{index}].path")
        _text(item["summary"], f"baseline_inputs[{index}].summary")

    criteria = state["acceptance_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise UsageError("state.acceptance_criteria: expected nonempty list")
    criterion_ids = set()
    for index, criterion in enumerate(criteria):
        _fields(criterion, {"id", "description"}, f"acceptance_criteria[{index}]")
        _text(criterion["id"], f"acceptance_criteria[{index}].id")
        _text(criterion["description"], f"acceptance_criteria[{index}].description")
        if criterion["id"] in criterion_ids:
            raise UsageError(f"duplicate acceptance criterion: {criterion['id']}")
        criterion_ids.add(criterion["id"])

    units = state["units"]
    if not isinstance(units, list) or not units:
        raise UsageError("state.units: expected nonempty list")
    unit_ids = set()
    covered_criteria = set()
    passed_criteria = set()
    required_incomplete = []
    for index, unit in enumerate(units):
        label = f"units[{index}]"
        _fields(
            unit,
            {
                "id", "title", "required", "status", "depends_on",
                "artifacts", "acceptance_results",
            },
            label,
        )
        _text(unit["id"], f"{label}.id")
        _text(unit["title"], f"{label}.title")
        if unit["id"] in unit_ids:
            raise UsageError(f"duplicate unit: {unit['id']}")
        unit_ids.add(unit["id"])
        if type(unit["required"]) is not bool:
            raise UsageError(f"{label}.required: expected boolean")
        _enum(unit["status"], UNIT_STATUSES, f"{label}.status")
        _text_list(unit["depends_on"], f"{label}.depends_on")
        if len(set(unit["depends_on"])) != len(unit["depends_on"]):
            raise UsageError(f"{label}.depends_on: duplicate dependency")
        if unit["required"] and unit["status"] != "completed":
            required_incomplete.append(unit["id"])

        artifacts = unit["artifacts"]
        if not isinstance(artifacts, list):
            raise UsageError(f"{label}.artifacts: expected list")
        for artifact_index, artifact in enumerate(artifacts):
            artifact_label = f"{label}.artifacts[{artifact_index}]"
            _fields(artifact, {"path", "sha256", "summary"}, artifact_label)
            resolved = _safe_artifact_path(artifact["path"], root, f"{artifact_label}.path")
            _sha256(artifact["sha256"], f"{artifact_label}.sha256")
            _text(artifact["summary"], f"{artifact_label}.summary")
            if verify_artifacts:
                if not resolved.is_file():
                    raise ConditionError(f"artifact missing: {artifact['path']}")
                if file_hash(resolved) != artifact["sha256"]:
                    raise ConditionError(f"artifact digest mismatch: {artifact['path']}")

        results = unit["acceptance_results"]
        if not isinstance(results, list):
            raise UsageError(f"{label}.acceptance_results: expected list")
        local_criteria = set()
        for result_index, result in enumerate(results):
            result_label = f"{label}.acceptance_results[{result_index}]"
            _fields(result, {"criterion_id", "status", "summary", "evidence"}, result_label)
            _text(result["criterion_id"], f"{result_label}.criterion_id")
            if result["criterion_id"] not in criterion_ids:
                raise UsageError(f"{result_label}: unknown acceptance criterion")
            if result["criterion_id"] in local_criteria:
                raise UsageError(f"{result_label}: duplicate criterion in unit")
            local_criteria.add(result["criterion_id"])
            covered_criteria.add(result["criterion_id"])
            _enum(result["status"], CHECK_STATUSES, f"{result_label}.status")
            _text(result["summary"], f"{result_label}.summary")
            _text_list(result["evidence"], f"{result_label}.evidence")
            if result["status"] == "passed":
                if not result["evidence"]:
                    raise UsageError(f"{result_label}: passed result needs evidence")
                if unit["status"] == "completed":
                    passed_criteria.add(result["criterion_id"])
        if unit["status"] == "completed":
            if not artifacts or not results:
                raise ConditionError(f"completed unit lacks artifacts or acceptance: {unit['id']}")
            if any(result["status"] != "passed" for result in results):
                raise ConditionError(f"completed unit has non-passing acceptance: {unit['id']}")

    for unit in units:
        for dependency in unit["depends_on"]:
            if dependency not in unit_ids:
                raise UsageError(f"unit {unit['id']}: unknown dependency {dependency}")
            if dependency == unit["id"]:
                raise UsageError(f"unit {unit['id']}: self dependency")
    if _dependency_cycle(units):
        raise UsageError("unit dependency cycle detected")
    units_by_id = {unit["id"]: unit for unit in units}
    for unit in units:
        if unit["status"] != "completed":
            continue
        incomplete = [dependency for dependency in unit["depends_on"]
                      if units_by_id[dependency]["status"] != "completed"]
        if incomplete:
            raise ConditionError(
                f"completed unit has non-completed dependencies: {unit['id']} -> {incomplete}")
    missing_coverage = criterion_ids - covered_criteria
    if missing_coverage:
        raise UsageError(f"acceptance criteria lack unit coverage: {sorted(missing_coverage)}")

    blockers = state["blockers"]
    if not isinstance(blockers, list):
        raise UsageError("state.blockers: expected list")
    blocker_ids = set()
    open_blockers = []
    for index, blocker in enumerate(blockers):
        label = f"blockers[{index}]"
        _fields(blocker, {"id", "summary", "unit_ids", "status"}, label)
        _text(blocker["id"], f"{label}.id")
        _text(blocker["summary"], f"{label}.summary")
        if blocker["id"] in blocker_ids:
            raise UsageError(f"duplicate blocker: {blocker['id']}")
        blocker_ids.add(blocker["id"])
        _text_list(blocker["unit_ids"], f"{label}.unit_ids")
        if set(blocker["unit_ids"]) - unit_ids:
            raise UsageError(f"{label}: unknown unit")
        _enum(blocker["status"], {"open", "resolved"}, f"{label}.status")
        if blocker["status"] == "open":
            open_blockers.append(blocker["id"])

    decisions = state["decisions"]
    if not isinstance(decisions, list):
        raise UsageError("state.decisions: expected list")
    for index, decision in enumerate(decisions):
        label = f"decisions[{index}]"
        _fields(decision, {"timestamp", "summary", "reason", "affected_units"}, label)
        _timestamp(decision["timestamp"], f"{label}.timestamp")
        _text(decision["summary"], f"{label}.summary")
        _text(decision["reason"], f"{label}.reason")
        _text_list(decision["affected_units"], f"{label}.affected_units")
        if set(decision["affected_units"]) - unit_ids:
            raise UsageError(f"{label}: unknown affected unit")

    operations = state["operations"]
    if not isinstance(operations, list):
        raise UsageError("state.operations: expected list")
    operation_ids = set()
    idempotency_keys = set()
    unresolved_operations = []
    query_failed = False
    for index, operation in enumerate(operations):
        _validate_operation(operation, index)
        if operation["operation_id"] in operation_ids:
            raise UsageError(f"duplicate operation: {operation['operation_id']}")
        operation_ids.add(operation["operation_id"])
        if operation["idempotency_key"] in idempotency_keys:
            raise UsageError(f"duplicate idempotency key: {operation['idempotency_key']}")
        idempotency_keys.add(operation["idempotency_key"])
        if operation["status"] in {"executing", "unknown"}:
            unresolved_operations.append(operation["operation_id"])
        query = operation["query_observation"]
        query_failed = query_failed or (query is not None and query["status"] == "query_failed")
    if query_failed and not open_blockers:
        raise UsageError("failed external query must be represented by an open blocker")

    if state["overall_status"] == "completed":
        unmet = []
        if required_incomplete:
            unmet.append(f"required units incomplete: {required_incomplete}")
        if criterion_ids - passed_criteria:
            unmet.append(f"acceptance not passed: {sorted(criterion_ids - passed_criteria)}")
        if open_blockers:
            unmet.append(f"open blockers: {open_blockers}")
        if unresolved_operations:
            unmet.append(f"external operations unresolved: {unresolved_operations}")
        if unmet:
            raise ConditionError("; ".join(unmet))
    return state


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise UsageError(f"invalid JSON constant: {value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise UsageError(f"non-finite JSON number: {value}")
    return number


def _markdown_lines(text):
    return text.replace("\r\n", "\n").split("\n")


def load_record(path):
    raw = Path(path).read_text(encoding="utf-8-sig")
    marker_lines = [
        line for line in _markdown_lines(raw)
        if line.strip() == AUTHORITY_MARKER
    ]
    if len(marker_lines) != 1:
        raise UsageError("record must contain exactly one authority marker")
    pattern = re.compile(
        r"^[ \t]*" + re.escape(AUTHORITY_MARKER) +
        r"[ \t]*\r?\n```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*(?:\r?\n|$)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(raw)
    if match is None:
        raise UsageError("authority marker must be followed by a fenced JSON block")
    try:
        return json.loads(
            match.group(1),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except json.JSONDecodeError as error:
        raise UsageError(f"invalid authoritative JSON: {error.msg}") from error


def _md(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_record(state):
    status_names = {
        "in_progress": "进行中", "awaiting_acceptance": "待验收",
        "blocked": "阻塞", "paused": "已暂停", "completed": "已完成",
        "cancelled": "已取消", "pending": "待开始",
        "awaiting_verification": "待验证",
    }
    check_names = {
        "passed": "通过", "failed": "失败", "blocked": "阻塞",
        "unverified": "未验证",
    }
    lines = [
        f"# {_md(state['title'])}", "",
        f"- 任务标识：`{_md(state['task_id'])}`",
        f"- 修订版本：`{state['revision']}`",
        f"- 最近更新：`{_md(state['updated_at'])}`",
        f"- 目标：{_md(state['goal'])}", "",
        "## 当前状态", "",
        f"整体状态：{status_names.get(state['overall_status'], state['overall_status'])}", "",
        "## 约束", "",
    ]
    lines.extend(f"- {_md(item)}" for item in state["constraints"])
    if not state["constraints"]:
        lines.append("- 无")
    lines.extend(["", "## 输入基线", ""])
    lines.extend(
        f"- `{_md(item['path'])}`：{_md(item['summary'])}"
        for item in state["baseline_inputs"]
    )
    if not state["baseline_inputs"]:
        lines.append("- 无")
    lines.extend(["", "## 验收项", ""])
    lines.extend(
        f"- `{_md(item['id'])}`：{_md(item['description'])}"
        for item in state["acceptance_criteria"]
    )
    lines.extend([
        "", "## 交付单元", "",
        "| 单元 | 必需 | 状态 | 依赖 | 产物 | 验收 |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for unit in state["units"]:
        artifacts = ", ".join(item["path"] for item in unit["artifacts"]) or "尚未产出"
        results = ", ".join(
            f"{item['criterion_id']}={check_names.get(item['status'], item['status'])}"
            for item in unit["acceptance_results"]
        ) or "无"
        lines.append(
            f"| `{_md(unit['id'])}` {_md(unit['title'])} | "
            f"{'是' if unit['required'] else '否'} | "
            f"{status_names.get(unit['status'], unit['status'])} | "
            f"{_md(', '.join(unit['depends_on']) or '无')} | {_md(artifacts)} | {_md(results)} |"
        )
    lines.extend(["", "## 阻塞与未决事项", ""])
    lines.extend(
        f"- `{_md(item['id'])}` [{_md(item['status'])}] {_md(item['summary'])}"
        for item in state["blockers"]
    )
    if not state["blockers"]:
        lines.append("- 无")
    lines.extend(["", "## 下一步", ""])
    lines.extend(f"- {_md(item)}" for item in state["next_steps"])
    if not state["next_steps"]:
        lines.append("- 无")
    lines.extend(["", "## 关键决策", ""])
    lines.extend(
        f"- `{_md(item['timestamp'])}` {_md(item['summary'])}：{_md(item['reason'])}"
        for item in state["decisions"]
    )
    if not state["decisions"]:
        lines.append("- 无")
    lines.extend(["", "## 外部操作账目", ""])
    lines.extend(
        f"- `{_md(item['operation_id'])}` {_md(item['target'])} "
        f"[{_md(item['status'])}] idempotency=`{_md(item['idempotency_key'])}`"
        for item in state["operations"]
    )
    if not state["operations"]:
        lines.append("- 无")
    serialized = json.dumps(state, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    lines.extend(["", AUTHORITY_MARKER, "```json", serialized, "```", ""])
    return "\n".join(lines)


def _operations_by_id(state):
    return {operation["operation_id"]: operation for operation in state["operations"]}


def _validate_imported_operations(state):
    for operation in state["operations"]:
        if operation["status"] == "executing" and not operation["recovery_required"]:
            raise UsageError(
                f"operation {operation['operation_id']}: imported executing state must require recovery"
            )


def _validate_transition(old, new):
    old_operations = _operations_by_id(old)
    new_operations = _operations_by_id(new)
    if set(old_operations) - set(new_operations):
        raise UsageError("existing operations cannot be removed")
    immutable = {
        "operation_id", "target", "target_revision", "authorization", "idempotency_key",
    }
    for identifier, current in old_operations.items():
        proposed = new_operations[identifier]
        if any(current[field] != proposed[field] for field in immutable):
            raise UsageError(f"operation {identifier}: immutable identity changed")
        prior_evidence = current["evidence"]
        next_evidence = proposed["evidence"]
        if next_evidence[:len(prior_evidence)] != prior_evidence:
            raise UsageError(f"operation {identifier}: evidence is append-only")
        before = current["status"]
        after = proposed["status"]
        if before == after:
            if proposed["attempt"] != current["attempt"]:
                raise UsageError(f"operation {identifier}: attempt changed without new execution")
            if proposed["recovery_required"] != current["recovery_required"]:
                marking_recovery = (
                    before == "executing" and
                    not current["recovery_required"] and
                    proposed["recovery_required"] and
                    len(next_evidence) > len(prior_evidence)
                )
                if not marking_recovery:
                    raise UsageError(f"operation {identifier}: invalid recovery flag change")
            continue
        allowed = {
            "prepared": {"executing"},
            "executing": {"unknown", "succeeded", "failed", "prepared"},
            "unknown": {"succeeded", "prepared"},
            "succeeded": set(),
            "failed": set(),
        }
        if after not in allowed[before]:
            raise UsageError(f"operation {identifier}: invalid transition {before} -> {after}")
        query = proposed["query_observation"]
        result = proposed.get("result_observation")
        if before == "prepared" and after == "executing":
            if len(next_evidence) <= len(prior_evidence):
                raise UsageError(f"operation {identifier}: execution intent needs new evidence")
            prior_attempt = current["attempt"]
            next_attempt = proposed["attempt"]
            expected_sequence = 1 if prior_attempt is None else prior_attempt["sequence"] + 1
            if next_attempt is None or next_attempt["sequence"] != expected_sequence:
                raise UsageError(f"operation {identifier}: execution needs next attempt sequence")
            if (prior_attempt is not None and
                    next_attempt["attempt_id"] == prior_attempt["attempt_id"]):
                raise UsageError(f"operation {identifier}: execution needs a new attempt id")
            prior_query = current["query_observation"]
            if (prior_query is not None and
                    _timestamp(next_attempt["started_at"], "attempt.started_at") <
                    _timestamp(prior_query["observed_at"], "query.observed_at")):
                raise UsageError(f"operation {identifier}: attempt starts before retry query")
            if proposed["recovery_required"]:
                raise UsageError(f"operation {identifier}: new live attempt cannot require recovery")
        elif proposed["attempt"] != current["attempt"]:
            raise UsageError(f"operation {identifier}: attempt changed outside execution start")
        if before == "executing" and after == "unknown":
            if not proposed["recovery_required"]:
                raise UsageError(f"operation {identifier}: unknown state must require recovery")
        if before in {"executing", "unknown"} and after == "succeeded":
            direct_success = (
                before == "executing" and not current["recovery_required"] and
                result is not None and
                result["status"] == "succeeded"
            )
            recovered_success = query is not None and query["status"] == "applied"
            if not direct_success and not recovered_success:
                raise UsageError(
                    f"operation {identifier}: recovery success needs applied query evidence"
                )
        if before == "executing" and after == "failed":
            if (current["recovery_required"] or result is None or
                    result["status"] != "failed"):
                raise UsageError(f"operation {identifier}: failure needs direct result evidence")
        if before in {"executing", "unknown"} and after == "prepared":
            not_applied = query is not None and query["status"] == "not_applied"
            if not not_applied or proposed["authorization_still_valid"] is not True:
                raise UsageError(
                    f"operation {identifier}: retry needs not_applied query and valid authorization"
                )
    for identifier in set(new_operations) - set(old_operations):
        operation = new_operations[identifier]
        if (operation["status"] != "prepared" or operation["attempt"] is not None or
                operation["query_observation"] is not None or
                operation.get("result_observation") is not None or
                operation["recovery_required"]):
            raise UsageError(f"new operation {identifier}: must start prepared")


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _parser():
    parser = _JSONArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "upgrade"):
        command = subparsers.add_parser(name)
        command.add_argument("path")
        command.add_argument("--state", required=True)
        command.add_argument("--root")
        command.add_argument("--no-write", action="store_true")
    update = subparsers.add_parser("update")
    update.add_argument("path")
    update.add_argument("--state", required=True)
    update.add_argument("--expected-revision", required=True, type=int)
    update.add_argument("--root")
    update.add_argument("--no-write", action="store_true")
    check = subparsers.add_parser("check")
    check.add_argument("path")
    check.add_argument("--root")
    return parser


def _output(status, **values):
    print(json.dumps({"status": status, **values}, ensure_ascii=True, sort_keys=True))


def _record_root(target, explicit_root):
    root = Path(explicit_root).resolve() if explicit_root else target.parent.resolve()
    if root.exists() and not root.is_dir():
        raise UsageError("root must be a directory")
    if not target.resolve().is_relative_to(root):
        raise UsageError("record path must be inside root")
    return root


def _init(args):
    target = Path(args.path)
    root = _record_root(target, args.root)
    state = read_json(args.state)
    validate_state(state, root)
    _validate_imported_operations(state)
    if state["revision"] != 1:
        raise UsageError("init requires revision 1")
    if args.no_write:
        _output("validated", written=False, revision=1)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with locked(target):
        if os.path.lexists(target):
            raise ConditionError("target already exists")
        try:
            atomic_create(target, render_record(state))
        except FileExistsError as error:
            raise ConditionError("target already exists") from error
    _output("initialized", written=True, revision=1, path=str(target))
    return 0


def _update(args):
    target = Path(args.path)
    root = _record_root(target, args.root)
    proposed = read_json(args.state)
    if not isinstance(proposed, dict):
        raise UsageError("state: expected object")

    def prepare(current):
        validate_state(current, root, verify_artifacts=False)
        if current["revision"] != args.expected_revision:
            raise ConditionError(
                f"revision conflict: expected {args.expected_revision}, actual {current['revision']}"
            )
        if proposed.get("revision") != args.expected_revision:
            raise UsageError("state revision must equal expected revision; the tool increments it")
        if proposed.get("task_id") != current["task_id"]:
            raise UsageError("task_id is immutable")
        validate_state(proposed, root)
        _validate_transition(current, proposed)
        updated = dict(proposed)
        updated["revision"] = current["revision"] + 1
        validate_state(updated, root)
        return updated

    if args.no_write:
        updated = prepare(load_record(target))
        _output("validated", written=False, revision=updated["revision"])
        return 0
    with locked(target):
        updated = prepare(load_record(target))
        atomic_write(target, render_record(updated))
    _output("updated", written=True, revision=updated["revision"], path=str(target))
    return 0


def _check(args):
    target = Path(args.path)
    root = _record_root(target, args.root)
    state = load_record(target)
    validate_state(state, root)
    _output(
        "valid",
        revision=state["revision"],
        task_id=state["task_id"],
        semantically_certified=False,
    )
    return 0


def _upgrade(args):
    target = Path(args.path)
    root = _record_root(target, args.root)
    state = read_json(args.state)
    validate_state(state, root)
    _validate_imported_operations(state)
    if state["revision"] != 1:
        raise UsageError("upgrade requires caller-supplied revision 1 state")
    if not target.is_file():
        raise ConditionError("legacy record does not exist")
    if args.no_write:
        _output("validated", written=False, revision=1)
        return 0
    backup = target.with_name(target.name + ".pre-upgrade")
    with locked(target):
        original = target.read_bytes().decode("utf-8")
        if any(line.strip() == AUTHORITY_MARKER for line in _markdown_lines(original)):
            raise ConditionError("record is already upgraded")
        if os.path.lexists(backup):
            if not backup.is_file():
                raise ConditionError(f"backup path is occupied: {backup}")
            existing = backup.read_bytes().decode("utf-8")
            if existing != original:
                raise ConditionError(f"backup already exists with different content: {backup}")
        else:
            try:
                atomic_create(backup, original)
            except FileExistsError:
                if not backup.is_file():
                    raise ConditionError(f"backup path is occupied: {backup}")
                existing = backup.read_bytes().decode("utf-8")
                if existing != original:
                    raise ConditionError(
                        f"backup already exists with different content: {backup}"
                    )
        atomic_write(target, render_record(state))
    _output(
        "upgraded", written=True, revision=1,
        path=str(target), backup=str(backup),
    )
    return 0


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        return {
            "init": _init,
            "update": _update,
            "check": _check,
            "upgrade": _upgrade,
        }[args.command](args)
    except ConditionError as error:
        _output("not_met", errors=[str(error)])
        return 1
    except (
        UsageError, OSError, UnicodeError, RecursionError,
        TypeError, AttributeError, KeyError,
    ) as error:
        _output("invalid", errors=[str(error)])
        return 2


if __name__ == "__main__":
    sys.exit(main())
