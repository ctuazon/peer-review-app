"""Invoke Claude via WSL Claude Code (SSO), Windows CLI, or Anthropic API."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.auth_flows import find_wsl_claude, win_path_to_wsl
from app.cost import DEFAULT_REVIEW_MODEL, model_info
from app.process_util import no_window_kwargs

ClaudeEventCallback = Callable[[dict[str, str]], None]

# Pre-approve a narrow set of read-only lookups (repo/PR context) so headless
# runs can verify a claim against the real repo instead of stalling on a
# permission prompt nobody is present to answer. Anything outside this list
# (writes, arbitrary shell commands) still requires approval it will never
# get, so Claude just reports it couldn't check further rather than acting.
ALLOWED_TOOLS = "Bash(gh api *),Bash(gh pr *),Bash(git log *),Read"
# With a local checkout as the working directory, searching it is read-only too.
REPO_TOOLS = ALLOWED_TOOLS + ",Grep,Glob"
NO_TOOLS = "Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch,Task"

DEFAULT_TIMEOUT_SECONDS = 15 * 60
API_MAX_TOKENS = 32_000
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Model ids/aliases only ever come from our own config/constants, but the WSL
# path interpolates this into a shell string (unlike the CLI path, which
# passes it as a separate argv element) -- so it's still validated before
# being embedded, rather than trusted blindly.
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9\-]+$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class ClaudeError(RuntimeError):
    def __init__(self, message: str, partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial


class ClaudeRefused(ClaudeError):
    pass


@dataclass
class ClaudeResult:
    text: str
    structured: dict[str, Any] | None = None
    session_id: str = ""
    cost_usd: float | None = None
    stop_reason: str = ""


@dataclass
class _StreamState:
    final_text: str = ""
    structured: dict[str, Any] | None = None
    session_id: str = ""
    cost_usd: float | None = None
    stop_reason: str = ""
    is_error: bool = False


def resolve_claude_cli(configured_path: str = "claude") -> str | None:
    path = shutil.which(configured_path)
    if path:
        return path
    candidates = [
        configured_path,
        r"%LOCALAPPDATA%\Programs\claude\claude.exe",
    ]
    for candidate in candidates:
        expanded = os.path.expandvars(candidate)
        if shutil.which(expanded):
            return expanded
        if os.path.isfile(expanded):
            return expanded
    return None


def _emit(on_event: ClaudeEventCallback | None, kind: str, text: str) -> None:
    if on_event is None or not text:
        return
    on_event({"kind": kind, "text": text})


def _emit_usage(
    on_event: ClaudeEventCallback | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float | None = None,
) -> None:
    if on_event is None:
        return
    event = {
        "kind": "usage",
        "input_tokens": str(input_tokens),
        "output_tokens": str(output_tokens),
        "cache_read_tokens": str(cache_read_tokens),
        "cache_write_tokens": str(cache_write_tokens),
    }
    if cost_usd is not None:
        event["cost_usd"] = f"{cost_usd:.6f}"
    on_event(event)


def _parse_stream_line(
    line: str,
    on_event: ClaudeEventCallback | None,
    text_acc: list[str] | None = None,
    state: _StreamState | None = None,
) -> str | None:
    """
    Parse one NDJSON stream-json line.
    Returns final result text if this is the result event, else None.
    Emits thinking/text/status chunks via on_event.
    Appends visible answer text to text_acc when provided.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_type = obj.get("type")

    if event_type == "system":
        subtype = obj.get("subtype") or ""
        if state is not None and obj.get("session_id"):
            state.session_id = str(obj["session_id"])
        if subtype == "status":
            status = obj.get("status") or subtype
            _emit(on_event, "status", str(status))
        elif subtype == "thinking_tokens":
            # Flavored progress while reasoning tokens are being generated.
            count = obj.get("tokens") or obj.get("count") or obj.get("estimated_tokens")
            if count is not None:
                _emit(on_event, "status", f"thinking ({count} tokens)")
            else:
                _emit(on_event, "status", "thinking")
        elif subtype and subtype != "init":
            _emit(on_event, "status", subtype)
        return None

    if event_type == "stream_event":
        ev = obj.get("event") or {}
        if not isinstance(ev, dict):
            return None
        et = ev.get("type")
        if et == "content_block_delta":
            delta = ev.get("delta") or {}
            if not isinstance(delta, dict):
                return None
            dtype = delta.get("type")
            if dtype == "thinking_delta":
                thinking = delta.get("thinking") or ""
                if thinking:
                    _emit(on_event, "thinking", thinking)
                elif delta.get("estimated_tokens"):
                    _emit(
                        on_event,
                        "status",
                        f"thinking (~{delta.get('estimated_tokens')} tokens)",
                    )
            elif dtype == "text_delta":
                text = delta.get("text") or ""
                if text:
                    if text_acc is not None:
                        text_acc.append(text)
                    _emit(on_event, "text", text)
        elif et == "content_block_start":
            block = ev.get("content_block") or {}
            if isinstance(block, dict) and block.get("type") == "thinking":
                _emit(on_event, "status", "thinking")
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                _emit(on_event, "status", f"using {block.get('name') or 'a tool'}")
        return None

    if event_type == "assistant":
        message = obj.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                thinking = block.get("thinking") or ""
                if thinking:
                    _emit(on_event, "thinking", thinking)
            elif btype == "text":
                text = str(block.get("text") or "")
                if not text:
                    continue
                # Prefer full assistant text when stream deltas were missing.
                if text_acc is not None and not text_acc:
                    text_acc.append(text)
                    _emit(on_event, "text", text)
        return None

    if event_type == "result":
        usage = obj.get("usage")
        cost = obj.get("total_cost_usd")
        if state is not None:
            if isinstance(obj.get("structured_output"), dict):
                state.structured = obj["structured_output"]
            if obj.get("session_id"):
                state.session_id = str(obj["session_id"])
            if isinstance(cost, (int, float)):
                state.cost_usd = float(cost)
            state.stop_reason = str(obj.get("stop_reason") or obj.get("terminal_reason") or "")
            state.is_error = bool(obj.get("is_error"))
        if isinstance(usage, dict):
            _emit_usage(
                on_event,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0),
                float(cost) if isinstance(cost, (int, float)) else None,
            )
        result = obj.get("result")
        if isinstance(result, str) and result.strip():
            if state is not None:
                state.final_text = result
            return result
        # Some builds put the final answer only on the assistant message.
        return None

    return None


