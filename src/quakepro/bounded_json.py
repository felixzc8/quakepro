"""Bounded, lossless JSON record reading and streaming validation."""
from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import BinaryIO, Optional


JSONL_RECORD_LIMIT = 64 * 1024 * 1024
JSON_NESTING_LIMIT = 128


@dataclass(frozen=True)
class JsonNumberToken:
    lexeme: str


@dataclass(frozen=True)
class BoundedJsonRecord:
    raw: bytes
    value: object


def reject_json_constant(value: str) -> object:
    raise ValueError("non-finite JSON number: " + value)


def decode_json_record(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=reject_json_constant,
        parse_int=JsonNumberToken,
        parse_float=JsonNumberToken,
    )


class StreamingJsonValidator:
    """Validate one JSON object with fixed nesting and constant parser state."""

    _WHITESPACE = " \t\r\n"
    _NUMBER_CHARS = frozenset("0123456789.eE+-")
    _NUMBER_END = frozenset({"zero", "integer", "fraction", "exponent_digits"})

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._stack = [["root", "value"]]
        self._mode = "normal"
        self._string_role = ""
        self._escaped = False
        self._unicode_digits = 0
        self._number_state = ""
        self._literal = ""
        self._invalid = False
        self._saw_token = False

    def _value_complete(self) -> None:
        kind, phase = self._stack[-1]
        if kind == "root" and phase == "value":
            self._stack[-1][1] = "done"
        elif kind == "object" and phase == "value":
            self._stack[-1][1] = "comma_or_end"
        elif kind == "array" and phase in {"value", "value_or_end"}:
            self._stack[-1][1] = "comma_or_end"
        else:
            self._invalid = True

    def _push(self, kind: str) -> None:
        if len(self._stack) > JSON_NESTING_LIMIT:
            self._invalid = True
            return
        phase = "key_or_end" if kind == "object" else "value_or_end"
        self._stack.append([kind, phase])

    def _start_value(self, character: str) -> None:
        self._value_complete()
        if character == "{":
            self._push("object")
        elif character == "[":
            self._push("array")
        elif character == '"':
            self._mode = "string"
            self._string_role = "value"
        elif character in "tfn":
            self._mode = "literal"
            self._literal = {"t": "rue", "f": "alse", "n": "ull"}[character]
        elif character == "-":
            self._mode = "number"
            self._number_state = "sign"
        elif character == "0":
            self._mode = "number"
            self._number_state = "zero"
        elif character in "123456789":
            self._mode = "number"
            self._number_state = "integer"
        else:
            self._invalid = True

    def _normal(self, character: str) -> None:
        if character in self._WHITESPACE:
            return
        self._saw_token = True
        kind, phase = self._stack[-1]
        if kind == "root":
            if phase == "value":
                self._start_value(character)
            else:
                self._invalid = True
            return
        if kind == "object":
            if phase in {"key", "key_or_end"}:
                if character == "}":
                    if phase == "key_or_end":
                        self._stack.pop()
                    else:
                        self._invalid = True
                elif character == '"':
                    self._mode = "string"
                    self._string_role = "key"
                else:
                    self._invalid = True
            elif phase == "colon":
                if character == ":":
                    self._stack[-1][1] = "value"
                else:
                    self._invalid = True
            elif phase == "value":
                self._start_value(character)
            elif phase == "comma_or_end":
                if character == ",":
                    self._stack[-1][1] = "key"
                elif character == "}":
                    self._stack.pop()
                else:
                    self._invalid = True
            return
        if phase in {"value", "value_or_end"}:
            if phase == "value_or_end" and character == "]":
                self._stack.pop()
            else:
                self._start_value(character)
        elif phase == "comma_or_end":
            if character == ",":
                self._stack[-1][1] = "value"
            elif character == "]":
                self._stack.pop()
            else:
                self._invalid = True

    def _string(self, character: str) -> None:
        if self._unicode_digits:
            if character not in "0123456789abcdefABCDEF":
                self._invalid = True
                return
            self._unicode_digits -= 1
            return
        if self._escaped:
            self._escaped = False
            if character == "u":
                self._unicode_digits = 4
            elif character not in '"\\/bfnrt':
                self._invalid = True
            return
        if character == "\\":
            self._escaped = True
        elif character == '"':
            self._mode = "normal"
            if self._string_role == "key":
                self._stack[-1][1] = "colon"
        elif ord(character) < 0x20:
            self._invalid = True

    def _number(self, character: str) -> None:
        state = self._number_state
        if state == "sign" and character in "0123456789":
            self._number_state = "zero" if character == "0" else "integer"
        elif state == "zero" and character == ".":
            self._number_state = "decimal"
        elif state in {"zero", "integer"} and character in "eE":
            self._number_state = "exponent"
        elif state == "integer" and character in "0123456789":
            pass
        elif state == "integer" and character == ".":
            self._number_state = "decimal"
        elif state == "decimal" and character in "0123456789":
            self._number_state = "fraction"
        elif state == "fraction" and character in "0123456789":
            pass
        elif state == "fraction" and character in "eE":
            self._number_state = "exponent"
        elif state == "exponent" and character in "+-":
            self._number_state = "exponent_sign"
        elif state in {"exponent", "exponent_sign"} and character in "0123456789":
            self._number_state = "exponent_digits"
        elif state == "exponent_digits" and character in "0123456789":
            pass
        elif character in self._NUMBER_CHARS or state not in self._NUMBER_END:
            self._invalid = True
        else:
            self._mode = "normal"
            self._normal(character)

    def _feed(self, value: str) -> None:
        for character in value:
            if self._invalid:
                return
            if self._mode == "string":
                self._string(character)
            elif self._mode == "number":
                self._number(character)
            elif self._mode == "literal":
                if not self._literal or character != self._literal[0]:
                    self._invalid = True
                else:
                    self._literal = self._literal[1:]
                    if not self._literal:
                        self._mode = "normal"
            else:
                self._normal(character)

    def update(self, raw: bytes) -> None:
        if self._invalid:
            return
        try:
            self._feed(self._decoder.decode(raw))
        except UnicodeDecodeError:
            self._invalid = True

    def finish(self) -> bool:
        try:
            self._feed(self._decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            self._invalid = True
        if self._mode == "number":
            self._invalid = self._invalid or self._number_state not in self._NUMBER_END
            self._mode = "normal"
        return (
            not self._invalid
            and self._mode == "normal"
            and len(self._stack) == 1
            and (self._stack[0] == ["root", "done"] or not self._saw_token)
        )


class StreamingJsonlRecord:
    """Validate and emit one normalized, bounded JSONL record."""

    def __init__(self) -> None:
        self._validator = StreamingJsonValidator()
        self._size = 0
        self._pending_cr = False
        self._complete = False
        self._valid = False
        self._saw_value = False

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def valid(self) -> bool:
        return self._valid

    def update(self, chunk: bytes) -> bytes:
        if self._complete:
            return b""
        end = chunk.find(b"\n")
        fragment = chunk if end < 0 else chunk[:end]
        if self._pending_cr:
            if end != 0:
                fragment = b"\r" + fragment
            self._pending_cr = False
        if fragment.endswith(b"\r"):
            fragment = fragment[:-1]
            self._pending_cr = end < 0
        self._size += len(fragment)
        if self._size <= JSONL_RECORD_LIMIT:
            self._validator.update(fragment)
            self._saw_value = self._saw_value or bool(fragment.strip())
        if end >= 0:
            self._complete = True
            self._valid = (
                self._size <= JSONL_RECORD_LIMIT
                and self._saw_value
                and self._validator.finish()
            )
        return fragment


def validate_json_record(raw: bytes) -> bool:
    if len(raw) > JSONL_RECORD_LIMIT:
        return False
    validator = StreamingJsonValidator()
    validator.update(raw)
    return validator.finish()


def parse_jsonl_line(raw_line: bytes) -> Optional[BoundedJsonRecord]:
    if not raw_line.endswith(b"\n") or raw_line.find(b"\n") != len(raw_line) - 1:
        return None
    record = StreamingJsonlRecord()
    raw = record.update(raw_line)
    if not record.valid:
        return None
    try:
        return BoundedJsonRecord(raw, decode_json_record(raw))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None


def read_jsonl_stream(stream: BinaryIO) -> Optional[BoundedJsonRecord]:
    return parse_jsonl_line(stream.readline(JSONL_RECORD_LIMIT + 2))


def read_jsonl_record(path: str) -> Optional[BoundedJsonRecord]:
    try:
        with open(path, "rb") as stream:
            return read_jsonl_stream(stream)
    except (OSError, TypeError, ValueError):
        return None
