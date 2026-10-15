"""进度记录工具的合成测试；不接触真实外部服务。"""

import contextlib
import copy
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import delivery_io
import progress


class SyntheticInterruption(Exception):
    pass


class SyntheticQueryFailure(Exception):
    pass


class IsolatedExternalService:
    def __init__(self, path):
        self.path = Path(path)
        self.path.write_text(json.dumps({
            "applied": {}, "attempt_calls": {}, "apply_count": 0,
        }), encoding="utf-8")

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state):
        self.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def execute(self, idempotency_key, attempt_id, *, interrupt=None):
        state = self._read()
        state["attempt_calls"][attempt_id] = state["attempt_calls"].get(attempt_id, 0) + 1
        if interrupt == "before_apply":
            self._write(state)
            raise SyntheticInterruption("interrupted before apply")
        if idempotency_key not in state["applied"]:
            state["applied"][idempotency_key] = attempt_id
            state["apply_count"] += 1
        self._write(state)
        if interrupt == "after_apply":
            raise SyntheticInterruption("response lost after apply")
        return {"status": "succeeded", "attempt_id": attempt_id}

    def query(self, idempotency_key, *, fail=False):
        if fail:
            raise SyntheticQueryFailure("synthetic query unavailable")
        state = self._read()
        return "applied" if idempotency_key in state["applied"] else "not_applied"

    @property
    def state(self):
        return self._read()


def concurrent_update(path, state_path, expected_revision, start, results):
    start.wait()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = progress.main([
            "update", path, "--state", state_path,
            "--expected-revision", str(expected_revision),
        ])
    results.put((code, json.loads(output.getvalue())))


def crash_while_holding_lock(path, ready):
    with delivery_io.locked(path):
        ready.set()
        os._exit(17)


def acquire_lock_after_crash(path, acquired):
    with delivery_io.locked(path):
        acquired.set()


class DeliveryIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery-io-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_read_json_accepts_bom_and_rejects_unsafe_inputs(self):
        path = self.root / "value.json"
        path.write_bytes(b"\xef\xbb\xbf{\"answer\": 42}")
        self.assertEqual(delivery_io.read_json(path), {"answer": 42})

        for raw in (
            b'{"a": 1, "a": 2}',
            b'{"value": NaN}',
            b'{"value": 1e400}',
        ):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(delivery_io.UsageError):
                    delivery_io.read_json(path)

        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(delivery_io.UsageError):
            delivery_io.read_json(path)

    def test_canonical_and_file_hashes_are_stable(self):
        left = {"b": [2, 1], "a": "\u4e2d"}
        right = {"a": "\u4e2d", "b": [2, 1]}
        self.assertEqual(delivery_io.canonical_hash(left), delivery_io.canonical_hash(right))
        path = self.root / "artifact.txt"
        path.write_bytes(b"artifact")
        self.assertEqual(
            delivery_io.file_hash(path),
            "c7c5c1d70c5dec4416ab6158afd0b223ef40c29b1dc1f97ed9428b94d4cadb1c",
        )

    def test_atomic_write_keeps_old_file_and_cleans_temp_on_replace_failure(self):
        path = self.root / "record.md"
        path.write_text("old", encoding="utf-8")
        with mock.patch("delivery_io.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                delivery_io.atomic_write(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.root.glob(".record.md.*.tmp")), [])

    def test_atomic_create_never_replaces_competing_target(self):
        creator = getattr(delivery_io, "atomic_create", None)
        self.assertIsNotNone(creator)
        target = self.root / "new-record.md"
        real_link = os.link

        def competing_link(source, destination):
            Path(destination).write_text("outsider", encoding="utf-8")
            return real_link(source, destination)

        with mock.patch("delivery_io.os.link", side_effect=competing_link):
            with self.assertRaises(FileExistsError):
                creator(target, "ours")
        self.assertEqual(target.read_text(encoding="utf-8"), "outsider")
        self.assertEqual(list(self.root.glob(".new-record.md.*.tmp")), [])

    def test_process_crash_releases_os_lock_for_next_process(self):
        target = self.root / "record.md"
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        crashed = context.Process(target=crash_while_holding_lock, args=(str(target), ready))
        crashed.start()
        self.assertTrue(ready.wait(timeout=5))
        crashed.join(timeout=5)
        self.assertEqual(crashed.exitcode, 17)

        acquired = context.Event()
        follower = context.Process(
            target=acquire_lock_after_crash,
            args=(str(target), acquired),
        )
        follower.start()
        self.assertTrue(acquired.wait(timeout=5))
        follower.join(timeout=5)
        if follower.is_alive():
            follower.terminate()
            follower.join(timeout=5)
            self.fail("follower acquired the lock but did not exit")
        self.assertEqual(follower.exitcode, 0)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="progress-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "progress.md"
        self.artifact = self.root / "result.txt"
        self.artifact.write_text("verified result", encoding="utf-8")
        self.state = self.make_state()

    def make_state(self):
        return {
            "schema_version": 1,
            "revision": 1,
            "task_id": "task-fixture",
            "title": "\u5408\u6210\u4efb\u52a1",
            "goal": "\u4ea4\u4ed8\u53ef\u9a8c\u8bc1\u7ed3\u679c",
            "constraints": ["\u4e0d\u8bbf\u95ee\u771f\u5b9e\u5916\u90e8\u670d\u52a1"],
            "baseline_inputs": [{"path": "input.txt", "summary": "\u5408\u6210\u8f93\u5165"}],
            "acceptance_criteria": [{"id": "AC-1", "description": "\u7ed3\u679c\u53ef\u6838\u9a8c"}],
            "overall_status": "in_progress",
            "units": [{
                "id": "U-1",
                "title": "\u751f\u6210\u7ed3\u679c",
                "required": True,
                "status": "in_progress",
                "depends_on": [],
                "artifacts": [],
                "acceptance_results": [{
                    "criterion_id": "AC-1",
                    "status": "unverified",
                    "summary": "\u5c1a\u672a\u9a8c\u8bc1",
                    "evidence": [],
                }],
            }],
            "blockers": [],
            "next_steps": ["\u751f\u6210\u5e76\u9a8c\u8bc1\u7ed3\u679c"],
            "decisions": [],
            "operations": [],
            "updated_at": "2026-10-15T12:00:00+08:00",
        }

    def run_cli(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = progress.main(argv)
        return code, json.loads(output.getvalue())

    def run_process_cli(self, argv):
        return subprocess.run(
            [sys.executable, "-B", str(Path(progress.__file__)), *argv],
            cwd=Path(progress.__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_state(self, name="state.json", state=None):
        path = self.root / name
        path.write_text(json.dumps(state or self.state, ensure_ascii=False), encoding="utf-8")
        return path

    def update_record(self, state, expected_revision, name):
        state = copy.deepcopy(state)
        state["revision"] = expected_revision
        state_path = self.write_state(name, state)
        code, result = self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", str(expected_revision),
        ])
        self.assertEqual((code, result["status"]), (0, "updated"))
        return progress.load_record(self.path)

    def prepared_operation(self, operation_id, idempotency_key):
        return {
            "operation_id": operation_id,
            "target": "isolated-service",
            "target_revision": "v1",
            "authorization": "fixture-only",
            "idempotency_key": idempotency_key,
            "status": "prepared",
            "evidence": ["authorization captured"],
            "query_observation": None,
            "authorization_still_valid": True,
            "attempt": None,
            "recovery_required": False,
        }

    def start_attempt(self, state, attempt_id, sequence, started_at, evidence):
        state = copy.deepcopy(state)
        operation = state["operations"][0]
        operation.update({
            "status": "executing",
            "attempt": {
                "attempt_id": attempt_id,
                "sequence": sequence,
                "started_at": started_at,
            },
            "query_observation": None,
            "recovery_required": False,
        })
        operation["evidence"].append(evidence)
        return state

    def query_observation(self, operation, status, observed_at, evidence):
        return {
            "attempt_id": operation["attempt"]["attempt_id"],
            "attempt_sequence": operation["attempt"]["sequence"],
            "status": status,
            "observed_at": observed_at,
            "summary": f"isolated query returned {status}",
            "evidence": [evidence],
        }

    def result_observation(self, operation, status, observed_at, evidence):
        return {
            "attempt_id": operation["attempt"]["attempt_id"],
            "attempt_sequence": operation["attempt"]["sequence"],
            "status": status,
            "observed_at": observed_at,
            "summary": f"isolated call returned {status}",
            "evidence": [evidence],
        }

    def init_record(self):
        state_path = self.write_state()
        code, result = self.run_cli(["init", str(self.path), "--state", str(state_path)])
        self.assertEqual((code, result["status"]), (0, "initialized"))

    def complete_state(self, state=None):
        state = copy.deepcopy(state or self.state)
        state["overall_status"] = "completed"
        unit = state["units"][0]
        unit["status"] = "completed"
        unit["artifacts"] = [{
            "path": "result.txt",
            "sha256": delivery_io.file_hash(self.artifact),
            "summary": "\u5408\u6210\u7ed3\u679c\u6587\u4ef6",
        }]
        unit["acceptance_results"][0] = {
            "criterion_id": "AC-1",
            "status": "passed",
            "summary": "\u5df2\u5728\u9694\u79bb\u73af\u5883\u6838\u9a8c",
            "evidence": ["result.txt sha256 matched"],
        }
        state["next_steps"] = []
        return state

    def test_init_generates_authoritative_json_and_chinese_view_without_overwrite(self):
        state_path = self.write_state()
        code, result = self.run_cli(["init", str(self.path), "--state", str(state_path)])
        self.assertEqual((code, result["status"], result["revision"]), (0, "initialized", 1))
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count(progress.AUTHORITY_MARKER), 1)
        self.assertIn("## \u5f53\u524d\u72b6\u6001", raw)
        self.assertIn("```json", raw)

        code, result = self.run_cli(["init", str(self.path), "--state", str(state_path)])
        self.assertEqual((code, result["status"]), (1, "not_met"))

    def test_init_refuses_dangling_symlink_without_replacing_it(self):
        missing = self.root / "missing-target.md"
        try:
            self.path.symlink_to(missing)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        state_path = self.write_state()
        code, result = self.run_cli(["init", str(self.path), "--state", str(state_path)])
        self.assertEqual((code, result["status"]), (1, "not_met"))
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(missing.exists())

    def test_no_write_validates_without_creating_directories_or_locks(self):
        state_path = self.write_state()
        target = self.root / "absent" / "progress.md"
        code, result = self.run_cli([
            "init", str(target), "--state", str(state_path), "--no-write",
        ])
        self.assertEqual((code, result["status"], result["written"]), (0, "validated", False))
        self.assertFalse(target.parent.exists())
        self.assertEqual(list(self.root.rglob("*.lock")), [])

    def test_no_write_subprocess_does_not_create_bytecode_in_fresh_scripts(self):
        scripts = self.root / "fresh-scripts"
        scripts.mkdir()
        shutil.copy2(progress.__file__, scripts / "progress.py")
        shutil.copy2(delivery_io.__file__, scripts / "delivery_io.py")
        state_path = self.write_state("fresh-state.json")
        target = self.root / "absent" / "progress.md"
        result = subprocess.run(
            [
                sys.executable, str(scripts / "progress.py"),
                "init", str(target), "--state", str(state_path), "--no-write",
            ],
            cwd=scripts,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["written"], False)
        self.assertFalse(target.parent.exists())
        self.assertEqual(list(scripts.rglob("__pycache__")), [])
        self.assertEqual(list(scripts.rglob("*.pyc")), [])

    def test_check_rejects_duplicate_marker_and_dependency_cycle_as_format_errors(self):
        self.init_record()
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw + "\n" + progress.AUTHORITY_MARKER, encoding="utf-8")
        self.assertEqual(self.run_cli(["check", str(self.path)])[0], 2)

        state = copy.deepcopy(self.state)
        state["units"].append({
            "id": "U-2", "title": "cycle", "required": False,
            "status": "pending", "depends_on": ["U-1"],
            "artifacts": [], "acceptance_results": [],
        })
        state["units"][0]["depends_on"] = ["U-2"]
        with self.assertRaises(delivery_io.UsageError):
            progress.validate_state(state, self.root)

    def test_completed_unit_rejects_noncompleted_optional_dependencies(self):
        for dependency_status in ("pending", "blocked", "cancelled"):
            with self.subTest(dependency_status=dependency_status):
                state = self.complete_state()
                upstream = state["units"][0]
                upstream["required"] = False
                upstream["status"] = dependency_status
                downstream = copy.deepcopy(state["units"][0])
                downstream.update({
                    "id": "U-2", "title": "dependent delivery", "required": True,
                    "status": "completed", "depends_on": ["U-1"],
                })
                state["units"].append(downstream)
                with self.assertRaises(delivery_io.ConditionError):
                    progress.validate_state(state, self.root)

    def test_update_rejects_completed_dependent_while_optional_upstream_is_pending(self):
        initial = copy.deepcopy(self.state)
        initial["units"][0]["required"] = False
        initial["units"].append({
            "id": "U-2", "title": "dependent delivery", "required": True,
            "status": "in_progress", "depends_on": ["U-1"],
            "artifacts": [], "acceptance_results": [],
        })
        self.state = initial
        self.init_record()

        proposed = self.complete_state(initial)
        proposed["units"][0].update({
            "required": False, "status": "pending", "artifacts": [],
            "acceptance_results": [],
        })
        proposed["units"][1] = copy.deepcopy(proposed["units"][0])
        proposed["units"][1].update({
            "id": "U-2", "title": "dependent delivery", "required": True,
            "status": "completed", "depends_on": ["U-1"],
            "artifacts": [{
                "path": "result.txt", "sha256": delivery_io.file_hash(self.artifact),
                "summary": "dependent result",
            }],
            "acceptance_results": [{
                "criterion_id": "AC-1", "status": "passed",
                "summary": "dependent result checked", "evidence": ["result.txt matched"],
            }],
        })
        state_path = self.write_state("invalid-completion.json", proposed)
        code, result = self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "1",
        ])
        self.assertEqual((code, result["status"]), (1, "not_met"))
        self.assertEqual(progress.load_record(self.path)["revision"], 1)

    def test_overall_completion_ignores_passed_results_from_incomplete_units(self):
        state = self.complete_state()
        state["acceptance_criteria"] = [
            {"id": "AC-1", "description": "pending-only evidence must not complete the task"},
            {"id": "AC-2", "description": "completed delivery evidence"},
        ]
        state["units"][0]["acceptance_results"][0]["criterion_id"] = "AC-2"
        state["units"].append({
            "id": "U-2", "title": "unfinished optional evidence", "required": False,
            "status": "pending", "depends_on": [], "artifacts": [],
            "acceptance_results": [{
                "criterion_id": "AC-1", "status": "passed",
                "summary": "text-only preliminary result", "evidence": ["draft observation"],
            }],
        })
        with self.assertRaises(delivery_io.ConditionError):
            progress.validate_state(state, self.root)

    def test_in_progress_allows_missing_artifacts_but_completed_requires_real_evidence(self):
        self.init_record()
        code, result = self.run_cli(["check", str(self.path)])
        self.assertEqual((code, result["status"]), (0, "valid"))
        self.assertFalse(result["semantically_certified"])

        completed = self.complete_state()
        completed["units"][0]["artifacts"][0]["path"] = "../outside.txt"
        state_path = self.write_state("completed.json", completed)
        code, _ = self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "1",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(progress.load_record(self.path)["revision"], 1)

    def test_explicit_root_supports_nested_record_and_workspace_artifact(self):
        workspace = self.root / "workspace"
        outputs = workspace / "outputs"
        outputs.mkdir(parents=True)
        nested_record = workspace / "work" / "task-progress" / "task.md"
        artifact = outputs / "result.txt"
        artifact.write_text("workspace result", encoding="utf-8")
        completed = self.complete_state()
        completed["units"][0]["artifacts"][0] = {
            "path": "outputs/result.txt",
            "sha256": delivery_io.file_hash(artifact),
            "summary": "workspace output",
        }
        state_path = self.write_state("workspace-state.json", completed)
        code, result = self.run_cli([
            "init", str(nested_record), "--state", str(state_path),
            "--root", str(workspace),
        ])
        self.assertEqual((code, result["status"]), (0, "initialized"))
        self.assertEqual(self.run_cli([
            "check", str(nested_record), "--root", str(workspace),
        ])[0], 0)

        outside = self.root / "outside.md"
        self.assertEqual(self.run_cli([
            "init", str(outside), "--state", str(state_path),
            "--root", str(workspace), "--no-write",
        ])[0], 2)

    def test_update_can_retract_completion_after_old_artifact_becomes_stale(self):
        completed = self.complete_state()
        self.state = completed
        self.init_record()
        self.artifact.write_text("changed after completion", encoding="utf-8")

        retracted = copy.deepcopy(completed)
        retracted["overall_status"] = "in_progress"
        retracted["units"][0].update({
            "status": "in_progress",
            "artifacts": [],
            "acceptance_results": [{
                "criterion_id": "AC-1", "status": "unverified",
                "summary": "input changed; prior evidence withdrawn", "evidence": [],
            }],
        })
        retracted["next_steps"] = ["regenerate and reverify"]
        state_path = self.write_state("retracted.json", retracted)
        code, result = self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "1",
        ])
        self.assertEqual((code, result["status"]), (0, "updated"))
        self.assertEqual(progress.load_record(self.path)["overall_status"], "in_progress")

    def test_update_uses_cas_and_assigns_revision(self):
        self.init_record()
        changed = copy.deepcopy(self.state)
        changed["next_steps"] = ["next revision"]
        changed_path = self.write_state("changed.json", changed)
        code, result = self.run_cli([
            "update", str(self.path), "--state", str(changed_path),
            "--expected-revision", "1",
        ])
        self.assertEqual((code, result["revision"]), (0, 2))

        code, result = self.run_cli([
            "update", str(self.path), "--state", str(changed_path),
            "--expected-revision", "1",
        ])
        self.assertEqual((code, result["status"]), (1, "not_met"))
        self.assertEqual(progress.load_record(self.path)["revision"], 2)

    def test_concurrent_updates_allow_exactly_one_revision_winner(self):
        self.init_record()
        first = copy.deepcopy(self.state)
        first["next_steps"] = ["first"]
        second = copy.deepcopy(self.state)
        second["next_steps"] = ["second"]
        first_path = self.write_state("first.json", first)
        second_path = self.write_state("second.json", second)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=concurrent_update, args=(
                str(self.path), str(state_path), 1, start, results,
            ))
            for state_path in (first_path, second_path)
        ]
        for worker in workers:
            worker.start()
        start.set()
        observed = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(code for code, _ in observed), [0, 1])
        self.assertEqual(progress.load_record(self.path)["revision"], 2)

    def test_operation_recovery_requires_observation_and_supports_idempotent_replay(self):
        interrupted = copy.deepcopy(self.state)
        interrupted["operations"] = [{
            "operation_id": "op-1",
            "target": "synthetic-service",
            "target_revision": "v1",
            "authorization": "fixture-only",
            "idempotency_key": "fixture-op-1",
            "status": "executing",
            "evidence": ["intent recorded before synthetic call"],
            "query_observation": None,
            "authorization_still_valid": False,
            "attempt": {
                "attempt_id": "attempt-1", "sequence": 1,
                "started_at": "2026-10-15T12:01:00+08:00",
            },
            "recovery_required": True,
        }]
        self.state = interrupted
        self.init_record()

        succeeded = copy.deepcopy(interrupted)
        succeeded["operations"][0].update({
            "status": "succeeded",
            "recovery_required": False,
            "result_observation": {
                "attempt_id": "attempt-1", "attempt_sequence": 1,
                "status": "succeeded",
                "observed_at": "2026-10-15T12:04:00+08:00",
                "summary": "untrusted direct result after recovery",
                "evidence": ["synthetic direct result"],
            },
        })
        succeeded_path = self.write_state("succeeded.json", succeeded)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(succeeded_path),
            "--expected-revision", "1",
        ])[0], 2)

        succeeded["operations"][0].pop("result_observation")
        succeeded["operations"][0]["query_observation"] = {
            "attempt_id": "attempt-1",
            "attempt_sequence": 1,
            "status": "applied",
            "observed_at": "2026-10-15T12:05:00+08:00",
            "summary": "synthetic query found target revision",
            "evidence": ["fixture query response: applied"],
        }
        succeeded["operations"][0]["recovery_required"] = False
        succeeded_path = self.write_state("succeeded.json", succeeded)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(succeeded_path),
            "--expected-revision", "1",
        ])[0], 0)

        replay = copy.deepcopy(succeeded)
        replay["revision"] = 2
        replay_path = self.write_state("replay.json", replay)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(replay_path),
            "--expected-revision", "2",
        ])[0], 0)
        self.assertEqual(progress.load_record(self.path)["operations"][0]["status"], "succeeded")

    def test_normal_executing_success_accepts_direct_result_without_query(self):
        prepared = copy.deepcopy(self.state)
        prepared["operations"] = [{
            "operation_id": "op-direct", "target": "synthetic-service",
            "target_revision": "v1", "authorization": "fixture-only",
            "idempotency_key": "fixture-direct", "status": "prepared",
            "evidence": ["authorization captured"],
            "query_observation": None, "authorization_still_valid": False,
            "attempt": None, "recovery_required": False,
        }]
        self.state = prepared
        self.init_record()
        executing = copy.deepcopy(prepared)
        executing["operations"][0].update({
            "status": "executing",
            "attempt": {
                "attempt_id": "attempt-direct-1", "sequence": 1,
                "started_at": "2026-10-15T12:03:00+08:00",
            },
        })
        executing["operations"][0]["evidence"].append("execution intent recorded")
        executing_path = self.write_state("direct-executing.json", executing)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(executing_path),
            "--expected-revision", "1",
        ])[0], 0)

        succeeded = copy.deepcopy(executing)
        succeeded["revision"] = 2
        succeeded["operations"][0].update({
            "status": "succeeded",
            "result_observation": {
                "attempt_id": "attempt-direct-1",
                "attempt_sequence": 1,
                "status": "succeeded",
                "observed_at": "2026-10-15T12:04:00+08:00",
                "summary": "synthetic call returned success",
                "evidence": ["fixture response: 200"],
            },
        })
        state_path = self.write_state("direct.json", succeeded)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "2",
        ])[0], 0)

    def test_prepared_to_executing_must_append_intent_evidence(self):
        prepared = copy.deepcopy(self.state)
        prepared["operations"] = [{
            "operation_id": "op-intent", "target": "synthetic-service",
            "target_revision": "v1", "authorization": "fixture-only",
            "idempotency_key": "fixture-intent", "status": "prepared",
            "evidence": ["authorization captured"],
            "query_observation": None, "authorization_still_valid": True,
            "attempt": None, "recovery_required": False,
        }]
        self.state = prepared
        self.init_record()
        executing = copy.deepcopy(prepared)
        executing["operations"][0]["status"] = "executing"
        executing["operations"][0]["attempt"] = {
            "attempt_id": "attempt-intent-1", "sequence": 1,
            "started_at": "2026-10-15T12:03:00+08:00",
        }
        state_path = self.write_state("executing.json", executing)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "1",
        ])[0], 2)

        executing["operations"][0]["evidence"].append("execution intent recorded")
        state_path = self.write_state("executing.json", executing)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "1",
        ])[0], 0)

    def test_not_applied_query_allows_retry_but_query_failure_blocks_completion(self):
        interrupted = copy.deepcopy(self.state)
        interrupted["operations"] = [{
            "operation_id": "op-1", "target": "synthetic-service",
            "target_revision": "v1", "authorization": "fixture-only",
            "idempotency_key": "fixture-op-1", "status": "unknown",
            "evidence": ["synthetic interruption"],
            "query_observation": None, "authorization_still_valid": False,
            "attempt": {
                "attempt_id": "attempt-1", "sequence": 1,
                "started_at": "2026-10-15T12:01:00+08:00",
            },
            "recovery_required": True,
        }]
        self.state = interrupted
        self.init_record()

        retry = copy.deepcopy(interrupted)
        retry["operations"][0].update({
            "status": "prepared", "authorization_still_valid": True,
            "recovery_required": False,
            "query_observation": {
                "attempt_id": "attempt-1",
                "attempt_sequence": 1,
                "status": "not_applied",
                "observed_at": "2026-10-15T12:05:00+08:00",
                "summary": "synthetic query found no application",
                "evidence": ["fixture query response: not_applied"],
            },
        })
        retry_path = self.write_state("retry.json", retry)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(retry_path),
            "--expected-revision", "1",
        ])[0], 0)

        query_failed = self.complete_state(interrupted)
        query_failed["operations"][0]["query_observation"] = {
            "attempt_id": "attempt-1",
            "attempt_sequence": 1,
            "status": "query_failed",
            "observed_at": "2026-10-15T12:05:00+08:00",
            "summary": "synthetic query timed out",
            "evidence": ["fixture timeout"],
        }
        query_failed["blockers"] = [{
            "id": "B-1", "summary": "external state unknown",
            "unit_ids": ["U-1"], "status": "open",
        }]
        with self.assertRaises(delivery_io.ConditionError):
            progress.validate_state(query_failed, self.root)

    def test_stale_not_applied_query_cannot_authorize_another_attempt(self):
        unknown = copy.deepcopy(self.state)
        unknown["operations"] = [{
            "operation_id": "op-stale", "target": "synthetic-service",
            "target_revision": "v1", "authorization": "fixture-only",
            "idempotency_key": "fixture-stale", "status": "unknown",
            "evidence": ["attempt-1 response lost"],
            "query_observation": None, "authorization_still_valid": False,
            "attempt": {
                "attempt_id": "attempt-1", "sequence": 1,
                "started_at": "2026-10-15T12:01:00+08:00",
            },
            "recovery_required": True,
        }]
        self.state = unknown
        self.init_record()

        retry = copy.deepcopy(unknown)
        retry["operations"][0].update({
            "status": "prepared", "authorization_still_valid": True,
            "recovery_required": False,
            "query_observation": {
                "attempt_id": "attempt-1",
                "attempt_sequence": 1,
                "status": "not_applied",
                "observed_at": "2026-10-15T12:05:00+08:00",
                "summary": "attempt-1 was not applied",
                "evidence": ["query Q1"],
            },
        })
        retry_path = self.write_state("stale-retry-1.json", retry)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(retry_path),
            "--expected-revision", "1",
        ])[0], 0)

        executing = copy.deepcopy(retry)
        executing["revision"] = 2
        executing["operations"][0]["status"] = "executing"
        executing["operations"][0]["attempt"] = {
            "attempt_id": "attempt-2", "sequence": 2,
            "started_at": "2026-10-15T12:06:00+08:00",
        }
        executing["operations"][0]["query_observation"] = None
        executing["operations"][0]["evidence"].append("attempt-2 intent")
        executing_path = self.write_state("stale-executing-2.json", executing)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(executing_path),
            "--expected-revision", "2",
        ])[0], 0)

        lost = copy.deepcopy(executing)
        lost["revision"] = 3
        lost["operations"][0]["status"] = "unknown"
        lost["operations"][0]["recovery_required"] = True
        lost["operations"][0]["evidence"].append("attempt-2 response lost")
        lost_path = self.write_state("stale-lost-2.json", lost)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(lost_path),
            "--expected-revision", "3",
        ])[0], 0)

        stale_retry = copy.deepcopy(lost)
        stale_retry["revision"] = 4
        stale_retry["operations"][0].update({
            "status": "prepared", "authorization_still_valid": True,
            "recovery_required": False,
            "query_observation": copy.deepcopy(retry["operations"][0]["query_observation"]),
        })
        stale_path = self.write_state("stale-retry-2.json", stale_retry)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(stale_path),
            "--expected-revision", "4",
        ])[0], 2)

    def test_imported_prepared_attempt_requires_matching_not_applied_query(self):
        prepared = copy.deepcopy(self.state)
        prepared["operations"] = [self.prepared_operation("op-import", "key-import")]
        prepared["operations"][0]["attempt"] = {
            "attempt_id": "import-attempt-1", "sequence": 1,
            "started_at": "2026-10-15T12:01:00+08:00",
        }
        state_path = self.write_state("invalid-imported-prepared.json", prepared)
        self.assertEqual(self.run_cli([
            "init", str(self.path), "--state", str(state_path), "--no-write",
        ])[0], 2)

    def test_malformed_json_shapes_return_one_json_object_and_exit_two(self):
        malformed = copy.deepcopy(self.state)
        malformed["overall_status"] = []
        malformed_path = self.write_state("malformed-enum.json", malformed)
        result = self.run_process_cli([
            "init", str(self.path), "--state", str(malformed_path), "--no-write",
        ])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid")
        self.assertEqual(result.stderr, "")

        self.init_record()
        list_path = self.root / "list-state.json"
        list_path.write_text("[]", encoding="utf-8")
        result = self.run_process_cli([
            "update", str(self.path), "--state", str(list_path),
            "--expected-revision", "1", "--no-write",
        ])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid")
        self.assertEqual(result.stderr, "")

    def test_authority_marker_inside_field_does_not_break_generated_record(self):
        self.state["goal"] = f"goal contains {progress.AUTHORITY_MARKER} as data"
        self.init_record()
        code, result = self.run_cli(["check", str(self.path)])
        self.assertEqual((code, result["status"]), (0, "valid"))

    def test_unicode_separator_marker_field_roundtrips_init_and_check(self):
        self.state["goal"] = (
            "line one\u2028" + progress.AUTHORITY_MARKER + "\u2028line two"
        )
        self.init_record()
        code, result = self.run_cli(["check", str(self.path)])
        self.assertEqual((code, result["status"]), (0, "valid"))

    def test_unicode_separator_marker_in_legacy_data_does_not_block_upgrade(self):
        marker_data = "legacy\u2028" + progress.AUTHORITY_MARKER + "\u2028data"
        legacy = self.root / "legacy-unicode.md"
        legacy.write_text("# Legacy\n\n" + marker_data + "\n", encoding="utf-8")
        self.state["goal"] = marker_data
        state_path = self.write_state("unicode-upgrade.json")
        code, result = self.run_cli([
            "upgrade", str(legacy), "--state", str(state_path),
        ])
        self.assertEqual((code, result["status"]), (0, "upgraded"))
        self.assertEqual(self.run_cli(["check", str(legacy)])[0], 0)

    def test_applied_then_disconnected_recovers_once_without_reexecution(self):
        service = IsolatedExternalService(self.root / "service-after.json")
        state = copy.deepcopy(self.state)
        state["operations"] = [self.prepared_operation("op-after", "key-after")]
        self.state = state
        self.init_record()
        executing = self.start_attempt(
            state, "after-attempt-1", 1,
            "2026-10-15T12:01:00+08:00", "attempt-1 intent",
        )
        executing = self.update_record(executing, 1, "after-executing.json")

        with self.assertRaises(SyntheticInterruption):
            service.execute("key-after", "after-attempt-1", interrupt="after_apply")
        unknown = copy.deepcopy(executing)
        unknown["operations"][0].update({"status": "unknown", "recovery_required": True})
        unknown["operations"][0]["evidence"].append("response lost after apply")
        unknown = self.update_record(unknown, 2, "after-unknown.json")

        self.assertEqual(service.query("key-after"), "applied")
        succeeded = copy.deepcopy(unknown)
        operation = succeeded["operations"][0]
        operation.update({
            "status": "succeeded",
            "recovery_required": False,
            "query_observation": self.query_observation(
                operation, "applied", "2026-10-15T12:02:00+08:00",
                "isolated applied state",
            ),
        })
        succeeded = self.update_record(succeeded, 3, "after-succeeded.json")
        replayed = self.update_record(succeeded, 4, "after-replayed.json")
        self.assertEqual(replayed["operations"][0]["status"], "succeeded")
        self.assertEqual(service.state["apply_count"], 1)
        self.assertEqual(service.state["attempt_calls"], {"after-attempt-1": 1})

    def test_interrupted_before_apply_queries_then_retries_new_attempt(self):
        service = IsolatedExternalService(self.root / "service-before.json")
        state = copy.deepcopy(self.state)
        state["operations"] = [self.prepared_operation("op-before", "key-before")]
        self.state = state
        self.init_record()
        executing = self.start_attempt(
            state, "before-attempt-1", 1,
            "2026-10-15T12:01:00+08:00", "attempt-1 intent",
        )
        executing = self.update_record(executing, 1, "before-executing-1.json")

        with self.assertRaises(SyntheticInterruption):
            service.execute("key-before", "before-attempt-1", interrupt="before_apply")
        unknown = copy.deepcopy(executing)
        unknown["operations"][0].update({"status": "unknown", "recovery_required": True})
        unknown["operations"][0]["evidence"].append("interrupted before apply")
        unknown = self.update_record(unknown, 2, "before-unknown.json")

        self.assertEqual(service.query("key-before"), "not_applied")
        retry = copy.deepcopy(unknown)
        operation = retry["operations"][0]
        operation.update({
            "status": "prepared",
            "recovery_required": False,
            "authorization_still_valid": True,
            "query_observation": self.query_observation(
                operation, "not_applied", "2026-10-15T12:02:00+08:00",
                "isolated not-applied state",
            ),
        })
        retry = self.update_record(retry, 3, "before-retry.json")
        second = self.start_attempt(
            retry, "before-attempt-2", 2,
            "2026-10-15T12:03:00+08:00", "attempt-2 intent",
        )
        second = self.update_record(second, 4, "before-executing-2.json")

        result = service.execute("key-before", "before-attempt-2")
        succeeded = copy.deepcopy(second)
        operation = succeeded["operations"][0]
        operation.update({
            "status": "succeeded",
            "result_observation": self.result_observation(
                operation, result["status"], "2026-10-15T12:04:00+08:00",
                "isolated direct response",
            ),
        })
        self.update_record(succeeded, 5, "before-succeeded.json")
        self.assertEqual(service.state["apply_count"], 1)
        self.assertEqual(service.state["attempt_calls"], {
            "before-attempt-1": 1,
            "before-attempt-2": 1,
        })

    def test_query_failure_persists_unknown_blocker_then_recovers(self):
        service = IsolatedExternalService(self.root / "service-query.json")
        state = copy.deepcopy(self.state)
        state["operations"] = [self.prepared_operation("op-query", "key-query")]
        self.state = state
        self.init_record()
        executing = self.start_attempt(
            state, "query-attempt-1", 1,
            "2026-10-15T12:01:00+08:00", "attempt-1 intent",
        )
        executing = self.update_record(executing, 1, "query-executing.json")
        with self.assertRaises(SyntheticInterruption):
            service.execute("key-query", "query-attempt-1", interrupt="after_apply")
        unknown = copy.deepcopy(executing)
        unknown["operations"][0].update({"status": "unknown", "recovery_required": True})
        unknown["operations"][0]["evidence"].append("response lost after apply")
        unknown = self.update_record(unknown, 2, "query-unknown.json")

        with self.assertRaises(SyntheticQueryFailure):
            service.query("key-query", fail=True)
        blocked = copy.deepcopy(unknown)
        operation = blocked["operations"][0]
        operation["query_observation"] = self.query_observation(
            operation, "query_failed", "2026-10-15T12:02:00+08:00",
            "isolated query failure",
        )
        operation["evidence"].append("query failure persisted")
        blocked["blockers"] = [{
            "id": "B-query", "summary": "external state unknown",
            "unit_ids": ["U-1"], "status": "open",
        }]
        blocked = self.update_record(blocked, 3, "query-blocked.json")
        self.assertEqual(blocked["operations"][0]["status"], "unknown")
        self.assertEqual(blocked["blockers"][0]["status"], "open")

        self.assertEqual(service.query("key-query"), "applied")
        succeeded = copy.deepcopy(blocked)
        operation = succeeded["operations"][0]
        operation.update({
            "status": "succeeded",
            "recovery_required": False,
            "query_observation": self.query_observation(
                operation, "applied", "2026-10-15T12:03:00+08:00",
                "isolated applied state",
            ),
        })
        succeeded["blockers"][0]["status"] = "resolved"
        self.update_record(succeeded, 4, "query-succeeded.json")
        self.assertEqual(service.state["apply_count"], 1)
        self.assertEqual(service.state["attempt_calls"], {"query-attempt-1": 1})

    def test_succeeded_cannot_carry_query_failed_even_with_direct_result(self):
        state = copy.deepcopy(self.state)
        state["operations"] = [self.prepared_operation("op-conflict", "key-conflict")]
        self.state = state
        self.init_record()
        executing = self.start_attempt(
            state, "conflict-attempt-1", 1,
            "2026-10-15T12:01:00+08:00", "attempt-1 intent",
        )
        executing = self.update_record(executing, 1, "conflict-executing.json")

        conflicted = copy.deepcopy(executing)
        operation = conflicted["operations"][0]
        operation.update({
            "status": "succeeded",
            "result_observation": self.result_observation(
                operation, "succeeded", "2026-10-15T12:02:00+08:00",
                "direct success response",
            ),
            "query_observation": self.query_observation(
                operation, "query_failed", "2026-10-15T12:03:00+08:00",
                "later query failed",
            ),
        })
        conflicted["blockers"] = [{
            "id": "B-conflict", "summary": "query failed",
            "unit_ids": ["U-1"], "status": "open",
        }]
        conflicted["revision"] = 2
        state_path = self.write_state("conflicted-success.json", conflicted)
        self.assertEqual(self.run_cli([
            "update", str(self.path), "--state", str(state_path),
            "--expected-revision", "2",
        ])[0], 2)

    def test_upgrade_preserves_original_in_backup_and_failure_keeps_original(self):
        original = "# Legacy\n\nOriginal text must remain byte-for-byte.\n"
        self.path.write_text(original, encoding="utf-8")
        state_path = self.write_state()
        code, result = self.run_cli([
            "upgrade", str(self.path), "--state", str(state_path),
        ])
        self.assertEqual((code, result["status"]), (0, "upgraded"))
        backup = Path(result["backup"])
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

        second = self.root / "second.md"
        second.write_text(original, encoding="utf-8")
        real_atomic_write = delivery_io.atomic_write

        def fail_target(path, text):
            if Path(path) == second:
                raise OSError("synthetic target failure")
            return real_atomic_write(path, text)

        with mock.patch("progress.atomic_write", side_effect=fail_target):
            code, result = self.run_cli([
                "upgrade", str(second), "--state", str(state_path),
            ])
        self.assertEqual((code, result["status"]), (2, "invalid"))
        self.assertEqual(second.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