def _finalize_claude_output(
    *,
    final_text: str,
    text_acc: list[str],
    return_code: int,
    stderr: str,
    on_event: ClaudeEventCallback | None,
    empty_message: str,
    structured: dict[str, Any] | None = None,
) -> str:
    output = (final_text or "".join(text_acc)).strip()
    if structured is not None and not output:
        output = json.dumps(structured)
    if output and return_code == 0:
        _emit(on_event, "status", "done")
        return output
    # Keep a usable answer even when the process exits non-zero after streaming it.
    if output and len(output) >= 20:
        _emit(on_event, "status", "done")
        return output
    err = (stderr or output).strip()
    if "not logged in" in err.lower() or "please run /login" in err.lower():
        raise ClaudeError(
            "Claude is not signed in. Use Settings → Login with Claude SSO."
        )
    raise ClaudeError(err or empty_message)


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                **no_window_kwargs(),
            )
        else:
            process.kill()
    except OSError:
        pass


def _stream_process(
    process: subprocess.Popen,
    *,
    on_event: ClaudeEventCallback | None,
    timeout: float,
    label: str,
) -> tuple[_StreamState, list[str], str, int]:
    """Read the NDJSON stream with a wall-clock limit. On timeout the whole
    process tree is killed and ClaudeError carries the partial answer."""
    state = _StreamState()
    text_acc: list[str] = []
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        _kill_tree(process)

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            result = _parse_stream_line(raw_line, on_event, text_acc, state)
            if result is not None:
                state.final_text = result
        stderr = process.stderr.read() if process.stderr is not None else ""
        try:
            return_code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            return_code = 1
    finally:
        timer.cancel()
    if timed_out.is_set():
        minutes = round(timeout / 60, 1)
        raise ClaudeError(
            f"{label} timed out after {minutes:g} min and was stopped.",
            partial="".join(text_acc),
        )
    return state, text_acc, stderr, return_code


