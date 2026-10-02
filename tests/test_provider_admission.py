import builtins
from dataclasses import FrozenInstanceError, fields
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT_ID = "019f0000-0000-7000-8000-000000000001"
CHILD_ID = "019f0000-0000-7000-8000-000000000002"
FOREIGN_ID = "019f0000-0000-7000-8000-000000000003"


def _admit(path, provider):
    from quakepro.provider_admission import read_admission

    return read_admission(str(path), provider)


def _read(path, provider):
    admitted = _admit(path, provider)
    return admitted.proof if admitted is not None else None


def _codex_bytes(
    thread_id=ROOT_ID,
    session_id=ROOT_ID,
    *,
    parent=None,
    extension=None,
):
    payload = {
        "id": thread_id,
        "session_id": session_id,
        "cwd": "/work/project",
        "thread_source": "subagent" if parent else "user",
        "source": "cli",
    }
    if parent:
        payload["parent_thread_id"] = parent
        payload["source"] = {"subagent": {"thread_spawn": {
            "parent_thread_id": parent,
        }}}
    encoded = json.dumps(
        {"type": "session_meta", "payload": payload},
        separators=(",", ":"),
    ).encode("utf-8")
    if extension is not None:
        encoded = encoded[:-1] + b',"future":' + extension + b"}"
    return encoded + b"\n"


def _pi_bytes(extension=None):
    encoded = (
        b'{"type":"session","version":3,"id":"pi-root",'
        b'"cwd":"/work/project"'
    )
    if extension is not None:
        encoded += b',"future":' + extension
    return encoded + b"}\n"


