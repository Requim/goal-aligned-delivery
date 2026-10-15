"""Harness tests use a simulated executor, never real model certification."""

import sys

sys.dont_write_bytecode = True

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import regress
from delivery_io import ConditionError, UsageError, canonical_hash, file_hash


FAKE_EXECUTOR = r'''
import json, os, pathlib, subprocess, sys, time, uuid
if "--version" in sys.argv:
    print("simulated-codex 1")
    sys.exit(0)
args = sys.argv[1:]
assert args[:4] == ["exec", "--json", "--ephemeral", "--skip-git-repo-check"]
assert args[-1] == "-"
assert os.environ["GAD_EVALUATION"] == "1"
assert os.environ["GAD_LEARNING_DISABLED"] == "1"
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
cwd = pathlib.Path(args[args.index("--cd") + 1])
assert cwd.resolve() == pathlib.Path.cwd().resolve()
prompt = sys.stdin.read()
final = pathlib.Path(args[args.index("--output-last-message") + 1])
sandbox = args[args.index("--sandbox") + 1]
review = "GAD_REVIEW_PROTOCOL_V1" in prompt
assert sandbox == ("read-only" if review or "NO_WRITE_CASE" in prompt else "workspace-write")
if not review:
    assert not list(cwd.rglob("oracle.json")), "oracle leaked"
    assert "PRIVATE_ORACLE_CANARY" not in prompt, "oracle leaked in prompt"
    assert not list((cwd / "skill").rglob("cases"))
mode = os.environ.get("GAD_TEST_FAKE_MODE", "ok")
thread_id = uuid.uuid4().hex
if mode == "timeout":
    print(json.dumps({"type":"thread.started","thread_id":"test"}), flush=True)
    time.sleep(60)
if mode == "timeout_tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (cwd / "child-pid.txt").write_text(str(child.pid))
    print(json.dumps({"type":"thread.started","thread_id":"test"}), flush=True)
    time.sleep(60)
if mode == "malformed":
    print("this is not JSON")
    sys.exit(0)
print(json.dumps({"type":"thread.started","thread_id":thread_id}))
print(json.dumps({"type":"turn.started"}))
if mode == "failed":
    print(json.dumps({"type":"turn.failed","error":{"message":"fake failed"}}))
    sys.exit(0)
if review:
    # This fixture stands in for the independent model process only.
    packet = json.loads(prompt.split("GAD_REVIEW_PACKET\n",1)[1])
    evidence = packet["allowed_evidence"][0]
    reply = {"checks":[{"criterion_id":c["id"],"status":"passed",
        "observation":"Located supporting evidence for this fixture.",
        "evidence":[{"path":evidence,"locator":"line:1"}]}
        for c in packet["criteria"]]}
    if mode in {"vague_review", "wrong_and_vague"}:
        reply["checks"][0]["evidence"] = []
    if mode == "review_timeout":
        time.sleep(60)
    if mode == "failed_review":
        reply["checks"][0]["status"] = "failed"
    message = json.dumps(reply)
else:
    if mode == "lightweight":
        source = cwd / "source.csv"
        source.write_text(source.read_text(encoding="utf-8").replace("\u63d0\u4ea4", "\u4fdd\u5b58"), encoding="utf-8")
    if mode == "mutate_input":
        (cwd / "source.csv").write_text("tampered")
    if mode == "mutate_no_write":
        (cwd / "forbidden.txt").write_text("bad")
    if "NO_WRITE_CASE" not in prompt and mode != "lightweight":
        (cwd / "result.json").write_text(json.dumps({"total":38 if mode == "wrong_and_vague" else 37}))
    message = "Finished the requested fixture."
if mode != "no_final":
    final.write_text(message, encoding="utf-8")
print(json.dumps({"type":"item.completed","item":{"id":"item_1",
    "type":"agent_message","text":message}}))
if mode != "partial":
    usage = {"input_tokens":7,"cached_input_tokens":0,"output_tokens":3}
    if mode == "unknown_usage":
        usage = {"unrecognized_cost": 0}
    event = {"type":"turn.completed"}
    if mode != "no_usage":
        event["usage"] = usage
    print(json.dumps(event))
'''


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "source-skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
        (self.skill / "work").mkdir()
        (self.skill / "work" / "old-report.json").write_text("private")
        (self.skill / "references").mkdir()
        (self.skill / "references" / "behavior-cases.md").write_text("PRIVATE_ORACLE_CANARY")
        self.cases = self.skill / "cases"
        self.case = self.cases / "fixture"
        (self.case / "inputs").mkdir(parents=True)
        (self.case / "request.md").write_text("Calculate and save result.json.", encoding="utf-8")
        (self.case / "inputs" / "source.csv").write_text("x\n37\n", encoding="utf-8")
        self.oracle = {
            "schema_version": 1, "case_id": "fixture", "route": "standard",
            "stages": [{"request": "request.md"}],
            "mutable_inputs": [],
            "checks": [{"id": "total", "kind": "json", "path": "result.json",
                        "expected": {"total": 37}}],
            "semantic_criteria": [{"id": "meaning", "description": "PRIVATE_ORACLE_CANARY"}],
        }
        self.save_oracle()
        self.executor = self.root / "fake_executor.py"
        self.executor.write_text(FAKE_EXECUTOR, encoding="utf-8")
        self.output = self.root / "out"

    def save_oracle(self):
        (self.case / "oracle.json").write_text(json.dumps(self.oracle), encoding="utf-8")

    def case_definition_source(self, path=None):
        evaluation = {
            "category": "fixture-correction", "task_token": "a" * 64,
            "acceptance": "failed", "user_feedback": [{
                "text": "The fixture must preserve the requested total without exposing the answer.",
                "explicit_correction": True,
            }],
            "inference": [],
        }
        value = {
            "schema_version": 1,
            "case_ids": ["fixture"],
            "category": "fixture-correction",
            "generalized_lesson": {
                "deidentified": True, "generalized": True,
                "text": "Verify the requested total from isolated inputs.",
            },
            "source_records": [{
                "record_id": "b" * 32,
                "evaluation_sha256": canonical_hash(evaluation),
                "evaluation": evaluation,
            }],
        }
        target = path or (self.root / "case-definition-source.json")
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    def run_cli(self, *extra, mode="ok", output=None):
        arguments = ["run", "--mode", "live", "--skill", str(self.skill),
                     "--cases-root", str(self.cases), "--output", str(output or self.output),
                     "--executor", str(self.executor), "--model", "fixture-model",
                     "--timeout", "3", *extra]
        with patch.dict(os.environ, {"GAD_TEST_FAKE_MODE": mode}):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = regress.main(arguments)
        return code, json.loads(buffer.getvalue())

    def report(self):
        return regress.load_report(self.output / "report.json")

    def test_offline_never_certifies_behavior_and_import_does_not_write(self):
        code, result = self.run_cli("--mode", "offline")
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual(report["status"], "unverified")
        self.assertIn("offline checks cannot establish live behavior", report["observation"])
        self.assertEqual(report["cases"][0]["status"], "unverified")
        self.assertFalse(list(self.skill.rglob("__pycache__")))

    def test_real_process_protocol_and_complete_evidence(self):
        code, result = self.run_cli()
        self.assertEqual(code, 0, result)
        report = self.report()
        self.assertEqual(report["status"], "passed")
        case = report["cases"][0]
        self.assertEqual(len(case["actors"]), 1)
        self.assertEqual(case["reviewer"]["status"], "completed")
        self.assertEqual(case["metrics"]["tokens"]["value"]["input_tokens"], 14)
        self.assertTrue(report["evidence_manifest"])
        self.assertTrue(report["simulated_executor"], "tests must not masquerade as real models")

    def test_retries_and_nonfatal_error_events_do_not_override_complete_turn(self):
        with self.executor.open("a", encoding="utf-8") as stream:
            stream.write('\nprint(json.dumps({"type":"error","message":"transient retry"}))\n')
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.report()["status"], "passed")

    def test_missing_executor_timeout_failed_and_partial_are_unverified(self):
        for mode in ("timeout", "malformed", "failed", "partial", "no_final"):
            with self.subTest(mode=mode):
                output = self.root / mode
                code, _ = self.run_cli("--timeout", "0.15", mode=mode, output=output)
                self.assertEqual(code, 1)
                report = regress.load_report(output / "report.json")
                self.assertEqual(report["status"], "unverified")
        code, _ = self.run_cli("--executor", str(self.root / "missing-engine"))
        self.assertEqual(code, 1)
        self.assertEqual(self.report()["status"], "unverified")

    def test_vague_review_is_unverified_and_reviewer_failure_is_failed(self):
        self.assertEqual(self.run_cli(mode="vague_review")[0], 1)
        self.assertEqual(self.report()["status"], "unverified")
        other = self.root / "review-failed"
        self.assertEqual(self.run_cli(mode="failed_review", output=other)[0], 1)
        self.assertEqual(regress.load_report(other / "report.json")["status"], "failed")

    def test_fixed_values_and_immutable_inputs_are_enforced(self):
        self.oracle["checks"][0]["expected"] = {"total": 38}
        self.save_oracle()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.report()["status"], "failed")
        other = self.root / "input-mutated"
        self.assertEqual(self.run_cli(mode="mutate_input", output=other)[0], 1)
        report = regress.load_report(other / "report.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(next(c for c in report["cases"][0]["checks"]
                              if c["criterion_id"] == "input-integrity")["status"], "failed")

    def test_no_write_compares_files_instead_of_self_report(self):
        self.oracle.update(route="no-write", checks=[])
        (self.case / "request.md").write_text("NO_WRITE_CASE: analyze only.")
        self.save_oracle()
        self.assertEqual(self.run_cli(mode="mutate_no_write")[0], 1)
        report = self.report()
        self.assertEqual(report["status"], "failed")
        check = next(c for c in report["cases"][0]["checks"]
                     if c["criterion_id"] == "no-write")
        self.assertEqual(check["status"], "failed")

    def test_corrupt_or_omitted_events_final_inputs_or_artifact_evidence_rejected(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = json.loads((self.output / "report.json").read_text())
        case = original["cases"][0]
        paths = [case["actors"][0]["events"]["path"], case["reviewer"]["final"]["path"],
                 case["source"]["oracle"]["path"], case["artifacts"][0]["path"]]
        for relative in paths:
            target = self.output / relative
            previous = target.read_bytes()
            target.write_bytes(previous + b"\nCORRUPTED")
            with self.subTest(path=relative), self.assertRaises((ConditionError, UsageError)):
                self.report()
            target.write_bytes(previous)
        broken = json.loads(json.dumps(original))
        broken["cases"][0]["actors"] = []
        with self.assertRaises((ConditionError, UsageError)):
            regress.validate_report(broken, self.output)

    def test_evidence_path_traversal_is_rejected(self):
        self.assertEqual(self.run_cli()[0], 0)
        report = self.report()
        report["evidence_manifest"][0]["path"] = "../outside"
        with self.assertRaises((ConditionError, UsageError)):
            regress.validate_report(report, self.output)

    def test_resume_identity_changes_and_unknown_model_are_refused(self):
        self.assertEqual(self.run_cli()[0], 0)
        run_id = self.report()["run_id"]
        self.assertEqual(self.run_cli("--resume")[0], 0)
        self.assertEqual(self.report()["run_id"], run_id)
        self.assertTrue(self.report()["resumed"])
        original_oracle = json.loads(json.dumps(self.oracle))
        for kind in ("model", "skill", "oracle"):
            with self.subTest(kind=kind):
                (self.skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
                self.oracle = json.loads(json.dumps(original_oracle))
                self.save_oracle()
                if kind == "skill":
                    (self.skill / "SKILL.md").write_text("changed")
                if kind == "oracle":
                    self.oracle["semantic_criteria"][0]["description"] += " changed"
                    self.save_oracle()
                extra = ["--model", "different"] if kind == "model" else []
                self.assertEqual(self.run_cli("--resume", *extra)[0], 2)

    def test_compare_reports_detects_regression_and_offline_is_not_upgrade(self):
        self.assertEqual(self.run_cli()[0], 0)
        changed = self.root / "candidate"
        self.assertEqual(self.run_cli(mode="failed_review", output=changed)[0], 1)
        result = regress.compare_reports(self.output / "report.json", changed / "report.json")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["regressions"], ["fixture"])
        offline = self.root / "offline"
        self.run_cli("--mode", "offline", output=offline)
        result = regress.compare_reports(self.output / "report.json", offline / "report.json")
        self.assertEqual(result["status"], "unverified")

    def test_candidate_review_has_independent_boundary_checks_and_evidence(self):
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n")
        source = self.case_definition_source()
        self.assertEqual(self.run_cli(
            "--baseline-skill", str(baseline),
            "--case-definition-source", str(source),
        )[0], 0)
        review = self.report()["candidate_review"]
        self.assertEqual(review["status"], "passed")
        self.assertGreaterEqual(len(review["checks"]), 4)
        self.assertTrue(review["diff"]["sha256"])
        self.assertEqual(review["candidate_skill_sha256"], self.report()["identity"]["skill_sha256"])

    def test_new_case_definition_review_precedes_actor_and_binds_full_change(self):
        baseline = self.root / "baseline-full-change"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n", encoding="utf-8")
        for folder in ("work", "outputs", "__pycache__"):
            nested = self.case / "inputs" / folder / "source.txt"
            nested.parent.mkdir(parents=True)
            nested.write_text("original case input\n", encoding="utf-8")
        source = self.case_definition_source()

        code, result = self.run_cli(
            "--baseline-skill", str(baseline),
            "--case-definition-source", str(source),
        )
        self.assertEqual(code, 0, result)
        report = self.report()
        review = report["case_definition_review"]
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["case_ids"], ["fixture"])
        self.assertEqual(review["source"]["sha256"], file_hash(source))
        self.assertTrue(review["reviewer"]["invocation_id"])
        self.assertNotIn("cases/fixture/request.md", {entry["path"] for entry in report["source_manifest"]})
        changed_paths = {entry["path"] for entry in report["change_manifest"]}
        self.assertTrue({
            "cases/fixture/request.md", "cases/fixture/inputs/source.csv", "cases/fixture/oracle.json",
            "cases/fixture/inputs/work/source.txt", "cases/fixture/inputs/outputs/source.txt",
            "cases/fixture/inputs/__pycache__/source.txt",
        }.issubset(changed_paths))
        self.assertEqual(report["identity"]["change_sha256"], canonical_hash(report["change_manifest"]))
        boundary = report["candidate_review"]
        self.assertEqual(boundary["candidate_change_sha256"], report["identity"]["change_sha256"])
        diff = (self.output / boundary["diff"]["path"]).read_text(encoding="utf-8")
        self.assertIn("cases/fixture/request.md", diff)
        definition_timing = json.loads((self.output / review["reviewer"]["timing"]["path"]).read_text())
        actor = report["cases"][0]["actors"][0]
        actor_timing = json.loads((self.output / actor["timing"]["path"]).read_text())
        self.assertLessEqual(definition_timing["finished_ns"], actor_timing["started_ns"])
        self.assertEqual(report["cases"][0]["metrics"]["tokens"]["value"]["input_tokens"], 14)
        self.assertEqual(report["metrics"]["tokens"]["value"]["input_tokens"], 28)

    def test_unreviewed_or_failed_new_case_never_starts_actor(self):
        baseline = self.root / "baseline-review-gate"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n", encoding="utf-8")

        missing = self.root / "missing-definition-source"
        code, result = self.run_cli("--baseline-skill", str(baseline), output=missing)
        self.assertEqual((code, result["status"]), (2, "invalid"))
        self.assertFalse((missing / "report.json").exists())

        source = self.case_definition_source(self.root / "review-source.json")
        failed = self.root / "failed-definition-review"
        code, _ = self.run_cli(
            "--baseline-skill", str(baseline),
            "--case-definition-source", str(source),
            mode="failed_review", output=failed,
        )
        self.assertEqual(code, 1)
        report = regress.load_report(failed / "report.json")
        self.assertEqual(report["case_definition_review"]["status"], "failed")
        self.assertEqual(report["cases"], [])

        exhausted = self.root / "definition-budget-exhausted"
        code, _ = self.run_cli(
            "--baseline-skill", str(baseline),
            "--case-definition-source", str(source),
            "--budget-seconds", "0", output=exhausted,
        )
        self.assertEqual(code, 1)
        report = regress.load_report(exhausted / "report.json")
        self.assertEqual(report["case_definition_review"]["status"], "unverified")
        self.assertEqual(report["cases"], [])

    def test_definition_checkpoint_after_save_loads_and_public_resume_repeats_review(self):
        baseline = self.root / "checkpoint-baseline"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n", encoding="utf-8")
        source = self.case_definition_source()
        arguments = ("--baseline-skill", str(baseline), "--case-definition-source", str(source))
        with patch.object(regress, "_case_definition_review", side_effect=KeyboardInterrupt):
            with patch.object(regress, "_run_case", wraps=regress._run_case) as actor:
                with self.assertRaises(KeyboardInterrupt):
                    self.run_cli(*arguments)
                self.assertEqual(actor.call_count, 0)
        saved = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "unverified")
        self.assertEqual(saved["cases"], [])
        self.assertIsNone(saved["case_definition_review"])
        loaded = regress.load_report(self.output / "report.json")
        self.assertEqual(loaded["run_id"], saved["run_id"])
        changed = json.loads(json.dumps(loaded))
        changed["status"] = "passed"
        with self.assertRaises((ConditionError, UsageError)):
            regress.validate_report(changed, self.output)
        self.assertEqual(regress.compare_reports(
            self.output / "report.json", self.output / "report.json")["status"], "unverified")
        with patch.object(regress, "_run_case", wraps=regress._run_case) as actor:
            code, result = self.run_cli(*arguments, "--resume", "--budget-seconds", "0")
            self.assertEqual(code, 1, result)
            self.assertEqual(actor.call_count, 0)
        self.assertEqual(self.report()["status"], "unverified")
        code, result = self.run_cli(*arguments, "--resume")
        self.assertEqual(code, 0, result)
        completed = self.report()
        self.assertEqual(completed["run_id"], saved["run_id"])
        self.assertTrue(completed["resumed"])
        self.assertEqual(completed["case_definition_review"]["status"], "passed")
        self.assertEqual(completed["cases"][0]["status"], "passed")

    def test_interrupted_definition_originals_are_uncommitted_and_resume_runs_fresh_review(self):
        baseline = self.root / "partial-baseline"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n", encoding="utf-8")
        source = self.case_definition_source()
        arguments = ("--baseline-skill", str(baseline), "--case-definition-source", str(source))
        actual_review = regress._review
        for phase in ("partial", "complete-uncommitted", "retry-unverified"):
            with self.subTest(phase=phase):
                output = self.root / phase
                if phase == "retry-unverified":
                    self.assertEqual(self.run_cli(
                        *arguments, "--budget-seconds", "0", output=output)[0], 1)
                uncommitted = []

                def interrupt_review(*args, **kwargs):
                    directory = args[1]
                    if phase == "partial":
                        directory.mkdir(parents=True, exist_ok=True)
                        raw = directory / "events.jsonl"
                        raw.write_text('{"type":"thread.started","thread_id":"interrupted"}\n',
                                       encoding="utf-8")
                        uncommitted.append(raw)
                    else:
                        result = actual_review(*args, **kwargs)
                        self.assertEqual(result["status"], "passed")
                        uncommitted.append(output / result["invocation"]["execution"]["path"])
                    raise KeyboardInterrupt()

                with patch.object(regress, "_review", side_effect=interrupt_review):
                    with patch.object(regress, "_run_case", wraps=regress._run_case) as actor:
                        with self.assertRaises(KeyboardInterrupt):
                            self.run_cli(*arguments, *(("--resume",) if output.exists() else ()),
                                         output=output)
                        self.assertEqual(actor.call_count, 0)
                before = {path: path.read_bytes() for path in uncommitted}
                checkpoint = regress.load_report(output / "report.json")
                self.assertEqual(checkpoint["status"], "unverified")
                self.assertEqual(checkpoint["cases"], [])
                self.assertNotEqual((checkpoint["case_definition_review"] or {}).get("status"), "passed")
                unrelated = output / "unexpected.txt"
                unrelated.write_text("outside uncommitted preflight originals", encoding="utf-8")
                with self.assertRaises((ConditionError, UsageError)):
                    regress.load_report(output / "report.json")
                unrelated.unlink()
                if phase == "retry-unverified":
                    committed = output / checkpoint["case_definition_review"]["rubric"]["path"]
                    extra_committed = committed.parent / "unexpected.txt"
                    extra_committed.write_text("inside a committed attempt", encoding="utf-8")
                    with self.assertRaises((ConditionError, UsageError)):
                        regress.load_report(output / "report.json")
                    extra_committed.unlink()
                code, result = self.run_cli(*arguments, "--resume", output=output)
                self.assertEqual(code, 0, result)
                completed = regress.load_report(output / "report.json")
                self.assertEqual(completed["run_id"], checkpoint["run_id"])
                new_execution = output / completed["case_definition_review"]["reviewer"]["execution"]["path"]
                self.assertNotIn(new_execution, before)
                self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
                definition_timing = json.loads((output / completed["case_definition_review"]["reviewer"]["timing"]["path"]).read_text())
                actor_timing = json.loads((output / completed["cases"][0]["actors"][0]["timing"]["path"]).read_text())
                self.assertLessEqual(definition_timing["finished_ns"], actor_timing["started_ns"])
                changed = json.loads(json.dumps(completed))
                changed["case_definition_review"] = None
                changed["status"] = "unverified"
                changed["metrics"] = regress._report_metrics(changed)
                with self.assertRaises((ConditionError, UsageError)):
                    regress.validate_report(changed, output)
                extra = output / "case-definition-review" / ("a" * 32) / "extra.txt"
                extra.parent.mkdir(parents=True)
                extra.write_text("not part of the completed report", encoding="utf-8")
                with self.assertRaises((ConditionError, UsageError)):
                    regress.load_report(output / "report.json")

    def test_budget_leaves_explicit_unverified_cases(self):
        self.assertEqual(self.run_cli("--budget-seconds", "0")[0], 1)
        report = self.report()
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["cases"][0]["actors"], [])

    def test_fingerprint_ignores_runtime_cache_but_tracks_source_changes(self):
        original = regress.fingerprint(self.skill)
        (self.skill / "work" / "new.tmp").write_text("new")
        self.assertEqual(regress.fingerprint(self.skill), original)
        (self.skill / "SKILL.md").write_text("new")
        self.assertNotEqual(regress.fingerprint(self.skill), original)

    def test_builtin_cases_have_eight_routes_and_stage_identity(self):
        root = Path(__file__).resolve().parent.parent / "cases"
        cases = regress.load_cases(root)
        self.assertEqual({c["case_id"] for c in cases}, {
            "normal-delivery", "missing-data", "input-correction", "resume",
            "lightweight", "no-write", "external-interruption", "multi-skill"})
        correction = next(c for c in cases if c["case_id"] == "input-correction")
        self.assertEqual(len(correction["oracle"]["stages"]), 2)

    def test_incomplete_tool_item_is_not_a_complete_trace(self):
        events = self.root / "events.jsonl"
        final = self.root / "final.txt"
        final.write_text("done")
        events.write_text("\n".join(json.dumps(event) for event in [
            {"type": "thread.started"}, {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "cmd1", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}}]))
        self.assertEqual(regress.parse_trace(events, final)["status"], "unverified")

    def test_saved_reviewer_cannot_be_replaced_by_passing_summary(self):
        self.assertEqual(self.run_cli(mode="failed_review")[0], 1)
        report = self.report()
        report["status"] = report["cases"][0]["status"] = "passed"
        for check in report["cases"][0]["checks"]:
            check["status"] = "passed"
        with self.assertRaises((UsageError, ConditionError)):
            regress.validate_report(report, self.output)

    def test_stage_snapshots_preserve_real_intermediate_artifacts(self):
        (self.case / "request-2.md").write_text("Recalculate using the existing workspace.")
        self.oracle["stages"].append({"request": "request-2.md"})
        self.save_oracle()
        self.assertEqual(self.run_cli()[0], 0)
        report = self.report()
        self.assertEqual(len(report["cases"][0]["stage_snapshots"]), 2)
        for stage in report["cases"][0]["stage_snapshots"]:
            self.assertTrue(any(entry["path"].endswith("/result.json") for entry in stage["artifacts"]))

    def test_external_fixed_check_requires_query_observation_counter(self):
        self.oracle["checks"] = [{"id": "service", "kind": "external", "path": "result.json",
                                 "expected": {"total": 37}, "minimum": {"query_count": 1}}]
        self.save_oracle()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.report()["status"], "failed")

    def test_runtime_snapshot_excludes_evaluation_rubrics(self):
        self.assertEqual(self.run_cli()[0], 0)
        report = self.report()
        workspace = self.output / report["cases"][0]["workspace"]
        self.assertFalse((workspace / "skill" / "references" / "behavior-cases.md").exists())

    def test_no_write_cannot_be_relabelled_passed_in_summary(self):
        self.oracle.update(route="no-write", checks=[])
        (self.case / "request.md").write_text("NO_WRITE_CASE: analyze only.")
        self.save_oracle()
        self.assertEqual(self.run_cli(mode="mutate_no_write")[0], 1)
        report = self.report()
        report["status"] = report["cases"][0]["status"] = "passed"
        for check in report["cases"][0]["checks"]:
            check["status"] = "passed"
        with self.assertRaises((UsageError, ConditionError)):
            regress.validate_report(report, self.output)

    def test_partial_report_can_resume_only_missing_cases(self):
        second = self.cases / "second"
        shutil.copytree(self.case, second)
        oracle = json.loads((second / "oracle.json").read_text())
        oracle["case_id"] = "second"
        (second / "oracle.json").write_text(json.dumps(oracle))
        self.assertEqual(self.run_cli()[0], 0)
        report = self.report()
        first = report["cases"][0]
        report["cases"] = [first]
        report["status"] = "unverified"
        report["metrics"] = json.loads(json.dumps(first["metrics"]))
        (self.output / "report.json").write_text(json.dumps(report))
        self.assertEqual(self.run_cli("--resume")[0], 0)
        resumed = self.report()
        self.assertEqual(len(resumed["cases"]), 2)
        self.assertEqual(resumed["cases"][0]["workspace"], first["workspace"])
        self.assertEqual(resumed["cases"][0]["actors"], first["actors"])

    def test_unknown_model_refuses_cached_resume(self):
        unknown = {"model": None, "provider": None, "reasoning_effort": None, "source": "unknown"}
        with patch.object(regress, "_model_config", return_value=unknown):
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.report()["status"], "unverified")
            self.assertEqual(self.run_cli("--resume")[0], 2)

    def test_offline_rejects_incomplete_oracle_instead_of_hiding_it(self):
        del self.oracle["checks"][0]["expected"]
        self.save_oracle()
        self.assertEqual(self.run_cli("--mode", "offline")[0], 2)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree verification")
    def test_timeout_terminates_windows_descendant_process(self):
        import subprocess
        self.assertEqual(self.run_cli("--timeout", "0.4", mode="timeout_tree")[0], 1)
        report = self.report()
        workspace = self.output / report["cases"][0]["workspace"]
        pid = int((workspace / "child-pid.txt").read_text())
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, check=True)
        self.assertNotIn(f'"{pid}"', listing.stdout)

    def test_lightweight_success_proves_absence_with_tree_evidence(self):
        self.oracle.update(route="lightweight", mutable_inputs=["source.csv"], checks=[
            {"id": "text", "kind": "text", "path": "source.csv", "contains": ["保存"]},
            {"id": "no-record", "kind": "file-count", "glob": "work/task-progress/*.md", "count": 0}])
        (self.case / "inputs" / "source.csv").write_text("提交\n", encoding="utf-8")
        self.save_oracle()
        code, result = self.run_cli(mode="lightweight")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.report()["status"], "passed")

    def test_boundary_diff_is_bound_to_frozen_candidate_source(self):
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n")
        original = regress._run_case

        def edit_installed_source_after_actor(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.skill / "SKILL.md").write_text("CHANGED_DURING_ACTOR\n")
            return result

        source = self.case_definition_source()
        with patch.object(regress, "_run_case", side_effect=edit_installed_source_after_actor):
            self.assertEqual(self.run_cli(
                "--baseline-skill", str(baseline),
                "--case-definition-source", str(source),
            )[0], 0)
        report = self.report()
        diff = (self.output / report["candidate_review"]["diff"]["path"]).read_text()
        self.assertNotIn("CHANGED_DURING_ACTOR", diff)

    def test_summary_cannot_relabel_effective_model_executor_or_simulation(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        mutations = [
            lambda report: report["identity"]["model_config"].update(model="a-model-that-never-ran"),
            lambda report: report["identity"].update(executor_version="codex-cli 999.0"),
            lambda report: report.update(simulated_executor=False),
            lambda report: report.pop("execution_identity", None),
        ]
        for mutate in mutations:
            changed = json.loads(json.dumps(original))
            mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises((UsageError, ConditionError)):
                regress.validate_report(changed, self.output)
        changed = json.loads(json.dumps(original))
        changed["identity"]["model_config"]["model"] = "a-model-that-never-ran"
        changed["identity"]["executor_version"] = "codex-cli 999.0"
        changed["simulated_executor"] = False
        with self.assertRaises((UsageError, ConditionError)):
            regress.validate_report(changed, self.output)

    def test_actor_and_reviewer_commands_bind_the_frozen_execution_identity(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        for role in ("actor", "reviewer"):
            changed = json.loads(json.dumps(original))
            invocation = (changed["cases"][0]["actors"][0] if role == "actor"
                          else changed["cases"][0]["reviewer"])
            invocation["command"][0] = "a-never-executed-engine"
            with self.subTest(role=role), self.assertRaises((UsageError, ConditionError)):
                regress.validate_report(changed, self.output)

    def test_copied_trace_and_changed_run_id_cannot_count_as_independent(self):
        self.assertEqual(self.run_cli()[0], 0)
        copied = self.root / "copied-run"
        shutil.copytree(self.output, copied)
        report = json.loads((copied / "report.json").read_text())
        report["run_id"] = "a-different-summary-run-id"
        (copied / "report.json").write_text(json.dumps(report))
        try:
            compared = regress.compare_reports(self.output / "report.json", copied / "report.json")
        except (UsageError, ConditionError):
            pass
        else:
            self.assertFalse(compared["independent_runs"])
        shutil.rmtree(copied)
        shutil.copytree(self.output, copied)
        compared = regress.compare_reports(self.output / "report.json", copied / "report.json")
        self.assertFalse(compared["independent_runs"])
        independent = self.root / "independent"
        self.assertEqual(self.run_cli(output=independent)[0], 0)
        compared = regress.compare_reports(self.output / "report.json", independent / "report.json")
        self.assertTrue(compared["independent_runs"])

    def test_independent_invocation_ids_still_reject_a_reused_actor_trace(self):
        self.assertEqual(self.run_cli()[0], 0)
        independent = self.root / "independent"
        self.assertEqual(self.run_cli(output=independent)[0], 0)
        base = self.report()
        candidate = regress.load_report(independent / "report.json")
        actor = candidate["cases"][0]["actors"][0]
        source_actor = base["cases"][0]["actors"][0]
        shutil.copyfile(self.output / source_actor["events"]["path"], independent / actor["events"]["path"])
        actor["thread_id"] = source_actor["thread_id"]

        def rebind(value, relative):
            if isinstance(value, dict):
                if value.get("path") == relative and "sha256" in value:
                    value["sha256"] = file_hash(independent / relative)
                for item in value.values():
                    rebind(item, relative)
            elif isinstance(value, list):
                for item in value:
                    rebind(item, relative)

        rebind(candidate, actor["events"]["path"])
        execution_path = independent / actor["execution"]["path"]
        execution = json.loads(execution_path.read_text())
        execution["thread_id"] = actor["thread_id"]
        execution["files"]["events"] = actor["events"]
        execution_path.write_text(json.dumps(execution))
        rebind(candidate, actor["execution"]["path"])
        (independent / "report.json").write_text(json.dumps(candidate))
        regress.load_report(independent / "report.json")
        compared = regress.compare_reports(self.output / "report.json", independent / "report.json")
        self.assertFalse(compared["independent_runs"])
        self.assertFalse(compared["execution_evidence"]["disjoint"])

    def test_confirmed_fixed_failure_survives_unverified_semantic_review(self):
        self.oracle["checks"][0]["expected"] = {"total": 38}
        self.save_oracle()
        for mode in ("vague_review", "review_timeout"):
            output = self.root / mode
            with self.subTest(mode=mode):
                code, result = self.run_cli("--timeout", "0.4", mode=mode, output=output)
                self.assertEqual(code, 1, result)
                report = regress.load_report(output / "report.json")
                self.assertEqual(report["status"], "failed")
                case = report["cases"][0]
                self.assertEqual(case["status"], "failed")
                checks = {c["criterion_id"]: c["status"] for c in case["checks"]}
                self.assertEqual(checks["total"], "failed")
                self.assertEqual(checks["meaning"], "unverified")
        incomplete = self.root / "incomplete"
        self.assertEqual(self.run_cli(mode="partial", output=incomplete)[0], 1)
        report = regress.load_report(incomplete / "report.json")
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["cases"][0]["status"], "unverified")
        budget = self.root / "review-budget"
        with patch.object(regress, "_remaining", side_effect=[3, 3, 0]):
            self.assertEqual(self.run_cli(output=budget)[0], 1)
        report = regress.load_report(budget / "report.json")
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["cases"][0]["reviewer"])

    def test_confirmed_failure_is_a_quality_regression_even_if_review_unverified(self):
        self.assertEqual(self.run_cli()[0], 0)
        candidate = self.root / "wrong"
        self.assertEqual(self.run_cli(mode="wrong_and_vague", output=candidate)[0], 1)
        result = regress.compare_reports(self.output / "report.json", candidate / "report.json")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["regressions"], ["fixture"])

    def test_invocation_metrics_cannot_be_relabelled_in_summary(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        updates = [
            {"usage": {"total_tokens": 0}}, {"usage": {"input_tokens": True, "output_tokens": 3}},
            {"tool_calls": 99}, {"tool_calls": False}, {"elapsed_seconds": 0},
            {"elapsed_seconds": True}, {"elapsed_seconds": -1}, {"elapsed_seconds": float("inf")},
            {"elapsed_source": "invented-measurement"},
        ]
        for role in ("actor", "reviewer"):
            for update in updates:
                changed = json.loads(json.dumps(original))
                invocation = (changed["cases"][0]["actors"][0] if role == "actor"
                              else changed["cases"][0]["reviewer"])
                invocation.update(update)
                with self.subTest(role=role, update=update), self.assertRaises((UsageError, ConditionError)):
                    regress.validate_report(changed, self.output)

    def test_case_and_report_metrics_recompute_all_values_and_sources(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        updates = [
            ("tokens", {"value": {"total_tokens": 0}, "source": "turn.completed.usage"}),
            ("tokens", {"value": {"input_tokens": False, "output_tokens": 6}, "source": "turn.completed.usage"}),
            ("tokens", {"value": None, "source": "turn.completed.usage"}),
            ("tokens", {"value": {"input_tokens": 14, "output_tokens": 6}, "source": "unavailable"}),
            ("tool_calls", {"value": 99, "source": "item.completed"}),
            ("tool_calls", {"value": False, "source": "item.completed"}),
            ("elapsed_seconds", {"value": 0, "source": "monotonic-clock"}),
            ("elapsed_seconds", {"value": True, "source": "monotonic-clock"}),
            ("elapsed_seconds", {"value": -1, "source": "monotonic-clock"}),
            ("elapsed_seconds", {"value": float("nan"), "source": "monotonic-clock"}),
            ("elapsed_seconds", {"value": float("inf"), "source": "monotonic-clock"}),
            ("elapsed_seconds", {"value": 0, "source": "unknown-clock"}),
            ("unknown_cost", {"value": 0, "source": "measured"}),
        ]
        for scope in ("case", "report"):
            for key, metric in updates:
                changed = json.loads(json.dumps(original))
                target = changed["cases"][0] if scope == "case" else changed
                target["metrics"][key] = metric
                with self.subTest(scope=scope, key=key, metric=metric), self.assertRaises((UsageError, ConditionError)):
                    regress.validate_report(changed, self.output)

    def test_missing_or_corrupt_timing_cannot_downgrade_a_new_report(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        frozen = json.loads((self.output / original["execution_identity"]["path"]).read_text())
        self.assertEqual(frozen["metrics_version"], 2)
        case = original["cases"][0]
        for target in (case["actors"][0], case["reviewer"], case):
            timing = target["timing"]
            path = self.output / timing["path"]
            previous = path.read_bytes()
            path.write_bytes(previous + b"corrupt")
            with self.assertRaises((UsageError, ConditionError)):
                self.report()
            path.write_bytes(previous)
        for scope in ("actor", "case"):
            changed = json.loads(json.dumps(original))
            target = changed["cases"][0]["actors"][0] if scope == "actor" else changed["cases"][0]
            target.pop("timing")
            changed.pop("metrics_version", None)
            with self.assertRaises((UsageError, ConditionError)):
                regress.validate_report(changed, self.output)

    def test_absent_or_unrecognized_usage_remains_unavailable(self):
        for mode in ("no_usage", "unknown_usage"):
            output = self.root / mode
            with self.subTest(mode=mode):
                self.assertEqual(self.run_cli(mode=mode, output=output)[0], 0)
                report = regress.load_report(output / "report.json")
                self.assertEqual(report["metrics"]["tokens"], {"value": None, "source": "unavailable"})
                self.assertEqual(report["cases"][0]["metrics"]["tokens"], {"value": None, "source": "unavailable"})
                self.assertIsNone(report["cases"][0]["actors"][0]["usage"])
                self.assertEqual(report["metrics"]["tool_calls"]["value"], 0)

    def test_timing_record_rejects_invalid_numbers_clock_and_scope(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        reference = original["cases"][0]["timing"]
        path = self.output / reference["path"]
        previous = path.read_bytes()
        timing = json.loads(previous)
        updates = [
            {"started_ns": True}, {"started_ns": -1}, {"finished_ns": float("inf")},
            {"finished_ns": timing["started_ns"] - 1}, {"clock": "invented-clock"},
            {"scope_id": "a-different-case"}, {"subject": {"workspace": "elsewhere", "invocation_ids": []}},
            {"unknown_cost": 0},
        ]
        try:
            for update in updates:
                changed = json.loads(json.dumps(original))
                mutated = {**timing, **update}
                path.write_text(json.dumps(mutated), encoding="utf-8")
                changed["cases"][0]["timing"]["sha256"] = file_hash(path)
                for entry in changed["evidence_manifest"]:
                    if entry["path"] == reference["path"]:
                        entry["sha256"] = file_hash(path)
                with self.subTest(update=update), self.assertRaises((UsageError, ConditionError)):
                    regress.validate_report(changed, self.output)
        finally:
            path.write_bytes(previous)

    def test_report_duration_includes_cases_and_extra_boundary_review_once(self):
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "SKILL.md").write_text("# Baseline\n", encoding="utf-8")
        source = self.case_definition_source()
        self.assertEqual(self.run_cli(
            "--baseline-skill", str(baseline),
            "--case-definition-source", str(source),
        )[0], 0)
        report = self.report()
        case = report["cases"][0]
        definition_reviewer = report["case_definition_review"]["reviewer"]
        reviewer = report["candidate_review"]["reviewer"]
        self.assertAlmostEqual(report["metrics"]["elapsed_seconds"]["value"],
                         case["metrics"]["elapsed_seconds"]["value"] +
                         definition_reviewer["elapsed_seconds"] + reviewer["elapsed_seconds"], places=12)
        for invocation in case["actors"] + [case["reviewer"], definition_reviewer, reviewer]:
            timing = json.loads((self.output / invocation["timing"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(invocation["elapsed_seconds"],
                             (timing["finished_ns"] - timing["started_ns"]) / 1_000_000_000)
        self.assertEqual(report["metrics"]["tokens"]["value"]["input_tokens"], 28)
        changed = json.loads(json.dumps(report))
        changed["candidate_review"]["reviewer"]["elapsed_seconds"] = 0
        with self.assertRaises((UsageError, ConditionError)):
            regress.validate_report(changed, self.output)

    def test_real_legacy_baseline_remains_readonly_with_elapsed_unavailable(self):
        root = Path(__file__).resolve().parents[2] / "live-baseline"
        if not (root / "report.json").is_file():
            self.skipTest("existing real baseline not present in this checkout")
        before = {entry["path"]: entry["sha256"] for entry in regress._tree(root)}
        raw = json.loads((root / "report.json").read_text(encoding="utf-8"))
        report = regress.load_report(root / "report.json")
        self.assertEqual(report["status"], raw["status"])
        self.assertEqual(report["metrics"]["tokens"], raw["metrics"]["tokens"])
        self.assertEqual(report["metrics"]["tool_calls"], raw["metrics"]["tool_calls"])
        self.assertEqual(report["metrics"]["elapsed_seconds"], {"value": None, "source": "unavailable"})
        for case in report["cases"]:
            self.assertEqual(case["metrics"]["elapsed_seconds"], {"value": None, "source": "unavailable"})
            for invocation in case["actors"] + ([case["reviewer"]] if case.get("reviewer") else []):
                self.assertIsNone(invocation["elapsed_seconds"])
                self.assertEqual(invocation["elapsed_source"], "unavailable")
        changed = json.loads(json.dumps(raw))
        changed["metrics"]["elapsed_seconds"] = {"value": 0, "source": "monotonic-clock"}
        normalized = regress.validate_report(changed, root)
        self.assertEqual(normalized["metrics"]["elapsed_seconds"], {"value": None, "source": "unavailable"})
        self.assertEqual({entry["path"]: entry["sha256"] for entry in regress._tree(root)}, before)


if __name__ == "__main__":
    unittest.main()