@dataclass
class CliOptions:
    model: str | None = None
    effort: str | None = None
    json_schema: dict[str, Any] | None = None
    cwd: str | None = None
    allowed_tools: str = ALLOWED_TOOLS
    disallowed_tools: str = ""
    resume_session: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS


def _effort_supported(model: str | None, effort: str | None) -> bool:
    if not effort or effort not in _EFFORTS:
        return False
    info = model_info(model or "")
    return info is None or info.supports_effort


def run_claude_wsl_result(
    prompt: str, on_event: ClaudeEventCallback | None = None, options: CliOptions | None = None
) -> ClaudeResult:
    opts = options or CliOptions()
    claude = find_wsl_claude()
    if not claude:
        raise ClaudeError(
            "Claude Code not found in WSL. Use Settings → Login with Claude SSO, "
            "or install Claude Code."
        )
    if opts.model and not _SAFE_MODEL_RE.match(opts.model):
        raise ClaudeError(f"Invalid model id: {opts.model!r}")
    if opts.resume_session and not _SAFE_SESSION_RE.match(opts.resume_session):
        raise ClaudeError("Invalid session id.")

    temp_paths: list[Path] = []

    def temp(text: str, suffix: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=suffix, prefix="peer-review-", delete=False
        ) as handle:
            handle.write(text)
        path = Path(handle.name)
        temp_paths.append(path)
        return path

    prompt_path = temp(prompt, ".txt")
    flags = [f"--allowedTools {shlex.quote(opts.allowed_tools)}"]
    if opts.disallowed_tools:
        flags.append(f"--disallowedTools {shlex.quote(opts.disallowed_tools)}")
    if opts.model:
        flags.append(f'--model "{opts.model}"')
    if _effort_supported(opts.model, opts.effort):
        flags.append(f"--effort {opts.effort}")
    if opts.json_schema is not None:
        schema_path = temp(json.dumps(opts.json_schema), ".json")
        flags.append(f'--json-schema "$(cat {shlex.quote(win_path_to_wsl(schema_path))})"')
    if opts.resume_session:
        flags.append(f"--resume {opts.resume_session}")
    cd = f"cd {shlex.quote(win_path_to_wsl(Path(opts.cwd)))} && " if opts.cwd else ""
    # Stream NDJSON so the UI can show thinking / live text. The prompt is
    # piped in via stdin redirection rather than passed as a CLI argument --
    # a large diff/triage prompt embedded in argv can exceed the OS's
    # argument-length limit ("Argument list too long").
    bash = (
        "set -euo pipefail; "
        f'{cd}"{claude}" -p {" ".join(flags)} '
        "--output-format stream-json --verbose --include-partial-messages "
        f"< {shlex.quote(win_path_to_wsl(prompt_path))}"
    )
    try:
        _emit(on_event, "status", "starting Claude")
        process = subprocess.Popen(
            ["wsl", "-e", "bash", "-lc", bash],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **no_window_kwargs(),
        )
        state, text_acc, stderr, return_code = _stream_process(
            process, on_event=on_event, timeout=opts.timeout, label="Claude (WSL)"
        )
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    text = _finalize_claude_output(
        final_text=state.final_text,
        text_acc=text_acc,
        return_code=return_code,
        stderr=stderr,
        on_event=on_event,
        empty_message="Claude (WSL) returned no output.",
        structured=state.structured,
    )
    return ClaudeResult(text, state.structured, state.session_id, state.cost_usd, state.stop_reason)


