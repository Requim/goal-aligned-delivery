"""只读校验验收证据的覆盖、状态、版本与摘要，不证明业务语义正确。"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath


class InvalidInput(ValueError):
    pass


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{label}: expected nonempty text")


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise InvalidInput(f"{label}: unexpected or missing fields")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise InvalidInput(f"invalid JSON constant: {value}")


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise InvalidInput("JSON input exceeds 2 MiB")
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                      parse_constant=reject_constant)
    return data, hashlib.sha256(raw).hexdigest()


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            hasher.update(block)
    return hasher.hexdigest()


def valid_digest(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidInput(f"{label}: expected SHA-256")
    if any(char not in "0123456789abcdef" for char in value):
        raise InvalidInput(f"{label}: expected lowercase hexadecimal")


def state_map(value, label):
    if not isinstance(value, dict) or not value:
        raise InvalidInput(f"{label}: expected nonempty version map")
    for key, version in value.items():
        text(key, label)
        text(version, f"{label}.{key}")


def timestamp(value):
    text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidInput("timestamp: expected ISO-8601 with timezone") from error
    if parsed.utcoffset() is None:
        raise InvalidInput("timestamp: timezone is required")
    return parsed.astimezone(timezone.utc)


def freshness_errors(check, now, max_age_seconds, not_before):
    raw = check.get("verified_at")
    if raw is None:
        return ["freshness evidence missing"] if max_age_seconds is not None or not_before else []
    verified = timestamp(raw)
    errors = []
    if verified > now:
        errors.append("verification time is in the future")
    if max_age_seconds is not None and (now - verified).total_seconds() > max_age_seconds:
        errors.append("verification evidence is too old")
    if not_before is not None and verified < not_before:
        errors.append("verification predates required change boundary")
    return errors


def validate_policy(now, max_age_seconds, not_before):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise InvalidInput("now must be timezone-aware")
    if max_age_seconds is not None:
        if type(max_age_seconds) is not int or max_age_seconds <= 0:
            raise InvalidInput("max_age_seconds must be a positive integer")
    if not_before is not None:
        if not isinstance(not_before, datetime) or not_before.utcoffset() is None:
            raise InvalidInput("not_before must be timezone-aware")


def validate_contract(contract):
    fields(contract, "schema_version task_id goal state criteria", "contract")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise InvalidInput("contract: unsupported schema_version")
    text(contract["task_id"], "task_id")
    text(contract["goal"], "goal")
    state_map(contract["state"], "state")
    criteria = contract["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise InvalidInput("criteria: expected nonempty list")
    by_id = {}
    for item in criteria:
        fields(item, "id description environment", "criterion")
        for key in item:
            text(item[key], f"criterion.{key}")
        if item["id"] in by_id:
            raise InvalidInput(f"duplicate criterion: {item['id']}")
        by_id[item["id"]] = item
    return by_id


def validate_evidence(evidence):
    fields(evidence, "schema_version task_id contract_sha256 observed_state checks",
           "evidence")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] not in (1, 2):
        raise InvalidInput("evidence: unsupported schema_version")
    text(evidence["task_id"], "evidence.task_id")
    valid_digest(evidence["contract_sha256"], "contract_sha256")
    state_map(evidence["observed_state"], "observed_state")
    if not isinstance(evidence["checks"], list):
        raise InvalidInput("checks: expected list")
    by_id = {}
    check_fields = "criterion_id status environment observation artifacts"
    if evidence["schema_version"] == 2:
        check_fields += " verified_at"
    for check in evidence["checks"]:
        fields(check, check_fields, "check")
        if evidence["schema_version"] == 2:
            timestamp(check["verified_at"])
        for key in ("criterion_id", "status", "environment", "observation"):
            text(check[key], f"check.{key}")
        if check["status"] not in {"passed", "failed", "blocked", "unverified"}:
            raise InvalidInput(f"invalid status: {check['status']}")
        if not isinstance(check["artifacts"], list):
            raise InvalidInput("artifacts: expected list")
        if check["criterion_id"] in by_id:
            raise InvalidInput(f"duplicate check: {check['criterion_id']}")
        by_id[check["criterion_id"]] = check
    return by_id


def artifact_errors(artifact, root):
    fields(artifact, "path sha256", "artifact")
    text(artifact["path"], "artifact.path")
    valid_digest(artifact["sha256"], "artifact.sha256")
    relative = Path(artifact["path"])
    windows = PureWindowsPath(artifact["path"])
    if relative.is_absolute() or windows.drive or windows.root:
        raise InvalidInput("artifact path must be relative")
    if ".." in relative.parts or ".." in windows.parts or ":" in artifact["path"]:
        raise InvalidInput("artifact path traversal or stream is not allowed")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise InvalidInput("artifact resolves outside evidence root")
    if not resolved.is_file():
        return [f"artifact missing: {artifact['path']}"]
    if digest(resolved) != artifact["sha256"]:
        return [f"artifact digest mismatch: {artifact['path']}"]
    return []


def check_errors(criterion, check, root):
    identifier = criterion["id"]
    if check is None:
        return [f"{identifier}: missing check"]
    errors = []
    if check["status"] != "passed":
        errors.append(f"{identifier}: status={check['status']}")
    if check["environment"] != criterion["environment"]:
        errors.append(f"{identifier}: environment mismatch")
    if not check["artifacts"]:
        errors.append(f"{identifier}: no evidence artifact")
    for artifact in check["artifacts"]:
        errors.extend(f"{identifier}: {error}" for error in artifact_errors(artifact, root))
    return errors


def verify(contract, evidence, contract_hash, root, *, current_state=None,
           now=None, max_age_seconds=None, not_before=None):
    """验证固定契约对应的证据；调用方仍须运行独立业务测试。"""
    now = datetime.now(timezone.utc) if now is None else now
    validate_policy(now, max_age_seconds, not_before)
    criteria = validate_contract(contract)
    checks = validate_evidence(evidence)
    if set(checks) - set(criteria):
        raise InvalidInput("evidence contains unknown criterion")
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise InvalidInput("evidence root must be a directory")
    errors = []
    if contract["task_id"] != evidence["task_id"]:
        errors.append("task identity mismatch")
    if evidence["contract_sha256"] != contract_hash:
        errors.append("contract digest mismatch")
    if evidence["observed_state"] != contract["state"]:
        errors.append("state mismatch: revalidate affected criteria")
    if current_state is not None:
        state_map(current_state, "current_state")
        if current_state != contract["state"]:
            errors.append("current state differs from accepted baseline")
    for identifier, criterion in criteria.items():
        errors.extend(check_errors(criterion, checks.get(identifier), root))
        if identifier in checks:
            issues = freshness_errors(checks[identifier], now, max_age_seconds, not_before)
            errors.extend(f"{identifier}: {issue}" for issue in issues)
    return errors


def main(argv=None):
    """命令行入口：0 表示证据一致，1 表示未通过，2 表示输入错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract")
    parser.add_argument("evidence")
    parser.add_argument("--root", required=True)
    parser.add_argument("--current-state", help="path to independently sampled version map")
    parser.add_argument("--max-age-seconds", type=int)
    parser.add_argument("--not-before", help="last relevant change, ISO-8601 with timezone")
    args = parser.parse_args(argv)
    try:
        contract, contract_hash = read_json(args.contract)
        evidence, _ = read_json(args.evidence)
        current = read_json(args.current_state)[0] if args.current_state else None
        cutoff = timestamp(args.not_before) if args.not_before else None
        errors = verify(contract, evidence, contract_hash, args.root,
                        current_state=current, max_age_seconds=args.max_age_seconds,
                        not_before=cutoff)
    except (OSError, ValueError, RecursionError) as error:
        print(json.dumps({"status": "invalid", "errors": [str(error)]}, ensure_ascii=True))
        return 2
    status = "not_passed" if errors else "evidence_consistent"
    print(json.dumps({"status": status, "errors": errors}, ensure_ascii=True))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
