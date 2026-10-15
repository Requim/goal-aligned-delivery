"""Private task evaluation and controlled local skill improvement (standard library).

Shared data contains only allowlisted deidentified aggregates and generic lessons.
Private control root: WORKSPACE/work/skill-learning/goal-aligned-delivery.
Detailed evaluations: WORKSPACE/work/task-evaluations.
"""

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import time
import uuid

import regress
from delivery_io import canonical_hash, ConditionError, UsageError, file_hash, locked, read_json, write_json


FIXED_CASES = ("normal-delivery", "missing-data", "input-correction", "resume",
               "lightweight", "no-write", "external-interruption", "multi-skill")
PROTECTED_REFERENCES = {"authority.md", "learning.md", "behavior-cases.md",
                        "capabilities.md", "adapters.md"}
CORE_REFERENCES = {"execution.md", "progress.md", "contracts.md"}
BUDGET_SECONDS = 600
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
OPAQUE = re.compile(r"[0-9a-f]{32}\Z")
METRICS = ("tool_calls", "subagent_calls", "tokens")
ELAPSED = ("task_execution", "workflow_maintenance", "learning_validation")


def _need(value, message):
    if not value:
        raise UsageError(message)
    return value


def _slug(value, label):
    _need(isinstance(value, str) and SLUG.fullmatch(value), f"{label} must be a generic lowercase category/id")
    return value


def _id(value):
    _need(isinstance(value, str) and OPAQUE.fullmatch(value), "invalid opaque ID")
    return value


def _plain(value, label):
    _need(isinstance(value, str) and value.strip(), f"{label} must be nonempty text")
    return value


def _object(value, label, required):
    _need(isinstance(value, dict), f"{label} must be an object")
    _need(set(required).issubset(value), f"{label} is missing required fields")
    return value


def _sha(value, label):
    _need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), f"invalid {label}")


def _number(value, label, *, integer=False):
    _need(type(value) in ((int,) if integer else (int, float)), f"invalid {label}")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise UsageError(f"invalid {label}") from error
    _need(finite and value >= 0, f"invalid {label}")


def _array(value, label):
    _need(isinstance(value, list), f"{label} must be an array")
    return value


def _manifest_state(value, label):
    for entry in _array(value, label):
        _object(entry, label + " entry", {"path", "sha256"})
        _plain(entry["path"], label + " path")
        _sha(entry["sha256"], label + " digest")


def _report_refs(value):
    for ref in _array(value, "validation report references"):
        _object(ref, "validation report reference", {"path", "sha256", "run_id"})
        _plain(ref["path"], "report path")
        _sha(ref["sha256"], "report digest")
        _plain(ref["run_id"], "report run ID")


def _current(private):
    value = _object(read_json(private / "current.json"), "current state", {"candidate_id"})
    _id(value["candidate_id"])
    return value


def _head_value(value):
    _object(value, "version head", {"candidate_id", "version"})
    _id(value["candidate_id"])
    _id(value["version"])
    return value


def _head(private):
    path = private / "head.json"
    return _head_value(read_json(path)) if path.exists() else None


def _entries(value):
    _need(_array(value, "transaction entries"), "transaction entries must be nonempty")
    for entry in value:
        _object(entry, "transaction entry", {"path", "before", "after"})
        _plain(entry["path"], "transaction path")
        if entry["before"] is not None:
            _sha(entry["before"], "before digest")
        _sha(entry["after"], "after digest")
        for key in ("temporary", "recovery_temporary"):
            if key in entry:
                _plain(entry[key], key)


def _journal(path, identifier):
    value = _object(read_json(path), "journal",
                    {"schema_version", "candidate_id", "version", "status", "entries", "validation_reports"})
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid journal schema")
    _need(value["candidate_id"] == identifier, "journal candidate mismatch")
    _id(value["version"])
    _need(isinstance(value["status"], str) and value["status"] in
          {"prepared", "applying", "applied", "rolling_back", "rolled_back", "conflict"}, "invalid journal status")
    _entries(value["entries"])
    _report_refs(value["validation_reports"])
    if "conflicts" in value:
        for path in _array(value["conflicts"], "journal conflicts"):
            _plain(path, "conflict path")
    return value


def _version(private, identifier):
    value = _object(read_json(_safe(private, "versions/" + _id(identifier) + ".json")), "version",
                    {"schema_version", "version", "candidate_id", "target", "original_skill_sha256",
                     "candidate_skill_sha256", "validation_reports", "entries", "previous"})
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid version schema")
    _need(value["version"] == identifier, "version identity mismatch")
    _id(value["candidate_id"])
    _plain(value["target"], "version target")
    _sha(value["original_skill_sha256"], "version baseline digest")
    _sha(value["candidate_skill_sha256"], "version candidate digest")
    _entries(value["entries"])
    _report_refs(value["validation_reports"])
    if value["previous"] is not None:
        _head_value(value["previous"])
    return value


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _clean_root(value):
    path = Path(value).absolute()
    for component in [path, *path.parents]:
        _need(not _linked(component), "symlink/junction roots are not allowed")
    return path.resolve()


def _safe(root, relative):
    _need(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
          "unsafe relative path")
    parts = relative.split("/")
    _need(all(part not in ("", ".", "..") and not part.endswith((" ", ".")) for part in parts),
          "unsafe path component")
    _need(not Path(relative).is_absolute(), "absolute paths are not allowed")
    _need(all(not re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts),
          "reserved filesystem name")
    path = root.joinpath(*parts)
    for component in [path, *path.parents]:
        if component == root.parent:
            break
        _need(not _linked(component), "symlink/junction paths are not allowed")
    _need(path.resolve().is_relative_to(root.resolve()), "path escapes root")
    return path


def _tree(root, *, case_root=False):
    """Physical protection deliberately includes cases/inputs/outputs and all fixtures."""
    _need(root.is_dir() and not _linked(root), "skill/case root missing or linked")
    entries = []
    names = set()
    for path in sorted(root.rglob("*")):
        _need(not _linked(path), "symlinks/junctions are not allowed")
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        if not (case_root or parts[0] == "cases") and any(p in {".git", "__pycache__"} for p in parts):
            continue
        _safe(root, relative)
        _need(relative.casefold() not in names, "case-insensitive path collision")
        names.add(relative.casefold())
        if path.is_file():
            entries.append({"path": relative, "sha256": file_hash(path)})
    return entries


