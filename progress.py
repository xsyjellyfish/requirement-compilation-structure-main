"""Adapter-only progress hooks; upstream query/retry and execution stay unchanged."""

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime

IMAGE_DATA_URL = re.compile(r"data:image/[A-Za-z]+;base64,[A-Za-z0-9+/=]+")


def elide_images(text):
    """Replace image data URLs with placeholders keeping type, base64 length and a
    content hash — the exact payload stays reconstructible from the original files."""

    def repl(match):
        raw = match.group(0)
        header, _, b64 = raw.partition(";base64,")
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"{header};base64,[elided sha256={digest} len={len(b64)}]"

    return IMAGE_DATA_URL.sub(repl, text)


def redact(text):
    for name, value in os.environ.items():
        if value and any(
            word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            text = text.replace(value, "[REDACTED]")
    # An endpoint may carry credentials in its URL.
    value = os.environ.get("OPENAI_BASE_URL")
    if value:
        text = text.replace(value, "[API_ENDPOINT]")
    return text


def emit(event, **fields):
    record = {"time": datetime.now(UTC).isoformat(), "event": event, **fields}
    print(redact(json.dumps(record, ensure_ascii=False, default=str)), flush=True)


def _append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


TRACKED_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ARC_MODEL", "MODEL")


def variable_presence():
    """Presence (never values) of the platform-injected variables, for event logs."""
    return {name: bool(os.environ.get(name)) for name in TRACKED_VARS}


def exception_chain(exc):
    """Walk the __cause__/__context__ chain; every message is redacted."""
    chain, seen, cause = [], set(), exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        chain.append({"type": type(cause).__name__, "message": redact(str(cause))})
        cause = cause.__cause__ or cause.__context__
    return chain


def failure(event, exc, started, **fields):
    emit(
        event,
        seconds=round(time.monotonic() - started, 3),
        exception_chain=exception_chain(exc),
        **fields,
    )


class ModelProgress:
    """Per-HTTP-attempt records: stdout events, calls.jsonl, payloads.jsonl deltas.

    The adapter assigns ``calllog_dir`` (the run's .arc_adapter directory); when it
    is None nothing is written and records stay in memory on ``_call_records``.
    """

    calllog_dir = None

    # Reasoning models return chain-of-thought in these message fields. Replay is the
    # safe default: the DeepSeek thinking contract requires it on tool-call turns and
    # re-encoding gateways recover a thinking signature from the replayed text; plain
    # OpenAI-compatible endpoints ignore it. Strip only as an explicit opt-in.
    _RESEND_BLOCKLIST = ("reasoning_content", "provider_specific_fields")

    def _sanitize_for_transport(self, messages):
        """Transport policy for reasoning fields, via ARC_TRANSPORT_REASONING.

        passthrough (default): replay byte-exact, upstream behavior unchanged.
        strip: copy-on-write removal for endpoints where window/billing pressure
        dominates and reasoning replay is confirmed unnecessary.
        """
        if os.environ.get("ARC_TRANSPORT_REASONING", "passthrough") != "strip":
            return messages
        cleaned = []
        for message in messages:
            if any(key in message for key in self._RESEND_BLOCKLIST):
                message = {
                    k: v for k, v in message.items() if k not in self._RESEND_BLOCKLIST
                }
            cleaned.append(message)
        return cleaned

    def _record_call(self, record):
        records = getattr(self, "_call_records", None)
        if records is None:
            records = self._call_records = []
        records.append(record)
        if self.calllog_dir is None:
            return
        _append_jsonl(self.calllog_dir / "calls.jsonl", record)

    def _log_payload(self, number, messages):
        """Append one delta line to payloads.jsonl; history is append-only, so the
        exact payload for any attempt is the fold of all appends up to that line."""
        if self.calllog_dir is None:
            return None
        prev = getattr(self, "_sent_prefix_len", 0)
        if len(messages) >= prev:
            record = {"request": number, "prefix_len": prev, "append": messages[prev:]}
        else:  # append-only invariant violated: fall back to a full snapshot line
            record = {"request": number, "prefix_len": 0, "append": list(messages)}
        line = json.dumps(record, ensure_ascii=False, default=str)
        if os.environ.get("ARC_LOG_IMAGES") != "1":
            line = elide_images(line)
        with (self.calllog_dir / "payloads.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(line + "\n")
        self._sent_prefix_len = len(messages)
        return f"payloads.jsonl#{number}"

    def _query(self, messages, **kwargs):
        number = getattr(self, "_request_number", 0) + 1
        self._request_number = number
        messages = self._sanitize_for_transport(messages)
        started = time.monotonic()
        payload_bytes = len(
            json.dumps(messages, ensure_ascii=False, default=str).encode()
        )
        payload_ref = self._log_payload(number, messages)
        record = {
            "time": datetime.now(UTC).isoformat(),
            "request": number,
            "messages": len(messages),
            "payload_bytes": payload_bytes,
            "payload_snapshot": payload_ref,
        }
        # Single source of truth: the start event reuses the record's fields verbatim.
        emit(
            "model_request_start",
            **record,
            timeout_seconds=self.config.model_kwargs.get("timeout"),
        )
        try:
            response = super()._query(messages, **kwargs)
        except BaseException as exc:
            failure("model_request_error", exc, started, request=number)
            record.update(
                duration_seconds=round(time.monotonic() - started, 3),
                error=exception_chain(exc),
            )
            self._record_call(record)
            raise
        usage = getattr(response, "usage", None)
        usage = usage.model_dump(exclude_none=True) if usage is not None else None
        finish_reason = response.choices[0].finish_reason if response.choices else None
        emit(
            "model_request_end",
            request=number,
            seconds=round(time.monotonic() - started, 3),
            finish_reason=finish_reason,
            usage=usage,
        )
        record.update(
            duration_seconds=round(time.monotonic() - started, 3),
            finish_reason=finish_reason,
            usage=usage,
        )
        self._record_call(record)
        return response


class BashProgress:
    def execute(self, action, *args, **kwargs):
        self._bash_number = getattr(self, "_bash_number", 0) + 1
        number = self._bash_number
        started = time.monotonic()
        # Commands/output may contain secrets: report execution metadata only.
        emit(
            "bash_start", execution=number, command_chars=len(action.get("command", ""))
        )
        try:
            result = super().execute(action, *args, **kwargs)
        except BaseException as exc:
            from minisweagent.exceptions import Submitted

            if isinstance(exc, Submitted):
                emit(
                    "bash_submitted",
                    execution=number,
                    seconds=round(time.monotonic() - started, 3),
                )
            else:
                failure("bash_error", exc, started, execution=number)
            raise
        emit(
            "bash_end",
            execution=number,
            seconds=round(time.monotonic() - started, 3),
            returncode=result.get("returncode"),
        )
        if result.get("exception_info"):
            emit("bash_error", execution=number, reason=result["exception_info"])
        return result
