"""Backend contracts use native-shaped synthetic events, never real model claims."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import regress
from delivery_io import ConditionError, UsageError, file_hash
import test_regress

try:
    import executors
except ImportError:
    executors = None


CLAUDE_EXECUTOR = r'''
import json, os, pathlib, sys, uuid
if "--version" in sys.argv:
    print("simulated-claude 2.1.63")
    sys.exit(0)
args = sys.argv[1:]
assert args[:4] == ["--print", "--output-format", "stream-json", "--verbose"]
assert "--no-session-persistence" in args
assert args[args.index("--setting-sources")+1] == ""
assert args[args.index("--mcp-config")+1] == '{"mcpServers":{}}'
assert "--strict-mcp-config" in args
assert "--disable-slash-commands" in args
assert "--no-chrome" in args
assert "--dangerously-skip-permissions" not in args
assert os.environ["DISABLE_AUTOUPDATER"] == "1"
assert os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
assert os.environ["DISABLE_TELEMETRY"] == "1"
assert os.environ["DISABLE_ERROR_REPORTING"] == "1"
host_state = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
assert host_state.is_absolute() and host_state.name == "host-state"
assert not host_state.exists(), "host state must start isolated"
(host_state / "state").mkdir(parents=True)
(host_state / "state/session.json").write_text('{"synthetic":true}')
assert args[args.index("--permission-mode")+1] == "dontAsk"
assert json.loads(args[args.index("--settings")+1]) == {
    "disableAllHooks":True, "enableAllProjectMcpServers":False,
    "enabledPlugins":{}, "autoUpdatesChannel":"stable"}
prompt = sys.stdin.read()
review = "GAD_REVIEW_PROTOCOL_V1" in prompt
readonly = review or "NO_WRITE_CASE" in prompt
tools = args[args.index("--tools")+1]
assert tools == ("Read,Glob,Grep" if readonly else "Read,Glob,Grep,Edit,Write,Bash")
assert args[args.index("--allowedTools")+1] == tools
cwd = pathlib.Path.cwd()
if review:
    packet = json.loads(prompt.split("GAD_REVIEW_PACKET\n",1)[1])
    message = json.dumps({"checks":[{
        "criterion_id":c["id"],"status":"passed",
        "observation":"Located supporting independent synthetic evidence.",
        "evidence":[{"path":packet["allowed_evidence"][0],"locator":"line:1"}]
    } for c in packet["criteria"]]})
else:
    assert (cwd / "CLAUDE.md").is_file()
    assert not (cwd / "AGENTS.md").exists()
    assert not list(cwd.rglob("oracle.json"))
    if not readonly:
        (cwd / "result.json").write_text('{"total":37}')
    message = "Finished the requested fixture."
sid = str(uuid.uuid4())
model = args[args.index("--model")+1]
mode = os.environ.get("GAD_TEST_CLAUDE_MODE","ok")
if mode == "wrong-model":
    model = "never-requested-model"
print(json.dumps({"type":"system","subtype":"init","session_id":sid,"model":model}))
print(json.dumps({"type":"assistant","session_id":sid,
    "message":{"id":"msg-1","model":model,"content":[{"type":"text","text":message}]}}))
result = {"type":"result","subtype":"success","is_error":False,
    "session_id":sid,"result":message,"modelUsage":{model:{"inputTokens":7,"outputTokens":3}},
    "usage":{"input_tokens":7,"output_tokens":3,"cache_read_input_tokens":2,
             "cache_creation_input_tokens":1}}
if mode == "no-usage":
    result.pop("usage")
if mode == "error":
    result.update(subtype="error_during_execution",is_error=True)
if mode == "bad-final":
    result["result"] = "fabricated final"
print(json.dumps(result))
'''


class AdapterProtocolTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(executors, "versioned backend registry is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / "events.jsonl"
        self.final = self.root / "final.txt"
        self.adapter = executors.get_backend("claude")

    def trace(self, mutation=None, final="done", usage=True):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1", "model": "m-1"},
            {"type": "assistant", "session_id": "s-1", "message": {
                "id": "msg-1", "model": "m-1", "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "a"}}]}},
            {"type": "user", "session_id": "s-1", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "a"}]}},
            {"type": "assistant", "session_id": "s-1", "message": {
                "id": "msg-2", "model": "m-1", "content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "s-1", "result": "done", "modelUsage": {"m-1": {}}},
        ]
        if usage:
            events[-1]["usage"] = {"input_tokens": 7, "output_tokens": 3,
                                   "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}
        if mutation:
            mutation(events)
        self.events.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        self.final.write_text(final, encoding="utf-8")
        return self.adapter.parse_trace(self.events, self.final, model="m-1", readonly=True)

    def test_native_claude_events_bind_session_result_model_and_metrics(self):
        parsed = self.trace()
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["thread_id"], "s-1")
        self.assertEqual(parsed["tool_calls"], 1)
        self.assertEqual(parsed["usage"], {"input_tokens": 10, "output_tokens": 3,
                         "cached_input_tokens": 2, "cache_write_input_tokens": 1})
        self.assertEqual(parsed["usage_source"], "claude.result.usage")

    def test_missing_usage_remains_unavailable(self):
        self.assertIsNone(self.trace(usage=False)["usage"])

    def test_incomplete_session_error_model_final_and_write_tools_cannot_complete(self):
        mutations = [
            lambda e: e.pop(2),
            lambda e: e[2].update(session_id="other"),
            lambda e: e[-1].update(is_error=True),
            lambda e: e[-1].update(result="fabricated"),
            lambda e: e[0].update(model="other"),
            lambda e: e[-1].update(modelUsage={"other": {}}),
            lambda e: e[1]["message"]["content"][0].update(name="Write"),
            lambda e: e.append(copy.deepcopy(e[-1])),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertEqual(self.trace(mutation)["status"], "unverified")
        self.assertEqual(self.trace(final="different")["status"], "unverified")
        self.events.write_text("not json")
        self.assertEqual(self.adapter.parse_trace(self.events, self.final, model="m-1")["status"], "unverified")

    def test_registry_refuses_unknown_backend_and_version(self):
        for name, version in (("invented", None), ("claude", "999"), ("codex", "999")):
            with self.subTest(name=name, version=version), self.assertRaises(UsageError):
                executors.get_backend(name, version)

    def test_claude_does_not_read_codex_identity(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            (self.root / "config.toml").write_text('model="foreign-model"')
            self.assertIsNone(self.adapter.model_config(None)["model"])
            self.assertEqual(self.adapter.model_config("m-1")["model"], "m-1")

    def test_native_init_cannot_advertise_broader_tools_mcp_plugins_or_bypass(self):
        for fields in ({"tools": ["Read", "Bash"]}, {"mcp_servers": [{"name": "external"}]},
                       {"plugins": [{"name": "external"}]}, {"permissionMode": "bypassPermissions"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.trace(lambda e: e[0].update(fields))["status"], "unverified")

    def test_claude_command_rejects_missing_empty_and_blank_models(self):
        for model in (None, "", " \t\n"):
            with self.subTest(model=model), self.assertRaises(UsageError):
                self.adapter.command(["claude"], self.root, self.final, readonly=True, model=model)

    def test_explicit_model_is_preserved_and_codex_can_keep_its_config_default(self):
        for name in ("codex", "claude"):
            command = executors.get_backend(name).command(
                [name], self.root, self.final, readonly=True, model="gpt-6.1-sol")
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        codex = executors.get_backend("codex").command(
            ["codex"], self.root, self.final, readonly=True, model=None)
        self.assertNotIn("--model", codex)


class ClaudeIntegrationTests(unittest.TestCase):
    setUp = test_regress.RegressionTests.setUp
    save_oracle = test_regress.RegressionTests.save_oracle
    report = test_regress.RegressionTests.report

    def run_cli(self, *extra, output=None, backend="claude", mode="ok"):
        self.executor.write_text(CLAUDE_EXECUTOR, encoding="utf-8")
        with patch.dict(os.environ, {"GAD_TEST_CLAUDE_MODE": mode}):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = regress.main(["run", "--mode", "live", "--skill", str(self.skill),
                    "--cases-root", str(self.cases), "--output", str(output or self.output),
                    "--backend", backend, "--executor", str(self.executor), "--model", "m-1",
                    "--timeout", "3", *extra])
        return code, json.loads(captured.getvalue())

    def test_missing_claude_model_rejected_before_process_probe_or_output(self):
        for index, model_args in enumerate(([], ["--model", ""], ["--model", " \t\n"])):
            target = self.root / f"missing-model-{index}"
            with self.subTest(model_args=model_args):
                with patch.object(regress.subprocess, "run", side_effect=AssertionError("CLI version probe reached")):
                    with patch.object(regress.subprocess, "Popen", side_effect=AssertionError("model process reached")):
                        with contextlib.redirect_stdout(io.StringIO()) as captured:
                            code = regress.main(["run", "--mode", "live", "--backend", "claude",
                                "--skill", str(self.skill), "--cases-root", str(self.cases),
                                "--output", str(target), "--executor", str(self.executor), *model_args])
                result = json.loads(captured.getvalue())
                self.assertEqual((code, result["status"]), (2, "invalid"))
                self.assertIn("model", result["errors"][0])
                self.assertFalse(target.exists())

    def test_direct_invocation_rejects_missing_claude_model_before_files_or_process(self):
        for index, model in enumerate((None, "", " \t\n")):
            target = self.root / f"direct-missing-model-{index}"
            frozen = {"backend": "claude", "adapter_version": "1", "run_id": "direct",
                      "model_config": executors.get_backend("claude").model_config(model),
                      "executor": {"sha256": file_hash(self.executor)}, "metrics_version": 2}
            context = {"record": frozen, "evidence": {"path": "identity.json", "sha256": "a" * 64,
                                                     "locator": "file"}}
            with self.subTest(model=model):
                with patch.object(regress.subprocess, "Popen", side_effect=AssertionError("model process reached")):
                    with self.assertRaises(UsageError):
                        regress._invoke(self.executor, self.skill, "Read only.", target, self.root,
                            readonly=True, timeout=1, model=model, execution_context=context, role="actor")
                self.assertFalse(target.exists())

    def test_offline_claude_missing_model_remains_unverified_without_model_process(self):
        with patch.object(regress, "_invoke", side_effect=AssertionError("model invocation reached")):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = regress.main(["run", "--mode", "offline", "--backend", "claude",
                    "--skill", str(self.skill), "--cases-root", str(self.cases),
                    "--output", str(self.output), "--executor", str(self.executor)])
        self.assertEqual(code, 0, captured.getvalue())
        self.assertEqual(self.report()["status"], "unverified")

    def test_native_cli_process_passes_and_tampering_is_rejected(self):
        code, result = self.run_cli()
        self.assertEqual(code, 0, result)
        original = self.report()
        self.assertTrue(original["simulated_executor"])
        self.assertEqual(original["identity"]["backend"], "claude")
        self.assertEqual(original["metrics"]["tokens"]["source"], "claude.result.usage")
        actor = original["cases"][0]["actors"][0]
        for mutation in (
            lambda r: r["identity"].update(backend="codex"),
            lambda r: r["identity"].update(adapter_version="999"),
            lambda r: r["cases"][0]["actors"][0]["command"].append("--dangerously-skip-permissions"),
            lambda r: r["cases"][0]["actors"][0].update(effective_model="fabricated"),
        ):
            changed = copy.deepcopy(original)
            mutation(changed)
            with self.assertRaises((UsageError, ConditionError)):
                regress.validate_report(changed, self.output)
        raw = self.output / actor["events"]["path"]
        raw.write_text(raw.read_text() + "\nCORRUPT")
        with self.assertRaises((UsageError, ConditionError)):
            self.report()

    def test_unknown_backend_fails_before_output_and_bad_native_evidence_cannot_pass(self):
        code, _ = self.run_cli(backend="invented")
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())
        for mode in ("wrong-model", "bad-final", "error", "no-usage"):
            target = self.root / mode
            code, result = self.run_cli(output=target, mode=mode)
            self.assertEqual(code, 0 if mode == "no-usage" else 1, result)
            report = regress.load_report(target / "report.json")
            if mode == "no-usage":
                self.assertEqual(report["metrics"]["tokens"], {"value": None, "source": "unavailable"})
            else:
                self.assertEqual(report["status"], "unverified")

    def test_comparison_refuses_cross_backend(self):
        self.assertEqual(self.run_cli()[0], 0)
        # Real independently created Codex protocol evidence exercises the comparator.
        from test_regress import FAKE_EXECUTOR
        self.executor.write_text(FAKE_EXECUTOR, encoding="utf-8")
        target = self.root / "codex"
        with contextlib.redirect_stdout(io.StringIO()):
            code = regress.main(["run", "--mode", "live", "--skill", str(self.skill),
                "--cases-root", str(self.cases), "--output", str(target),
                "--executor", str(self.executor), "--model", "m-1", "--timeout", "3"])
        self.assertEqual(code, 0)
        result = regress.compare_reports(self.output / "report.json", target / "report.json")
        self.assertFalse(result["identity_compatible"])
        self.assertEqual(result["status"], "unverified")

    def test_rehashed_raw_evidence_and_execution_argv_still_require_native_consistency(self):
        self.assertEqual(self.run_cli()[0], 0)
        original = self.report()
        actor = original["cases"][0]["actors"][0]
        events_path = self.output / actor["events"]["path"]
        execution_path = self.output / actor["execution"]["path"]
        events_bytes, execution_bytes = events_path.read_bytes(), execution_path.read_bytes()

        def rebind(value, relative):
            if isinstance(value, dict):
                if value.get("path") == relative and "sha256" in value:
                    value["sha256"] = file_hash(self.output / relative)
                for item in value.values():
                    rebind(item, relative)
            elif isinstance(value, list):
                for item in value:
                    rebind(item, relative)

        for kind in ("argv", "raw", "environment"):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(original)
                invocation = changed["cases"][0]["actors"][0]
                recorded = json.loads(execution_bytes)
                if kind == "argv":
                    invocation["command"].append("--dangerously-skip-permissions")
                    recorded["command"] = invocation["command"]
                elif kind == "raw":
                    events = [json.loads(line) for line in events_bytes.decode().splitlines()]
                    events[-1]["result"] = "fabricated completion"
                    events_path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
                    rebind(changed, actor["events"]["path"])
                    recorded["files"]["events"] = invocation["events"]
                else:
                    recorded["environment"]["CLAUDE_CONFIG_DIR"] = str(self.root / "foreign-state")
                execution_path.write_text(json.dumps(recorded), encoding="utf-8")
                rebind(changed, actor["execution"]["path"])
                with self.assertRaises((UsageError, ConditionError)):
                    regress.validate_report(changed, self.output)
                events_path.write_bytes(events_bytes)
                execution_path.write_bytes(execution_bytes)

    def test_claude_no_write_uses_readonly_tools_and_no_workspace_writes(self):
        self.oracle.update(route="no-write", checks=[])
        (self.case / "request.md").write_text("NO_WRITE_CASE: analyze only.")
        self.save_oracle()
        code, result = self.run_cli()
        self.assertEqual(code, 0, result)
        case = self.report()["cases"][0]
        self.assertEqual(json.loads((self.output / case["before"]["path"]).read_text()),
                         json.loads((self.output / case["after"]["path"]).read_text()))
        actor = case["actors"][0]
        command = actor["command"]
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")


if __name__ == "__main__":
    unittest.main()
