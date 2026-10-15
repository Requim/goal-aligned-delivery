"""Data-root and fixture isolation behavior, with no real model execution."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import learning
import regress
from delivery_io import UsageError
import test_regress


class DataRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_explicit_environment_and_platform_precedence(self):
        with patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(learning.resolve_data_dir(None, platform="win32"),
                             self.root / "AppData/Local/goal-aligned-delivery")
            self.assertEqual(learning.resolve_data_dir(None, platform="darwin"),
                             self.root / "Library/Application Support/goal-aligned-delivery")
            self.assertEqual(learning.resolve_data_dir(None, platform="linux"),
                             self.root / ".local/share/goal-aligned-delivery")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local"),
                                        "XDG_DATA_HOME": str(self.root / "xdg")}):
                self.assertEqual(learning.resolve_data_dir(None, platform="win32"), self.root / "local/goal-aligned-delivery")
                self.assertEqual(learning.resolve_data_dir(None, platform="linux"), self.root / "xdg/goal-aligned-delivery")
            with patch.dict(os.environ, {"GAD_DATA_DIR": str(self.root / "env")}):
                self.assertEqual(learning.resolve_data_dir(None), self.root / "env")
                self.assertEqual(learning.resolve_data_dir(str(self.root / "explicit")), self.root / "explicit")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_and_relative_environment_are_rejected(self):
        for name in ("GAD_DATA_DIR", "LOCALAPPDATA", "XDG_DATA_HOME"):
            for value in ("", "relative"):
                with self.subTest(name=name, value=value), patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaises(UsageError):
                        learning.resolve_data_dir(None, platform="win32" if name == "LOCALAPPDATA" else "linux")

    def test_legacy_selection_pending_preserves_bytes_and_no_write_preempts_resolution(self):
        legacy = self.root / "Documents/Codex/skill-data/goal-aligned-delivery"
        legacy.mkdir(parents=True)
        old = legacy / "experience.json"
        old.write_bytes(b"preserve exactly\r\n")
        with patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = learning.main(["summarize"])
            self.assertEqual(code, 1)
            self.assertIn("--data-dir", json.loads(captured.getvalue())["reason"])
            self.assertEqual(old.read_bytes(), b"preserve exactly\r\n")
            self.assertFalse((self.root / ".local").exists())
            self.assertEqual(learning.resolve_data_dir(str(legacy)), legacy)
            with patch.dict(os.environ, {"GAD_DATA_DIR": ""}):
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    code = learning.main(["record", "--no-write"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(captured.getvalue())["status"], "no_write")
        self.assertEqual(list(legacy.iterdir()), [old])

    def test_verified_claude_cost_provenance_is_accepted_unknown_source_refused(self):
        self.assertEqual(learning._measured_cost({"value": {"input_tokens": 10, "output_tokens": 3},
                         "source": "claude.result.usage"}, "tokens"), 13)
        self.assertEqual(learning._measured_cost({"value": 2, "source": "claude.assistant.tool_use"}, "tool_calls"), 2)
        for source in ("reported", "measured", "invented"):
            with self.assertRaises(Exception):
                learning._measured_cost({"value": 2, "source": source}, "tool_calls")

    def test_capability_and_adapter_boundaries_cannot_be_automatically_rewritten(self):
        skill = self.root / "skill"
        (skill / "references").mkdir(parents=True)
        for name in ("capabilities.md", "adapters.md"):
            with self.subTest(name=name), self.assertRaises(UsageError):
                learning._change(skill, {"path": "references/" + name, "content": "remove guards"}, [])


class FixtureTests(unittest.TestCase):
    setUp = test_regress.RegressionTests.setUp
    save_oracle = test_regress.RegressionTests.save_oracle
    run_cli = test_regress.RegressionTests.run_cli
    report = test_regress.RegressionTests.report

    def test_distribution_has_no_discoverable_nested_skill(self):
        source = Path(__file__).resolve().parent.parent
        self.assertEqual(list((source / "cases").rglob("SKILL.md")), [])
        fixture = source / "cases/multi-skill/inputs/domain-skill/SKILL.md.fixture"
        self.assertTrue(fixture.is_file())

    def test_fixture_materializes_only_in_workspace_and_integrity_uses_mapped_path(self):
        fixture = self.case / "inputs/domain-skill/SKILL.md.fixture"
        fixture.parent.mkdir()
        fixture.write_text("domain instructions\n", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 0)
        report = self.report()
        workspace = self.output / report["cases"][0]["workspace"]
        self.assertEqual((workspace / "domain-skill/SKILL.md").read_text(), "domain instructions\n")
        self.assertFalse((workspace / "domain-skill/SKILL.md.fixture").exists())
        self.assertFalse((self.case / "inputs/domain-skill/SKILL.md").exists())

    def test_mapped_fixture_collision_refused_before_output(self):
        directory = self.case / "inputs/domain-skill"
        directory.mkdir()
        (directory / "SKILL.md.fixture").write_text("fixture")
        (directory / "SKILL.md").write_text("other")
        self.assertEqual(self.run_cli()[0], 2)
        self.assertFalse(self.output.exists())

    def test_file_directory_mapping_collision_refused_before_output(self):
        directory = self.case / "inputs/domain-skill"
        directory.mkdir()
        (directory / "SKILL.md.fixture").write_text("fixture")
        nested = directory / "SKILL.md/other.txt"
        nested.parent.mkdir()
        nested.write_text("other")
        self.assertEqual(self.run_cli()[0], 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
