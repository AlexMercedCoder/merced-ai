"""Turn a harness's stdout into the reply text, the structured payload, and a session ID.

These helpers are part of the adapter plugin API: a plugin can pass ``output="json"`` or
``output="text"`` to :class:`~merced_ai.harnesses.api.HarnessSpec`, or call them from its own
normalizer.
"""

from __future__ import annotations

import json
import re
from typing import Any

NormalizedOutput = tuple[str, dict[str, Any] | None, str | None]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def normalize_text(stdout: str) -> NormalizedOutput:
    return stdout.strip(), None, None


def normalize_json(stdout: str) -> NormalizedOutput:
    """Reply from a JSON document, a JSON Lines stream, or a JSON document after status lines.

    An explicit top-level answer (``result``, ``response``, ``output``, ``answer``) wins.
    Otherwise the assistant's own turn is used: transcripts (Goose ``messages``, JSONL event
    streams) also contain the user's message, which must never come back as the reply.
    """
    text = stdout.strip()
    streamed = False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _parse_json_lines(text)
        streamed = payload is not None
        if payload is None:
            payload = _parse_trailing_object(text)
        if payload is None:
            return text, None, None
    if not isinstance(payload, dict):
        return text, None, None
    output = _top_level_text(payload) or _find_assistant_text(payload)
    if output is None and not streamed:
        output = _find_text(payload)
    return output or "", payload, _find_string(payload, ("session_id", "sessionId"))


def normalize_repl(stdout: str, prompt_label: str) -> NormalizedOutput:
    """Last reply from a REPL transcript such as ``you> ... anton> ...``."""
    clean = ANSI_ESCAPE_RE.sub("", stdout.strip())
    label = re.escape(prompt_label)
    responses = re.findall(rf"(?:^|\n){label}>\s*(.*?)(?=\n(?:you>|{label}>)|\Z)", clean, re.DOTALL)
    return (responses[-1].strip() if responses else clean), None, None


LORO_SUMMARY_RE = re.compile(r"^Loro \w+ mode completed\.", re.MULTILINE)
LORO_RUN_TRAILER_RE = re.compile(r"\n+Run [0-9a-fA-F-]{36} \(export:[^\n]*\)\s*$")


def normalize_loro(text: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Return the model's reply from `loro run`'s plain-text summary.

    `loro run` prints a run summary (provider, stop reason, steps, prompt, tools) that ends with
    "Model response: ...". Only the response is the bot's answer; the stop reason is kept so a
    provider error is reported as a failure instead of as the reply.
    """
    text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "stop_reason" in payload and "response" in payload:
        # `loro run --json` (Loro 0.22+).
        response = payload.get("response")
        return (
            response.strip() if isinstance(response, str) else "",
            payload,
            payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
        )
    if not LORO_SUMMARY_RE.search(text) or "\nModel response: " not in text:
        return text, None, None
    response = text.rsplit("\nModel response: ", 1)[1]
    response = LORO_RUN_TRAILER_RE.sub("", response).strip()
    raw: dict[str, Any] = {}
    if match := re.search(r"^Stop reason: (\S+)", text, re.MULTILINE):
        raw["stop_reason"] = match.group(1)
    if match := re.search(r"^Provider: (.+)$", text, re.MULTILINE):
        raw["provider"] = match.group(1).strip()
    if match := LORO_RUN_TRAILER_RE.search(text):
        raw["run_id"] = match.group(0).split()[1]
    return response, raw, None


def _parse_json_lines(text: str) -> dict[str, Any] | None:
    values: list[Any] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return {"events": values} if values else None


def _parse_trailing_object(text: str) -> dict[str, Any] | None:
    """Parse one pretty-printed JSON object that follows status lines (MagAgent prints a
    "Loaded N skills" line before its --json document)."""
    for match in re.finditer(r"^\{", text, re.MULTILINE):
        try:
            value, end = json.JSONDecoder().raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[end:].strip():
            return value
    return None


def find_error(value: Any) -> str | None:
    """An ``errorMessage``/``error_message`` anywhere in a structured payload."""
    return _find_error(value)


def _top_level_text(value: dict[str, Any]) -> str | None:
    for key in ("result", "response", "output", "answer"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _find_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("result", "response", "output", "answer", "content", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            found = _find_text(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_text(candidate)
            if found:
                return found
    return None


def _find_assistant_text(value: Any) -> str | None:
    candidates: list[str] = []
    # OpenCode `run --format json` streams parts: {"type": "text", "part": {"type": "text", ...}}.
    part_texts: dict[str, list[str]] = {}
    part_order: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            part = item.get("part")
            if (
                item.get("type") == "text"
                and isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                message_id = str(part.get("messageID") or "")
                if message_id not in part_texts:
                    part_texts[message_id] = []
                    part_order.append(message_id)
                part_texts[message_id].append(part["text"].strip())
                return
            if item.get("type") == "assistant_message" and isinstance(item.get("content"), str):
                # MagAgent --events records.
                if item["content"].strip():
                    candidates.append(item["content"].strip())
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    candidates.append(content.strip())
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text")
                            if isinstance(text, str) and text.strip():
                                candidates.append(text.strip())
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if part_order:
        return "\n\n".join(part_texts[part_order[-1]])
    return candidates[-1] if candidates else None


def _find_error(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("errorMessage", "error_message"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _find_error(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_error(child)
            if found:
                return found
    return None


def _find_string(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _last_nonempty_line(value: str) -> str | None:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None