def _metric(value=None):
    if value is None:
        return {"value": None, "source": "unavailable"}
    _need(isinstance(value, dict) and set(value) == {"value", "source"}, "metric needs value and source")
    number, source = value["value"], value["source"]
    _need(source in {"measured", "reported", "unavailable"}, "invalid metric source")
    if number is None:
        _need(source == "unavailable", "missing metric cannot be measured/reported")
    else:
        _number(number, "metric value")
        _need(source != "unavailable", "unavailable metric needs null")
    return {"value": number, "source": source}


def _metrics(value):
    _need(isinstance(value, dict), "metrics must be an object")
    _need(set(value) <= set(METRICS) | {"elapsed"}, "unknown metric")
    elapsed = value.get("elapsed", {})
    _need(isinstance(elapsed, dict) and set(elapsed) <= set(ELAPSED), "invalid elapsed categories")
    return {**{name: _metric(value.get(name)) for name in METRICS},
            "elapsed": {name: _metric(elapsed.get(name)) for name in ELAPSED}}


def _lesson(value):
    _need(isinstance(value, dict) and value.get("deidentified") is True and value.get("generalized") is True,
          "experience needs explicit deidentified=true and generalized=true")
    return _plain(value.get("text"), "generalized experience")


def _roots(args):
    shared = _clean_root(resolve_data_dir(args.data_dir))
    private = None
    if args.workspace:
        workspace = _clean_root(args.workspace)
        _need(not shared.is_relative_to(workspace) and not workspace.is_relative_to(shared),
              "shared data must be outside the original task workspace")
        private = workspace / "work" / "skill-learning" / "goal-aligned-delivery"
        _clean_root(private)
        _need(not shared.is_relative_to(private) and not private.is_relative_to(shared),
              "shared and private control directories must be disjoint")
    return shared, private


def resolve_data_dir(explicit=None, *, platform=None):
    """Select without creating or migrating anything; caller enforces no-write first."""
    if explicit is not None:
        _need(isinstance(explicit, str) and bool(explicit.strip()), "--data-dir cannot be empty")
        return Path(explicit).expanduser().resolve()
    platform = platform or sys.platform

    def environment_root(name):
        value = os.environ[name]
        _need(bool(value.strip()) and Path(value).is_absolute(),
              f"{name} must be a nonempty absolute path")
        return Path(value)

    if "GAD_DATA_DIR" in os.environ:
        return environment_root("GAD_DATA_DIR")
    legacy = Path.home() / "Documents/Codex/skill-data/goal-aligned-delivery"
    if legacy.exists():
        raise ConditionError(
            f"legacy data exists at {legacy}; select --data-dir {legacy} to continue, "
            "or explicitly choose another --data-dir; no data has been moved or written")
    if platform == "win32":
        root = environment_root("LOCALAPPDATA") if "LOCALAPPDATA" in os.environ else Path.home() / "AppData/Local"
    elif platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = environment_root("XDG_DATA_HOME") if "XDG_DATA_HOME" in os.environ else Path.home() / ".local/share"
    return root / "goal-aligned-delivery"


