"""Built-in, versioned CLI adapters. Synthetic protocol tests do not certify hosts."""

import json
import os
from pathlib import Path

from delivery_io import UsageError


USAGE_FIELDS = {"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens"}
INSTRUCTIONS = (
    "Evaluation only. Apply ./skill/SKILL.md and explicitly requested domain skill only. "
    "Learning is disabled (GAD_EVALUATION=1, GAD_LEARNING_DISABLED=1). "
    "No shared learning or evaluation recursion. Do not alter the skill snapshot.\n"
)


def _text(path):
    return Path(path).read_text(encoding="utf-8-sig")


class Codex:
    backend = "codex"
    adapter_version = "1"
    instruction_file = "AGENTS.md"
    usage_source = "turn.completed.usage"
    tools_source = "item.completed"

    def validate_model(self, model):
        # Legacy Codex may resolve its model from its own configuration.
        pass

    def command(self, prefix, cwd, final, *, readonly, model):
        args = list(prefix) + [
            "exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "--cd", str(Path(cwd).resolve()), "--sandbox", "read-only" if readonly else "workspace-write",
            "--output-last-message", str(Path(final).resolve())]
        if model:
            args += ["--model", model]
        return args + ["-"]

    def instructions(self):
        return INSTRUCTIONS

    def environment(self, directory):
        return {}

    def model_config(self, explicit):
        return codex_model_config(explicit)

    def prepare_final(self, events_path, final_path):
        # Codex writes --output-last-message itself.
        pass

    def parse_trace(self, events_path, final_path, *, model=None, readonly=False):
        return parse_codex_trace(events_path, final_path)


def _claude_events(path):
    events = []
    for line in _text(path).splitlines():
        if line.strip():
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be a typed object")
            events.append(event)
    return events


def _claude_usage(value):
    if not isinstance(value, dict):
        return None
    fields = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
    if (not {"input_tokens", "output_tokens"}.issubset(value) or
            any(type(value[key]) is not int or value[key] < 0 for key in fields & set(value))):
        return None
    # Claude input_tokens excludes cache reads/writes; normalized input includes both.
    result = {"input_tokens": value["input_tokens"] + value.get("cache_read_input_tokens", 0) +
              value.get("cache_creation_input_tokens", 0), "output_tokens": value["output_tokens"]}
    for source, target in (("cache_read_input_tokens", "cached_input_tokens"),
                           ("cache_creation_input_tokens", "cache_write_input_tokens")):
        if source in value:
            result[target] = value[source]
    return result


