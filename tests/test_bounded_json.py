from dataclasses import FrozenInstanceError

import pytest


JSONL_RECORD_LIMIT = 64 * 1024 * 1024


def read_record(path):
    from quakepro.bounded_json import read_jsonl_record

    return read_jsonl_record(str(path))


def test_public_record_preserves_lossless_number_tokens(tmp_path):
    from quakepro.bounded_json import BoundedJsonRecord, JsonNumberToken

    path = tmp_path / "numbers.jsonl"
    raw = (
        b'{"integer":123456789012345678901234567890,'
        b'"fraction":-1.2300e+04,"values":[true,null,"7"]}'
    )
    path.write_bytes(raw + b"\n")

    result = read_record(path)

    assert isinstance(result, BoundedJsonRecord)
    assert result.raw == raw
    integer = result.value["integer"]
    fraction = result.value["fraction"]
    assert isinstance(integer, JsonNumberToken)
    assert isinstance(fraction, JsonNumberToken)
    assert integer.lexeme == "123456789012345678901234567890"
    assert fraction.lexeme == "-1.2300e+04"
    assert not isinstance(integer, (str, int, float))
    assert not isinstance(fraction, (str, int, float))
    assert result.value["values"] == [True, None, "7"]


def test_public_number_and_record_values_are_frozen():
    from quakepro.bounded_json import BoundedJsonRecord, JsonNumberToken

    token = JsonNumberToken("10e2")
    record = BoundedJsonRecord(b'{"value":10e2}', {"value": token})

    with pytest.raises(FrozenInstanceError):
        token.lexeme = "1000"
    with pytest.raises(FrozenInstanceError):
        record.raw = b"{}"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"ok":true}', id="missing-terminator"),
        pytest.param(b'{"text":"bad\xff"}\n', id="invalid-utf8"),
        pytest.param(b'{"value":NaN}\n', id="nan"),
        pytest.param(b'{"value":Infinity}\n', id="infinity"),
        pytest.param(b'{"value":-Infinity}\n', id="negative-infinity"),
        pytest.param(b'{"broken":}\n', id="malformed"),
    ],
)
def test_public_reader_rejects_incomplete_or_invalid_records(tmp_path, raw):
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(raw)

    assert read_record(path) is None


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(128, True, id="depth-128"),
        pytest.param(129, False, id="depth-129"),
    ],
)
def test_public_reader_uses_exact_container_depth_limit(tmp_path, depth, accepted):
    path = tmp_path / "nested.jsonl"
    nested = b"[" * (depth - 1) + b"0" + b"]" * (depth - 1)
    path.write_bytes(b'{"future":' + nested + b"}\n")

    assert (read_record(path) is not None) is accepted


def test_public_reader_accepts_one_record_and_ignores_later_bytes(tmp_path):
    path = tmp_path / "two.jsonl"
    first = b'{"value":1}'
    path.write_bytes(first + b"\n" + b"bad\xfflater")

    result = read_record(path)

    assert result is not None
    assert result.raw == first
    assert result.value["value"].lexeme == "1"


def test_public_reader_uses_exact_64_mib_record_limit(tmp_path):
    path = tmp_path / "boundary.jsonl"
    prefix = b'{"padding":"'
    suffix = b'"}'
    chunk = b"x" * (1024 * 1024)

    def write(raw_size):
        remaining = raw_size - len(prefix) - len(suffix)
        with path.open("wb") as stream:
            stream.write(prefix)
            while remaining:
                size = min(remaining, len(chunk))
                stream.write(chunk[:size])
                remaining -= size
            stream.write(suffix + b"\n")

    write(JSONL_RECORD_LIMIT)
    accepted = read_record(path)
    assert accepted is not None
    assert len(accepted.raw) == JSONL_RECORD_LIMIT

    write(JSONL_RECORD_LIMIT + 1)
    assert read_record(path) is None
