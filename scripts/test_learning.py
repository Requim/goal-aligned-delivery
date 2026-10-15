"""Controlled local-learning tests; report fixtures do not prove live model quality."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from delivery_io import canonical_hash


SCRIPT = Path(__file__).with_name("learning.py")


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "private-project"
        self.workspace.mkdir()
        self.shared = self.root / "shared"
        self.skill = self.root / "installed"
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "scripts").mkdir()
        (self.skill / "cases" / "old" / "outputs").mkdir(parents=True)
        self.write(self.skill / "SKILL.md", "# Skill\n"
                   "<!-- GAD:AUTHORITY:BEGIN -->\nKeep authority.\n<!-- GAD:AUTHORITY:END -->\n"
                   "<!-- GAD:LEARNING:BEGIN -->\nKeep learning.\n<!-- GAD:LEARNING:END -->\n")
        self.write(self.skill / "references" / "principles.md", "Before\n")
        for name in ("authority", "learning", "behavior-cases", "execution", "progress", "contracts"):
            self.write(self.skill / "references" / (name + ".md"), name + "\n")
        self.write(self.skill / "scripts" / "engine.py", "pass\n")
        self.write(self.skill / "cases" / "old" / "outputs" / "raw.txt", "original fixture")
        for case in ("normal-delivery", "missing-data", "input-correction", "resume",
                     "lightweight", "no-write", "external-interruption", "multi-skill"):
            self.write(self.skill / "cases" / case / "request.md", "fixed controlled request")
            self.write(self.skill / "cases" / case / "oracle.json", "{}")
        self.l = None
        if SCRIPT.exists():
            spec = importlib.util.spec_from_file_location("learning_under_test", SCRIPT)
            self.l = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.l)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def invoke(self, command, *args, expected=0, env=None):
        self.assertTrue(SCRIPT.exists(), "learning CLI is missing")
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            with patch.dict(os.environ, env or {}, clear=False):
                code = self.l.main([command, "--data-dir", str(self.shared),
                                    "--workspace", str(self.workspace), *map(str, args)])
        value = json.loads(captured.getvalue())
        self.assertEqual(code, expected, value)
        return value

    def record(self, task="secret-task", correction=True, expected=0, **extra):
        value = {"task_id": task, "category": "recovery",
                 "acceptance": "failed", "user_feedback": [
                     {"text": "PRIVATE USER WORDS", "explicit_correction": correction}],
                 "inference": [{"text": "private inference"}],
                 "experience": {"deidentified": True, "generalized": True,
                                "text": "Confirm durable state before repeating an operation."},
                 **extra}
        path = self.write(self.root / "record.json", json.dumps(value))
        return self.invoke("record", "--input", path, expected=expected)

    def proposal(self, changes=None, records=None, **extra):
        return {"category": "recovery", "kind": "functional",
                "generalized_lesson": {"deidentified": True, "generalized": True,
                                       "text": "Confirm durable state before repeating an operation."},
                "record_ids": records or [self.record()["record_id"]],
                "target_cases": ["resume"],
                "changes": changes or [{"path": "references/principles.md", "content": "After\n"}],
                **extra}

    def new_case_changes(self, *, include_reference=False):
        oracle = {
            "schema_version": 1, "case_id": "new-correction", "route": "standard",
            "stages": [{"request": "request.md"}], "mutable_inputs": [],
            "checks": [{"id": "result", "kind": "text", "path": "result.txt",
                        "contains": ["verified"]}],
            "semantic_criteria": [{
                "id": "source-fidelity",
                "description": "The response must follow the supplied source without inventing facts.",
            }],
        }
        changes = [
            {"path": "cases/new-correction/request.md",
             "content": "Read inputs/source.txt and save a verified result to result.txt.\n"},
            {"path": "cases/new-correction/inputs/source.txt", "content": "source fact\n"},
            {"path": "cases/new-correction/oracle.json", "content": json.dumps(oracle)},
        ]
        if include_reference:
            changes.append({"path": "references/principles.md", "content": "After\n"})
        return changes

    def stage_new_case(self, *, include_reference=False):
        proposal = self.proposal(
            changes=self.new_case_changes(include_reference=include_reference),
            target_cases=["new-correction"],
        )
        return self.stage(proposal)

    def stage(self, proposal=None, **kwargs):
        path = self.write(self.root / "proposal.json", json.dumps(proposal or self.proposal()))
        return self.invoke("stage", "--skill", self.skill, "--proposal", path, **kwargs)

    def candidate(self, identifier):
        return self.workspace / "work" / "skill-learning" / "goal-aligned-delivery" / "candidates" / identifier

    def manifest(self, path):
        return {p.relative_to(path).as_posix(): p.read_bytes()
                for p in path.rglob("*") if p.is_file()}

    def reports(self, staged, kind="functional", count=2):
        """Gate fixture replaces external validated-report IO, never production evidence."""
        candidate = json.loads((self.candidate(staged["candidate_id"]) / "candidate.json").read_text())
        required = candidate["required_cases"]
        targets = set(candidate["target_cases"])
        original_change_manifest = candidate.get("original_change_manifest", candidate["original_manifest"])
        candidate_change_manifest = candidate.get("candidate_change_manifest", candidate["candidate_manifest"])
        original_change_sha = canonical_hash(original_change_manifest)
        candidate_change_sha = canonical_hash(candidate_change_manifest)
        new_cases = candidate.get("new_cases") or sorted({
            path.split("/")[1] for path in candidate["changes"] if path.startswith("cases/")
        })
        source = candidate.get("case_definition_source") or {"sha256": "1" * 64}
        reports = {}
        for role in ("base", "candidate"):
            for i in range(count):
                path = self.root / f"{role}-{i}.json"
                self.write(path, "{}")
                identity = {"skill_sha256": candidate["original_skill_sha256" if role == "base" else "candidate_skill_sha256"],
                            "change_sha256": original_change_sha if role == "base" else candidate_change_sha,
                            "case_definition_source_sha256": source["sha256"] if new_cases else None,
                            "cases_sha256": "fixed-cases", "rubric_sha256": "fixed-rubric",
                            "model_config": {"model": "controlled-fixture"}, "executor_version": "fixture-v1",
                            "executor_sha256": "executor"}
                reports[str(path)] = {"run_id": f"{role}-{i}", "mode": "live",
                    "simulated_executor": False, "status": "failed" if role == "base" and kind == "functional" else "passed",
                    "started_at": f"2026-10-15T00:00:{i:02d}Z", "identity": identity,
                    "change_manifest": original_change_manifest if role == "base" else candidate_change_manifest,
                    "case_plan": [{"case_id": case, "manifest": candidate["case_manifests"][case]} for case in required],
                    "cases": [{"case_id": case, "status": "failed" if role == "base" and case in targets and kind == "functional" else "passed",
                               "metrics": {"elapsed_seconds": {"value": 10 if role == "base" else 5, "source": "monotonic-clock"},
                                           "tokens": {"value": {"input_tokens": 100 if role == "base" else 50,
                                                                "output_tokens": 10}, "source": "turn.completed.usage"},
                                           "tool_calls": {"value": 10 if role == "base" else 5, "source": "item.completed"}}}
                              for case in required],
                    "case_definition_review": None if not new_cases else {
                        "status": "passed", "case_ids": new_cases,
                        "case_manifests": {case: candidate["case_manifests"][case] for case in new_cases},
                        "source": {"path": "case-definition-source.json",
                                   "sha256": source["sha256"], "locator": "file"},
                        "reviewer": {"status": "completed"},
                        "checks": [{"criterion_id": item["id"], "status": "passed",
                                    "evidence": [{"path": "case-definition-source.json", "locator": "line:1"}]}
                                   for item in self.l.regress.CASE_DEFINITION_CRITERIA],
                    },
                    "candidate_review": None if role == "base" else {
                        "status": "passed", "baseline_skill_sha256": candidate["original_skill_sha256"],
                        "candidate_skill_sha256": candidate["candidate_skill_sha256"],
                        "baseline_change_sha256": original_change_sha,
                        "candidate_change_sha256": candidate_change_sha,
                        "reviewer": {"status": "completed"}, "checks": [{"evidence": [{"locator": "line:1"}]}]}}
        def compare(a, b):
            ra, rb = reports[str(a)], reports[str(b)]
            return {"identity_compatible": True, "independent_runs": ra["run_id"] != rb["run_id"],
                    "regressions": [], "unverified": [], "status": "passed",
                    "execution_evidence": {"disjoint": True, "unique_within_run": True}}
        return reports, compare

    def apply_fixture(self, staged, reports=None, compare=None, **kwargs):
        if reports is None:
            reports, compare = self.reports(staged)
        with patch.object(self.l.regress, "load_report", side_effect=lambda p: copy.deepcopy(reports[str(p)])):
            with patch.object(self.l.regress, "compare_reports", side_effect=compare):
                return self.invoke("apply", "--candidate", staged["candidate_id"],
                                   "--reports", *reports.keys(), **kwargs)

    def test_private_details_shared_allowlist_and_missing_metrics(self):
        recorded = self.record()
        self.record()
        result = self.invoke("summarize")
        self.assertEqual(result["categories"][0]["independent_tasks"], 1)
        self.assertEqual(result["categories"][0]["validated_experience"], [])
        self.assertEqual(result["categories"][0]["metrics"]["tokens"]["available"], 0)
        shared_text = "\n".join(p.read_text() for p in self.shared.rglob("*") if p.is_file() and not p.name.endswith(".lock"))
        for secret in ("secret-task", "PRIVATE USER WORDS", "private inference", str(self.workspace), str(self.skill)):
            self.assertNotIn(secret, shared_text)
        detail = self.workspace / "work" / "task-evaluations" / (recorded["record_id"] + ".json")
        self.assertIn("PRIVATE USER WORDS", detail.read_text())
        self.assertEqual(json.loads(detail.read_text())["metrics"]["tokens"],
                         {"value": None, "source": "unavailable"})

    def test_metrics_reject_invalid_and_unbacked_validation(self):
        for metric in ({"value": -1, "source": "measured"},
                       {"value": None, "source": "measured"},
                       {"value": True, "source": "reported"},
                       {"value": float("nan"), "source": "measured"}):
            path = self.write(self.root / "bad.json", json.dumps({"task_id": "x", "category": "recovery",
                              "metrics": {"tokens": metric}}))
            self.invoke("record", "--input", path, expected=2)
        path = self.write(self.root / "bad.json", json.dumps({"task_id": "x", "category": "recovery",
                          "validated_experience": ["trust me"]}))
        self.invoke("record", "--input", path, expected=2)

    def test_no_write_all_commands_and_environment_are_side_effect_free(self):
        for command in ("record", "summarize", "stage", "apply", "rollback"):
            before = self.manifest(self.root)
            self.invoke(command, "--no-write")
            self.assertEqual(before, self.manifest(self.root))
        self.assertFalse(self.shared.exists())
        for flag in ("GAD_EVALUATION", "GAD_LEARNING_DISABLED"):
            self.invoke("record", env={flag: "1"}, expected=1)
            self.assertFalse(self.shared.exists())

    def test_fresh_subprocess_no_write_creates_no_bytecode_or_directories(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        for name in ("learning.py", "regress.py", "executors.py", "progress.py", "delivery_io.py"):
            shutil.copyfile(SCRIPT.with_name(name), fresh / name)
        before = self.manifest(self.root)
        for command in ("record", "summarize", "stage", "apply", "rollback"):
            process = subprocess.run([sys.executable, str(fresh / "learning.py"), command, "--no-write",
                                      "--workspace", str(self.workspace), "--data-dir", str(self.shared)],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(before, self.manifest(self.root))

    def test_same_task_not_two_task_gate_but_correction_is_candidate_source(self):
        one = self.record(correction=False)
        two = self.record(correction=False)
        proposal = self.proposal(records=[one["record_id"], two["record_id"]])
        self.stage(proposal, expected=1)
        third = self.record(task="other-task", correction=False)
        staged = self.stage(self.proposal(records=[one["record_id"], third["record_id"]]))
        self.assertEqual(staged["status"], "staged")

    def test_two_tasks_without_optional_feedback_record_summarize_and_stage(self):
        records = []
        for task in ("first-task", "second-task"):
            path = self.write(self.root / "record.json", json.dumps({
                "task_id": task, "category": "recovery", "acceptance": "failed"}))
            records.append(self.invoke("record", "--input", path)["record_id"])
        summary = self.invoke("summarize")["categories"][0]
        self.assertEqual(summary["independent_tasks"], 2)
        self.assertEqual(summary["user_feedback"]["count"], 0)
        self.assertEqual(self.stage(self.proposal(records=records))["status"], "staged")

    def test_historical_missing_feedback_keeps_independent_task_gate(self):
        records = []
        for task in ("same-task", "same-task", "different-task"):
            recorded = self.record(task=task, user_feedback=[])
            path = Path(recorded["detail_path"])
            value = json.loads(path.read_text())
            value.pop("user_feedback", None)
            self.write(path, json.dumps(value))
            records.append(recorded["record_id"])
            if len(records) == 2:
                self.assertEqual(self.invoke("summarize")["categories"][0]["independent_tasks"], 1)
                self.stage(self.proposal(records=records), expected=1)
        self.assertEqual(self.invoke("summarize")["categories"][0]["independent_tasks"], 2)
        self.assertEqual(self.stage(self.proposal(records=records))["status"], "staged")

    def test_explicit_invalid_feedback_is_rejected_on_record_and_stage(self):
        invalid = (None, {}, "feedback", 0, False, [None],
                   [{"text": "feedback", "explicit_correction": "yes"}])
        for feedback in invalid:
            with self.subTest(boundary="record", feedback=feedback):
                self.assertEqual(self.record(user_feedback=feedback, expected=2)["status"], "invalid")
        records = [self.record(task=task, user_feedback=[]) for task in ("first-task", "second-task")]
        path = Path(records[0]["detail_path"])
        original = json.loads(path.read_text())
        for feedback in invalid:
            with self.subTest(boundary="stage", feedback=feedback):
                self.write(path, json.dumps({**original, "user_feedback": feedback}))
                result = self.stage(self.proposal(records=[r["record_id"] for r in records]), expected=2)
                self.assertEqual(result["status"], "invalid")
        self.assertFalse((self.workspace / "work/skill-learning/goal-aligned-delivery/current.json").exists())

    def test_stage_copy_required_cases_and_one_candidate_per_cycle(self):
        before = self.manifest(self.skill)
        staged = self.stage()
        self.assertEqual(set(staged["required_cases"]), {"resume", "lightweight", "no-write"})
        self.assertEqual(before, self.manifest(self.skill))
        self.assertEqual((self.candidate(staged["candidate_id"]) / "skill/references/principles.md").read_text(), "After\n")
        self.stage(expected=1)

    def test_stage_new_case_persists_private_definition_source_and_change_identity(self):
        staged = self.stage_new_case()
        candidate_dir = self.candidate(staged["candidate_id"])
        state = json.loads((candidate_dir / "candidate.json").read_text())
        self.assertEqual(state.get("new_cases"), ["new-correction"])
        self.assertEqual(staged.get("case_definition_source"),
                         str(candidate_dir / state.get("case_definition_source", {}).get("path", "missing")))
        source_path = Path(staged.get("case_definition_source", candidate_dir / "missing"))
        self.assertTrue(source_path.is_file())
        self.assertEqual(state.get("case_definition_source", {}).get("sha256"),
                         self.l.file_hash(source_path))
        source = json.loads(source_path.read_text())
        self.assertEqual(source["case_ids"], ["new-correction"])
        self.assertTrue(source["source_records"][0]["evaluation"]["user_feedback"][0]["explicit_correction"])
        self.assertEqual(state["original_skill_sha256"], state["candidate_skill_sha256"])
        self.assertNotEqual(canonical_hash(state.get("original_change_manifest", [])),
                            canonical_hash(state.get("candidate_change_manifest", [])))

    def test_case_only_candidate_applies_with_bound_definition_review_and_change_identity(self):
        staged = self.stage_new_case()
        result = self.apply_fixture(staged)
        self.assertEqual(result["status"], "applied")
        self.assertTrue((self.skill / "cases/new-correction/oracle.json").is_file())

    def test_new_case_cannot_apply_without_bound_definition_review(self):
        staged = self.stage_new_case(include_reference=True)
        reports, compare = self.reports(staged)
        for report in reports.values():
            report["case_definition_review"] = None
        result = self.apply_fixture(staged, reports=reports, compare=compare, expected=1)
        self.assertEqual(result["status"], "pending")

    def test_new_case_source_and_request_input_oracle_bindings_reject_tampering(self):
        staged = self.stage_new_case(include_reference=True)
        candidate = json.loads((self.candidate(staged["candidate_id"]) / "candidate.json").read_text())
        for mutation in ("source", "request", "input", "oracle", "change-manifest"):
            reports, compare = self.reports(staged)
            with self.subTest(mutation=mutation):
                for report in reports.values():
                    if mutation == "source":
                        report["case_definition_review"]["source"]["sha256"] = "0" * 64
                    elif mutation == "change-manifest":
                        report["change_manifest"] = [entry for entry in report["change_manifest"]
                                                     if entry["path"] != "cases/new-correction/request.md"]
                    else:
                        suffix = {"request": "request.md", "input": "inputs/source.txt",
                                  "oracle": "oracle.json"}[mutation]
                        plan = next(item for item in report["case_plan"]
                                    if item["case_id"] == "new-correction")
                        for entry in plan["manifest"]:
                            if entry["path"] == suffix:
                                entry["sha256"] = "0" * 64
                result = self.apply_fixture(staged, reports=reports, compare=compare, expected=1)
                self.assertEqual(result["status"], "pending")
        self.assertFalse((self.skill / "cases/new-correction").exists())

    def test_scope_and_old_case_protections(self):
        for path in ("scripts/engine.py", "cases/old/outputs/raw.txt", "../escape.md",
                     "References/principles.md", "references/authority.md", "references/learning.md",
                     "references/behavior-cases.md", "references/x.md:ads", "references/../SKILL.md"):
            with self.subTest(path=path):
                self.stage(self.proposal(changes=[{"path": path, "content": "unsafe"}]), expected=2)
        self.assertFalse((self.workspace / "work/skill-learning/goal-aligned-delivery/current.json").exists())

    def test_core_execution_changes_require_full_fixed_suite(self):
        staged = self.stage(self.proposal(changes=[{"path": "references/progress.md", "content": "new"}]))
        self.assertEqual(len(staged["required_cases"]), 8)

    def test_protected_skill_sections_missing_repeated_or_changed_are_rejected(self):
        original = (self.skill / "SKILL.md").read_text()
        self.stage(self.proposal(changes=[{"path": "SKILL.md", "content": original.replace("Keep authority.", "Do anything.")}]), expected=2)
        self.write(self.skill / "SKILL.md", "# no markers")
        self.stage(self.proposal(changes=[{"path": "SKILL.md", "content": "# edited"}]), expected=2)
        self.write(self.skill / "SKILL.md", original + "<!-- GAD:AUTHORITY:BEGIN -->")
        self.stage(self.proposal(changes=[{"path": "SKILL.md", "content": original}]), expected=2)

    def test_budget_expiry_and_explicit_next_cycle_preserve_same_candidate(self):
        staged = self.stage()
        candidate_file = self.candidate(staged["candidate_id"]) / "candidate.json"
        state = json.loads(candidate_file.read_text())
        state["cycles"][-1]["started_at"] -= 601
        self.write(candidate_file, json.dumps(state))
        self.invoke("apply", "--candidate", staged["candidate_id"], expected=1)
        resumed = self.invoke("stage", "--candidate", staged["candidate_id"], "--new-cycle")
        self.assertEqual(resumed["candidate_id"], staged["candidate_id"])
        state = json.loads(candidate_file.read_text())
        self.assertEqual(len(state["cycles"]), 2)
        self.assertGreaterEqual(state["cycles"][0]["elapsed_seconds"], 600)

    def test_unverified_corrupt_offline_simulated_and_duplicate_reports_cannot_promote(self):
        staged = self.stage()
        bad = self.write(self.root / "bad-report.json", "{}")
        self.invoke("apply", "--candidate", staged["candidate_id"], "--reports", bad, expected=1)
        for field, value in (("mode", "offline"), ("simulated_executor", True), ("status", "unverified")):
            reports, compare = self.reports(staged)
            for report in reports.values():
                report[field] = value
            self.apply_fixture(staged, reports, compare, expected=1)
        reports, compare = self.reports(staged)
        for report in reports.values():
            report["run_id"] = "same-run"
        self.apply_fixture(staged, reports, compare, expected=1)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")

    def test_functional_gate_requires_reproducible_failure_twice_and_candidate_independence(self):
        staged = self.stage()
        reports, compare = self.reports(staged, count=1)
        self.apply_fixture(staged, reports, compare, expected=1)
        reports, compare = self.reports(staged)
        def reused(a, b):
            result = compare(a, b)
            result["independent_runs"] = False
            return result
        self.apply_fixture(staged, reports, reused, expected=1)
        for path, report in reports.items():
            if "base-" in path:
                for case in report["cases"]:
                    case["status"] = "passed"
        self.apply_fixture(staged, reports, compare, expected=1)

    def test_candidate_or_physical_old_fixture_or_target_drift_blocks_apply(self):
        for location in ("candidate", "case", "new-target"):
            with self.subTest(location=location):
                with tempfile.TemporaryDirectory() as isolated:
                    # Each branch must begin with a separately staged target.
                    branch = Path(isolated)
                    original_workspace = self.workspace
                    self.workspace = branch / "workspace"
                    self.workspace.mkdir()
                    staged = self.stage()
                    reports, compare = self.reports(staged)
                    if location == "candidate":
                        self.write(self.candidate(staged["candidate_id"]) / "skill/references/principles.md", "tampered")
                    elif location == "case":
                        self.write(self.skill / "cases/old/outputs/raw.txt", "user changed")
                    else:
                        self.write(self.skill / "references/new-user-file.md", "user content")
                    self.apply_fixture(staged, reports, compare, expected=1)
                    self.workspace = original_workspace
                    if location == "case":
                        self.write(self.skill / "cases/old/outputs/raw.txt", "original fixture")
                    if location == "new-target":
                        (self.skill / "references/new-user-file.md").unlink()

    def test_controlled_fixture_end_to_end_apply_progress_resume_rollback(self):
        staged = self.stage()
        applied = self.apply_fixture(staged)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")
        # Real progress CLI survives a fresh process; candidate control stays in the original task.
        progress_state = {
            "schema_version": 1, "revision": 1, "task_id": "controlled-learning-e2e",
            "title": "Controlled fixture", "goal": "Restore the task after local application",
            "constraints": ["Report fixtures are protocol evidence only"],
            "baseline_inputs": [], "acceptance_criteria": [{"id": "AC-1", "description": "Local state restored"}],
            "overall_status": "in_progress",
            "units": [{"id": "U-1", "title": "Resume", "required": True, "status": "in_progress",
                       "depends_on": [], "artifacts": [], "acceptance_results": [
                           {"criterion_id": "AC-1", "status": "unverified", "summary": "Pending", "evidence": []}]}],
            "blockers": [], "next_steps": ["Rollback candidate " + staged["candidate_id"]],
            "decisions": [{"timestamp": "2026-10-15T12:00:00+08:00",
                           "summary": "Private candidate: " + str(self.candidate(staged["candidate_id"])),
                           "reason": "Continue the same controlled task", "affected_units": ["U-1"]}],
            "operations": [], "updated_at": "2026-10-15T12:00:00+08:00"}
        source = self.write(self.root / "progress-input.json", json.dumps(progress_state))
        progress_path = self.workspace / "work/task-progress/task.json"
        progress_script = SCRIPT.with_name("progress.py")
        initialized = subprocess.run([sys.executable, "-B", str(progress_script), "init", str(progress_path),
                                      "--state", str(source), "--root", str(self.workspace)],
                                     capture_output=True, text=True)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        restored = subprocess.run([sys.executable, "-B", str(progress_script), "check", str(progress_path),
                                   "--root", str(self.workspace)], capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(self.l.regress.progress.load_record(progress_path)["decisions"][0]["summary"],
                         "Private candidate: " + str(self.candidate(staged["candidate_id"])))
        self.assertEqual(self.invoke("summarize")["categories"][0]["validated_experience"],
                         ["Confirm durable state before repeating an operation."])
        rolled = self.invoke("rollback", "--reason", "controlled fixture restoration")
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")
        self.assertEqual(self.invoke("rollback", "--reason", "repeat")["status"], "rolled_back")
        shared = "\n".join(p.read_text() for p in self.shared.rglob("*.json"))
        self.assertNotIn(str(self.workspace), shared)
        self.assertNotIn(str(self.skill), shared)

    def test_efficiency_requires_three_measured_independent_pairs_and_lower_median(self):
        staged = self.stage(self.proposal(kind="efficiency", efficiency_metric="elapsed_seconds"))
        reports, compare = self.reports(staged, kind="efficiency", count=2)
        self.apply_fixture(staged, reports, compare, expected=1)
        reports, compare = self.reports(staged, kind="efficiency", count=3)
        for path, report in reports.items():
            if "candidate-" in path:
                for case in report["cases"]:
                    case["metrics"]["elapsed_seconds"]["source"] = "reported"
        self.apply_fixture(staged, reports, compare, expected=1)
        self.apply_fixture(staged, *self.reports(staged, kind="efficiency", count=3))

    def test_functional_accepts_one_reproduced_baseline_and_two_candidate_runs(self):
        staged = self.stage()
        reports, compare = self.reports(staged)
        reports.pop(next(path for path in reports if path.endswith("base-1.json")))
        self.assertEqual(self.apply_fixture(staged, reports, compare)["status"], "applied")

    def test_efficiency_uses_real_regress_token_provenance(self):
        staged = self.stage(self.proposal(kind="efficiency", efficiency_metric="tokens"))
        self.assertEqual(self.apply_fixture(staged, *self.reports(staged, kind="efficiency", count=3))["status"], "applied")

    def test_real_process_exit_between_file_writes_recovers_and_releases_lock(self):
        staged = self.stage(self.proposal(changes=[
            {"path": "references/principles.md", "content": "After\n"},
            {"path": "references/new.md", "content": "New\n"}]))
        reports, _ = self.reports(staged)
        fixture = self.write(self.root / "CONTROLLED-REPORT-FIXTURE.json", json.dumps(reports))
        child = (
            "import sys,os,json; sys.dont_write_bytecode=True; "
            "sys.path.insert(0,sys.argv[1]); import learning; "
            "reports=json.load(open(sys.argv[2])); "
            "learning.regress.load_report=lambda p:reports[str(p)]; "
            "learning.regress.compare_reports=lambda a,b:dict(identity_compatible=True,independent_runs=True,"
            "regressions=[],unverified=[],status='passed'); "
            "real=learning._replace_target\n"
            "def crash(path,source,**kwargs):\n"
            " real(path,source,**kwargs)\n"
            " os._exit(77)\n"
            "learning._replace_target=crash\n"
            "learning.main(sys.argv[3:])"
        )
        process = subprocess.run([sys.executable, "-B", "-c", child, str(SCRIPT.parent), str(fixture),
                                  "apply", "--candidate", staged["candidate_id"], "--reports", *reports,
                                  "--workspace", str(self.workspace), "--data-dir", str(self.shared)],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 77, process.stdout + process.stderr)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")
        self.assertEqual(self.invoke("rollback", "--reason", "recover actual process exit")["status"], "rolled_back")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")
        self.assertFalse((self.skill / "references/new.md").exists())

    def test_interrupted_multifile_apply_recovers_from_persistent_journal(self):
        staged = self.stage(self.proposal(changes=[
            {"path": "references/principles.md", "content": "After\n"},
            {"path": "references/new.md", "content": "New\n"}]))
        real = self.l._replace_target
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SystemExit("simulated process interruption; no exception rollback")
            return real(*args, **kwargs)
        with patch.object(self.l, "_replace_target", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.apply_fixture(staged)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")
        recovered = self.invoke("apply", "--candidate", staged["candidate_id"], expected=1)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")
        self.assertFalse((self.skill / "references/new.md").exists())

    def test_rollback_preserves_user_edits_and_new_files(self):
        staged = self.stage()
        self.apply_fixture(staged)
        self.write(self.skill / "references/principles.md", "USER EDIT")
        self.write(self.skill / "references/user.md", "USER FILE")
        self.invoke("rollback", "--reason", "user wants restoration", expected=1)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "USER EDIT")
        self.assertEqual((self.skill / "references/user.md").read_text(), "USER FILE")

    def test_data_dir_cannot_capture_private_evaluations(self):
        self.shared = self.workspace / "work/task-evaluations"
        self.record(expected=2)

    def test_existing_empty_case_directory_is_protected(self):
        (self.skill / "cases/empty-original").mkdir()
        self.stage(self.proposal(changes=[{"path": "cases/empty-original/new.txt", "content": "not new case"}]),
                   expected=2)

    def test_cross_workspace_target_cas_rejects_second_candidate(self):
        first = self.stage()
        first_workspace = self.workspace
        self.workspace = self.root / "second-workspace"
        self.workspace.mkdir()
        second = self.stage()
        second_workspace = self.workspace
        self.workspace = first_workspace
        self.apply_fixture(first)
        self.workspace = second_workspace
        self.apply_fixture(second, expected=1)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")

    def test_atomic_replace_failure_retains_recoverable_journal(self):
        staged = self.stage()
        original = self.manifest(self.skill)
        real = os.replace
        with patch.object(self.l.os, "replace", wraps=os.replace) as replace:
            def fail_target(source, target):
                if Path(target) == self.skill / "references/principles.md":
                    raise OSError("controlled atomic replacement failure")
                return real(source, target)
            replace.side_effect = fail_target
            self.apply_fixture(staged, expected=1)
        self.assertEqual(self.manifest(self.skill), original)
        self.assertEqual(self.invoke("rollback", "--reason", "recover failed atomic replace")["status"], "rolled_back")

    def test_missing_review_different_case_or_report_identity_cannot_promote(self):
        staged = self.stage()
        for mutation in ("review", "case", "model"):
            reports, compare = self.reports(staged)
            for path, report in reports.items():
                if "candidate-" not in path:
                    continue
                if mutation == "review":
                    report["candidate_review"] = None
                elif mutation == "case":
                    report["case_plan"][0]["manifest"] = []
                else:
                    report["identity"]["model_config"] = {"model": "different"}
            def actual_identity(a, b):
                result = compare(a, b)
                result["identity_compatible"] = reports[str(a)]["identity"]["model_config"] == reports[str(b)]["identity"]["model_config"]
                return result
            self.apply_fixture(staged, reports, actual_identity, expected=1)

    def test_missing_reports_are_pending_and_preserve_candidate(self):
        staged = self.stage()
        self.invoke("apply", "--candidate", staged["candidate_id"], expected=1)
        self.assertTrue((self.candidate(staged["candidate_id"]) / "candidate.json").exists())

    def test_production_rejects_an_actual_complete_simulated_task2_report(self):
        from test_regress import FAKE_EXECUTOR
        staged = self.stage()
        engine = self.write(self.root / "simulated_executor.py", FAKE_EXECUTOR)
        protocol_skill = self.root / "protocol-skill"
        self.write(protocol_skill / "SKILL.md", "# Fixed protocol skill\n")
        case_root = protocol_skill / "cases"
        self.write(case_root / "fixture/request.md", "Calculate and save result.json.")
        self.write(case_root / "fixture/inputs/source.csv", "x\n37\n")
        self.write(case_root / "fixture/oracle.json", json.dumps({
            "schema_version": 1, "case_id": "fixture", "route": "standard",
            "stages": [{"request": "request.md"}], "mutable_inputs": [],
            "checks": [{"id": "total", "kind": "json", "path": "result.json", "expected": {"total": 37}}],
            "semantic_criteria": [{"id": "meaning", "description": "Confirm the fixed result"}]}))
        output = self.root / "actual-protocol-report"
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.l.regress.main(["run", "--mode", "live", "--skill", str(protocol_skill),
                "--cases-root", str(case_root), "--output", str(output), "--executor", str(engine),
                "--model", "controlled-fixture", "--timeout", "3"])
        self.assertEqual(code, 0)
        verified = self.l.regress.load_report(output / "report.json")
        self.assertEqual(verified["status"], "passed")
        self.assertIs(verified["simulated_executor"], True)
        result = self.invoke("apply", "--candidate", staged["candidate_id"],
                             "--reports", output / "report.json", expected=1)
        self.assertIn("simulated", result["reason"])
        (output / verified["execution_identity"]["path"]).write_text("{}", encoding="utf-8")
        result = self.invoke("apply", "--candidate", staged["candidate_id"],
                             "--reports", output / "report.json", expected=1)
        self.assertIn("incomplete or invalid", result["reason"])

    def test_crash_before_atomic_replace_cleans_owned_temporary_on_recovery(self):
        staged = self.stage()
        original = self.manifest(self.skill)
        real = os.replace
        def crash_before_replace(source, target):
            if Path(target) == self.skill / "references/principles.md":
                raise SystemExit("crash before target replace")
            return real(source, target)
        # Simulate fatal interruption skipping _replace_target cleanup, as a killed process would.
        with patch.object(self.l.os, "replace", side_effect=crash_before_replace):
            with patch.object(self.l.Path, "unlink", side_effect=SystemExit("no cleanup after kill")):
                with self.assertRaises(SystemExit):
                    self.apply_fixture(staged)
        self.assertNotEqual(original, self.manifest(self.skill))
        self.invoke("rollback", "--reason", "recover temp-file crash")
        self.assertEqual(original, self.manifest(self.skill))

    def test_two_successive_candidates_keep_versions_and_rollback_one_at_a_time(self):
        first = self.stage()
        self.apply_fixture(first)
        second = self.stage(self.proposal(changes=[{"path": "references/principles.md", "content": "After second\n"}]))
        self.apply_fixture(second)
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After second\n")
        self.invoke("rollback", "--reason", "undo second version")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")
        self.invoke("rollback", "--reason", "repeat is idempotent")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "After\n")
        self.invoke("rollback", "--candidate", first["candidate_id"], "--reason", "explicitly undo first version")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")
        self.invoke("rollback", "--candidate", first["candidate_id"], "--reason", "explicit repeat is also idempotent")
        versions = self.workspace / "work/skill-learning/goal-aligned-delivery/versions"
        self.assertEqual(len(list(versions.glob("*.json"))), 2)

    def test_rollback_replay_finishes_head_update_after_process_interruption(self):
        staged = self.stage()
        self.apply_fixture(staged)
        head = self.workspace / "work/skill-learning/goal-aligned-delivery/head.json"
        real = Path.unlink
        def crash_before_head_pop(path, *args, **kwargs):
            if path == head:
                raise SystemExit("crash after restoration before version head update")
            return real(path, *args, **kwargs)
        with patch.object(Path, "unlink", new=crash_before_head_pop):
            with self.assertRaises(SystemExit):
                self.invoke("rollback", "--reason", "first attempt")
        self.assertTrue(head.exists())
        self.invoke("rollback", "--reason", "resume rollback")
        self.assertFalse(head.exists())

    def test_rollback_preserves_user_replacement_directory_as_conflict(self):
        staged = self.stage()
        self.apply_fixture(staged)
        target = self.skill / "references/principles.md"
        target.unlink()
        target.mkdir()
        self.write(target / "user.txt", "USER DIRECTORY CONTENT")
        result = self.invoke("rollback", "--reason", "preserve user replacement", expected=1)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual((target / "user.txt").read_text(), "USER DIRECTORY CONTENT")

    def test_old_case_raw_bytes_are_protected_even_in_cache_named_fixture_directory(self):
        material = self.write(self.skill / "cases/old/inputs/__pycache__/raw.txt", "fixed raw material")
        staged = self.stage()
        self.write(material, "USER CHANGED RAW MATERIAL")
        self.apply_fixture(staged, expected=1)

    def _finalization_crash_roundtrip(self, legacy_applied=False):
        first = self.stage()
        real_write = self.l.write_json
        def crash_before_version(path, value):
            if Path(path).parent.name == "versions":
                raise SystemExit("after target commit, before durable version")
            return real_write(path, value)
        with patch.object(self.l, "write_json", side_effect=crash_before_version):
            with self.assertRaises(SystemExit):
                self.apply_fixture(first)
        first_file = self.candidate(first["candidate_id"]) / "candidate.json"
        interrupted = json.loads(first_file.read_text(encoding="utf-8"))
        if legacy_applied:
            interrupted["status"] = "applied"
            interrupted.pop("finalization_complete", None)
            self.write(first_file, json.dumps(interrupted))
        second = self.stage(self.proposal(changes=[
            {"path": "references/principles.md", "content": "After second\n"}]))
        self.apply_fixture(second)
        self.invoke("rollback", "--reason", "undo second version after interrupted first finalization")
        self.invoke("rollback", "--candidate", first["candidate_id"], "--reason", "undo recovered first version")
        self.assertEqual((self.skill / "references/principles.md").read_text(), "Before\n")
        versions = self.workspace / "work/skill-learning/goal-aligned-delivery/versions"
        self.assertEqual(len(list(versions.glob("*.json"))), 2)
        if not legacy_applied:
            self.assertNotEqual(interrupted["status"], "applied")
        complete = json.loads(first_file.read_text(encoding="utf-8"))
        self.assertIs(complete["finalization_complete"], True)

    def test_crash_before_version_next_direct_stage_preserves_both_rollback_entries(self):
        self._finalization_crash_roundtrip()

    def test_stage_repairs_legacy_applied_state_missing_its_version(self):
        self._finalization_crash_roundtrip(legacy_applied=True)

    def test_malformed_persisted_states_return_json_two_from_fresh_cli(self):
        staged = self.stage()
        applied = self.apply_fixture(staged)
        private = self.workspace / "work/skill-learning/goal-aligned-delivery"
        identifier = staged["candidate_id"]
        paths = {
            "experience": self.shared / "experience.json",
            "candidate": self.candidate(identifier) / "candidate.json",
            "current": private / "current.json",
            "head": private / "head.json",
            "journal": self.candidate(identifier) / "journal.json",
            "version": private / "versions" / (applied["version"] + ".json"),
        }
        baseline = self.manifest(self.root)
        for name, path in paths.items():
            good = json.loads(path.read_text(encoding="utf-8"))
            nested = {
                "experience": (("records", [None]), ("lessons", [None]), ("salt", [])),
                "candidate": (("cycles", [None]), ("proposal", [])),
                "current": (("candidate_id", []),),
                "head": (("version", []),),
                "journal": (("entries", [None]),),
                "version": (("previous", []), ("entries", [None])),
            }[name]
            malformed = [[], None, "malformed state", {}]
            malformed += [{**copy.deepcopy(good), field: value} for field, value in nested]
            for value in malformed:
                with self.subTest(state=name, value=value):
                    self.write(path, json.dumps(value))
                    command = (["summarize"] if name == "experience" else
                               ["rollback", "--reason", "malformed state check"] if name in {"head", "version"} else
                               ["apply", "--candidate", identifier])
                    process = subprocess.run([sys.executable, "-B", str(SCRIPT), *command,
                        "--workspace", str(self.workspace), "--data-dir", str(self.shared)],
                        capture_output=True, text=True)
                    try:
                        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                        self.assertEqual(json.loads(process.stdout)["status"], "invalid")
                        self.assertEqual(process.stderr, "")
                    finally:
                        for relative, data in baseline.items():
                            target = self.root / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(data)

    def _make_protocol_report(self, *, usage="known"):
        """Actual Task2 subprocess report; explicitly simulated, never promotion evidence."""
        from test_regress import FAKE_EXECUTOR
        source = FAKE_EXECUTOR
        if usage == "absent":
            source = source.replace('"input_tokens":7,"cached_input_tokens":0,"output_tokens":3', '')
        engine = self.write(self.root / ("simulated-" + usage + ".py"), source)
        protocol_skill = self.root / "metric-protocol-skill"
        self.write(protocol_skill / "SKILL.md", "# Fixed metric protocol skill\n")
        case_root = protocol_skill / "cases"
        self.write(case_root / "fixture/request.md", "Calculate and save result.json.")
        self.write(case_root / "fixture/inputs/source.csv", "x\n37\n")
        self.write(case_root / "fixture/oracle.json", json.dumps({
            "schema_version": 1, "case_id": "fixture", "route": "standard",
            "stages": [{"request": "request.md"}], "mutable_inputs": [],
            "checks": [{"id": "total", "kind": "json", "path": "result.json", "expected": {"total": 37}}],
            "semantic_criteria": [{"id": "meaning", "description": "Confirm the fixed result"}]}))
        output = self.root / ("metric-protocol-" + usage)
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.l.regress.main(["run", "--mode", "live", "--skill", str(protocol_skill),
                "--cases-root", str(case_root), "--output", str(output), "--executor", str(engine),
                "--model", "controlled-fixture", "--timeout", "3"])
        self.assertEqual(code, 0)
        return output / "report.json"

    def test_task2_metric_summary_tampering_is_rejected_before_promotion(self):
        staged = self.stage(self.proposal(kind="efficiency", efficiency_metric="tokens"))
        report_path = self._make_protocol_report()
        original = json.loads(report_path.read_text(encoding="utf-8"))
        verified = self.l.regress.load_report(report_path)
        self.assertEqual(verified["cases"][0]["metrics"]["tokens"]["value"]["input_tokens"], 14)
        for scope in ("case", "report"):
            for metric, value, source in (
                ("tokens", {"total_tokens": 0}, "turn.completed.usage"),
                ("tool_calls", 0, "item.completed"),
                ("elapsed_seconds", 0, "monotonic-clock")):
                with self.subTest(scope=scope, metric=metric):
                    changed = copy.deepcopy(original)
                    location = changed["cases"][0] if scope == "case" else changed
                    # The fixture has no tool items; make tool count nonzero to alter its true zero.
                    location["metrics"][metric] = {"value": 1 if metric == "tool_calls" else value,
                                                    "source": source}
                    self.write(report_path, json.dumps(changed))
                    result = self.invoke("apply", "--candidate", staged["candidate_id"],
                                         "--reports", report_path, expected=1)
                    self.assertIn("incomplete or invalid", result["reason"])
        self.write(report_path, json.dumps(original))
        self.assertEqual(self.l.regress.load_report(report_path)["status"], "passed")

    def test_task2_missing_usage_remains_unavailable_and_cannot_be_zeroed(self):
        staged = self.stage(self.proposal(kind="efficiency", efficiency_metric="tokens"))
        report_path = self._make_protocol_report(usage="absent")
        verified = self.l.regress.load_report(report_path)
        self.assertEqual(verified["cases"][0]["metrics"]["tokens"],
                         {"value": None, "source": "unavailable"})
        with self.assertRaises(self.l.ConditionError):
            self.l._measured_cost(verified["cases"][0]["metrics"]["tokens"], "tokens")
        changed = json.loads(report_path.read_text(encoding="utf-8"))
        changed["cases"][0]["metrics"]["tokens"] = {"value": {"total_tokens": 0}, "source": "turn.completed.usage"}
        self.write(report_path, json.dumps(changed))
        result = self.invoke("apply", "--candidate", staged["candidate_id"],
                             "--reports", report_path, expected=1)
        self.assertIn("incomplete or invalid", result["reason"])

    def test_real_legacy_elapsed_cannot_supply_efficiency_cost_and_is_readonly(self):
        report_path = SCRIPT.resolve().parents[2] / "live-baseline/report.json"
        self.assertTrue(report_path.is_file(), "required existing real baseline is missing")
        before = self.manifest(report_path.parent)
        verified = self.l.regress.load_report(report_path)
        compared = self.l.regress.compare_reports(report_path, report_path)
        self.assertTrue(compared["identity_compatible"], compared)
        self.assertEqual(verified["metrics"]["elapsed_seconds"], {"value": None, "source": "unavailable"})
        for case in verified["cases"]:
            with self.assertRaises(self.l.ConditionError):
                self.l._measured_cost(case["metrics"]["elapsed_seconds"], "elapsed_seconds")
        staged = self.stage(self.proposal(kind="efficiency", efficiency_metric="elapsed_seconds"))
        reports, compare = self.reports(staged, kind="efficiency", count=3)
        for report in reports.values():
            for case in report["cases"]:
                case["metrics"]["elapsed_seconds"] = copy.deepcopy(verified["metrics"]["elapsed_seconds"])
        self.apply_fixture(staged, reports, compare, expected=1)
        self.assertEqual(before, self.manifest(report_path.parent))


if __name__ == "__main__":
    unittest.main()