def run_claude_cli_result(
    prompt: str,
    cli_path: str = "claude",
    on_event: ClaudeEventCallback | None = None,
    options: CliOptions | None = None,
) -> ClaudeResult:
    opts = options or CliOptions()
    resolved = resolve_claude_cli(cli_path)
    if not resolved:
        return run_claude_wsl_result(prompt, on_event=on_event, options=opts)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="peer-review-prompt-", delete=False
    ) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    args = ["--allowedTools", opts.allowed_tools]
    if opts.disallowed_tools:
        args += ["--disallowedTools", opts.disallowed_tools]
    if opts.model:
        args += ["--model", opts.model]
    if _effort_supported(opts.model, opts.effort):
        args += ["--effort", str(opts.effort)]
    if opts.json_schema is not None:
        args += ["--json-schema", json.dumps(opts.json_schema)]
    if opts.resume_session:
        args += ["--resume", opts.resume_session]
    try:
        # Prompt is piped in via stdin, not passed as a CLI argument -- a
        # large diff/triage prompt embedded in argv can exceed the OS's
        # command-line length limit (Windows caps this around 32K chars).
        cmd = [resolved, "-p", *args, "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        _emit(on_event, "status", "starting Claude")
        with open(prompt_path, "r", encoding="utf-8") as stdin_handle:
            process = subprocess.Popen(
                cmd,
                stdin=stdin_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=opts.cwd or None,
                **no_window_kwargs(),
            )
        state, text_acc, stderr, return_code = _stream_process(
            process, on_event=on_event, timeout=opts.timeout, label="Claude CLI"
        )
        try:
            text = _finalize_claude_output(
                final_text=state.final_text,
                text_acc=text_acc,
                return_code=return_code,
                stderr=stderr,
                on_event=on_event,
                empty_message="Claude CLI returned no output.",
                structured=state.structured,
            )
            return ClaudeResult(text, state.structured, state.session_id, state.cost_usd, state.stop_reason)
        except ClaudeError:
            # Fall back to non-stream text mode.
            pass
        with open(prompt_path, "r", encoding="utf-8") as stdin_handle:
            completed = subprocess.run(
                [resolved, "-p", *args, "--output-format", "text"],
                stdin=stdin_handle,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=opts.timeout,
                check=False,
                cwd=opts.cwd or None,
                **no_window_kwargs(),
            )
        if completed.returncode == 0 and (completed.stdout or "").strip():
            text = completed.stdout.strip()
            _emit(on_event, "text", text)
            return ClaudeResult(text)
        raise ClaudeError((stderr or completed.stderr or "Claude CLI returned no output.").strip())
    except FileNotFoundError as exc:
        raise ClaudeError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"Claude CLI timed out after {opts.timeout / 60:g} minutes.") from exc
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_claude_wsl(
    prompt: str,
    on_event: ClaudeEventCallback | None = None,
    model: str | None = None,
) -> str:
    return run_claude_wsl_result(prompt, on_event, CliOptions(model=model)).text


def run_claude_cli(
    prompt: str,
    cli_path: str = "claude",
    on_event: ClaudeEventCallback | None = None,
    model: str | None = None,
) -> str:
    return run_claude_cli_result(prompt, cli_path, on_event, CliOptions(model=model)).text


# --- API mode ----------------------------------------------------------------

def _api_client(api_key: str):
    if not api_key:
        raise ClaudeError("Anthropic API key is missing. Set it in Settings.")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise ClaudeError(
            "The anthropic package is not installed. Run: pip install anthropic"
        ) from exc
    return Anthropic(api_key=api_key)