def _store(shared):
    path = shared / "experience.json"
    if path.exists():
        value = _object(read_json(path), "shared store", {"schema_version", "salt", "records", "lessons"})
        _need(set(value) == {"schema_version", "salt", "records", "lessons"}, "unknown shared fields")
        _need(type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid shared schema")
        _sha(value["salt"], "shared salt")
        for record in _array(value["records"], "shared records"):
            fields = {"record_id", "task_token", "category", "acceptance", "metrics", "explicit_correction",
                      "user_feedback_count", "inference_count"}
            _object(record, "shared record", fields)
            _need(set(record) == fields, "unknown shared record fields")
            _id(record["record_id"])
            _sha(record["task_token"], "task token")
            _slug(record["category"], "shared category")
            _need(isinstance(record["acceptance"], str) and record["acceptance"] in
                  {"passed", "failed", "blocked", "unverified"}, "invalid shared acceptance")
            _need(_metrics(record["metrics"]) == record["metrics"], "incomplete shared metrics")
            _need(type(record["explicit_correction"]) is bool, "invalid shared correction flag")
            for field in ("user_feedback_count", "inference_count"):
                _number(record[field], field, integer=True)
        for lesson in _array(value["lessons"], "shared lessons"):
            fields = {"record_id", "category", "text", "type"}
            _object(lesson, "shared lesson", fields)
            _need(set(lesson) == fields, "unknown shared lesson fields")
            _id(lesson["record_id"])
            _slug(lesson["category"], "lesson category")
            _plain(lesson["text"], "lesson text")
            _need(isinstance(lesson["type"], str) and lesson["type"] in
                  {"inference", "validated_experience"}, "invalid lesson type")
        return value
    return {"schema_version": 1, "salt": uuid.uuid4().hex + uuid.uuid4().hex, "records": [], "lessons": []}


def _live(report):
    return (report.get("mode") == "live" and report.get("simulated_executor") is False
            and report.get("identity", {}).get("model_config") not in (None, {}, "unknown"))


def _record(args, shared, private):
    _need(args.input, "record requires --input")
    value = read_json(args.input)
    _need(isinstance(value, dict), "record input must be an object")
    _plain(value.get("task_id"), "task_id")
    category = _slug(value.get("category"), "category")
    _need("validated_experience" not in value, "validated experience is derived from verified reports")
    metrics = _metrics(value.get("metrics", {}))
    feedback, inference = value.get("user_feedback", []), value.get("inference", [])
    _need(isinstance(feedback, list) and isinstance(inference, list), "feedback and inference must be arrays")
    for item in feedback:
        _need(isinstance(item, dict), "feedback must be an object")
        _plain(item.get("text"), "feedback text")
        _need(type(item.get("explicit_correction", False)) is bool, "explicit_correction must be boolean")
    for item in inference:
        _need(isinstance(item, dict), "inference must be an object")
        _plain(item.get("text"), "inference text")
    acceptance = value.get("acceptance", "unverified")
    _need(acceptance in {"passed", "failed", "blocked", "unverified"}, "invalid acceptance result")
    lesson = _lesson(value["experience"]) if value.get("experience") else None
    validation = []
    for report_path in value.get("validation_reports", []):
        report = regress.load_report(report_path)
        if (not _live(report) or report["status"] != "passed"
                or not regress.compare_reports(report_path, report_path)["identity_compatible"]):
            raise ConditionError("experience validation requires complete real live passed evidence")
        validation.append({"path": str(Path(report_path).resolve()), "sha256": file_hash(report_path)})
    identifier = uuid.uuid4().hex
    shared.mkdir(parents=True, exist_ok=True)
    evaluations = Path(args.workspace).resolve() / "work" / "task-evaluations"
    _clean_root(evaluations)
    evaluations.mkdir(parents=True, exist_ok=True)
    with locked(shared / "experience.json"):
        store = _store(shared)
        token = hashlib.sha256((store["salt"] + "\0" + value["task_id"]).encode()).hexdigest()
        detail = {**value, "schema_version": 1, "record_id": identifier, "task_token": token,
                  "metrics": metrics, "validation_evidence": validation,
                  "validated_experience": [lesson] if lesson and validation else []}
        write_json(evaluations / (identifier + ".json"), detail)
        store["records"].append({"record_id": identifier, "task_token": token, "category": category,
                                 "acceptance": acceptance, "metrics": metrics,
                                 "explicit_correction": any(f.get("explicit_correction") for f in feedback),
                                 "user_feedback_count": len(feedback), "inference_count": len(inference)})
        if lesson:
            store["lessons"].append({"record_id": identifier, "category": category, "text": lesson,
                                    "type": "validated_experience" if validation else "inference"})
        write_json(shared / "experience.json", store)
    return {"status": "recorded", "record_id": identifier, "detail_path": str(evaluations / (identifier + ".json"))}


def _summarize(args, shared):
    store = _store(shared)
    categories = sorted({r["category"] for r in store["records"]} |
                        {r["category"] for r in store["lessons"]})
    output = []
    for category in categories:
        if args.category and args.category != category:
            continue
        records = [r for r in store["records"] if r["category"] == category]
        lessons = [r for r in store["lessons"] if r["category"] == category]
        metrics = {}
        for name in (*METRICS, *("elapsed." + e for e in ELAPSED)):
            samples = [r["metrics"]["elapsed"][name.split(".")[1]] if name.startswith("elapsed.")
                       else r["metrics"][name] for r in records]
            metrics[name] = {source: sum(v["source"] == source for v in samples)
                             for source in ("measured", "reported", "unavailable")}
            numbers = [v["value"] for v in samples if v["value"] is not None]
            metrics[name].update(available=len(numbers), median=statistics.median(numbers) if numbers else None)
        output.append({"category": category, "records": len(records),
                       "independent_tasks": len({r["task_token"] for r in records}),
                       "failures": sum(r["acceptance"] == "failed" for r in records), "metrics": metrics,
                       "user_feedback": {"count": sum(r["user_feedback_count"] for r in records),
                                         "explicit_corrections": sum(r["explicit_correction"] for r in records)},
                       "inference": sorted({r["text"] for r in lessons if r["type"] == "inference"}),
                       "validated_experience": sorted({r["text"] for r in lessons if r["type"] == "validated_experience"})})
    return {"status": "summarized", "categories": output}


def _protected(text):
    sections = {}
    for name in ("AUTHORITY", "LEARNING"):
        begin, end = f"<!-- GAD:{name}:BEGIN -->", f"<!-- GAD:{name}:END -->"
        _need(text.count(begin) == text.count(end) == 1, f"SKILL.md needs exactly one protected {name} marker pair")
        start, finish = text.index(begin), text.index(end)
        _need(start < finish, "reversed protection markers")
        sections[name] = (start, finish + len(end), text[start:finish + len(end)])
    ordered = sorted(sections.values())
    _need(ordered[0][1] <= ordered[1][0], "nested or overlapping protection sections")
    return {key: value[2] for key, value in sections.items()}


def _change(root, item, original):
    _need(isinstance(item, dict) and set(item) <= {"path", "content", "source"}, "invalid change object")
    relative = item.get("path")
    path = _safe(root, relative)
    parts = relative.split("/")
    old_case_ids = {p.split("/")[1] for p in original if p.startswith("cases/")}
    if (root / "cases").is_dir():
        old_case_ids.update(p.name for p in (root / "cases").iterdir())
    allowed = relative == "SKILL.md"
    allowed |= len(parts) == 2 and parts[0] == "references" and parts[1].endswith(".md") and parts[1] not in PROTECTED_REFERENCES
    allowed |= len(parts) >= 3 and parts[0] == "cases" and bool(SLUG.fullmatch(parts[1])) and parts[1] not in old_case_ids
    _need(allowed, "change path is protected or outside automatic scope")
    _need(sum(key in item for key in ("content", "source")) == 1, "change needs content or source")
    text = item.get("content") if "content" in item else Path(item["source"]).read_text(encoding="utf-8")
    _need(isinstance(text, str) and len(text.encode("utf-8")) <= 2 * 1024 * 1024, "change content must be text <=2 MiB")
    if relative == "SKILL.md":
        _need(_protected(path.read_text(encoding="utf-8")) == _protected(text), "protected SKILL.md sections changed")
    return relative, text


def _candidate(private, identifier):
    directory = _safe(private, "candidates/" + _id(identifier))
    path = directory / "candidate.json"
    state = _object(read_json(path), "candidate",
                    {"schema_version", "candidate_id", "status", "target", "proposal", "category", "kind",
                     "lesson", "target_cases", "required_cases", "original_manifest", "candidate_manifest",
                     "original_skill_sha256", "candidate_skill_sha256", "original_change_manifest",
                     "candidate_change_manifest", "case_manifests", "new_cases", "case_definition_source",
                     "changes", "cycles"})
    _need(state["candidate_id"] == identifier and type(state["schema_version"]) is int
          and state["schema_version"] == 1, "candidate identity mismatch")
    _need(isinstance(state["status"], str) and state["status"] in
          {"staged", "pending", "applying", "finalizing", "applied", "rolled_back", "conflict"}, "invalid candidate status")
    _plain(state["target"], "candidate target")
    _plain(state["lesson"], "candidate lesson")
    _slug(state["category"], "candidate category")
    _need(isinstance(state["kind"], str) and state["kind"] in {"functional", "efficiency"}, "invalid candidate kind")
    _object(state["proposal"], "candidate proposal", {"kind", "category"})
    _sha(state["original_skill_sha256"], "baseline source digest")
    _sha(state["candidate_skill_sha256"], "candidate source digest")
    for field in ("target_cases", "required_cases", "changes"):
        _need(_array(state[field], field), field + " must be nonempty")
        for item in state[field]:
            _plain(item, field)
    _array(state["new_cases"], "new cases")
    _need(len(set(state["new_cases"])) == len(state["new_cases"]), "new cases must be unique")
    for case in state["new_cases"]:
        _slug(case, "new case")
        _need(case in state["required_cases"], "new case must be required")
    _manifest_state(state["original_manifest"], "baseline manifest")
    _manifest_state(state["candidate_manifest"], "candidate manifest")
    _manifest_state(state["original_change_manifest"], "baseline complete change manifest")
    _manifest_state(state["candidate_change_manifest"], "candidate complete change manifest")
    source = state["case_definition_source"]
    if state["new_cases"]:
        _object(source, "case definition source", {"path", "sha256", "case_ids"})
        _need(source["path"] == "case-definition-source.json", "case definition source path changed")
        _sha(source["sha256"], "case definition source digest")
        _need(source["case_ids"] == state["new_cases"], "case definition source coverage changed")
    else:
        _need(source is None, "case definition source is only valid for new cases")
    _object(state["case_manifests"], "case manifests", state["required_cases"])
    for manifest in state["case_manifests"].values():
        _manifest_state(manifest, "case manifest")
    _need(_array(state["cycles"], "cycles"), "cycles must be nonempty")
    for cycle in state["cycles"]:
        _object(cycle, "cycle", {"started_at", "elapsed_seconds", "checked_reports"})
        _number(cycle["started_at"], "cycle start")
        _number(cycle["elapsed_seconds"], "cycle elapsed")
        _report_refs(cycle["checked_reports"])
    if "version" in state:
        _id(state["version"])
    if "finalization_complete" in state:
        _need(type(state["finalization_complete"]) is bool, "invalid finalization flag")
    if "rollback_history" in state:
        for item in _array(state["rollback_history"], "rollback history"):
            _object(item, "rollback history entry", {"reason", "at", "conflicts"})
            _plain(item["reason"], "rollback reason")
            _number(item["at"], "rollback time")
            for conflict in _array(item["conflicts"], "rollback conflicts"):
                _plain(conflict, "rollback conflict")
    return directory, state


def _elapsed(state):
    return max(0, time.time() - state["cycles"][-1]["started_at"])


def _budget(state):
    if _elapsed(state) >= BUDGET_SECONDS:
        raise ConditionError("600 second cycle exhausted; explicitly use --new-cycle to continue the same candidate")


def _renew(directory, state):
    previous = state["cycles"][-1]
    previous["elapsed_seconds"] = _elapsed(state)
    _need(previous["elapsed_seconds"] >= BUDGET_SECONDS, "current cycle has not expired")
    state["cycles"].append({"started_at": time.time(), "elapsed_seconds": 0, "checked_reports": []})
    write_json(directory / "candidate.json", state)


def _stage(args, shared, private):
    if args.candidate:
        with locked(private / "control"):
            directory, state = _candidate(private, args.candidate)
            _need(args.new_cycle and not args.proposal and not args.skill,
                  "stage --candidate requires --new-cycle and no new proposal/skill")
            _need(state["status"] in {"staged", "pending"}, "candidate is no longer active")
            _renew(directory, state)
        return {"status": "staged", "candidate_id": state["candidate_id"], "private_path": str(directory),
                "required_cases": state["required_cases"]}
    _need(args.skill and args.proposal, "stage requires --skill and --proposal")
    _need(not args.new_cycle, "--new-cycle requires the existing --candidate")
    started = time.time()
    skill = _clean_root(args.skill)
    _need((skill / "SKILL.md").is_file(), "skill needs SKILL.md")
    _need(not private.is_relative_to(skill) and not skill.is_relative_to(private)
          and not shared.is_relative_to(skill) and not skill.is_relative_to(shared),
          "skill, private state and shared data must be disjoint")
    proposal = read_json(args.proposal)
    _need(isinstance(proposal, dict), "proposal must be an object")
    category = _slug(proposal.get("category"), "category")
    _need(proposal.get("kind") in {"functional", "efficiency"}, "kind must be functional or efficiency")
    lesson = _lesson(proposal.get("generalized_lesson"))
    refs = proposal.get("record_ids")
    _need(isinstance(refs, list) and refs, "proposal needs private record_ids")
    records = []
    evaluations = Path(args.workspace).resolve() / "work" / "task-evaluations"
    for identifier in refs:
        record = _object(read_json(_safe(evaluations, _id(identifier) + ".json")), "private evaluation",
                         {"category", "task_token"})
        _sha(record["task_token"], "private task token")
        for feedback in _array(record.get("user_feedback", []), "private user feedback"):
            _object(feedback, "private feedback", {"text"})
            _plain(feedback["text"], "private feedback text")
            _need(type(feedback.get("explicit_correction", False)) is bool, "invalid private correction flag")
        _need(record["category"] == category, "record category differs from proposal")
        records.append({"record_id": identifier, "evaluation": record})
    correction = any(
        f.get("explicit_correction") is True
        for item in records for f in item["evaluation"].get("user_feedback", []))
    if not correction and len({item["evaluation"]["task_token"] for item in records}) < 2:
        raise ConditionError("candidate needs explicit user correction or two independent tasks")
    targets = proposal.get("target_cases")
    _need(isinstance(targets, list) and targets and len(set(targets)) == len(targets), "target_cases must be unique and nonempty")
    for identifier in targets:
        _slug(identifier, "target case")
    original_manifest = _tree(skill)
    original = {e["path"]: e["sha256"] for e in original_manifest}
    _need(isinstance(proposal.get("changes"), list) and proposal["changes"], "proposal needs changes")
    changes = [_change(skill, item, original) for item in proposal["changes"]]
    _need(len({p.casefold() for p, _ in changes}) == len(changes), "duplicate change path")
    original_names = {name.casefold(): name for name in original}
    for path, _ in changes:
        _need(path.casefold() not in original_names or original_names[path.casefold()] == path, "path case bypass")
    full = any(path == "SKILL.md" or path in {"references/" + f for f in CORE_REFERENCES} for path, _ in changes)
    new_cases = sorted({path.split("/")[1] for path, _ in changes if path.startswith("cases/")})
    required = sorted(set(FIXED_CASES if full else ("lightweight", "no-write", "resume")) |
                      set(targets) | set(new_cases))
    private.mkdir(parents=True, exist_ok=True)
    with locked(private / "control"):
        current = private / "current.json"
        if current.exists():
            previous_directory, previous = _candidate(private, _current(private)["candidate_id"])
            previous_journal_path = previous_directory / "journal.json"
            if previous_journal_path.exists() and previous.get("finalization_complete") is not True:
                previous_journal = _journal(previous_journal_path, previous["candidate_id"])
                if previous_journal["status"] == "applied":
                    target = _clean_root(previous["target"])
                    with locked(target.parent / (target.name + ".gad-learning-control")):
                        if any(_hash_optional(_safe(target, e["path"])) != e["after"]
                               for e in previous_journal["entries"]):
                            raise ConditionError("committed target changed before pending finalization")
                        _finalize(private, shared, previous_directory, previous, previous_journal,
                                  Path(args.workspace).resolve())
            if previous["status"] in {"staged", "pending", "applying", "finalizing", "conflict"}:
                raise ConditionError("one active candidate per workspace; continue or rollback it")
        identifier = uuid.uuid4().hex
        directory = private / "candidates" / identifier
        (directory / "skill").mkdir(parents=True)
        (directory / "snapshot").mkdir()
        for entry in original_manifest:
            for sub in ("skill", "snapshot"):
                destination = _safe(directory / sub, entry["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_safe(skill, entry["path"]), destination)
                with destination.open("r+b") as copied:
                    os.fsync(copied.fileno())
        for relative, text in changes:
            destination = _safe(directory / "skill", relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            from delivery_io import atomic_write
            atomic_write(destination, text)
        if new_cases:
            regress.load_cases(directory / "skill" / "cases", new_cases)
        _need(_tree(skill) == original_manifest, "target changed while staging")
        case_manifests = {case: _tree(directory / "skill" / "cases" / case, case_root=True) for case in required}
        definition_source = None
        if new_cases:
            source_path = directory / "case-definition-source.json"
            write_json(source_path, {
                "schema_version": 1,
                "case_ids": new_cases,
                "category": category,
                "generalized_lesson": proposal["generalized_lesson"],
                "source_records": [{
                    "record_id": item["record_id"],
                    "evaluation_sha256": canonical_hash(item["evaluation"]),
                    "evaluation": item["evaluation"],
                } for item in records],
            })
            definition_source = {
                "path": source_path.name,
                "sha256": file_hash(source_path),
                "case_ids": new_cases,
            }
        state = {"schema_version": 1, "candidate_id": identifier, "status": "staged",
                 "target": str(skill), "proposal": proposal, "category": category, "kind": proposal["kind"],
                 "lesson": lesson, "target_cases": targets, "required_cases": required,
                 "original_manifest": original_manifest, "original_skill_sha256": regress.fingerprint(skill),
                 "candidate_manifest": _tree(directory / "skill"),
                 "candidate_skill_sha256": regress.fingerprint(directory / "skill"),
                 "original_change_manifest": regress.build_change_manifest(skill),
                 "candidate_change_manifest": regress.build_change_manifest(directory / "skill"),
                 "case_manifests": case_manifests, "new_cases": new_cases,
                 "case_definition_source": definition_source, "changes": [p for p, _ in changes],
                 "cycles": [{"started_at": started, "elapsed_seconds": 0, "checked_reports": []}]}
        write_json(directory / "candidate.json", state)
        write_json(current, {"candidate_id": identifier})
    return {"status": "staged" if _elapsed(state) < BUDGET_SECONDS else "pending",
            "candidate_id": identifier, "private_path": str(directory), "required_cases": required,
            "case_definition_source": (str(directory / definition_source["path"])
                                       if definition_source else None)}


def _check_drift(directory, state):
    if _tree(Path(state["target"])) != state["original_manifest"]:
        raise ConditionError("target changed since stage; candidate retained")
    if _tree(directory / "skill") != state["candidate_manifest"]:
        raise ConditionError("candidate physical contents changed; validation invalid")
    if regress.fingerprint(directory / "skill") != state["candidate_skill_sha256"]:
        raise ConditionError("candidate identity changed")
    if _tree(directory / "snapshot") != state["original_manifest"]:
        raise ConditionError("pre-apply snapshot is damaged")
    for root, field in ((Path(state["target"]), "original_change_manifest"),
                        (directory / "snapshot", "original_change_manifest"),
                        (directory / "skill", "candidate_change_manifest")):
        if regress.build_change_manifest(root) != state[field]:
            raise ConditionError("complete change identity differs from staged contents")
    if state["new_cases"]:
        source = state["case_definition_source"]
        path = _safe(directory, source["path"])
        if file_hash(path) != source["sha256"]:
            raise ConditionError("private case definition source changed")
        regress._load_case_definition_source(path, state["new_cases"])


def _promotion(directory, state, paths):
    if not isinstance(paths, list) or not paths:
        raise ConditionError("apply is pending complete --reports evidence")
    reports = []
    original_digest = canonical_hash(state["original_change_manifest"])
    candidate_digest = canonical_hash(state["candidate_change_manifest"])
    for path in paths:
        _budget(state)
        try:
            report = regress.load_report(path)
        except (UsageError, ConditionError, KeyError, OSError, TypeError) as error:
            raise ConditionError(f"report is incomplete or invalid: {error}") from error
        if not _live(report):
            raise ConditionError("promotion requires real live evidence; offline/simulated/unknown rejected")
        checkpoint = {"path": str(Path(path).resolve()), "sha256": file_hash(path), "run_id": report["run_id"]}
        checked = state["cycles"][-1]["checked_reports"]
        if checkpoint not in checked:
            checked.append(checkpoint)
        state["cycles"][-1]["elapsed_seconds"] = _elapsed(state)
        write_json(directory / "candidate.json", state)
        digest = report["identity"].get("change_sha256")
        if digest not in {original_digest, candidate_digest}:
            raise ConditionError("report source does not match staged baseline/candidate")
        role = "original" if digest == original_digest else "candidate"
        if (report.get("change_manifest_legacy") or
                report.get("change_manifest") != state[role + "_change_manifest"] or
                report["identity"]["skill_sha256"] != state[role + "_skill_sha256"]):
            raise ConditionError("report lacks the staged complete change identity")
        statuses = {case["case_id"]: case["status"] for case in report["cases"]}
        plan = {case["case_id"]: case for case in report["case_plan"]}
        for case in state["required_cases"]:
            if case not in statuses or statuses[case] == "unverified":
                raise ConditionError("report is missing required verified cases")
            if plan.get(case, {}).get("manifest") != state["case_manifests"][case]:
                raise ConditionError("case/oracle evidence differs from staged physical case sources")
        if state["new_cases"]:
            review = report.get("case_definition_review") or {}
            checks = review.get("checks", [])
            if (review.get("status") != "passed"
                    or review.get("case_ids") != state["new_cases"]
                    or review.get("case_manifests") != {
                        case: state["case_manifests"][case] for case in state["new_cases"]}
                    or review.get("source", {}).get("sha256") != state["case_definition_source"]["sha256"]
                    or report["identity"].get("case_definition_source_sha256") != state["case_definition_source"]["sha256"]
                    or (review.get("reviewer") or {}).get("status") != "completed"
                    or len(checks) != len(regress.CASE_DEFINITION_CRITERIA)
                    or {check.get("criterion_id") for check in checks} != {
                        item["id"] for item in regress.CASE_DEFINITION_CRITERIA}
                    or any(check.get("status") != "passed" or not check.get("evidence") for check in checks)):
                raise ConditionError("new cases need the staged passed independent definition review")
        if digest == candidate_digest:
            review = report.get("candidate_review") or {}
            if (report["status"] != "passed" or any(statuses[c] != "passed" for c in state["required_cases"])
                    or review.get("status") != "passed"
                    or review.get("baseline_skill_sha256") != state["original_skill_sha256"]
                    or review.get("candidate_skill_sha256") != state["candidate_skill_sha256"]
                    or review.get("baseline_change_sha256") != original_digest
                    or review.get("candidate_change_sha256") != candidate_digest
                    or (review.get("reviewer") or {}).get("status") != "completed" or not review.get("checks")
                    or any(not c.get("evidence") for c in review["checks"])):
                raise ConditionError("candidate needs complete passed bound independent boundary review and cases")
        reports.append((Path(path), report))
    baselines = [(p, r) for p, r in reports if r["identity"]["change_sha256"] == original_digest]
    candidates = [(p, r) for p, r in reports if r["identity"]["change_sha256"] == candidate_digest]
    baseline_minimum, candidate_minimum = (1, 2) if state["kind"] == "functional" else (3, 3)
    if len(baselines) < baseline_minimum or len(candidates) < candidate_minimum:
        raise ConditionError(f"{state['kind']} needs {baseline_minimum} baseline and {candidate_minimum} candidate independent samples")
    for index, (a, _) in enumerate(reports):
        for b, _ in reports[index + 1:]:
            _budget(state)
            result = regress.compare_reports(a, b)
            if not result["identity_compatible"] or not result["independent_runs"]:
                raise ConditionError("evidence identities differ or executions reuse run/cache/trace/thread")
    for base, _ in baselines:
        for candidate, _ in candidates:
            result = regress.compare_reports(base, candidate)
            if result["regressions"] or result["unverified"] or result["status"] != "passed":
                raise ConditionError("required quality comparison has regression or unverified behavior")
    if state["kind"] == "functional":
        for _, report in baselines:
            statuses = {c["case_id"]: c["status"] for c in report["cases"]}
            if any(statuses[c] != "failed" for c in state["target_cases"]):
                raise ConditionError("baseline target failure is not independently reproduced")
    else:
        metric = state["proposal"].get("efficiency_metric")
        _need(metric in {"elapsed_seconds", "tokens", "tool_calls"}, "efficiency needs a supported metric")
        if len(baselines) != len(candidates):
            raise ConditionError("efficiency evidence needs equal baseline/candidate pairs")
        totals = []
        for group in (baselines, candidates):
            samples = []
            for _, report in group:
                total = 0
                for case in report["cases"]:
                    if case["case_id"] not in state["required_cases"]:
                        continue
                    total += _measured_cost(case.get("metrics", {}).get(metric), metric)
                samples.append(total)
            totals.append(statistics.median(samples))
        if totals[1] >= totals[0]:
            raise ConditionError("candidate measured cost median did not decrease")
    _budget(state)
    refs = [{"path": str(p.resolve()), "sha256": file_hash(p), "run_id": r["run_id"]} for p, r in reports]
    state["cycles"][-1]["checked_reports"] = refs
    state["cycles"][-1]["elapsed_seconds"] = _elapsed(state)
    write_json(directory / "candidate.json", state)
    return refs


def _measured_cost(value, metric):
    """Read cost from metrics already recomputed and checked by regress.load_report."""
    expected = {"elapsed_seconds": {"monotonic-clock"},
                "tokens": {"turn.completed.usage", "claude.result.usage"},
                "tool_calls": {"item.completed", "claude.assistant.tool_use"}}[metric]
    if not isinstance(value, dict) or value.get("source") not in expected:
        raise ConditionError("efficiency metric lacks measured regress provenance")
    number = value.get("value")
    if metric == "tokens":
        if not isinstance(number, dict):
            raise ConditionError("token usage fields unavailable")
        if "total_tokens" in number:
            number = number["total_tokens"]
        elif "input_tokens" in number and "output_tokens" in number:
            # Cached input tokens are already included in input_tokens.
            number = number["input_tokens"] + number["output_tokens"]
        else:
            raise ConditionError("complete token usage unavailable")
    if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
        raise ConditionError("measured metric is missing or invalid")
    return number


def _hash_optional(path):
    if not path.exists():
        return None
    _need(path.is_file() and not _linked(path), "target file replaced by directory/link")
    return file_hash(path)


def _replace_target(path, source, *, temporary=None):
    """Publish a fully fsynced same-directory temporary; journal precedes this operation."""
    temporary = temporary or path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _recover(directory, state, journal, reason):
    target = _clean_root(state["target"])
    journal["status"] = "rolling_back"
    journal["rollback_reason"] = reason
    write_json(directory / "journal.json", journal)
    conflicts = []
    for entry in reversed(journal["entries"]):
        blocked_restore = False
        for field, expected in (("temporary", entry["after"]), ("recovery_temporary", entry["before"])):
            if entry.get(field):
                try:
                    temporary = _safe(target, entry[field])
                    if temporary.exists():
                        if _hash_optional(temporary) != expected:
                            conflicts.append(entry[field])
                            blocked_restore |= field == "recovery_temporary"
                        else:
                            temporary.unlink()
                except (UsageError, OSError):
                    conflicts.append(entry[field])
                    blocked_restore |= field == "recovery_temporary"
        if blocked_restore:
            continue
        try:
            destination = _safe(target, entry["path"])
            current = _hash_optional(destination)
        except (UsageError, OSError):
            conflicts.append(entry["path"])
            continue
        if current == entry["before"]:
            continue
        if current != entry["after"]:
            conflicts.append(entry["path"])
            continue
        if entry["before"] is None:
            destination.unlink()
        else:
            source = _safe(directory / "snapshot", entry["path"])
            if file_hash(source) != entry["before"]:
                raise ConditionError("recovery snapshot damaged")
            _replace_target(destination, source,
                            temporary=_safe(target, entry["recovery_temporary"]) if entry.get("recovery_temporary") else None)
        entry["restored"] = True
        write_json(directory / "journal.json", journal)
    journal["status"] = "conflict" if conflicts else "rolled_back"
    journal["conflicts"] = conflicts
    state["status"] = journal["status"]
    state.setdefault("rollback_history", []).append({"reason": reason, "at": time.time(), "conflicts": conflicts})
    write_json(directory / "journal.json", journal)
    write_json(directory / "candidate.json", state)
    return conflicts


def _publish_lesson(shared, state):
    shared.mkdir(parents=True, exist_ok=True)
    with locked(shared / "experience.json"):
        store = _store(shared)
        identifier = state["candidate_id"]
        if not any(r["record_id"] == identifier for r in store["lessons"]):
            store["lessons"].append({"record_id": identifier, "category": state["category"],
                                    "text": state["lesson"], "type": "validated_experience"})
            write_json(shared / "experience.json", store)


def _finalize(private, shared, directory, state, journal, workspace):
    state["status"] = "finalizing"
    state["finalization_complete"] = False
    state["version"] = journal["version"]
    write_json(directory / "candidate.json", state)
    versions = _safe(private, "versions")
    versions.mkdir(exist_ok=True)
    version = {"schema_version": 1, "version": journal["version"], "candidate_id": state["candidate_id"],
               "target": state["target"], "original_skill_sha256": state["original_skill_sha256"],
               "candidate_skill_sha256": state["candidate_skill_sha256"],
               "validation_reports": journal["validation_reports"], "entries": journal["entries"]}
    version_path = versions / (journal["version"] + ".json")
    head_path = private / "head.json"
    if version_path.exists():
        version = _version(private, journal["version"])
    else:
        version["previous"] = _head(private)
        write_json(version_path, version)
    write_json(head_path, {"version": journal["version"], "candidate_id": state["candidate_id"]})
    evaluations = workspace / "work" / "task-evaluations"
    _clean_root(evaluations)
    evaluations.mkdir(parents=True, exist_ok=True)
    write_json(evaluations / (state["candidate_id"] + "-promotion.json"),
               {**version, "acceptance": "passed", "validated_experience": [state["lesson"]],
                "candidate_private_path": str(directory)})
    _publish_lesson(shared, state)
    state["status"] = "applied"
    state["finalization_complete"] = True
    write_json(directory / "candidate.json", state)


def _apply(args, shared, private):
    _need(args.candidate, "apply requires --candidate")
    directory, state = _candidate(private, args.candidate)
    if _current(private)["candidate_id"] != state["candidate_id"]:
        raise ConditionError("apply must continue the current workspace candidate")
    journal_path = directory / "journal.json"
    target = _clean_root(state["target"])
    with locked(target.parent / (target.name + ".gad-learning-control")):
        if journal_path.exists():
            journal = _journal(journal_path, state["candidate_id"])
            if journal["status"] == "applied":
                # Commit was durable before a process could die publishing the generic lesson.
                if any(_hash_optional(_safe(target, e["path"])) != e["after"] for e in journal["entries"]):
                    raise ConditionError("applied target changed")
                _finalize(private, shared, directory, state, journal, Path(args.workspace).resolve())
                return {"status": "applied", "candidate_id": state["candidate_id"], "version": journal["version"]}
            if journal["status"] in {"prepared", "applying", "rolling_back", "conflict"}:
                conflicts = _recover(directory, state, journal, "recover interrupted local application")
                return {"status": "conflict" if conflicts else "recovered", "candidate_id": state["candidate_id"],
                        "conflicts": conflicts, "_exit": 1}
        _need(state["status"] in {"staged", "pending"}, "candidate is no longer active")
        if args.new_cycle:
            _renew(directory, state)
        _budget(state)
        _check_drift(directory, state)
        refs = _promotion(directory, state, args.reports)
        _budget(state)
        _check_drift(directory, state)
        before = {e["path"]: e["sha256"] for e in state["original_manifest"]}
        after = {e["path"]: e["sha256"] for e in state["candidate_manifest"]}
        entries = []
        for relative in state["changes"]:
            path = Path(relative)
            prefix = path.parent.as_posix()
            prefix = "" if prefix == "." else prefix + "/"
            token = uuid.uuid4().hex
            entries.append({"path": relative, "before": before.get(relative), "after": after[relative],
                            "temporary": prefix + "." + path.name + "." + token + ".apply-tmp",
                            "recovery_temporary": prefix + "." + path.name + "." + token + ".restore-tmp"})
        journal = {"schema_version": 1, "candidate_id": state["candidate_id"], "status": "prepared",
                   "version": uuid.uuid4().hex, "entries": entries, "validation_reports": refs}
        write_json(journal_path, journal)
        state["status"] = "applying"
        write_json(directory / "candidate.json", state)
        journal["status"] = "applying"
        write_json(journal_path, journal)
        for entry in entries:
            destination = _safe(target, entry["path"])
            if _hash_optional(destination) != entry["before"]:
                raise ConditionError("per-file compare-and-swap conflict; journal retained for recovery")
            source = _safe(directory / "skill", entry["path"])
            if file_hash(source) != entry["after"]:
                raise ConditionError("candidate changed during application; journal retained")
            try:
                _replace_target(destination, source, temporary=_safe(target, entry["temporary"]))
            except OSError as error:
                raise ConditionError(f"atomic application failed; persistent journal retained: {error}") from error
            entry["written"] = True
            write_json(journal_path, journal)
        if _tree(target) != state["candidate_manifest"]:
            raise ConditionError("target changed during application; journal retained for conflict-aware recovery")
        journal["status"] = "applied"
        write_json(journal_path, journal)
        _finalize(private, shared, directory, state, journal, Path(args.workspace).resolve())
    return {"status": "applied", "candidate_id": state["candidate_id"], "version": state["version"]}


def _rollback(args, shared, private):
    _plain(args.reason, "rollback --reason")
    current = _current(private)
    identifier = args.candidate or current["candidate_id"]
    head_path = private / "head.json"
    head = _head(private)
    directory, state = _candidate(private, identifier)
    if identifier != current["candidate_id"] and (not head or head["candidate_id"] != identifier):
        if state["status"] == "rolled_back":
            return {"status": "rolled_back", "candidate_id": identifier}
        raise ConditionError("rollback must name the current candidate or the latest remaining applied version")
    journal_path = directory / "journal.json"
    target = _clean_root(state["target"])
    with locked(target.parent / (target.name + ".gad-learning-control")):
        if head and head["candidate_id"] == identifier:
            _version(private, head["version"])
        if not journal_path.exists():
            state["status"] = "rolled_back"
            state.setdefault("rollback_history", []).append({"reason": args.reason, "at": time.time(), "conflicts": []})
            write_json(directory / "candidate.json", state)
            return {"status": "rolled_back", "candidate_id": identifier}
        journal = _journal(journal_path, identifier)
        if journal["status"] == "rolled_back":
            _pop_head(private, head, identifier)
            return {"status": "rolled_back", "candidate_id": identifier}
        conflicts = _recover(directory, state, journal, args.reason)
        if not conflicts:
            _pop_head(private, head, identifier)
    return {"status": "conflict" if conflicts else "rolled_back", "candidate_id": identifier,
            "conflicts": conflicts, "_exit": 1 if conflicts else 0}


def _pop_head(private, head, identifier):
    if head and head["candidate_id"] == identifier:
        version = _version(private, head["version"])
        head_path = private / "head.json"
        if version.get("previous"):
            write_json(head_path, version["previous"])
        else:
            head_path.unlink()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _parser():
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("record", "summarize", "stage", "apply", "rollback"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--workspace", help="Private task root; required for every mutating command")
        command_parser.add_argument("--data-dir", help="Shared root; overrides GAD_DATA_DIR and the OS user-data default")
        command_parser.add_argument("--no-write", action="store_true", help="Highest priority: do not create files, directories, locks or bytecode")
        if command == "record":
            command_parser.add_argument("--input")
        if command == "summarize":
            command_parser.add_argument("--category")
        if command == "stage":
            command_parser.add_argument("--skill")
            command_parser.add_argument("--proposal")
            command_parser.add_argument("--candidate")
            command_parser.add_argument("--new-cycle", action="store_true", help="After expiry, explicitly continue this same candidate in a new 600-second cycle")
        if command == "apply":
            command_parser.add_argument("--candidate")
            command_parser.add_argument("--reports", nargs="+")
            command_parser.add_argument("--new-cycle", action="store_true")
        if command == "rollback":
            command_parser.add_argument("--candidate")
            command_parser.add_argument("--reason")
    return parser


def main(argv=None):
    import json
    try:
        args = _parser().parse_args(argv)
        if args.no_write:
            result = {"status": "no_write", "command": args.command}
        else:
            if args.command != "summarize":
                if os.environ.get("GAD_EVALUATION") or os.environ.get("GAD_LEARNING_DISABLED"):
                    raise ConditionError("evaluation/learning-disabled environment forbids learning writes")
                _need(args.workspace, f"{args.command} requires --workspace")
            shared, private = _roots(args)
            action = {"record": _record, "summarize": lambda a, s, p: _summarize(a, s),
                      "stage": _stage, "apply": _apply, "rollback": _rollback}[args.command]
            if args.command in {"apply", "rollback"}:
                _need(private.is_dir(), "no private candidate state in this workspace")
                with locked(private / "control"):
                    result = action(args, shared, private)
            else:
                result = action(args, shared, private)
        code = result.pop("_exit", 0)
    except ConditionError as error:
        result, code = {"status": "pending", "reason": str(error)}, 1
    except (UsageError, OSError, KeyError, TypeError, ValueError) as error:
        result, code = {"status": "invalid", "reason": str(error)}, 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
