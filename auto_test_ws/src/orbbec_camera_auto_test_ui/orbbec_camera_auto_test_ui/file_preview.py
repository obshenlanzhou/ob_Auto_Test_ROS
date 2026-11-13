"""Bounded readers for UI previews; original test artifacts remain untouched."""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, TextIO


PREVIEW_BYTES = 256 * 1024
JSON_VALUE_BYTES = 64 * 1024
SMALL_RESULT_BYTES = 1024 * 1024
READ_CHUNK = 64 * 1024
_STRING_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_DIGITS = re.compile(r'[0-9]+')
_WHITESPACE = re.compile(r'[ \t\r\n]+')
_ROUND_COLLECTIONS = {"cycles", "attempts", "tests", "runs"}
_ROUND_FIELDS = {"status", "cycle", "attempt", "test_index", "run_index", "error", "message"}
_FAILURE_FIELDS = {"camera", "target", "topic", "operation", "reason", "message"}
_INVOCATION_FIELDS = {"command", "argv", "cwd"}
_RESULT_FIELDS = {
    "schema_version", "test_id", "run_id", "status", "started_at", "ended_at",
    "duration_seconds", "request", "environment", "summary", "warnings", "details",
    "artifacts", "error", "invocation",
}
_PRIORITY_FIELDS = _RESULT_FIELDS - {"request", "environment", "warnings", "artifacts"}
_RESULT_LOCK = threading.Lock()


def read_text_tail(path: Path, max_bytes: int = PREVIEW_BYTES) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - max_bytes))
        return stream.read(max_bytes).decode("utf-8", errors="replace")


def read_text_head(path: Path, max_bytes: int = PREVIEW_BYTES) -> str:
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text += "\n\n… 页面仅展示部分内容，完整报告请在结果目录中查看。\n"
    return text


def read_event_tail(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - PREVIEW_BYTES)
            stream.seek(max(0, start - 1))
            data = stream.read(PREVIEW_BYTES + 1)
        if start:
            # Discard a record cut by the byte limit, including UTF-8 fragments.
            previous, data = data[:1], data[1:]
            if previous != b"\n":
                data = data.partition(b"\n")[2]
        events = []
        for line in reversed(data.splitlines()):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
                if len(events) == limit:
                    break
        events.reverse()
        return events
    except OSError:
        return []