def api_request_kwargs(
    *,
    model: str,
    system_blocks: list[tuple[str, bool]] | None,
    user_blocks: list[tuple[str, bool]],
    effort: str | None = None,
    json_schema: dict[str, Any] | None = None,
    max_tokens: int = API_MAX_TOKENS,
) -> dict[str, Any]:
    """Messages API kwargs. Blocks are (text, cache) pairs; `cache` puts a
    cache breakpoint after that block (at most 4 are honoured)."""

    def blocks(items: list[tuple[str, bool]]) -> list[dict[str, Any]]:
        out = []
        for text, cache in items:
            block: dict[str, Any] = {"type": "text", "text": text}
            if cache:
                block["cache_control"] = {"type": "ephemeral"}
            out.append(block)
        return out

    info = model_info(model)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": blocks(user_blocks)}],
    }
    if system_blocks:
        kwargs["system"] = blocks(system_blocks)
    output_config: dict[str, Any] = {}
    if effort in _EFFORTS and (info is None or info.supports_effort):
        output_config["effort"] = effort
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    if output_config:
        kwargs["output_config"] = output_config
    # Fable reasons on every request and rejects an explicit thinking config;
    # Haiku 4.5 has no adaptive thinking.
    if info is None or info.send_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    return kwargs


def _supports_fallbacks(model: str) -> bool:
    return model in {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}


