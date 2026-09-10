"""Invoke Claude via WSL Claude Code (SSO), Windows CLI, or Anthropic API."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.auth_flows import find_wsl_claude, win_path_to_wsl
from app.process_util import no_window_kwargs

ClaudeEventCallback = Callable[[dict[str, str]], None]

# Pre-approve a narrow set of read-only lookups (repo/PR context) so headless
# runs can verify a claim against the real repo instead of stalling on a
# permission prompt nobody is present to answer. Anything outside this list
# (writes, arbitrary shell commands) still requires approval it will never
# get, so Claude just reports it couldn't check further rather than acting.
ALLOWED_TOOLS = "Bash(gh api *),Bash(gh pr *),Bash(git log *),Read"

# Model ids/aliases only ever come from our own config/constants, but the WSL
# path interpolates this into a shell string (unlike the CLI path, which
# passes it as a separate argv element) -- so it's still validated before
# being embedded, rather than trusted blindly.
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ClaudeError(RuntimeError):
    pass


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
) -> None:
    if on_event is None:
        return
    on_event(
        {
            "kind": "usage",
            "input_tokens": str(input_tokens),
            "output_tokens": str(output_tokens),
            "cache_read_tokens": str(cache_read_tokens),
        }
    )


def _parse_stream_line(
    line: str,
    on_event: ClaudeEventCallback | None,
    text_acc: list[str] | None = None,
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
        if isinstance(usage, dict):
            _emit_usage(
                on_event,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
            )
        result = obj.get("result")
        if isinstance(result, str) and result.strip():
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
) -> str:
    output = (final_text or "".join(text_acc)).strip()
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


def run_claude_wsl(
    prompt: str,
    on_event: ClaudeEventCallback | None = None,
    model: str | None = None,
) -> str:
    claude = find_wsl_claude()
    if not claude:
        raise ClaudeError(
            "Claude Code not found in WSL. Use Settings → Login with Claude SSO, "
            "or install Claude Code."
        )
    if model and not _SAFE_MODEL_RE.match(model):
        raise ClaudeError(f"Invalid model id: {model!r}")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix="peer-review-prompt-",
        delete=False,
    ) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    wsl_prompt = win_path_to_wsl(prompt_path)
    model_flag = f'--model "{model}" ' if model else ""
    # Stream NDJSON so the UI can show thinking / live text. The prompt is
    # piped in via stdin redirection rather than passed as a CLI argument --
    # a large diff/triage prompt embedded in argv can exceed the OS's
    # argument-length limit ("Argument list too long").
    bash = (
        f'set -euo pipefail; '
        f'"{claude}" -p '
        f'--allowedTools "{ALLOWED_TOOLS}" '
        f"{model_flag}"
        f"--output-format stream-json --verbose --include-partial-messages "
        f'< "{wsl_prompt}"'
    )
    final_text = ""
    text_acc: list[str] = []
    process: subprocess.Popen[str] | None = None
    return_code = 1
    stderr = ""
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
        assert process.stdout is not None
        for raw_line in process.stdout:
            result = _parse_stream_line(raw_line, on_event, text_acc)
            if result is not None:
                final_text = result

        if process.stderr is not None:
            stderr = process.stderr.read()
        return_code = process.wait(timeout=60)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            process.kill()
        raise ClaudeError("Claude (WSL) timed out.") from exc
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except OSError:
            pass

    return _finalize_claude_output(
        final_text=final_text,
        text_acc=text_acc,
        return_code=return_code,
        stderr=stderr,
        on_event=on_event,
        empty_message="Claude (WSL) returned no output.",
    )


def run_claude_cli(
    prompt: str,
    cli_path: str = "claude",
    on_event: ClaudeEventCallback | None = None,
    model: str | None = None,
) -> str:
    resolved = resolve_claude_cli(cli_path)
    if not resolved:
        return run_claude_wsl(prompt, on_event=on_event, model=model)

    # Local CLI: try streaming first, then fall back to plain text.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix="peer-review-prompt-",
        delete=False,
    ) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    model_args = ["--model", model] if model else []
    try:
        # Prompt is piped in via stdin, not passed as a CLI argument -- a
        # large diff/triage prompt embedded in argv can exceed the OS's
        # command-line length limit (Windows caps this around 32K chars).
        cmd = [
            resolved,
            "-p",
            "--allowedTools",
            ALLOWED_TOOLS,
            *model_args,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
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
                **no_window_kwargs(),
            )
        assert process.stdout is not None
        final_text = ""
        text_acc: list[str] = []
        for raw_line in process.stdout:
            result = _parse_stream_line(raw_line, on_event, text_acc)
            if result is not None:
                final_text = result
        stderr = process.stderr.read() if process.stderr else ""
        return_code = process.wait(timeout=30)
        try:
            return _finalize_claude_output(
                final_text=final_text,
                text_acc=text_acc,
                return_code=return_code,
                stderr=stderr,
                on_event=on_event,
                empty_message="Claude CLI returned no output.",
            )
        except ClaudeError:
            # Fall back to non-stream text mode.
            pass
        with open(prompt_path, "r", encoding="utf-8") as stdin_handle:
            completed = subprocess.run(
                [
                    resolved,
                    "-p",
                    "--allowedTools",
                    ALLOWED_TOOLS,
                    *model_args,
                    "--output-format",
                    "text",
                ],
                stdin=stdin_handle,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
                **no_window_kwargs(),
            )
        if completed.returncode == 0 and (completed.stdout or "").strip():
            text = completed.stdout.strip()
            _emit(on_event, "text", text)
            return text
        raise ClaudeError((stderr or completed.stderr or "Claude CLI returned no output.").strip())
    except FileNotFoundError as exc:
        raise ClaudeError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError("Claude CLI timed out after 10 minutes.") from exc
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_claude_api(
    prompt: str,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    on_event: ClaudeEventCallback | None = None,
    system: str | None = None,
) -> str:
    if not api_key:
        raise ClaudeError("Anthropic API key is missing. Set it in Settings.")

    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise ClaudeError(
            "The anthropic package is not installed. Run: pip install anthropic"
        ) from exc

    client = Anthropic(api_key=api_key)
    _emit(on_event, "status", "requesting")
    parts: list[str] = []
    request_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        # Cache the (constant, often large) instruction text as its own
        # block: Anthropic will reuse it across calls that repeat this exact
        # prefix instead of rebilling it as fresh input tokens every time.
        request_kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    # Prefer streaming API when available.
    try:
        with client.messages.stream(**request_kwargs) as stream:
            for event in stream:
                et = getattr(event, "type", "")
                if et == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", "")
                    if dtype == "thinking_delta":
                        thinking = getattr(delta, "thinking", "") or ""
                        if thinking:
                            _emit(on_event, "thinking", thinking)
                    elif dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            parts.append(text)
                            _emit(on_event, "text", text)
            message = stream.get_final_message()
            if not parts:
                for block in message.content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
    except Exception:
        message = client.messages.create(**request_kwargs)
        for block in message.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                _emit(on_event, "text", text)

    usage = getattr(message, "usage", None)
    if usage is not None:
        _emit_usage(
            on_event,
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
            int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    result = "".join(parts).strip()
    if not result:
        raise ClaudeError("Claude API returned an empty response.")
    _emit(on_event, "status", "done")
    return result


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
    mode = (config.get("claude_mode") or "cli").lower()
    if mode == "api":
        return run_claude_api(
            prompt,
            api_key=config.get("anthropic_api_key") or "",
            model=model or config.get("claude_model") or "claude-sonnet-4-20250514",
            on_event=on_event,
            system=system,
        )
    combined_prompt = f"{system}\n\n{prompt}" if system else prompt
    if mode in {"wsl", "sso"} or bool(config.get("use_wsl_claude", True)):
        try:
            return run_claude_wsl(combined_prompt, on_event=on_event, model=model)
        except ClaudeError:
            if resolve_claude_cli(config.get("claude_cli_path") or "claude"):
                return run_claude_cli(
                    combined_prompt,
                    cli_path=config.get("claude_cli_path") or "claude",
                    on_event=on_event,
                    model=model,
                )
            raise
    return run_claude_cli(
        combined_prompt,
        cli_path=config.get("claude_cli_path") or "claude",
        on_event=on_event,
        model=model,
    )