class Claude:
    backend = "claude"
    adapter_version = "1"
    instruction_file = "CLAUDE.md"
    usage_source = "claude.result.usage"
    tools_source = "claude.assistant.tool_use"
    readonly_tools = {"Read", "Glob", "Grep"}
    write_tools = readonly_tools | {"Edit", "Write", "Bash"}

    def validate_model(self, model):
        if not isinstance(model, str) or not model.strip():
            raise UsageError("Claude live execution requires an explicit nonblank --model")

    def command(self, prefix, cwd, final, *, readonly, model):
        self.validate_model(model)
        tools = "Read,Glob,Grep" if readonly else "Read,Glob,Grep,Edit,Write,Bash"
        settings = {"disableAllHooks": True, "enableAllProjectMcpServers": False,
                    "enabledPlugins": {}, "autoUpdatesChannel": "stable"}
        args = list(prefix) + [
            "--print", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", "",
            "--settings", json.dumps(settings, separators=(",", ":")),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-chrome", "--permission-mode", "dontAsk",
            "--tools", tools, "--allowedTools", tools,
            "--append-system-prompt", self.instructions()]
        args += ["--model", model]
        return args

    def instructions(self):
        return INSTRUCTIONS

    def environment(self, directory):
        return {"CLAUDE_CONFIG_DIR": str((Path(directory) / "host-state").resolve()),
                "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}

    def model_config(self, explicit):
        # No implicit fallback to Codex or another vendor's local configuration.
        return {"model": explicit, "provider": "anthropic", "reasoning_effort": None,
                "source": "explicit" if explicit else "unknown"}

    def prepare_final(self, events_path, final_path):
        try:
            results = [e for e in _claude_events(events_path) if e["type"] == "result"]
            if len(results) == 1 and isinstance(results[0].get("result"), str):
                Path(final_path).write_text(results[0]["result"], encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            pass

    def parse_trace(self, events_path, final_path, *, model=None, readonly=False):
        invalid = {"status": "unverified", "observation": "incomplete or invalid Claude stream",
                   "usage": None, "tool_calls": None, "thread_id": None,
                   "effective_model": None, "usage_source": self.usage_source,
                   "tools_source": self.tools_source}
        try:
            events = _claude_events(events_path)
            init = [e for e in events if e["type"] == "system" and e.get("subtype") == "init"]
            results = [e for e in events if e["type"] == "result"]
            if len(init) != 1 or len(results) != 1 or events[0] != init[0] or events[-1] != results[0]:
                return invalid
            first, result = init[0], results[0]
            sid, actual_model = first.get("session_id"), first.get("model")
            invalid.update(thread_id=sid, effective_model=actual_model)
            if (not isinstance(sid, str) or not sid.strip() or not isinstance(actual_model, str) or
                    not actual_model.strip() or not model or actual_model != model or
                    result.get("subtype") != "success" or result.get("is_error") is not False or
                    not isinstance(result.get("result"), str) or not result["result"].strip()):
                return invalid
            pending, seen, texts = set(), set(), []
            calls = 0
            allowed = self.readonly_tools if readonly else self.write_tools
            if ("tools" in first and (not isinstance(first["tools"], list) or
                                      any(tool not in allowed for tool in first["tools"])) or
                    first.get("mcp_servers", []) != [] or first.get("plugins", []) != [] or
                    first.get("permissionMode", "dontAsk") != "dontAsk"):
                return invalid
            for event in events:
                if event.get("session_id") != sid:
                    return invalid
                kind = event["type"]
                if kind == "assistant":
                    message = event.get("message")
                    if (not isinstance(message, dict) or message.get("model") != actual_model or
                            event.get("error") or not isinstance(message.get("content"), list)):
                        return invalid
                    chunks = []
                    for block in message["content"]:
                        if not isinstance(block, dict):
                            return invalid
                        if block.get("type") == "text":
                            if not isinstance(block.get("text"), str):
                                return invalid
                            chunks.append(block["text"])
                        elif block.get("type") == "tool_use":
                            identifier = block.get("id")
                            if (not isinstance(identifier, str) or not identifier or identifier in seen or
                                    block.get("name") not in allowed or not isinstance(block.get("input"), dict)):
                                return invalid
                            pending.add(identifier)
                            seen.add(identifier)
                            calls += 1
                        elif block.get("type") not in {"thinking", "redacted_thinking"}:
                            return invalid
                    if chunks:
                        texts.append("".join(chunks))
                elif kind == "user":
                    message = event.get("message", {})
                    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                        return invalid
                    for block in message["content"]:
                        if (not isinstance(block, dict) or block.get("type") != "tool_result" or
                                block.get("tool_use_id") not in pending):
                            return invalid
                        pending.remove(block["tool_use_id"])
                elif kind not in {"system", "result", "rate_limit_event"}:
                    return invalid
            model_usage = result.get("modelUsage")
            if model_usage is not None and (not isinstance(model_usage, dict) or set(model_usage) != {actual_model}):
                return invalid
            if (pending or not texts or texts[-1] != result["result"] or
                    _text(final_path).rstrip("\r\n") != result["result"].rstrip("\r\n")):
                return invalid
            return {**invalid, "status": "completed", "observation": "complete Claude session and matching final",
                    "usage": _claude_usage(result.get("usage")), "tool_calls": calls}
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            return invalid


_REGISTRY = {"codex": Codex(), "claude": Claude()}


def get_backend(name="codex", version=None):
    if name not in _REGISTRY:
        raise UsageError(f"unknown backend: {name}")
    adapter = _REGISTRY[name]
    if version is not None and version != adapter.adapter_version:
        raise UsageError(f"unsupported {name} adapter version: {version}")
    return adapter


def for_record(record):
    if "backend" not in record and "adapter_version" not in record:
        return get_backend("codex")
    if "backend" not in record or "adapter_version" not in record:
        raise UsageError("backend identity is incomplete")
    return get_backend(record["backend"], record["adapter_version"])


def metric_sources(backend="codex", version=None):
    adapter = get_backend(backend, version)
    return adapter.usage_source, adapter.tools_source


def parse_codex_trace(events_path, final_path):
    """Require a complete Codex JSONL turn and a matching final agent message."""
    events = []
    try:
        for line in _text(events_path).splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be a typed object")
            events.append(event)
    except (ValueError, UnicodeError, OSError) as error:
        return {"status": "unverified", "observation": f"malformed events: {error}", "usage": None, "tool_calls": None}
    types = [event["type"] for event in events]
    messages = [event.get("item", {}).get("text") for event in events
                if event["type"] == "item.completed" and
                isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message"]
    completed = [event for event in events if event["type"] == "turn.completed"]
    thread_ids = [event.get("thread_id") for event in events if event["type"] == "thread.started"]
    thread_id = thread_ids[0] if len(thread_ids) == 1 else None
    pending_items = set()
    items_valid = True
    for event in events:
        item = event.get("item")
        if event["type"] in {"item.started", "item.completed"} and isinstance(item, dict):
            identifier = item.get("id")
            if event["type"] == "item.started":
                if not identifier or identifier in pending_items:
                    items_valid = False
                pending_items.add(identifier)
            else:
                pending_items.discard(identifier)
    valid = (types.count("thread.started") == 1 and types.count("turn.started") == 1 and len(completed) == 1 and
             "turn.failed" not in types and
             types.index("thread.started") < types.index("turn.started") < types.index("turn.completed") and
             not pending_items and items_valid and isinstance(thread_id, str) and bool(thread_id.strip()))
    if completed:
        complete_index = types.index("turn.completed")
        if any(event["type"] in {"item.started", "item.completed", "turn.started"} for event in events[complete_index + 1:]):
            valid = False
    if not Path(final_path).is_file() or not messages:
        valid = False
    elif not _text(final_path).strip() or messages[-1] != _text(final_path).strip():
        # output-last-message can append a final LF.
        valid = messages[-1] == _text(final_path).rstrip("\r\n") and valid
    usage = completed[0].get("usage") if completed else None
    if (not isinstance(usage, dict) or not {"input_tokens", "output_tokens"}.issubset(usage) or
            not set(usage).issubset(USAGE_FIELDS) or
            any(type(v) is not int or v < 0 for v in usage.values())):
        usage = None
    calls = sum(1 for event in events if event["type"] == "item.completed" and
                isinstance(event.get("item"), dict) and
                event["item"].get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"})
    return {"status": "completed" if valid else "unverified",
            "observation": "complete turn and matching final" if valid else "incomplete or failed turn",
            "usage": usage, "tool_calls": calls if valid else None, "thread_id": thread_id}


def codex_model_config(explicit):
    try:
        import tomllib
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
        if config.get("profile"):
            config = {**config, **config.get("profiles", {}).get(config["profile"], {})}
        return {"model": explicit or config.get("model"), "provider": config.get("model_provider", "openai"),
                "reasoning_effort": config.get("model_reasoning_effort", config.get("reasoning_effort")),
                "source": "explicit" if explicit else "local-config", "profile": config.get("profile")}
    except (OSError, ValueError, ImportError):
        if explicit:
            return {"model": explicit, "provider": "unknown", "reasoning_effort": None, "source": "explicit"}
        return {"model": None, "provider": None, "reasoning_effort": None, "source": "unknown"}