class _ResultPreview:
    """Validate the entire JSON stream, retaining bounded metadata and failures."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.buffer = ""
        self.offset = 0
        self.remaining = PREVIEW_BYTES
        self.truncated = False
        self.failure_remaining = JSON_VALUE_BYTES
        self.failure_details: list[dict[str, Any]] = []
        self.record_remaining = JSON_VALUE_BYTES
        self.round_counts: dict[str, dict[str, dict[str, int]]] = {}
        self.parts: list[str] | None = None
        self.capture_size = 0
        self.capture_limit = 0

    def peek(self) -> str:
        if self.offset == len(self.buffer):
            self.buffer = self.stream.read(READ_CHUNK)
            self.offset = 0
        return self.buffer[self.offset:self.offset + 1]

    def whitespace(self) -> str:
        while self.peek():
            match = _WHITESPACE.match(self.buffer, self.offset)
            if match is None:
                break
            self.advance(match.end())
        return self.peek()

    def advance(self, end: int) -> None:
        if self.parts is not None and self.capture_size <= self.capture_limit:
            part = self.buffer[self.offset:end]
            self.capture_size += len(part.encode("utf-8"))
            if self.capture_size <= self.capture_limit:
                self.parts.append(part)
            else:
                self.parts.clear()
        self.offset = end

    def expect(self, char: str) -> None:
        if self.whitespace() != char:
            raise ValueError(f"expected {char!r} in result.json")
        self.advance(self.offset + 1)

    def string(self) -> None:
        self.expect('"')
        while self.peek():
            match = _STRING_SPECIAL.search(self.buffer, self.offset)
            if match is None:
                self.advance(len(self.buffer))
                continue
            char = match.group()
            self.advance(match.end())
            if char == '"':
                return
            if char != "\\":
                raise ValueError("unescaped control character in result.json")
            escape = self.peek()
            if not escape or escape not in '"\\/bfnrtu':
                raise ValueError("invalid string escape in result.json")
            self.advance(self.offset + 1)
            if escape == "u":
                for _ in range(4):
                    digit = self.peek()
                    if not digit or digit not in "0123456789abcdefABCDEF":
                        raise ValueError("invalid Unicode escape in result.json")
                    self.advance(self.offset + 1)
        raise ValueError("incomplete result.json string")

    def literal(self, text: str) -> None:
        for expected in text:
            if self.peek() != expected:
                raise ValueError("invalid result.json literal")
            self.advance(self.offset + 1)

    def digits(self) -> None:
        if not self.peek() or self.peek() not in "0123456789":
            raise ValueError("invalid result.json number")
        while self.peek():
            match = _DIGITS.match(self.buffer, self.offset)
            if match is None:
                return
            self.advance(match.end())

    def number(self) -> None:
        if self.peek() == "-":
            self.advance(self.offset + 1)
            if self.peek() == "I":
                self.literal("Infinity")
                return
        if self.peek() == "0":
            self.advance(self.offset + 1)
        else:
            self.digits()
        if self.peek() == ".":
            self.advance(self.offset + 1)
            self.digits()
        if self.peek() and self.peek() in "eE":
            self.advance(self.offset + 1)
            if self.peek() and self.peek() in "+-":
                self.advance(self.offset + 1)
            self.digits()

    def scan_value(self, depth: int = 0) -> None:
        if depth > 256:
            raise ValueError("result.json is nested too deeply")
        first = self.whitespace()
        if first == '"':
            self.string()
        elif first in {"{", "["}:
            self.advance(self.offset + 1)
            closing = "}" if first == "{" else "]"
            if self.whitespace() == closing:
                self.advance(self.offset + 1)
                return
            while True:
                if first == "{":
                    if self.whitespace() != '"':
                        raise ValueError("expected result.json object key")
                    self.string()
                    self.expect(":")
                self.scan_value(depth + 1)
                if self.whitespace() == closing:
                    self.advance(self.offset + 1)
                    return
                self.expect(",")
        elif first in {"t", "f", "n", "N", "I"}:
            literals = {"t": "true", "f": "false", "n": "null", "N": "NaN", "I": "Infinity"}
            self.literal(literals[first])
        elif first and first in "-0123456789":
            self.number()
        else:
            raise ValueError("invalid or incomplete result.json value")

    def value(self, *, keep: bool = True, max_bytes: int | None = None) -> Any:
        first = self.whitespace()
        self.capture_limit = (
            min(JSON_VALUE_BYTES, self.remaining) if max_bytes is None else max_bytes
        )
        self.capture_size = 0
        self.parts = [] if keep else None
        self.scan_value()
        parts, self.parts = self.parts, None
        if keep and self.capture_size <= self.capture_limit:
            if max_bytes is None:
                self.remaining -= self.capture_size
            return json.loads("".join(parts))
        self.truncated = True
        return {"{": {}, "[": [], '"': "…"}.get(first)

    def children(self, depth: int) -> None:
        if depth > 128:
            raise ValueError("result.json is nested too deeply")
        if self.whitespace() == "{":
            self.object("record", depth + 1)
        elif self.whitespace() == "[":
            self.expect("[")
            if self.whitespace() == "]":
                self.expect("]")
                return
            while True:
                self.children(depth + 1)
                if self.whitespace() == "]":
                    self.expect("]")
                    return
                self.expect(",")
        else:
            self.value(keep=False)

    def failures(self, depth: int) -> list[dict[str, Any]]:
        self.expect("[")
        records = []
        if self.whitespace() == "]":
            self.expect("]")
            return records
        while True:
            if (
                self.failure_remaining > 0
                and len(self.failure_details) < 50
                and self.whitespace() == "{"
            ):
                record = self.object("failure", depth + 1)
                size = len(json.dumps(record, ensure_ascii=True).encode("utf-8"))
                if record and size <= self.failure_remaining:
                    self.failure_remaining -= size
                    self.failure_details.append(record)
                    records.append(record)
                else:
                    self.truncated = True
            else:
                self.value(keep=False)
            if self.whitespace() == "]":
                self.expect("]")
                return records
            self.expect(",")

    def rounds(self, scope: str, name: str, depth: int) -> list[dict[str, Any]]:
        self.expect("[")
        counts = {"passed": 0, "failed": 0, "interrupted": 0}
        self.round_counts.setdefault(scope, {})[name] = counts
        records = []
        self.truncated = True
        if self.whitespace() == "]":
            self.expect("]")
            return records
        while True:
            if self.whitespace() == "{":
                record = self.object("record", depth + 1)
                status = str(record.get("status") or "").lower()
                if status in counts:
                    counts[status] += 1
                size = len(json.dumps(record, ensure_ascii=True).encode("utf-8"))
                if status == "failed" and size <= self.record_remaining and len(records) < 10:
                    self.record_remaining -= size
                    records.append(record)
            else:
                self.value(keep=False)
            if self.whitespace() == "]":
                self.expect("]")
                return records
            self.expect(",")

    def object(self, scope: str = "root", depth: int = 0) -> dict[str, Any]:
        if depth > 128:
            raise ValueError("result.json is nested too deeply")
        self.expect("{")
        result = {}
        if self.whitespace() == "}":
            self.expect("}")
            return result
        while True:
            if self.whitespace() != '"':
                raise ValueError("expected result.json object key")
            key = self.value(max_bytes=4096)
            self.expect(":")
            retain = True
            if (
                scope == "root" and key in {"details", "invocation"}
                and self.whitespace() == "{"
            ):
                value = self.object(key, depth + 1)
            elif (
                scope in {"root", "details"} and key in _ROUND_COLLECTIONS
                and self.whitespace() == "["
            ):
                value = self.rounds(scope, key, depth + 1)
            elif (
                scope in {"root", "details", "record"} and key == "failure_details"
                and self.whitespace() == "["
            ):
                value = self.failures(depth + 1)
            elif (
                scope in {"root", "details", "record"}
                and key in _ROUND_COLLECTIONS | {"operations", "profile_switch"}
            ):
                self.children(depth + 1)
                retain = False
                value = None
            elif scope in {"record", "failure", "invocation"}:
                fields = {
                    "record": _ROUND_FIELDS,
                    "failure": _FAILURE_FIELDS,
                    "invocation": _INVOCATION_FIELDS,
                }[scope]
                value = self.value(keep=key in fields, max_bytes=4096)
                retain = key in fields
            elif scope == "root" and key in _PRIORITY_FIELDS:
                # Required scalar fields and summaries survive unrelated large metadata.
                value = self.value(max_bytes=JSON_VALUE_BYTES)
            else:
                value = self.value(keep=key != "artifacts")
            if retain and scope in {"root", "details"}:
                # Keys also consume the budget, even once all values are skipped.
                key_size = len(key.encode("utf-8", errors="surrogatepass"))
                if (
                    scope == "root" and key in _RESULT_FIELDS
                    or scope == "details" and key in _ROUND_COLLECTIONS | {"failure_details"}
                ):
                    result[key] = value
                elif key_size <= self.remaining:
                    self.remaining -= key_size
                    result[key] = value
                else:
                    self.truncated = True
            elif retain:
                result[key] = value
            char = self.whitespace()
            if char == "}":
                self.expect("}")
                return result
            self.expect(",")


@lru_cache(maxsize=8)
def _cached_result(path: Path, signature: tuple[int, int, int, int]) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        if signature[2] <= SMALL_RESULT_BYTES:
            return json.load(stream)
        reader = _ResultPreview(stream)
        if reader.whitespace() != "{":
            # Preserve the root type for contract validation without a full load.
            value = reader.value(keep=False)
            if reader.whitespace():
                raise ValueError("unexpected data after result.json")
            return value
        result = reader.object()
        if reader.whitespace():
            raise ValueError("unexpected data after result.json")
        details = result.get("details")
        scope = "details" if isinstance(details, dict) else "root"
        if reader.failure_details:
            target = details if isinstance(details, dict) else result
            target["failure_details"] = reader.failure_details
        result["_preview"] = {
            "truncated": reader.truncated,
            "size_bytes": signature[2],
            "round_counts": reader.round_counts.get(scope, {}),
        }
        return result


def read_result_preview(path: Path) -> Any:
    stat = path.stat()
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    # A single scan per version, including concurrent requests from several tabs.
    with _RESULT_LOCK:
        return _cached_result(path.resolve(), signature)


def result_preview_or_default(path: Path, default: Any) -> Any:
    try:
        return read_result_preview(path)
    except (OSError, ValueError, UnicodeError):
        return default