def test_import_preserves_interpreter_integer_digit_limit():
    script = """
import sys

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(4300)
    before = sys.get_int_max_str_digits()
else:
    before = None

import quakepro.provider_admission

after = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
if after != before:
    raise SystemExit(f"provider_admission changed int digit limit: {before} -> {after}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1] / "src",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("provider", "encoded"),
    [
        pytest.param("codex", _codex_bytes(), id="codex"),
        pytest.param("pi", _pi_bytes(), id="pi"),
    ],
)
def test_admission_proof_is_frozen_public_contract(tmp_path, provider, encoded):
    from quakepro.provider_admission import AdmissionProof

    path = tmp_path / "session.jsonl"
    path.write_bytes(encoded)

    proof = _read(path, provider)

    assert isinstance(proof, AdmissionProof)
    assert [field.name for field in fields(AdmissionProof)] == [
        "provider", "root_identity", "raw_digest",
    ]
    assert proof.provider == provider
    assert type(proof.root_identity) is tuple
    assert proof.root_identity
    assert all(type(value) is str for value in proof.root_identity)
    assert type(proof.raw_digest) is bytes
    assert proof.raw_digest
    with pytest.raises(FrozenInstanceError):
        proof.provider = "pi"


@pytest.mark.parametrize(
    ("provider", "encoded"),
    [
        pytest.param("codex", _codex_bytes(), id="codex"),
        pytest.param("pi", _pi_bytes(), id="pi"),
    ],
)
def test_admitted_record_returns_one_stable_public_result(
    tmp_path, provider, encoded,
):
    from quakepro.provider_admission import AdmittedRecord
    from quakepro.source_cohort import FileStamp

    target = tmp_path / "target.jsonl"
    target.write_bytes(encoded)
    candidate = tmp_path / "candidate.jsonl"
    candidate.symlink_to(target)
    info = target.stat()

    admitted = _admit(candidate, provider)

    assert isinstance(admitted, AdmittedRecord)
    assert admitted.path == os.path.realpath(candidate)
    assert isinstance(admitted.stamp, FileStamp)
    assert admitted.stamp == FileStamp(
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    assert admitted.record["type"] in {"session_meta", "session"}
    assert admitted.proof.provider == provider
    assert admitted.proof.raw_digest == hashlib.blake2b(
        encoded[:-1], digest_size=16,
    ).digest()
    with pytest.raises(FrozenInstanceError):
        admitted.path = str(target)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(b"3.0", id="float-kind"),
        pytest.param(b'"3"', id="string-kind"),
        pytest.param(b"true", id="boolean-kind"),
        pytest.param(b"9" * 129, id="integer-too-long"),
    ],
)
def test_known_number_field_rejects_unsupported_kind_or_length(
    tmp_path, version,
):
    path = tmp_path / "pi.jsonl"
    path.write_bytes(
        b'{"type":"session","version":' + version
        + b',"id":"pi","cwd":"/work"}\n'
    )

    assert _admit(path, "pi") is None


@pytest.mark.parametrize(
    ("spawn", "present"),
    [
        pytest.param(None, False, id="absent"),
        pytest.param(None, True, id="null"),
        pytest.param(
            {"parent_thread_id": ROOT_ID, "depth": 1},
            True,
            id="object",
        ),
    ],
)
def test_codex_thread_spawn_keeps_absent_object_and_null_shapes(
    tmp_path, spawn, present,
):
    payload = {
        "id": CHILD_ID,
        "session_id": ROOT_ID,
        "cwd": "/work/project",
        "thread_source": "subagent",
        "source": {"subagent": {}},
    }
    if present:
        payload["source"]["subagent"]["thread_spawn"] = spawn
    path = tmp_path / "codex.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )

    admitted = _admit(path, "codex")

    assert admitted is not None
    subagent = admitted.record["payload"]["source"]["subagent"]
    assert ("thread_spawn" in subagent) is present
    if present:
        assert subagent["thread_spawn"] == spawn


def test_read_admission_rejects_path_replaced_during_fenced_read(
    tmp_path, monkeypatch,
):
    import quakepro.bounded_json as bounded_json
    import quakepro.provider_admission as provider_admission

    path = tmp_path / "candidate.jsonl"
    path.write_bytes(_codex_bytes())
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(_codex_bytes(FOREIGN_ID, FOREIGN_ID))
    real_open = builtins.open
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def _replace(self, value):
            nonlocal replaced
            if value and not replaced:
                replaced = True
                os.replace(replacement, path)
            return value

        def read(self, *args):
            return self._replace(self.stream.read(*args))

        def readline(self, *args):
            return self._replace(self.stream.readline(*args))

        def readinto(self, buffer):
            count = self.stream.readinto(buffer)
            self._replace(count)
            return count

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def replacing_open(candidate, mode="r", *args, **kwargs):
        stream = real_open(candidate, mode, *args, **kwargs)
        if os.path.abspath(candidate) == os.path.abspath(path) and "r" in mode:
            return ReplacingStream(stream)
        return stream

    monkeypatch.setattr(provider_admission, "open", replacing_open, raising=False)
    monkeypatch.setattr(bounded_json, "open", replacing_open, raising=False)

    assert _admit(path, "codex") is None
    assert replaced is True


def test_codex_root_and_child_proofs_name_same_governing_root(tmp_path):
    root = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    foreign = tmp_path / "foreign.jsonl"
    root.write_bytes(_codex_bytes())
    child.write_bytes(_codex_bytes(CHILD_ID, ROOT_ID, parent=ROOT_ID))
    foreign.write_bytes(_codex_bytes(FOREIGN_ID, FOREIGN_ID))

    root_proof = _read(root, "codex")
    child_proof = _read(child, "codex")
    foreign_proof = _read(foreign, "codex")

    assert root_proof is not None
    assert child_proof is not None
    assert foreign_proof is not None
    assert child_proof.root_identity == root_proof.root_identity
    assert foreign_proof.root_identity != root_proof.root_identity


@pytest.mark.parametrize(
    ("provider", "record"),
    [
        pytest.param("codex", b"{not json}\n", id="malformed"),
        pytest.param("pi", _pi_bytes().rstrip(b"\n"), id="incomplete"),
        pytest.param(
            "codex",
            _codex_bytes(extension=b'"bad\xff"'),
            id="invalid-utf8",
        ),
        pytest.param(
            "pi",
            _pi_bytes(extension=b"NaN"),
            id="non-finite",
        ),
        pytest.param(
            "pi",
            _pi_bytes(extension=b"[" * 10_000 + b"0" + b"]" * 10_000),
            id="deep",
        ),
    ],
)
def test_expected_first_record_failures_are_tolerated(tmp_path, provider, record):
    path = tmp_path / "candidate.jsonl"
    path.write_bytes(record)

    assert _read(path, provider) is None


def test_vanished_candidate_is_tolerated(tmp_path):
    assert _read(tmp_path / "vanished.jsonl", "pi") is None


@pytest.mark.parametrize(
    ("provider", "record"),
    [
        pytest.param("codex", _pi_bytes(), id="pi-as-codex"),
        pytest.param("pi", _codex_bytes(), id="codex-as-pi"),
    ],
)
def test_foreign_provider_header_is_not_admitted(tmp_path, provider, record):
    path = tmp_path / "foreign.jsonl"
    path.write_bytes(record)

    assert _read(path, provider) is None


@pytest.mark.parametrize(
    ("provider", "record"),
    [
        pytest.param(
            "codex",
            {"type": "session_meta", "payload": {
                "id": [], "session_id": ROOT_ID, "cwd": "/work/project",
            }},
            id="codex-id",
        ),
        pytest.param(
            "codex",
            {"type": "session_meta", "payload": {
                "id": ROOT_ID, "session_id": [], "cwd": "/work/project",
            }},
            id="codex-session-id",
        ),
        pytest.param(
            "codex",
            {"type": "session_meta", "payload": {
                "id": CHILD_ID, "session_id": ROOT_ID,
                "parent_thread_id": 7, "cwd": "/work/project",
            }},
            id="codex-parent",
        ),
        pytest.param(
            "codex",
            {"type": "session_meta", "payload": {
                "id": CHILD_ID, "session_id": ROOT_ID,
                "forked_from_id": [], "cwd": "/work/project",
            }},
            id="codex-fork",
        ),
        pytest.param(
            "codex",
            {"type": "session_meta", "payload": {
                "id": ROOT_ID, "session_id": ROOT_ID,
                "thread_source": [], "cwd": "/work/project",
            }},
            id="codex-thread-source",
        ),
        pytest.param(
            "pi",
            {"type": "session", "version": True, "id": "pi", "cwd": "/work"},
            id="pi-version",
        ),
        pytest.param(
            "pi",
            {"type": "session", "version": 3, "id": [], "cwd": "/work"},
            id="pi-id",
        ),
        pytest.param(
            "pi",
            {"type": "session", "version": 3, "id": "pi", "cwd": []},
            id="pi-cwd",
        ),
        pytest.param(
            "pi",
            {"type": "session", "version": 3, "id": "pi", "cwd": "/work",
             "parentSession": 7},
            id="pi-parent",
        ),
    ],
)
def test_identity_fields_require_exact_shapes(tmp_path, provider, record):
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(json.dumps(record, separators=(",", ":")).encode() + b"\n")

    assert _read(path, provider) is None


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(65_536, True, id="maximum"),
        pytest.param(65_537, False, id="over-limit"),
    ],
)
def test_pi_identity_uses_decoded_length_limit(tmp_path, length, accepted):
    path = tmp_path / "pi.jsonl"
    record = {
        "type": "session",
        "version": 3,
        "id": "x" * length,
        "cwd": "/work/project",
    }
    path.write_bytes(json.dumps(record, separators=(",", ":")).encode() + b"\n")

    assert (_read(path, "pi") is not None) is accepted


@pytest.mark.parametrize(
    "first_record",
    [
        pytest.param(
            b'{"type":"session_meta","payload":{"id":7}}\n',
            id="invalid-codex-identity",
        ),
        pytest.param(_pi_bytes(), id="foreign-provider"),
        pytest.param(b'{"type":"event_msg","payload":{}}\n', id="non-session"),
    ],
)
def test_codex_first_record_denial_never_falls_through_to_later_metadata(
    tmp_path, first_record,
):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(first_record + _codex_bytes())

    assert _read(path, "codex") is None


def test_admission_accepts_unknown_700_digit_integer_at_low_runtime_limit():
    script = r'''
import pathlib
import json
import os
import sys
import tempfile

if not hasattr(sys, "set_int_max_str_digits"):
    raise SystemExit(0)

sys.set_int_max_str_digits(640)
before = sys.get_int_max_str_digits()
from quakepro.provider_admission import read_admission

with tempfile.TemporaryDirectory() as directory:
    base = pathlib.Path(directory)
    root = base / "work"
    root.mkdir()
    path = base / "pi" / "pi.jsonl"
    path.parent.mkdir()
    os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(path.parent)
    os.environ["QUAKEPRO_CACHE_DIR"] = str(base / "cache")
    path.write_bytes(
        b'{"type":"session","version":3,"id":"pi","cwd":'
        + json.dumps(str(root)).encode("utf-8")
        + b',"future":1' + b'0' * 699 + b'}\n'
        + b'{"type":"message","message":{"role":"assistant",'
        + b'"content":[{"type":"text","text":"visible"}]}}\n'
    )
    if read_admission(str(path), "pi") is None:
        raise SystemExit("700-digit unknown field was denied")
    from quakepro.pi_model import PiModel, newest_pi_session
    from quakepro.pi_snapshot import PiSnapshotCodec
    import quakepro.session_cache as session_cache

    runtime = PiModel(str(path))
    if runtime.poll() is not True:
        raise SystemExit("700-digit unknown field was denied by runtime")
    while runtime.poll():
        pass
    if newest_pi_session(str(root)) != str(path):
        raise SystemExit("700-digit unknown field was denied by discovery")
    first = session_cache.open_cached_model(
        str(path), str(root), PiSnapshotCodec(), lambda: PiModel(str(path)),
    )
    while first.poll():
        pass
    restored = session_cache.open_cached_model(
        str(path), str(root), PiSnapshotCodec(), lambda: PiModel(str(path)),
    )
    if restored.cache_restored is not True:
        raise SystemExit("700-digit unknown field was denied by cache")
if sys.get_int_max_str_digits() != before:
    raise SystemExit("admission changed interpreter digit limit")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1] / "src",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("digits", "admitted"),
    [
        pytest.param(128, True, id="signed-128-digits"),
        pytest.param(129, False, id="signed-129-digits"),
    ],
)
def test_pi_admission_bounds_known_signed_version_digits(tmp_path, digits, admitted):
    path = tmp_path / "pi.jsonl"
    version = b"-" + b"9" * digits
    path.write_bytes(
        b'{"type":"session","version":' + version
        + b',"id":"pi-version","cwd":"/work"}\n'
    )

    proof = _read(path, "pi")

    assert (proof is not None) is admitted
    if proof is not None:
        assert proof.root_identity[1] == version.decode("ascii")