def run_claude_api_result(
    *,
    api_key: str,
    model: str,
    system_blocks: list[tuple[str, bool]] | None,
    user_blocks: list[tuple[str, bool]],
    on_event: ClaudeEventCallback | None = None,
    effort: str | None = None,
    json_schema: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ClaudeResult:
    client = _api_client(api_key).with_options(timeout=timeout)
    import anthropic

    kwargs = api_request_kwargs(
        model=model, system_blocks=system_blocks, user_blocks=user_blocks, effort=effort, json_schema=json_schema
    )
    if _supports_fallbacks(model):
        # On a safety decline the API re-runs the request on a fallback model
        # inside the same call instead of returning nothing.
        kwargs["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
        kwargs["extra_body"] = {"fallbacks": "default"}

    _emit(on_event, "status", "requesting")
    parts: list[str] = []

    def stream_once(request: dict[str, Any]):
        parts.clear()
        with client.messages.stream(**request) as stream:
            for event in stream:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", "")
                if dtype == "thinking_delta":
                    _emit(on_event, "thinking", getattr(delta, "thinking", "") or "")
                elif dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        parts.append(text)
                        _emit(on_event, "text", text)
            return stream.get_final_message()

    try:
        try:
            message = stream_once(kwargs)
        except anthropic.BadRequestError as exc:
            if "fallback" not in str(exc).lower() or "extra_body" not in kwargs:
                raise
            kwargs.pop("extra_body", None)
            kwargs.pop("extra_headers", None)
            message = stream_once(kwargs)
    except anthropic.APITimeoutError as exc:
        raise ClaudeError(f"Claude API timed out after {timeout / 60:g} min.", partial="".join(parts)) from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}", partial="".join(parts)) from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError(f"Could not reach the Claude API: {exc}") from exc

    usage = getattr(message, "usage", None)
    if usage is not None:
        from app.cost import actual_cost

        in_t = int(getattr(usage, "input_tokens", 0) or 0)
        out_t = int(getattr(usage, "output_tokens", 0) or 0)
        read_t = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        write_t = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        _emit_usage(on_event, in_t, out_t, read_t, write_t, actual_cost(model, in_t, out_t, read_t, write_t))

    stop_reason = str(getattr(message, "stop_reason", "") or "")
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ClaudeRefused(
            "The model declined to review this PR"
            + (f" (category: {category})." if category else ".")
            + " Nothing was posted; try another model or narrow the diff."
        )
    text = "".join(parts).strip()
    if not text:
        text = "".join(getattr(b, "text", "") or "" for b in message.content).strip()
    if not text:
        raise ClaudeError("Claude API returned an empty response.")
    if stop_reason == "max_tokens":
        _emit(on_event, "status", "output hit max_tokens; the review may be cut short")
    _emit(on_event, "status", "done")
    return ClaudeResult(text=text, stop_reason=stop_reason)


def count_tokens_api(
    *, api_key: str, model: str, system_blocks: list[tuple[str, bool]] | None, user_blocks: list[tuple[str, bool]]
) -> int:
    client = _api_client(api_key)
    kwargs = api_request_kwargs(model=model, system_blocks=system_blocks, user_blocks=user_blocks)
    count_kwargs = {k: kwargs[k] for k in ("model", "messages", "system", "thinking") if k in kwargs}
    return int(client.messages.count_tokens(**count_kwargs).input_tokens)


def run_claude_api(
    prompt: str,
    api_key: str,
    model: str = DEFAULT_REVIEW_MODEL,
    on_event: ClaudeEventCallback | None = None,
    system: str | None = None,
) -> str:
    # Cache the (constant, often large) instruction text as its own block:
    # Anthropic reuses it across calls that repeat this exact prefix instead
    # of rebilling it as fresh input tokens every time.
    return run_claude_api_result(
        api_key=api_key,
        model=model,
        system_blocks=[(system, True)] if system else None,
        user_blocks=[(prompt, False)],
        on_event=on_event,
    ).text


def _timeout(config: dict[str, Any]) -> float:
    try:
        minutes = float(config.get("claude_timeout_minutes") or 15)
    except (TypeError, ValueError):
        minutes = 15
    return max(1.0, minutes) * 60


def resolve_api_key(config: dict[str, Any]) -> str:
    """Settings first, then the ANTHROPIC_API_KEY environment variable."""
    return (config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def effective_mode(config: dict[str, Any]) -> str:
    """"api" only when API mode is chosen AND a key is found; otherwise "cli"
    (Claude Code via WSL or Windows), so a missing key falls back to the CLI
    instead of failing the run."""
    mode = (config.get("claude_mode") or "cli").lower()
    return "api" if mode == "api" and resolve_api_key(config) else "cli"


def api_fallback_note(config: dict[str, Any]) -> str:
    if (config.get("claude_mode") or "").lower() == "api" and effective_mode(config) == "cli":
        return "API mode is selected but no Anthropic API key was found; using Claude Code CLI instead."
    return ""


def _prefers_wsl(config: dict[str, Any]) -> bool:
    mode = (config.get("claude_mode") or "cli").lower()
    return mode in {"wsl", "sso"} or bool(config.get("use_wsl_claude", True))


def run_claude_cli_any(
    prompt: str, config: dict[str, Any], on_event: ClaudeEventCallback | None, options: CliOptions
) -> ClaudeResult:
    """WSL first when preferred (falling back to the Windows CLI), else the CLI."""
    cli_path = config.get("claude_cli_path") or "claude"
    if _prefers_wsl(config):
        try:
            return run_claude_wsl_result(prompt, on_event=on_event, options=options)
        except ClaudeError as exc:
            if exc.partial or not resolve_claude_cli(cli_path):
                raise
            return run_claude_cli_result(prompt, cli_path=cli_path, on_event=on_event, options=options)
    return run_claude_cli_result(prompt, cli_path=cli_path, on_event=on_event, options=options)


def run_claude(
    prompt: str,
    config: dict[str, Any],
    on_event: ClaudeEventCallback | None = None,
    model: str | None = None,
    system: str | None = None,
) -> str:
    """model, when given, overrides config["claude_model"] for this one call
    -- e.g. a cheap classification stage that doesn't need the full model.

    system, when given, is constant instruction text shared across many
    calls (e.g. an output-format contract). In API mode it's sent as its own
    cache_control'd block so repeated calls don't rebill it as fresh input
    tokens. The WSL/local-CLI paths have no equivalent cache control to hook
    into, so there it's just folded into the prompt text as before.
    """
    chosen = model or config.get("claude_model") or DEFAULT_REVIEW_MODEL
    if effective_mode(config) == "api":
        return run_claude_api_result(
            api_key=resolve_api_key(config),
            model=chosen,
            system_blocks=[(system, True)] if system else None,
            user_blocks=[(prompt, False)],
            on_event=on_event,
            timeout=_timeout(config),
        ).text
    note = api_fallback_note(config)
    if note:
        _emit(on_event, "status", note)
    combined_prompt = f"{system}\n\n{prompt}" if system else prompt
    return run_claude_cli_any(
        combined_prompt, config, on_event, CliOptions(model=chosen, timeout=_timeout(config))
    ).text
