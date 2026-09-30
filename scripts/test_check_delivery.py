"""用合成夹具测试证据检查器，不代表任何真实项目验收通过。"""

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import check_delivery as gate


class DeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="goal-gate-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "result.txt"
        self.artifact.write_text("synthetic fixture: A=10, B=100", encoding="utf-8")
        self.contract = {
            "schema_version": 1, "task_id": "fixture", "goal": "A only",
            "state": {"source_digest": "version-A", "data": "fixture-v1"},
            "criteria": [{"id": "AC-1", "description": "A=10",
                          "environment": "isolated"}],
        }
        self.contract_path = self.root / "contract.json"
        self.write(self.contract_path, self.contract)
        self.evidence = {
            "schema_version": 1, "task_id": "fixture",
            "contract_sha256": gate.digest(self.contract_path),
            "observed_state": copy.deepcopy(self.contract["state"]),
            "checks": [{"criterion_id": "AC-1", "status": "passed",
                        "environment": "isolated", "observation": "synthetic A=10",
                        "artifacts": [{"path": "result.txt",
                                       "sha256": gate.digest(self.artifact)}]}],
        }

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def run_gate(self, extra=None):
        receipt = self.root / "evidence.json"
        self.write(receipt, self.evidence)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gate.main([str(self.contract_path), str(receipt),
                              "--root", str(self.root), *(extra or [])])
        return code, json.loads(output.getvalue())

    def test_complete_evidence_passes(self):
        code, result = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "evidence_consistent")

    def test_missing_check_fails(self):
        self.evidence["checks"] = []
        self.assertEqual(self.run_gate()[0], 1)

    def test_nonpass_statuses_fail(self):
        for status in ("failed", "blocked", "unverified"):
            with self.subTest(status=status):
                self.evidence["checks"][0]["status"] = status
                self.assertEqual(self.run_gate()[0], 1)

    def test_new_contract_cannot_reuse_receipt(self):
        self.contract["criteria"][0]["description"] = "changed requirement"
        self.write(self.contract_path, self.contract)
        self.assertEqual(self.run_gate()[0], 1)

    def test_state_drift_fails(self):
        self.evidence["observed_state"]["source_digest"] = "other-version"
        self.assertEqual(self.run_gate()[0], 1)

    def test_task_identity_mismatch_fails(self):
        self.evidence["task_id"] = "other-task"
        self.assertEqual(self.run_gate()[0], 1)

    def test_environment_substitution_fails(self):
        self.evidence["checks"][0]["environment"] = "local"
        self.assertEqual(self.run_gate()[0], 1)

    def test_missing_artifact_fails(self):
        self.evidence["checks"][0]["artifacts"][0]["path"] = "absent.txt"
        self.assertEqual(self.run_gate()[0], 1)

    def test_no_artifacts_fails(self):
        self.evidence["checks"][0]["artifacts"] = []
        self.assertEqual(self.run_gate()[0], 1)

    def test_changed_artifact_fails(self):
        self.artifact.write_text("changed", encoding="utf-8")
        self.assertEqual(self.run_gate()[0], 1)

    def test_duplicate_check_is_invalid(self):
        self.evidence["checks"].append(copy.deepcopy(self.evidence["checks"][0]))
        self.assertEqual(self.run_gate()[0], 2)

    def test_unknown_criterion_is_invalid(self):
        self.evidence["checks"][0]["criterion_id"] = "AC-other"
        self.assertEqual(self.run_gate()[0], 2)

    def test_bad_status_is_invalid(self):
        self.evidence["checks"][0]["status"] = "looks good"
        self.assertEqual(self.run_gate()[0], 2)

    def test_duplicate_contract_criterion_is_invalid(self):
        self.contract["criteria"].append(copy.deepcopy(self.contract["criteria"][0]))
        self.write(self.contract_path, self.contract)
        self.assertEqual(self.run_gate()[0], 2)

    def test_empty_contract_is_invalid(self):
        self.contract["criteria"] = []
        self.write(self.contract_path, self.contract)
        self.assertEqual(self.run_gate()[0], 2)

    def test_boolean_schema_version_is_invalid(self):
        self.evidence["schema_version"] = True
        self.assertEqual(self.run_gate()[0], 2)

    def test_cross_platform_unsafe_paths_are_invalid(self):
        for path in ("../outside.txt", "..\\outside.txt", "/etc/passwd",
                     "C:\\outside.txt", "C:outside.txt", "\\server\\file",
                     "result.txt:alternate"):
            with self.subTest(path=path):
                self.evidence["checks"][0]["artifacts"][0]["path"] = path
                self.assertEqual(self.run_gate()[0], 2)

    def test_wrong_json_shape_is_invalid(self):
        self.evidence["checks"][0]["artifacts"][0] = []
        self.assertEqual(self.run_gate()[0], 2)

    def test_empty_observation_is_invalid(self):
        self.evidence["checks"][0]["observation"] = " "
        self.assertEqual(self.run_gate()[0], 2)

    def test_invalid_digest_is_invalid(self):
        self.evidence["contract_sha256"] = "unknown"
        self.assertEqual(self.run_gate()[0], 2)

    def test_duplicate_json_key_is_invalid(self):
        self.contract_path.write_text('{"task_id":"A","task_id":"B"}', encoding="utf-8")
        self.assertEqual(self.run_gate()[0], 2)

    def test_invalid_json_is_invalid(self):
        self.contract_path.write_text("{", encoding="utf-8")
        self.assertEqual(self.run_gate()[0], 2)

    def test_nonfinite_json_is_invalid(self):
        self.contract_path.write_text('{"value":NaN}', encoding="utf-8")
        self.assertEqual(self.run_gate()[0], 2)

    def test_extra_fields_are_invalid(self):
        self.contract["criteria"][0]["required"] = False
        self.write(self.contract_path, self.contract)
        self.assertEqual(self.run_gate()[0], 2)

    def test_self_reported_content_is_not_semantically_verified(self):
        self.artifact.write_text("A=110 (incorrect business result)", encoding="utf-8")
        self.evidence["checks"][0]["artifacts"][0]["sha256"] = gate.digest(self.artifact)
        self.assertEqual(self.run_gate()[0], 0)

    def test_bom_is_supported_and_hashed_as_bytes(self):
        raw = self.contract_path.read_bytes()
        self.contract_path.write_bytes(b"\xef\xbb\xbf" + raw)
        self.evidence["contract_sha256"] = hashlib.sha256(b"\xef\xbb\xbf" + raw).hexdigest()
        self.assertEqual(self.run_gate()[0], 0)

    def fresh_verify(self, stamp, **options):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        self.evidence["schema_version"] = 2
        self.evidence["checks"][0]["verified_at"] = stamp
        return gate.verify(self.contract, self.evidence, gate.digest(self.contract_path),
                           self.root, now=now, **options)

    def test_v2_recent_evidence_passes(self):
        self.assertEqual(self.fresh_verify("2026-09-17T11:59:00Z",
                                          max_age_seconds=300), [])

    def test_v2_stale_evidence_fails(self):
        self.assertTrue(self.fresh_verify("2026-09-16T12:00:00Z",
                                         max_age_seconds=300))

    def test_exact_age_boundary_passes(self):
        self.assertEqual(self.fresh_verify("2026-09-17T11:55:00Z",
                                          max_age_seconds=300), [])

    def test_future_evidence_fails(self):
        self.assertTrue(self.fresh_verify("2026-09-17T12:00:01Z"))

    def test_evidence_before_last_change_fails(self):
        cutoff = datetime(2026, 9, 17, 11, 59, 30, tzinfo=timezone.utc)
        self.assertTrue(self.fresh_verify("2026-09-17T11:59:00Z", not_before=cutoff))

    def test_evidence_at_last_change_passes(self):
        cutoff = datetime(2026, 9, 17, 11, 59, 0, tzinfo=timezone.utc)
        self.assertEqual(self.fresh_verify("2026-09-17T11:59:00Z", not_before=cutoff), [])

    def test_naive_time_is_invalid(self):
        with self.assertRaises(gate.InvalidInput):
            self.fresh_verify("2026-09-17T11:59:00")

    def test_invalid_timestamp_is_invalid(self):
        with self.assertRaises(gate.InvalidInput):
            self.fresh_verify("last week")

    def test_timezone_offset_is_normalized(self):
        self.assertEqual(self.fresh_verify("2026-09-17T19:59:00+08:00",
                                          max_age_seconds=300), [])

    def test_v1_cannot_satisfy_freshness_requirement(self):
        result = gate.verify(self.contract, self.evidence, gate.digest(self.contract_path),
                             self.root, max_age_seconds=300)
        self.assertTrue(result)

    def test_actual_state_drift_fails(self):
        current = {"source_digest": "changed", "data": "fixture-v1"}
        result = gate.verify(self.contract, self.evidence, gate.digest(self.contract_path),
                             self.root, current_state=current)
        self.assertTrue(result)

    def test_actual_state_matches_passes(self):
        result = gate.verify(self.contract, self.evidence, gate.digest(self.contract_path),
                             self.root, current_state=copy.deepcopy(self.contract["state"]))
        self.assertEqual(result, [])

    def test_cli_current_state_drift_fails(self):
        path = self.root / "current.json"
        self.write(path, {"source_digest": "changed", "data": "fixture-v1"})
        self.assertEqual(self.run_gate(["--current-state", str(path)])[0], 1)

    def test_cli_v2_recent_passes(self):
        self.evidence["schema_version"] = 2
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.evidence["checks"][0]["verified_at"] = now.isoformat()
        self.assertEqual(self.run_gate(["--max-age-seconds", "300"])[0], 0)

    def test_cli_invalid_age_is_invalid(self):
        self.assertEqual(self.run_gate(["--max-age-seconds", "-1"])[0], 2)

    def test_cli_invalid_cutoff_is_invalid(self):
        self.assertEqual(self.run_gate(["--not-before", "yesterday"])[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
