import ast
import builtins
import copy
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest

import quakepro.claude_snapshot as claude_snapshot
import quakepro.codex_snapshot as codex_snapshot
import pi_fixture
import quakepro.pi_model as pi_model
import quakepro.provider_reload as provider_reload
import quakepro.session_cache as session_cache
import quakepro.sessions as sessions
from quakepro.overview import RunOverview
from quakepro.session_identity import claude_project_dir
from quakepro.session_model import Node


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def append_jsonl(path, records):
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def rewrite_target_after_first_binary_read(
        monkeypatch, module, path, replacement):
    original_open = builtins.open
    target = os.path.realpath(os.path.abspath(path))
    old_info = path.stat()
    rewritten = []

    class RewriteAfterRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def _rewrite(self, data):
            if not rewritten:
                path.write_bytes(replacement)
                os.utime(
                    path,
                    ns=(old_info.st_atime_ns, old_info.st_mtime_ns + 1_000_000),
                )
                rewritten.append(True)
            return data

        def read(self, *args, **kwargs):
            return self._rewrite(self.stream.read(*args, **kwargs))

        def readline(self, *args, **kwargs):
            return self._rewrite(self.stream.readline(*args, **kwargs))

        def seek(self, *args, **kwargs):
            return self.stream.seek(*args, **kwargs)

        def fileno(self):
            return self.stream.fileno()

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def intercept_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        candidate_path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        if candidate_path == target and "b" in mode:
            return RewriteAfterRead(stream)
        return stream

    monkeypatch.setattr(module, "open", intercept_open, raising=False)
    return rewritten


def assistant(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def pi_records(cwd):
    return [
        {"type": "session", "version": 3, "id": "pi-root", "cwd": str(cwd)},
        {
            "type": "message",
            "timestamp": "2026-08-02T10:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "pi old"}]},
        },
    ]


def codex_records(cwd):
    return [
        {
            "type": "session_meta",
            "payload": {
                "id": "codex-root",
                "session_id": "codex-root",
                "cwd": str(cwd),
                "thread_source": "user",
                "source": "cli",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-08-02T10:00:00Z",
            "payload": {"type": "agent_message", "message": "codex old"},
        },
    ]


def settle(model):
    while model.poll():
        pass


def cache_path(cache_dir, root, version=3):
    real_root = os.path.realpath(os.path.abspath(root))
    root_id = hashlib.sha256(real_root.encode("utf-8")).hexdigest()
    directory = "session-state-v3" if version == 3 else f"sessions-v{version}"
    return cache_dir / directory / f"{root_id}.json"


def mutate_cached_snapshot(cache_file, mutate):
    document = json.loads(cache_file.read_text())
    entry = next(iter(document["entries"].values()))
    mutate(entry["payload"])
    cache_file.write_text(json.dumps(document))


def encoded_state_field(payload, name):
    return next(
        value for key, value in payload["state"]["$dict"] if key == name
    )


def encoded_object(payload, reference):
    return payload["objects"][reference["$ref"]]


def encoded_node(payload, node_id="main"):
    nodes = encoded_state_field(payload, "nodes")
    reference = next(value for key, value in nodes["$dict"] if key == node_id)
    return encoded_object(payload, reference)


def encoded_step_references(payload, node_id="main"):
    return encoded_node(payload, node_id)["fields"]["steps"]["$list"]


def encoded_index(payload, name):
    return encoded_state_field(payload, name)["$dict"]


def node_projection(model):
    return {
        node_id: (
            node.label,
            node.detail,
            node.parent,
            node.status,
            node.result,
            tuple(node.children),
            tuple(
                (step.kind, step.title, step.body, step.status, step.child)
                for step in node.steps
            ),
        )
        for node_id, node in model.nodes.items()
    }


class PrivateState:
    def __init__(self, label):
        self.label = label


class FakeModel:
    provider = "claude"

    def __init__(self, path, label="source", changes=(), node=None):
        self.path = os.path.realpath(os.path.abspath(path))
        self.nodes = {"main": node or Node("main", label)}
        self.task_list = {}
        self.private = PrivateState(label)
        self.alias_node = self.nodes["main"]
        self._changes = list(changes)

    def poll(self):
        return self._changes.pop(0) if self._changes else False

    def observation_paths(self):
        return {self.path}

    def accepts_observation(self, paths):
        return self.path in paths

    def lifecycle_identity(self):
        return None


class RoundTripCodec:
    provider = "claude"
    schema_version = 1

    def __init__(
        self,
        source,
        *,
        resume=None,
        directory=None,
        reject_restore=False,
        capture_failures=0,
        capture_barrier=None,
    ):
        self.source = os.path.abspath(source)
        self.resume = resume
        self.directory = os.path.abspath(directory) if directory else None
        self.reject_restore = reject_restore
        self.capture_failures = capture_failures
        self.capture_barrier = capture_barrier
        self.capture_calls = 0
        self.restore_calls = 0

    def capture(self, model):
        self.capture_calls += 1
        if self.capture_barrier is not None:
            self.capture_barrier.wait(timeout=10)
        if self.capture_failures:
            self.capture_failures -= 1
            raise ValueError("capture rejected")
        info = os.stat(self.source)
        resume = session_cache.ResumePoint(
            self.resume.offset if self.resume is not None else info.st_size,
        )
        claims = [session_cache.SourceClaim(
            path=self.source,
            kind="file",
            resume=resume,
            device=info.st_dev,
            inode=info.st_ino,
            mode=stat.S_IMODE(info.st_mode),
        )]
        if self.directory is not None:
            claims.append(session_cache.SourceClaim(
                path=self.directory,
                kind="directory",
                recursive=True,
            ))
        return session_cache.Snapshot(
            schema_version=1,
            sources=tuple(claims),
            payload={
                "requested": model.path,
                "private": {"label": model.private.label},
                "node": model.nodes["main"],
                "node_alias": model.alias_node,
            },
        )

    def restore(self, requested_path, snapshot):
        self.restore_calls += 1
        if self.reject_restore:
            raise ValueError("codec rejected snapshot")
        if snapshot.schema_version != 1:
            raise ValueError("wrong fake schema")
        payload = snapshot.payload
        if not isinstance(payload, dict) or set(payload) != {
            "requested", "private", "node", "node_alias",
        }:
            raise ValueError("wrong fake payload fields")
        private = payload["private"]
        if (
            not isinstance(private, dict)
            or set(private) != {"label"}
            or type(private["label"]) is not str
            or payload["requested"] != os.path.realpath(os.path.abspath(requested_path))
            or type(payload["node"]) is not Node
            or payload["node"] is not payload["node_alias"]
        ):
            raise ValueError("wrong fake payload")
        model = FakeModel(requested_path, private["label"], node=payload["node"])
        model.provider = self.provider
        model.alias_node = payload["node_alias"]
        return model


def open_fake(path, root, codec, build):
    return session_cache.open_cached_model(str(path), str(root), codec, build)


def test_default_checkpoint_root_uses_os_temp(monkeypatch):
    monkeypatch.delenv("QUAKEPRO_CACHE_DIR", raising=False)

    assert session_cache._cache_dir() == Path(tempfile.gettempdir()) / "quakepro"


def test_generic_cache_has_only_provider_neutral_knowledge():
    tree = ast.parse(Path(session_cache.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    strings = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not imported.intersection({
        "model", "codex_model", "pi_model",
        "claude_snapshot", "codex_snapshot", "pi_snapshot",
        "provider_admission",
    })
    assert "bounded_json" not in imported
    assert not strings.intersection({
        "ClaudeModel", "CodexModel", "PiModel", "codex_model._FileState",
        "_offset", "_wf_offsets", "_states", "_header",
    })
    assert "_STATE_TYPES" not in names
    assert "__dict__" not in attributes
    assert "blake2b" not in attributes
    assert "blake2b" not in imported_names
    assert "blake2b" not in strings
    assert not strings.intersection({"claude", "codex", "pi"})


def test_generic_cache_restore_does_not_inspect_transcript_tail(
    tmp_path, monkeypatch,
):
    provider = "test-provider"
    cache_dir = tmp_path / "cache"
    source = tmp_path / "other" / "events.jsonl"
    source.parent.mkdir(parents=True)
    raw = b'{"event":"unfinished"'
    source.write_bytes(raw)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    resume = session_cache.ResumePoint(len(raw))
    codec = RoundTripCodec(source, resume=resume)
    codec.provider = provider

    def model(label):
        value = FakeModel(source, label)
        value.provider = provider
        return value

    root = tmp_path / "root"
    root.mkdir()
    settle(open_fake(source, root, codec, lambda: model("source")))

    reopened = open_fake(
        source, root, codec, lambda: model("fallback"),
    )

    assert reopened.cache_restored is True


@pytest.mark.parametrize(
    "filename",
    ["claude_snapshot.py", "codex_snapshot.py", "pi_snapshot.py"],
)
def test_provider_snapshot_codec_imports_only_public_cache_contract(filename):
    tree = ast.parse(Path(session_cache.__file__).with_name(filename).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "session_cache"
        for alias in node.names
    }

    assert imported <= {"ResumePoint", "Snapshot", "SourceClaim"}


@pytest.mark.parametrize(
    ("provider", "old_version"),
    [
        pytest.param("claude", 3, id="claude"),
        pytest.param("codex", 5, id="codex"),
        pytest.param("pi", 6, id="pi"),
    ],
)
def test_prior_provider_codec_cache_entry_rebuilds_from_source(
        tmp_path, monkeypatch, provider, old_version):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "claude":
        source = tmp_path / "claude" / "session.jsonl"
        records = [assistant("source projection")]
    elif provider == "codex":
        source = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
            / "rollout-root.jsonl"
        )
        records = codex_records(project)
    else:
        source = tmp_path / "pi" / "session.jsonl"
        records = pi_records(project)
    write_jsonl(source, records)
    first = sessions.open_model(
        str(source), provider, project_root=str(project),
    )
    settle(first)
    expected = node_projection(first)
    saved = cache_path(cache_dir, project)
    document = json.loads(saved.read_text())
    entry = next(
        value for value in document["entries"].values()
        if value["provider"] == provider
    )
    entry["codec_schema"] = old_version
    state = entry["payload"]["state"]["$dict"]
    entry["payload"]["state"]["$dict"] = [
        pair for pair in state
        if pair[0] not in {"_reload_directories", "_reload_denials"}
    ]
    saved.write_text(json.dumps(document))

    reopened = sessions.open_model(
        str(source), provider, project_root=str(project),
    )

    assert reopened.cache_restored is False
    settle(reopened)
    assert node_projection(reopened) == expected
    rebuilt = json.loads(saved.read_text())
    rebuilt_entry = next(
        value for value in rebuilt["entries"].values()
        if value["provider"] == provider
    )
    assert rebuilt_entry["codec_schema"] == old_version + 1


def test_v3_document_uses_cursor_only_source_manifest(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    directory = tmp_path / "watch"
    root.mkdir()
    directory.mkdir()
    source.write_bytes(b'{"n":1}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    resume = session_cache.ResumePoint(offset=8)
    codec = RoundTripCodec(source, resume=resume, directory=directory)
    model = FakeModel(source, "saved")

    cached = open_fake(source, root, codec, lambda: model)
    assert cached.cache_restored is False
    assert cached.poll() is False

    path = cache_path(cache_dir, root)
    document = json.loads(path.read_text())
    requested = os.path.realpath(source)
    entry_id = hashlib.sha256(("claude\0" + requested).encode("utf-8")).hexdigest()
    assert set(document) == {"format", "version", "root", "entries"}
    assert document["format"] == "quakepro-session-cache"
    assert document["version"] == 3
    assert document["root"] == os.path.realpath(root)
    assert set(document["entries"]) == {entry_id}

    entry = document["entries"][entry_id]
    assert set(entry) == {
        "provider", "requested", "codec_schema", "sources", "payload",
    }
    assert entry["provider"] == "claude"
    assert entry["requested"] == requested
    assert entry["codec_schema"] == 1
    assert set(entry["sources"]) == {"files", "directories"}
    assert entry["sources"]["directories"] == {
        os.path.abspath(directory): {
            "kind": "directory",
            "recursive": True,
        },
    }
    assert set(entry["sources"]["files"]) == {os.path.abspath(source)}

    saved_file = entry["sources"]["files"][os.path.abspath(source)]
    assert set(saved_file) == {"kind", "device", "inode", "mode", "offset"}
    assert saved_file["kind"] == "file"
    assert saved_file["offset"] == 8
    assert set(entry["payload"]) == {"state", "objects"}
    assert "PrivateState" not in path.read_text()


def test_round_trip_codec_restores_opaque_private_state_and_shared_graph(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"saved"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source)
    first = open_fake(source, root, codec, lambda: FakeModel(source, "private value"))
    settle(first)

    restored = open_fake(
        source,
        root,
        codec,
        lambda: (_ for _ in ()).throw(AssertionError("source build used")),
    )

    assert restored.cache_restored is True
    assert restored.private.label == "private value"
    assert restored.nodes == first.nodes
    assert restored.raw_model.alias_node is restored.nodes["main"]
    assert restored.poll() is False


def test_rejecting_codec_falls_back_without_hiding_source_error(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"saved"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    settle(open_fake(
        source, root, RoundTripCodec(source), lambda: FakeModel(source, "saved")
    ))
    rejecting = RoundTripCodec(source, reject_restore=True)

    fallback = open_fake(
        source, root, rejecting, lambda: FakeModel(source, "rebuilt")
    )
    assert fallback.cache_restored is False
    assert fallback.private.label == "rebuilt"
    assert rejecting.restore_calls == 1

    with pytest.raises(RuntimeError, match="source failed"):
        open_fake(
            source,
            root,
            rejecting,
            lambda: (_ for _ in ()).throw(RuntimeError("source failed")),
        )


def test_jsonl_append_with_resume_claim_can_restore(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "with-resume.jsonl"
    root.mkdir()
    source.write_bytes(b'{"event":"old"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))

    offset = source.stat().st_size
    resume = session_cache.ResumePoint(offset)
    resume_codec = RoundTripCodec(source, resume=resume)
    settle(open_fake(
        source,
        root,
        resume_codec,
        lambda: FakeModel(source, "resumable"),
    ))
    with source.open("ab") as stream:
        stream.write(b'{"event":"new"}\n')

    resumed = open_fake(
        source,
        root,
        RoundTripCodec(source, resume=resume),
        lambda: (_ for _ in ()).throw(AssertionError("resumable entry rebuilt")),
    )

    assert resumed.cache_restored is True
    assert resumed.private.label == "resumable"


def test_resume_offset_past_source_end_skips_save(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_bytes(b'{"n":1}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(
        source,
        resume=session_cache.ResumePoint(source.stat().st_size + 1),
    )

    cached = open_fake(source, root, codec, lambda: FakeModel(source, "usable"))

    assert cached.poll() is False
    assert cached.private.label == "usable"
    assert not cache_path(cache_dir, root).exists()


def test_cached_missing_directory_identity_is_rejected(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    missing = tmp_path / "missing"
    root.mkdir()
    missing.mkdir()
    source.write_bytes(b'{"n":1}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source, directory=missing)
    path = cache_path(cache_dir, root)

    settle(open_fake(source, root, codec, lambda: FakeModel(source, "saved")))
    missing.rmdir()
    document = json.loads(path.read_text())
    entry = next(iter(document["entries"].values()))
    entry["sources"]["directories"][os.path.abspath(missing)]["identity"] = {
        "kind": "missing",
        "entries": [],
    }
    path.write_text(json.dumps(document))

    restored = open_fake(
        source,
        root,
        codec,
        lambda: FakeModel(source, "rebuilt"),
    )
    assert restored.cache_restored is False
    assert restored.private.label == "rebuilt"


def test_corrupt_requested_entry_keeps_valid_sibling_now_and_after_save(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source_a = tmp_path / "a.jsonl"
    source_b = tmp_path / "b.jsonl"
    root.mkdir()
    source_a.write_text('{"entry":"a"}\n')
    source_b.write_text('{"entry":"b"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec_a = RoundTripCodec(source_a)
    codec_b = RoundTripCodec(source_b)
    settle(open_fake(source_a, root, codec_a, lambda: FakeModel(source_a, "a")))
    settle(open_fake(source_b, root, codec_b, lambda: FakeModel(source_b, "b")))
    path = cache_path(cache_dir, root)
    document = json.loads(path.read_text())
    bad_id = hashlib.sha256(
        ("claude\0" + os.path.realpath(source_a)).encode("utf-8")
    ).hexdigest()
    good_id = hashlib.sha256(
        ("claude\0" + os.path.realpath(source_b)).encode("utf-8")
    ).hexdigest()
    document["entries"][bad_id]["payload"] = {
        "state": "corrupt",
        "objects": [],
    }
    path.write_text(json.dumps(document))

    rebuilt = open_fake(
        source_a, root, codec_a, lambda: FakeModel(source_a, "rebuilt a")
    )
    sibling = open_fake(
        source_b,
        root,
        codec_b,
        lambda: (_ for _ in ()).throw(AssertionError("sibling rebuilt")),
    )
    assert rebuilt.cache_restored is False
    assert sibling.cache_restored is True
    assert sibling.private.label == "b"

    settle(rebuilt)
    assert good_id in json.loads(path.read_text())["entries"]
    sibling_again = open_fake(
        source_b,
        root,
        codec_b,
        lambda: (_ for _ in ()).throw(AssertionError("sibling lost")),
    )
    assert sibling_again.cache_restored is True


def test_malformed_sibling_shell_does_not_block_valid_entry_update(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    bad_source = tmp_path / "bad.jsonl"
    good_source = tmp_path / "good.jsonl"
    root.mkdir()
    bad_source.write_text('{"entry":"bad"}\n')
    good_source.write_text('{"entry":"good"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    bad_codec = RoundTripCodec(bad_source)
    good_codec = RoundTripCodec(good_source)
    settle(open_fake(
        bad_source, root, bad_codec, lambda: FakeModel(bad_source, "bad")
    ))
    settle(open_fake(
        good_source, root, good_codec, lambda: FakeModel(good_source, "old")
    ))
    path = cache_path(cache_dir, root)
    document = json.loads(path.read_text())
    bad_id = hashlib.sha256(
        ("claude\0" + os.path.realpath(bad_source)).encode("utf-8")
    ).hexdigest()
    document["entries"][bad_id]["requested"] = "/bad\0sibling"
    path.write_text(json.dumps(document))

    updated = open_fake(
        good_source,
        root,
        good_codec,
        lambda: (_ for _ in ()).throw(AssertionError("valid entry rebuilt")),
    )
    assert updated.cache_restored is True
    updated.private.label = "newest"
    updated.raw_model._changes.extend([True, False])
    assert updated.poll() is True
    updated.raw_model._provider_reload_revision = 1
    assert updated.poll() is False

    saved = json.loads(path.read_text())
    assert bad_id not in saved["entries"]
    newest = open_fake(
        good_source,
        root,
        good_codec,
        lambda: (_ for _ in ()).throw(AssertionError("valid update lost")),
    )
    assert newest.cache_restored is True
    assert newest.private.label == "newest"


@pytest.mark.parametrize("mutation", [
    lambda entry: entry.__setitem__("provider", "pi"),
    lambda entry: entry.__setitem__("codec_schema", 99),
    lambda entry: entry.__setitem__("unexpected", True),
])
def test_wrong_provider_schema_or_entry_shape_falls_back(
        tmp_path, monkeypatch, mutation):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source)
    settle(open_fake(source, root, codec, lambda: FakeModel(source, "saved")))
    path = cache_path(cache_dir, root)
    document = json.loads(path.read_text())
    mutation(next(iter(document["entries"].values())))
    path.write_text(json.dumps(document))
    restore_codec = RoundTripCodec(source)

    fallback = open_fake(
        source, root, restore_codec, lambda: FakeModel(source, "rebuilt")
    )

    assert fallback.cache_restored is False
    assert fallback.private.label == "rebuilt"
    assert restore_codec.restore_calls == 0


@pytest.mark.parametrize("non_finite", [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
])
def test_cache_reader_rejects_non_finite_float_in_shared_graph(
        tmp_path, monkeypatch, non_finite):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source)
    settle(open_fake(source, root, codec, lambda: FakeModel(source, "saved")))
    path = cache_path(cache_dir, root)

    def corrupt_graph(payload):
        node = next(
            item for item in payload["objects"]
            if item["type"] == "session_model.Node"
        )
        node["fields"]["last_ts"] = non_finite

    mutate_cached_snapshot(path, corrupt_graph)
    assert any(token in path.read_text() for token in ("NaN", "Infinity"))

    fallback = open_fake(
        source,
        root,
        RoundTripCodec(source),
        lambda: FakeModel(source, "finite rebuild"),
    )

    assert fallback.cache_restored is False
    assert fallback.private.label == "finite rebuild"
    assert math.isfinite(fallback.nodes["main"].last_ts)


def test_concurrent_writers_read_merge_and_replace_under_root_lock(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    count = 6
    barrier = threading.Barrier(count)
    wrappers = []
    codecs = []

    class BarrierModel(FakeModel):
        def poll(self):
            barrier.wait(timeout=10)
            return False

    for index in range(count):
        source = tmp_path / f"source-{index}.jsonl"
        source.write_text(json.dumps({"index": index}) + "\n")
        codec = RoundTripCodec(source)
        codecs.append(codec)
        wrappers.append(open_fake(
            source, root, codec, lambda source=source, index=index: BarrierModel(
                source, f"entry {index}"
            )
        ))
    errors = []

    def save(wrapper):
        try:
            wrapper.poll()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(wrapper,)) for wrapper in wrappers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    document = json.loads(cache_path(cache_dir, root).read_text())
    assert len(document["entries"]) == count
    for index, codec in enumerate(codecs):
        restored = open_fake(
            codec.source,
            root,
            codec,
            lambda: (_ for _ in ()).throw(AssertionError(f"entry {index} lost")),
        )
        assert restored.cache_restored is True
        assert restored.private.label == f"entry {index}"


def test_failed_save_is_attempted_once_per_process(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source, capture_failures=1)
    raw = FakeModel(source, "first", changes=[True, False, False])
    cached = open_fake(source, root, codec, lambda: raw)
    path = cache_path(cache_dir, root)

    assert cached.poll() is True
    assert not path.exists()
    assert cached.poll() is False
    assert not path.exists()
    assert cached.poll() is False
    assert not path.exists()
    assert codec.capture_calls == 1

    raw._provider_reload_revision = 1
    assert cached.poll() is False
    assert not path.exists()
    assert codec.capture_calls == 1


def test_oversized_save_is_attempted_once_per_process(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(session_cache, "_MAX_CACHE_BYTES", 1)
    codec = RoundTripCodec(source)
    raw = FakeModel(source)
    cached = open_fake(source, root, codec, lambda: raw)

    assert cached.poll() is False
    assert cached.poll() is False
    assert codec.capture_calls == 1

    raw._provider_reload_revision = 1
    assert cached.poll() is False
    assert codec.capture_calls == 1


def test_restored_model_saves_once_after_first_catchup(tmp_path, monkeypatch):
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    codec = RoundTripCodec(source)
    settle(open_fake(source, root, codec, lambda: FakeModel(source)))
    restored = open_fake(
        source,
        root,
        codec,
        lambda: (_ for _ in ()).throw(AssertionError("source build used")),
    )

    restored.raw_model._changes.extend([True, False])
    assert restored.poll() is True
    restored.raw_model._provider_reload_revision = 1
    assert restored.poll() is False
    assert codec.capture_calls == 2

    restored.raw_model._changes.extend([True, False])
    assert restored.poll() is True
    restored.raw_model._provider_reload_revision = 2
    assert restored.poll() is False
    assert codec.capture_calls == 2


def test_v3_cache_permissions_and_atomic_replace_failure(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    first_source = tmp_path / "first.jsonl"
    second_source = tmp_path / "second.jsonl"
    root.mkdir()
    first_source.write_text('{"entry":"first"}\n')
    second_source.write_text('{"entry":"second"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    settle(open_fake(
        first_source,
        root,
        RoundTripCodec(first_source),
        lambda: FakeModel(first_source, "first"),
    ))
    path = cache_path(cache_dir, root)
    lock = path.with_suffix(path.suffix + ".lock")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert lock.exists()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    before = path.read_bytes()
    original_replace = session_cache.os.replace

    def refuse_replace(source, destination):
        if os.path.abspath(destination) == os.path.abspath(path):
            raise OSError("interrupted before replace")
        return original_replace(source, destination)

    monkeypatch.setattr(session_cache.os, "replace", refuse_replace)
    settle(open_fake(
        second_source,
        root,
        RoundTripCodec(second_source),
        lambda: FakeModel(second_source, "second"),
    ))

    assert path.read_bytes() == before
    assert not list(path.parent.glob(path.name + ".*.tmp"))
    restored = open_fake(
        first_source,
        root,
        RoundTripCodec(first_source),
        lambda: (_ for _ in ()).throw(AssertionError("old document damaged")),
    )
    assert restored.cache_restored is True


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_cache_lock_falls_back_without_changing_sources(
        tmp_path, monkeypatch, link_kind):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    source.chmod(0o644)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    path = cache_path(cache_dir, root)
    path.parent.mkdir(parents=True, mode=0o700)
    lock = path.with_suffix(path.suffix + ".lock")
    if link_kind == "symlink":
        lock.symlink_to(source)
    elif link_kind == "hardlink":
        os.link(source, lock)
    else:
        os.mkfifo(lock, 0o644)
    original_bytes = source.read_bytes()
    original_info = source.stat()
    original_lock_mode = stat.S_IMODE(lock.lstat().st_mode)

    model = open_fake(
        source, root, RoundTripCodec(source), lambda: FakeModel(source, "source")
    )
    settle(model)

    assert model.cache_restored is False
    assert model.nodes["main"].label == "source"
    assert not path.exists()
    assert source.read_bytes() == original_bytes
    assert source.stat().st_mode == original_info.st_mode
    assert source.stat().st_mtime_ns == original_info.st_mtime_ns
    assert stat.S_IMODE(lock.lstat().st_mode) == original_lock_mode


def test_wrong_cache_file_mode_rebuilds_and_repairs_mode(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source)
    settle(open_fake(source, root, codec, lambda: FakeModel(source, "saved")))
    path = cache_path(cache_dir, root)
    path.chmod(0o644)
    restore_codec = RoundTripCodec(source)

    fallback = open_fake(
        source, root, restore_codec, lambda: FakeModel(source, "rebuilt")
    )
    assert fallback.cache_restored is False
    assert restore_codec.restore_calls == 0
    settle(fallback)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wrong_v3_parent_mode_rebuilds_then_settled_save_repairs_mode(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = tmp_path / "root"
    source = tmp_path / "source.jsonl"
    root.mkdir()
    source.write_text('{"event":"source"}\n')
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    codec = RoundTripCodec(source)
    settle(open_fake(source, root, codec, lambda: FakeModel(source, "saved")))
    path = cache_path(cache_dir, root)
    path.parent.chmod(0o755)

    fallback = open_fake(
        source,
        root,
        RoundTripCodec(source),
        lambda: FakeModel(source, "rebuilt"),
    )

    assert fallback.cache_restored is False
    assert fallback.private.label == "rebuilt"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    settle(fallback)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    restored = open_fake(
        source,
        root,
        RoundTripCodec(source),
        lambda: (_ for _ in ()).throw(AssertionError("repaired cache missed")),
    )
    assert restored.cache_restored is True
    assert restored.private.label == "rebuilt"


def test_session_cache_reuses_unchanged_transcript_prefix(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("old event")])

    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    assert first.cache_restored is False

    replayed = []
    original = first.raw_model.__class__._ingest

    def track_ingest(model, record):
        replayed.append(record)
        return original(model, record)

    monkeypatch.setattr(first.raw_model.__class__, "_ingest", track_ingest)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    assert restored.cache_restored is True
    assert restored.poll() is False
    assert replayed == []

    with path.open("a") as stream:
        stream.write(json.dumps(assistant("new event")) + "\n")
    assert restored.poll() is True
    assert [step.body for step in restored.nodes["main"].steps] == ["old event", "new event"]
    assert replayed == [assistant("new event")]


def test_restored_claude_append_does_not_reread_unchanged_child_prefix(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    child = tmp_path / "session" / "subagents" / "agent-child.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("old event")])
    write_jsonl(child, [{
        "uuid": "child-record",
        "timestamp": "2026-08-02T10:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "child event"}],
        },
    }])

    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    restored = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )
    assert restored.cache_restored is True
    assert restored.poll() is False

    target = os.path.realpath(child)

    def open_main_only(candidate, *args, **kwargs):
        if os.path.realpath(candidate) == target:
            raise AssertionError("unchanged child source was reread")
        return builtins.open(candidate, *args, **kwargs)

    monkeypatch.setattr(provider_reload, "open", open_main_only, raising=False)
    append_jsonl(path, [assistant("new event")])

    assert restored.poll() is True
    assert [step.body for step in restored.nodes["main"].steps] == [
        "old event", "new event",
    ]


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_cached_semantic_noop_preserves_mappings_and_saves_resume(
        tmp_path, monkeypatch, provider):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "claude":
        path = tmp_path / "claude" / "session.jsonl"
        old = [assistant("same")]
        old[0]["padding"] = "old padding"
        replacement = [assistant("same")]
        replacement[0]["padding"] = "new longer padding"
    elif provider == "codex":
        path = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
            / "rollout-root.jsonl"
        )
        old = codex_records(project)
        old[-1]["payload"]["padding"] = "old padding"
        replacement = copy.deepcopy(old)
        replacement[-1]["payload"]["padding"] = "new longer padding"
    else:
        path = tmp_path / "pi" / "session.jsonl"
        old = pi_records(project)
        old[0]["padding"] = "old padding"
        replacement = copy.deepcopy(old)
        replacement[0]["padding"] = "new longer padding"
    write_jsonl(path, old)
    first = sessions.open_model(str(path), provider, project_root=str(project))
    settle(first)
    restored = sessions.open_model(
        str(path), provider, project_root=str(project),
    )
    assert restored.cache_restored is True
    assert restored.poll() is False
    nodes = restored.nodes
    task_list = restored.task_list
    identity = restored.lifecycle_identity()

    write_jsonl(path, replacement)
    assert restored.poll() is False
    assert restored.nodes is nodes
    assert restored.task_list is task_list
    assert restored.lifecycle_identity() == identity
    assert restored.poll() is False

    reopened = sessions.open_model(
        str(path), provider, project_root=str(project),
    )
    assert reopened.cache_restored is True
    assert reopened.poll() is False


def test_missing_optional_claude_output_still_saves_and_restores_cache(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    output = tmp_path / "shell.output"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    output.write_text("initial output\n")
    acknowledgement = (
        "Command running in background with ID: shell-1. "
        f"Output is being written to: {output}"
    )
    write_jsonl(path, [
        {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Bash",
                "input": {"command": "serve", "run_in_background": True},
            }]},
        },
        {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": acknowledgement,
            }]},
        },
    ])
    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    output.unlink()
    append_jsonl(path, [assistant("main suffix")])

    assert first.poll() is True
    assert first.poll() is False
    reopened = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )

    assert reopened.cache_restored is True
    assert reopened.poll() is True
    assert reopened.poll() is False
    assert any(
        step.body == "main suffix"
        for step in reopened.nodes["main"].steps
    )


def test_claude_rewrite_after_settled_poll_cannot_save_old_projection(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    old_records = [assistant("old value")]
    new_records = [assistant("new value")]
    write_jsonl(path, old_records)
    old_bytes = path.read_bytes()
    old_info = path.stat()
    new_bytes = "".join(
        json.dumps(record) + "\n" for record in new_records
    ).encode()
    assert len(new_bytes) == len(old_bytes)

    session = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["old value"]
    original_poll = session.raw_model.poll
    rewritten = []

    def poll_then_rewrite():
        changed = original_poll()
        if not changed and not rewritten:
            path.write_bytes(new_bytes)
            os.utime(
                path,
                ns=(old_info.st_atime_ns, old_info.st_mtime_ns + 1_000_000),
            )
            rewritten.append(True)
        return changed

    monkeypatch.setattr(session.raw_model, "poll", poll_then_rewrite)
    assert session.poll() is False
    assert rewritten == [True]

    reopened = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )
    assert reopened.cache_restored is False
    settle(reopened)
    assert [step.body for step in reopened.nodes["main"].steps] == ["new value"]

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-claude-cache"))
    fresh = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )
    settle(fresh)
    assert node_projection(reopened) == node_projection(fresh)


def test_codex_main_rewrite_between_read_and_source_proof_rebuilds(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
        / "rollout-root.jsonl"
    )
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    old_records = codex_records(project)
    new_records = codex_records(project)
    new_records[-1]["payload"]["message"] = "codex new"
    write_jsonl(path, old_records)
    old_bytes = path.read_bytes()
    new_bytes = "".join(
        json.dumps(record) + "\n" for record in new_records
    ).encode()
    assert len(new_bytes) == len(old_bytes)

    rewritten = rewrite_target_after_first_binary_read(
        monkeypatch, provider_reload, path, new_bytes,
    )
    first = sessions.open_model(str(path), "codex")
    assert first.poll() is False
    assert rewritten == [True]
    assert path.read_bytes() == new_bytes
    assert first.nodes["main"].steps == []
    assert first.poll() is True
    assert [step.body for step in first.nodes["main"].steps][-1] == "codex new"
    assert first.poll() is False

    immediate = sessions.open_model(str(path), "codex")
    settle(immediate)
    assert [step.body for step in immediate.nodes["main"].steps][-1] == "codex new"

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-codex-cache"))
    fresh = sessions.open_model(str(path), "codex")
    settle(fresh)
    assert node_projection(immediate) == node_projection(fresh)


def test_pi_main_rewrite_between_read_and_source_proof_rebuilds(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "pi" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    old_records = pi_records(project)
    new_records = pi_records(project)
    new_records[-1]["message"]["content"][0]["text"] = "pi new"
    write_jsonl(path, old_records)
    old_bytes = path.read_bytes()
    new_bytes = "".join(
        json.dumps(record) + "\n" for record in new_records
    ).encode()
    assert len(new_bytes) == len(old_bytes)

    rewritten = rewrite_target_after_first_binary_read(
        monkeypatch, provider_reload, path, new_bytes,
    )
    first = sessions.open_model(str(path), "pi")
    assert first.poll() is False
    assert rewritten == [True]
    assert path.read_bytes() == new_bytes
    assert first.nodes["main"].steps == []
    assert first.poll() is True
    assert [step.body for step in first.nodes["main"].steps][-1] == "pi new"
    assert first.poll() is False

    immediate = sessions.open_model(str(path), "pi")
    settle(immediate)
    assert [step.body for step in immediate.nodes["main"].steps][-1] == "pi new"

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-pi-cache"))
    fresh = sessions.open_model(str(path), "pi")
    settle(fresh)
    assert node_projection(immediate) == node_projection(fresh)


def test_codex_constructor_rewrite_uses_current_identity_and_projection(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
        / "rollout-root.jsonl"
    )
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    old_records = codex_records(project)
    old_records[0]["payload"].update({
        "id": "codex-old",
        "session_id": "codex-old",
    })
    new_records = codex_records(project)
    new_records[0]["payload"].update({
        "id": "codex-new",
        "session_id": "codex-new",
    })
    new_records[-1]["payload"]["message"] = "codex new"
    write_jsonl(path, old_records)
    old_bytes = path.read_bytes()
    old_info = path.stat()
    new_bytes = "".join(
        json.dumps(record) + "\n" for record in new_records
    ).encode()
    assert len(new_bytes) == len(old_bytes)

    first = sessions.open_model(str(path), "codex")
    pending_identity = first.lifecycle_identity()
    assert pending_identity[0] == "codex"
    assert pending_identity[1].startswith("pending-")
    path.write_bytes(new_bytes)
    os.utime(
        path,
        ns=(old_info.st_atime_ns, old_info.st_mtime_ns + 1_000_000),
    )
    settle(first)
    assert first.lifecycle_identity() == ("codex", "codex-new")
    assert [step.body for step in first.nodes["main"].steps] == ["codex new"]

    reopened = sessions.open_model(str(path), "codex")
    settle(reopened)
    assert reopened.lifecycle_identity() == ("codex", "codex-new")
    assert [step.body for step in reopened.nodes["main"].steps] == ["codex new"]

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-codex-cache"))
    fresh = sessions.open_model(str(path), "codex")
    settle(fresh)
    assert reopened.lifecycle_identity() == fresh.lifecycle_identity()
    assert node_projection(reopened) == node_projection(fresh)


def test_pi_constructor_rewrite_uses_current_header_and_projection(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    old_project = tmp_path / "old-project"
    new_project = tmp_path / "new-project"
    path = tmp_path / "pi" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    old_project.mkdir()
    new_project.mkdir()
    old_records = pi_records(old_project)
    old_records[0]["id"] = "pi-old"
    new_records = pi_records(new_project)
    new_records[0]["id"] = "pi-new"
    new_records[-1]["message"]["content"][0]["text"] = "pi new"
    write_jsonl(path, old_records)
    old_bytes = path.read_bytes()
    old_info = path.stat()
    new_bytes = "".join(
        json.dumps(record) + "\n" for record in new_records
    ).encode()
    assert len(new_bytes) == len(old_bytes)

    first = sessions.open_model(str(path), "pi")
    assert first.cwd == ""
    path.write_bytes(new_bytes)
    os.utime(
        path,
        ns=(old_info.st_atime_ns, old_info.st_mtime_ns + 1_000_000),
    )
    settle(first)
    assert first.cwd == str(new_project)
    assert [step.body for step in first.nodes["main"].steps] == ["pi new"]

    reopened = sessions.open_model(str(path), "pi")
    settle(reopened)
    assert reopened.cwd == str(new_project)
    assert [step.body for step in reopened.nodes["main"].steps] == ["pi new"]

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-pi-cache"))
    fresh = sessions.open_model(str(path), "pi")
    settle(fresh)
    assert reopened.cwd == fresh.cwd
    assert node_projection(reopened) == node_projection(fresh)


def test_settled_shorter_malformed_codex_rewrite_recovers_without_exception(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
        / "rollout-root.jsonl"
    )
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, codex_records(project))
    session = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    settle(session)
    expected = node_projection(session)
    old_size = path.stat().st_size

    path.write_bytes(b"{not json}\n")
    assert path.stat().st_size < old_size
    changed = session.poll()
    recovered = node_projection(session)
    if not changed:
        assert recovered == expected
    assert session.poll() is False
    assert node_projection(session) == recovered


@pytest.mark.parametrize("provider", ["codex", "pi"])
def test_settled_larger_malformed_provider_rewrite_stops_redrawing(
        tmp_path, monkeypatch, provider):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    if provider == "codex":
        path = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
            / "rollout-root.jsonl"
        )
        records = codex_records(project)
    else:
        path = tmp_path / "pi" / "session.jsonl"
        records = pi_records(project)
    write_jsonl(path, records)
    session = sessions.open_model(
        str(path), provider, project_root=str(project),
    )
    settle(session)
    expected = node_projection(session)
    old_size = path.stat().st_size

    path.write_bytes(b"{not json}\n" + b"x" * old_size)
    assert path.stat().st_size > old_size
    polls = []
    projections = []
    for _ in range(4):
        polls.append(session.poll())
        projections.append(node_projection(session))

    assert polls.count(True) <= 1
    assert polls[-2:] == [False, False]
    assert projections[-2:] == [projections[-1], projections[-1]]
    assert projections[-1] == expected or polls[0] is True


def test_pi_100_digit_version_has_runtime_and_cache_admission_parity(
    tmp_path, monkeypatch,
):
    from quakepro.provider_admission import read_admission

    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "pi" / "session.jsonl"
    project.mkdir()
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    version = b"9" * 100
    header = (
        b'{"type":"session","version":' + version
        + b',"id":"pi-version","cwd":'
        + json.dumps(str(project)).encode("utf-8")
        + b"}\n"
    )
    message = json.dumps(pi_records(project)[1]).encode("utf-8") + b"\n"
    path.write_bytes(header + message)

    admitted = read_admission(str(path), "pi")
    assert admitted is not None
    assert admitted.proof.root_identity[1] == version.decode("ascii")
    runtime = pi_model.PiModel(str(path))
    assert runtime.poll() is True
    settle(runtime)

    first = sessions.open_model(
        str(path), "pi", project_root=str(project),
    )
    settle(first)
    restored = sessions.open_model(
        str(path), "pi", project_root=str(project),
    )

    assert restored.cache_restored is True
    assert restored.poll() is False
    assert node_projection(restored) == node_projection(first)


def test_restored_codex_skips_idle_admission_and_appends_once(
    tmp_path, monkeypatch,
):
    from quakepro.provider_admission import AdmissionResult, read_admission_result

    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "07"
        / "rollout.jsonl"
    )
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, codex_records(project))
    first = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    settle(first)
    restored = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    assert restored.cache_restored is True
    reads = []

    def count_admission(candidate, provider) -> AdmissionResult:
        reads.append((os.path.realpath(candidate), provider))
        return read_admission_result(candidate, provider)

    monkeypatch.setattr(
        provider_reload, "read_admission_result", count_admission,
    )

    assert restored.poll() is False
    assert reads == []
    assert restored.poll() is False
    assert reads == []

    append_jsonl(path, [{
        "type": "event_msg",
        "timestamp": "2026-08-07T10:01:00Z",
        "payload": {"type": "agent_message", "message": "one append"},
    }])
    assert restored.poll() is True
    assert restored.poll() is False
    assert reads == []
    assert [step.body for step in restored.nodes["main"].steps][-1] == "one append"


def test_restored_codex_preserves_foreign_denial_without_reopening(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    root = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "07"
        / "rollout-root.jsonl"
    )
    foreign = root.with_name("rollout-foreign.jsonl")
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(root, codex_records(project))
    write_jsonl(foreign, [{
        "type": "session_meta",
        "payload": {
            "id": "foreign-root",
            "session_id": "foreign-root",
            "cwd": str(project),
            "thread_source": "user",
            "source": "cli",
        },
    }])
    settle(sessions.open_model(
        str(root), "codex", project_root=str(project),
    ))
    restored = sessions.open_model(
        str(root), "codex", project_root=str(project),
    )
    assert restored.cache_restored is True
    directories, denials = provider_reload.reload_acceptance_state(
        restored.raw_model,
    )
    foreign_path = os.path.realpath(foreign)
    assert foreign_path in dict(denials)
    assert any(
        (foreign.name, "file") in entries
        for _directory, entries in directories or ()
    )

    def forbid_foreign_open(candidate, *args, **kwargs):
        if os.path.realpath(candidate) == foreign_path:
            raise AssertionError("restored foreign source was reopened")
        return builtins.open(candidate, *args, **kwargs)

    original_admission = provider_reload.read_admission_result

    def guarded_admission(candidate, provider):
        if os.path.realpath(candidate) == foreign_path:
            raise AssertionError("restored foreign source was read for admission")
        return original_admission(candidate, provider)

    monkeypatch.setattr(provider_reload, "open", forbid_foreign_open, raising=False)
    monkeypatch.setattr(provider_reload, "read_admission_result", guarded_admission)

    assert restored.accepts_observation(frozenset({foreign_path})) is False
    assert restored.poll() is False
    prior_denial = dict(denials)[foreign_path]
    with foreign.open("ab") as stream:
        stream.write(b'{"foreign":"suffix"}\n')

    assert restored.accepts_observation(frozenset({foreign_path})) is False
    assert restored.poll() is False
    _directories, updated_denials = provider_reload.reload_acceptance_state(
        restored.raw_model,
    )
    updated = dict(updated_denials)[foreign_path]
    assert updated[:3] == prior_denial[:3]
    assert updated[3] > prior_denial[3]


def test_codex_pending_candidate_rewrite_rebuilds_and_becomes_file_claim(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
        / "rollout-root.jsonl"
    )
    child = path.with_name("rollout-child.jsonl")
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, codex_records(project))
    child_record = {
        "type": "session_meta",
        "payload": {
            "id": "codex-child",
            "session_id": "codex-root",
            "parent_thread_id": "codex-root",
            "forked_from_id": "codex-root",
            "cwd": str(project),
            "thread_source": "subagent",
            "source": {
                "subagent": {"thread_spawn": {"parent_thread_id": "codex-root"}},
            },
            "agent_path": "/root/child",
        },
    }
    child_bytes = (json.dumps(child_record) + "\n").encode()
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_bytes(b" " * (len(child_bytes) - 1) + b"\n")
    old_times = child.stat()

    first = sessions.open_model(str(path), "codex")
    settle(first)
    assert "cx:codex-child" not in first.nodes

    child.write_bytes(child_bytes)
    os.utime(child, ns=(old_times.st_atime_ns, old_times.st_mtime_ns))
    assert child.stat().st_size == old_times.st_size
    assert child.stat().st_mtime_ns == old_times.st_mtime_ns

    reopened = sessions.open_model(str(path), "codex")
    assert reopened.cache_restored is False
    settle(reopened)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-codex-cache"))
    fresh = sessions.open_model(str(path), "codex")
    settle(fresh)

    assert node_projection(reopened) == node_projection(fresh)
    saved_path = cache_path(cache_dir, path)
    assert saved_path.exists()
    document = json.loads(saved_path.read_text())
    entry = next(iter(document["entries"].values()))
    assert os.path.realpath(child) in entry["sources"]["files"]


def test_symlink_spelled_pi_transcript_uses_one_canonical_cache_identity(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    target = tmp_path / "sessions" / "pi-target.jsonl"
    linked = tmp_path / "sessions" / "pi-linked.jsonl"
    project.mkdir()
    write_jsonl(target, pi_records(project))
    linked.symlink_to(target)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))

    first = sessions.open_model(str(linked), "pi")
    settle(first)

    saved_path = cache_path(cache_dir, linked)
    assert saved_path.exists()
    document = json.loads(saved_path.read_text())
    entry = next(iter(document["entries"].values()))
    canonical = os.path.realpath(linked)
    assert entry["requested"] == canonical
    assert set(entry["sources"]["files"]) == {canonical}

    restored = sessions.open_model(str(target), "pi")
    assert restored.cache_restored is True
    assert node_projection(restored) == node_projection(first)


def test_claude_snapshot_rejects_future_agent_alias_to_main_before_progress(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "claude.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, [assistant("root work")])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))
    cache_file = cache_path(cache_dir, project)

    def forge_future_agent(payload):
        encoded_index(payload, "_flat_nodes").append(["future-agent", "main"])

    mutate_cached_snapshot(cache_file, forge_future_agent)
    reopened = sessions.open_model(str(path), "claude", project_root=str(project))
    assert reopened.cache_restored is False

    append_jsonl(path, [{
        "type": "progress",
        "toolUseID": "future-tool",
        "data": {
            "type": "agent_progress",
            "agentId": "future-agent",
            "prompt": "future task",
            "message": {
                "timestamp": "2026-08-05T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "future work"}],
                },
            },
        },
    }])
    settle(reopened)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-claude-cache"))
    fresh = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(fresh)

    assert node_projection(reopened) == node_projection(fresh)
    assert "future-agent" in reopened.nodes


def test_codex_snapshot_rejects_future_thread_alias_to_main_before_activity(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
        / "rollout-root.jsonl"
    )
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, codex_records(project))
    settle(sessions.open_model(str(path), "codex"))
    cache_file = cache_path(cache_dir, path)

    def forge_future_thread(payload):
        encoded_index(payload, "_thread_nodes").append(["future-thread", "main"])

    mutate_cached_snapshot(cache_file, forge_future_thread)
    reopened = sessions.open_model(str(path), "codex")
    assert reopened.cache_restored is False

    append_jsonl(path, [{
        "type": "event_msg",
        "timestamp": "2026-08-05T10:00:00Z",
        "payload": {
            "type": "sub_agent_activity",
            "agent_thread_id": "future-thread",
            "agent_path": "/root/future",
            "event_id": "future-event",
            "kind": "started",
        },
    }])
    settle(reopened)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-codex-cache"))
    fresh = sessions.open_model(str(path), "codex")
    settle(fresh)

    assert node_projection(reopened) == node_projection(fresh)
    assert "cx:future-thread" in reopened.nodes


def test_pi_snapshot_rejects_lowered_child_counter_before_next_allocation(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "pi" / "session.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, [
        pi_fixture.header(cwd=str(project), session_id="pi-counter"),
        *pi_fixture.subagent_entries()[1:],
    ])
    settle(sessions.open_model(str(path), "pi"))
    cache_file = cache_path(cache_dir, path)

    def lower_child_counter(payload):
        pair = next(
            pair for pair in payload["state"]["$dict"] if pair[0] == "_children"
        )
        pair[1] = 0

    mutate_cached_snapshot(cache_file, lower_child_counter)
    reopened = sessions.open_model(str(path), "pi")
    assert reopened.cache_restored is False

    details = json.loads(json.dumps(
        pi_fixture.subagent_entries()[-1]["message"]["details"]
    ))
    details["results"][0]["agent"] = "reviewer"
    details["results"][0]["task"] = "Review next change"
    append_jsonl(path, [
        pi_fixture.message(
            "next-call",
            "previous",
            pi_fixture.assistant([
                pi_fixture.tool_call(
                    "call_10", "subagent",
                    {"agent": "reviewer", "task": "Review next change"},
                ),
            ], stop_reason="toolUse"),
        ),
        pi_fixture.message(
            "next-result",
            "next-call",
            pi_fixture.tool_result(
                "call_10", "subagent", "Reviewed", details=details,
            ),
        ),
    ])
    settle(reopened)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-pi-cache"))
    fresh = sessions.open_model(str(path), "pi")
    settle(fresh)

    assert node_projection(reopened) == node_projection(fresh)
    assert reopened.nodes["pi:1"].label == "researcher"
    assert reopened.nodes["pi:2"].label == "reviewer"


def test_pi_snapshot_rejects_header_identity_not_bound_to_source(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    forged_project = tmp_path / "forged-project"
    path = tmp_path / "pi" / "session.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, pi_records(project))
    settled = sessions.open_model(str(path), "pi")
    settle(settled)
    cache_file = cache_path(cache_dir, path)

    def forge_header(payload):
        header = encoded_state_field(payload, "_header")["$dict"]
        next(pair for pair in header if pair[0] == "id")[1] = "forged-pi"
        next(pair for pair in header if pair[0] == "cwd")[1] = str(forged_project)
        state = payload["state"]["$dict"]
        next(pair for pair in state if pair[0] == "cwd")[1] = str(forged_project)

    mutate_cached_snapshot(cache_file, forge_header)
    reopened = sessions.open_model(str(path), "pi")
    assert reopened.cache_restored is False
    settle(reopened)
    assert reopened.cwd == str(project)

    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-pi-cache"))
    fresh = sessions.open_model(str(path), "pi")
    settle(fresh)
    assert reopened.cwd == fresh.cwd
    assert node_projection(reopened) == node_projection(fresh)


def test_session_cache_rejects_stale_or_invalid_state(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("cached")])
    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    cache_file = cache_path(cache_dir, project)

    path.unlink()
    missing = session_cache.open_cached_model(
        str(path),
        str(project),
        claude_snapshot.ClaudeSnapshotCodec(),
        lambda: (_ for _ in ()).throw(AssertionError("missing source rebuilt")),
    )
    assert missing.cache_restored is True
    settle(missing)
    assert [step.body for step in missing.nodes["main"].steps] == ["cached"]

    replacement = path.with_suffix(".replacement")
    write_jsonl(replacement, [assistant("rewritten")])
    os.replace(replacement, path)
    stale = sessions.open_model(str(path), "claude", project_root=str(project))
    assert stale.cache_restored is True
    settle(stale)
    assert [step.body for step in stale.nodes["main"].steps] == ["cached"]

    cache_file.write_text("not json")
    corrupt = sessions.open_model(str(path), "claude", project_root=str(project))
    assert corrupt.cache_restored is False
    settle(corrupt)

    document = json.loads(cache_file.read_text())
    document["version"] = 999
    cache_file.write_text(json.dumps(document))
    incompatible = sessions.open_model(str(path), "claude", project_root=str(project))
    assert incompatible.cache_restored is False
    settle(incompatible)

    with path.open("a") as stream:
        stream.write("not-json\n")
    malformed = sessions.open_model(str(path), "claude", project_root=str(project))
    assert malformed.cache_restored is True
    settle(malformed)
    assert [step.body for step in malformed.nodes["main"].steps] == ["rewritten"]
    assert json.loads(cache_file.read_text())["entries"]


def test_session_cache_rejects_unknown_or_missing_snapshot_fields(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("source")])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))
    cache_file = cache_path(cache_dir, project)

    def assert_falls_back(mutate):
        mutate_cached_snapshot(cache_file, mutate)
        model = sessions.open_model(str(path), "claude", project_root=str(project))
        assert model.cache_restored is False
        settle(model)
        assert [step.body for step in model.nodes["main"].steps] == ["source"]

    def remove_nodes(snapshot):
        pairs = snapshot["state"]["$dict"]
        snapshot["state"]["$dict"] = [pair for pair in pairs if pair[0] != "nodes"]

    assert_falls_back(remove_nodes)

    def add_node_field(snapshot):
        node = next(
            item for item in snapshot["objects"]
            if item["type"].endswith(".Node")
        )
        node["fields"]["unknown"] = "value"

    assert_falls_back(add_node_field)

    def remove_node_field(snapshot):
        node = next(
            item for item in snapshot["objects"]
            if item["type"].endswith(".Node")
        )
        node["fields"].pop("children")

    assert_falls_back(remove_node_field)


@pytest.mark.parametrize(
    ("provider", "state_field", "encoded_value"),
    [
        pytest.param(
            "claude", "_tail_rewound", {"$set": ["/transient/source"]},
            id="claude-transient-rewind",
        ),
        pytest.param("codex", "_initial_changed", True, id="codex-transient-change"),
        pytest.param("pi", "_changed", True, id="pi-transient-change"),
    ],
)
def test_provider_snapshot_rejects_transient_reload_authority(
        tmp_path, monkeypatch, provider, state_field, encoded_value):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "claude":
        path = tmp_path / "claude" / "session.jsonl"
        records = [assistant("source")]
    elif provider == "codex":
        path = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
            / "rollout-root.jsonl"
        )
        records = codex_records(project)
    else:
        path = tmp_path / "pi" / "session.jsonl"
        records = pi_records(project)
    write_jsonl(path, records)
    first = sessions.open_model(
        str(path), provider, project_root=str(project),
    )
    settle(first)
    expected = node_projection(first)
    cache_file = cache_path(cache_dir, project)

    def forge_transient(payload):
        pair = next(
            pair for pair in payload["state"]["$dict"]
            if pair[0] == state_field
        )
        pair[1] = encoded_value

    mutate_cached_snapshot(cache_file, forge_transient)
    reopened = sessions.open_model(
        str(path), provider, project_root=str(project),
    )

    assert reopened.cache_restored is False
    settle(reopened)
    assert node_projection(reopened) == expected


def test_codex_snapshot_rejects_pending_candidate_as_committed_authority(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    root = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
        / "rollout-root.jsonl"
    )
    child = root.with_name("rollout-child.jsonl")
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(root, codex_records(project))
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_bytes(b"")

    first = sessions.open_model(str(root), "codex", project_root=str(project))
    settle(first)
    reopened = sessions.open_model(
        str(root), "codex", project_root=str(project),
    )

    assert reopened.cache_restored is False

    write_jsonl(child, [{
        "type": "session_meta",
        "payload": {
            "id": "codex-child",
            "session_id": "codex-root",
            "parent_thread_id": "codex-root",
            "cwd": str(project),
            "thread_source": "subagent",
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "codex-root",
            }}},
            "agent_path": "/root/child",
        },
    }])
    assert reopened.poll() is True
    assert "cx:codex-child" in reopened.nodes


def test_restored_codex_checkpoint_admits_new_child(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    root = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
        / "rollout-root.jsonl"
    )
    child = root.with_name("rollout-child.jsonl")
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(root, codex_records(project))

    first = sessions.open_model(str(root), "codex", project_root=str(project))
    settle(first)
    restored = sessions.open_model(
        str(root), "codex", project_root=str(project),
    )
    assert restored.cache_restored is True

    write_jsonl(child, [{
        "type": "session_meta",
        "payload": {
            "id": "codex-child",
            "session_id": "codex-root",
            "parent_thread_id": "codex-root",
            "cwd": str(project),
            "thread_source": "subagent",
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "codex-root",
            }}},
            "agent_path": "/root/child",
        },
    }])

    assert restored.poll() is True
    assert "cx:codex-child" in restored.nodes


def test_session_cache_rejects_cached_node_cycle_before_display(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("source")])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))
    cache_file = cache_path(cache_dir, project)

    def add_main_cycle(payload):
        nodes = encoded_state_field(payload, "nodes")
        main_reference = next(
            value for key, value in nodes["$dict"] if key == "main"
        )
        encoded_object(payload, main_reference)["fields"]["children"] = {
            "$list": ["main"],
        }

    mutate_cached_snapshot(cache_file, add_main_cycle)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))

    assert restored.cache_restored is False
    settle(restored)
    assert restored.nodes["main"].children == []


def test_claude_snapshot_rejects_step_index_clone_before_tool_result(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [{
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"file_path": "README.md"},
            }],
        },
    }])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))
    cache_file = cache_path(cache_dir, project)

    def clone_step_index(payload):
        nodes = encoded_state_field(payload, "nodes")
        main_reference = next(
            value for key, value in nodes["$dict"] if key == "main"
        )
        main = encoded_object(payload, main_reference)
        visible_reference = main["fields"]["steps"]["$list"][-1]
        clone_index = len(payload["objects"])
        payload["objects"].append(json.loads(json.dumps(
            encoded_object(payload, visible_reference)
        )))
        step_index = encoded_state_field(payload, "_step_by_tid")
        tool_pair = next(pair for pair in step_index["$dict"] if pair[0] == "tool-1")
        tool_pair[1] = {"$ref": clone_index}

    mutate_cached_snapshot(cache_file, clone_step_index)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(restored)
    visible_step = restored.nodes["main"].steps[-1]
    with path.open("a") as stream:
        stream.write(json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "result",
                }],
            },
        }) + "\n")

    assert restored.poll() is True
    assert visible_step.status == "done"
    assert visible_step.output == "result"
    assert restored.cache_restored is False


def test_claude_snapshot_rejects_spawn_index_disagreeing_with_visible_step(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "claude.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    spawn = {
        "type": "assistant",
        "timestamp": "2026-08-02T10:00:00Z",
        "message": {
            "timestamp": "2026-08-02T10:00:00Z",
            "content": [{
                "type": "tool_use",
                "id": "spawn-1",
                "name": "Task",
                "input": {
                    "subagent_type": "reviewer",
                    "description": "Review cache",
                },
            }],
        },
    }
    progress = {
        "type": "progress",
        "toolUseID": "spawn-1",
        "data": {
            "type": "agent_progress",
            "agentId": "worker-1",
            "prompt": "Review cache",
            "message": {
                "timestamp": "2026-08-02T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "working"}],
                },
            },
        },
    }
    write_jsonl(path, [spawn, progress])
    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    cache_file = cache_path(cache_dir, project)

    def forge_spawn_child(payload):
        spawn_pair = next(
            pair for pair in encoded_index(payload, "_spawn")
            if pair[0] == "spawn-1"
        )
        spawn_pair[1] = "main"

    mutate_cached_snapshot(cache_file, forge_spawn_child)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    assert restored.cache_restored is False
    settle(restored)

    result = {
        "type": "user",
        "timestamp": "2026-08-02T10:00:02Z",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "spawn-1",
            "content": "review done",
        }]},
    }
    append_jsonl(path, [result])
    assert restored.poll() is True
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-claude-cache"))
    fresh = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(fresh)

    restored_spawn = next(
        step for step in restored.nodes["main"].steps if step.tid == "spawn-1"
    )
    fresh_spawn = next(
        step for step in fresh.nodes["main"].steps if step.tid == "spawn-1"
    )
    assert (
        restored_spawn.child,
        restored_spawn.status,
        restored.nodes["worker-1"].status,
        restored.nodes["worker-1"].result,
    ) == (
        fresh_spawn.child,
        fresh_spawn.status,
        fresh.nodes["worker-1"].status,
        fresh.nodes["worker-1"].result,
    )


@pytest.mark.parametrize("corruption", ["missing", "forged"])
def test_codex_snapshot_rejects_bad_spawn_index_before_activity(
        tmp_path, monkeypatch, corruption):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "02"
        / "rollout-root.jsonl"
    )
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()

    def record(record_type, payload, timestamp="2026-08-02T10:00:00Z"):
        return {"timestamp": timestamp, "type": record_type, "payload": payload}

    def spawn_call(call_id, task_name):
        return record("response_item", {
            "type": "function_call",
            "name": "spawn_agent",
            "namespace": "collaboration",
            "call_id": call_id,
            "arguments": json.dumps({
                "task_name": task_name,
                "fork_turns": "all",
                "message": "work",
            }),
        })

    meta = record("session_meta", {
        "id": "codex-root",
        "session_id": "codex-root",
        "cwd": str(project),
        "thread_source": "user",
        "source": "cli",
    })
    write_jsonl(path, [
        meta,
        spawn_call("spawn-1", "first"),
        spawn_call("spawn-2", "second"),
    ])
    first = sessions.open_model(str(path), "codex", project_root=str(project))
    settle(first)
    cache_file = cache_path(cache_dir, project)

    def corrupt_spawn_index(payload):
        pairs = encoded_index(payload, "_spawn_steps")
        target = next(pair for pair in pairs if pair[0] == "spawn-1")
        if corruption == "missing":
            pairs.remove(target)
        else:
            other = next(pair for pair in pairs if pair[0] == "spawn-2")
            target[1] = json.loads(json.dumps(other[1]))

    mutate_cached_snapshot(cache_file, corrupt_spawn_index)
    restored = sessions.open_model(str(path), "codex", project_root=str(project))
    assert restored.cache_restored is False
    settle(restored)

    append_jsonl(path, [
        record("event_msg", {
            "type": "sub_agent_activity",
            "agent_thread_id": "codex-child",
            "agent_path": "/root/first",
            "event_id": "spawn-1",
            "kind": "started",
        }, "2026-08-02T10:00:01Z"),
        record("response_item", {
            "type": "function_call_output",
            "call_id": "spawn-1",
            "output": "child accepted",
        }, "2026-08-02T10:00:02Z"),
    ])
    assert restored.poll() is True
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-codex-cache"))
    fresh = sessions.open_model(str(path), "codex", project_root=str(project))
    settle(fresh)

    def projection(model):
        step = next(
            item for item in model.nodes["main"].steps if item.tid == "spawn-1"
        )
        child = model.nodes["cx:codex-child"]
        return (
            step.child,
            step.output,
            child.parent,
            child.status,
            tuple(model.nodes[child.parent].children),
        )

    assert projection(restored) == projection(fresh)


def test_codex_snapshot_accepts_closed_single_spawn_dispatch(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "rollout.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    write_jsonl(path, [
        {
            "type": "session_meta",
            "payload": {
                "id": "codex-root",
                "session_id": "codex-root",
                "cwd": str(project),
                "thread_source": "user",
                "source": "cli",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "spawn-1",
                "arguments": json.dumps({
                    "task_name": "worker",
                    "message": "work",
                }),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "next"}],
            },
        },
    ])
    model = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    settle(model)
    spawn = next(
        step for step in model.nodes["main"].steps if step.kind == "spawn"
    )
    state = next(iter(model.raw_model._states.values()))
    assert spawn.dispatch
    assert spawn.dispatch not in model.nodes
    assert state.open_dispatch == ""

    snapshot = codex_snapshot.CodexSnapshotCodec().capture(model.raw_model)
    restored = codex_snapshot.CodexSnapshotCodec().restore(str(path), snapshot)

    assert node_projection(restored) == node_projection(model)

    corrupt = copy.deepcopy(snapshot)
    state = corrupt.payload
    corrupt_spawn = state["_spawn_steps"]["spawn-1"]
    prior_dispatch = corrupt_spawn.dispatch
    forged_dispatch = "dispatch:other:spawn-1"
    indexed = state["_dispatch_spawns"].pop(prior_dispatch)
    state["_dispatch_spawns"][forged_dispatch] = indexed
    corrupt_spawn.dispatch = forged_dispatch

    with pytest.raises(ValueError, match="dispatch index"):
        codex_snapshot.CodexSnapshotCodec().restore(str(path), corrupt)


@pytest.mark.parametrize("corruption", ["missing", "forged"])
def test_pi_snapshot_rejects_bad_step_index_before_result(
        tmp_path, monkeypatch, corruption):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "pi" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    records = [
        pi_fixture.header(cwd=str(project), session_id="pi-cache"),
        pi_fixture.message("user", None, pi_fixture.user("Run tools")),
        pi_fixture.message("calls", "user", pi_fixture.assistant([
            pi_fixture.tool_call("call-1", "bash", {"command": "first"}),
            pi_fixture.tool_call("", "bash", {"command": "second"}),
        ], stop_reason="toolUse")),
    ]
    write_jsonl(path, records)
    first = sessions.open_model(str(path), "pi", project_root=str(project))
    settle(first)
    cache_file = cache_path(cache_dir, project)

    def corrupt_step_index(payload):
        pairs = encoded_index(payload, "_steps")
        target = next(pair for pair in pairs if pair[0] == "call-1")
        if corruption == "missing":
            pairs.remove(target)
            return
        visible = encoded_step_references(payload)
        second = next(
            reference for reference in visible
            if encoded_object(payload, reference)["fields"]["tid"] == ""
        )
        encoded_object(payload, second)["fields"]["tid"] = "call-1"
        target[1] = json.loads(json.dumps(second))

    mutate_cached_snapshot(cache_file, corrupt_step_index)
    restored = sessions.open_model(str(path), "pi", project_root=str(project))
    assert restored.cache_restored is False
    settle(restored)

    result = pi_fixture.message(
        "result",
        "calls",
        pi_fixture.tool_result("call-1", "bash", "first result"),
    )
    append_jsonl(path, [result])
    assert restored.poll() is True
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "fresh-pi-cache"))
    fresh = sessions.open_model(str(path), "pi", project_root=str(project))
    settle(fresh)

    def projection(model):
        return [
            (step.tid, step.status, step.output)
            for step in model.nodes["main"].steps
            if step.kind == "tool"
        ]

    assert projection(restored) == projection(fresh)


def test_session_cache_rejects_bad_pending_tool_result_shape(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("source")])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))
    cache_file = cache_path(cache_dir, project)

    def corrupt_pending_result(snapshot):
        pair = next(
            pair
            for pair in snapshot["state"]["$dict"]
            if pair[0] == "_pending_tool_results"
        )
        pair[1] = {"$dict": [["bad", 1]]}

    mutate_cached_snapshot(cache_file, corrupt_pending_result)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    assert restored.cache_restored is False

    with path.open("a") as stream:
        stream.write(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "bad",
                    "name": "Read",
                    "input": {"file_path": "README.md"},
                }],
            },
        }) + "\n")
    assert restored.poll() is True
    assert restored.nodes["main"].steps[-1].tid == "bad"


def test_session_cache_hit_does_not_read_saved_transcript_prefix(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "large.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("x" * 1000) for _ in range(1000)])
    settle(sessions.open_model(str(path), "claude", project_root=str(project)))

    reads = {"bytes": 0, "opens": 0, "readlines": 0}

    class CountedSource:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, *args):
            data = self.stream.read(*args)
            reads["bytes"] += len(data)
            return data

        def readline(self, *args):
            data = self.stream.readline(*args)
            reads["bytes"] += len(data)
            reads["readlines"] += 1
            return data

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def counted_open(file, *args, **kwargs):
        stream = builtins.open(file, *args, **kwargs)
        if os.path.abspath(file) != os.path.abspath(path):
            return stream
        reads["opens"] += 1
        return CountedSource(stream)

    monkeypatch.setattr(session_cache, "open", counted_open, raising=False)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))

    assert restored.cache_restored is True
    assert restored.poll() is False
    assert reads == {"bytes": 0, "opens": 0, "readlines": 0}


@pytest.mark.parametrize(
    ("provider", "selection", "with_project_root"),
    [
        pytest.param("claude", "explicit", True, id="claude-explicit-root"),
        pytest.param("codex", "auto", False, id="codex-auto-source"),
        pytest.param("pi", "auto", True, id="pi-auto-root"),
    ],
)
def test_open_model_restores_before_any_transcript_open(
        tmp_path, monkeypatch, provider, selection, with_project_root):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / provider / "session.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "claude":
        records = [{**assistant("cached"), "cwd": str(project)}]
        child = path.with_suffix("") / "subagents" / "agent-child.jsonl"
        write_jsonl(child, [{
            "uuid": "child-record",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "child"}],
            },
        }])
    elif provider == "codex":
        records = codex_records(project)
        child = path.with_name("child.jsonl")
        write_jsonl(child, [{
            "type": "session_meta",
            "payload": {
                "id": "codex-child",
                "session_id": "codex-root",
                "parent_thread_id": "codex-root",
                "cwd": str(project),
                "thread_source": "subagent",
                "source": {"subagent": {"thread_spawn": {
                    "parent_thread_id": "codex-root",
                }}},
            },
        }])
    else:
        records = pi_records(project)
    write_jsonl(path, records)
    root = str(project) if with_project_root else None
    settle(sessions.open_model(str(path), provider, project_root=root))
    expected_cache_root = project if with_project_root else path
    saved = cache_path(cache_dir, expected_cache_root)
    document = json.loads(saved.read_text())
    entry = next(
        entry for entry in document["entries"].values()
        if entry["provider"] == provider
    )
    claimed = set(entry["sources"]["files"])
    assert os.path.realpath(path) in claimed
    if provider in {"claude", "codex"}:
        assert len(claimed) >= 2

    original_open = builtins.open
    transcript_opens = []

    def forbid_transcript_open(candidate, *args, **kwargs):
        try:
            candidate_path = os.path.realpath(os.fspath(candidate))
        except TypeError:
            candidate_path = ""
        if candidate_path in claimed:
            transcript_opens.append((args, kwargs))
            raise AssertionError("cache restore opened transcript")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", forbid_transcript_open)
    requested_provider = provider if selection == "explicit" else "auto"
    restored = sessions.open_model(
        str(path), requested_provider, project_root=root,
    )

    assert restored.cache_restored is True
    assert restored.provider == provider
    assert transcript_opens == []


def test_provider_save_evicts_stale_same_requested_provider_entry(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, [{**assistant("claude"), "cwd": str(project)}])
    settle(sessions.open_model(
        str(path), "claude", project_root=str(project),
    ))

    write_jsonl(replacement, codex_records(project))
    os.replace(replacement, path)
    codex = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    assert codex.cache_restored is False
    settle(codex)

    document = json.loads(cache_path(cache_dir, project).read_text())
    matching = [
        entry for entry in document["entries"].values()
        if entry["requested"] == os.path.realpath(path)
    ]
    assert [entry["provider"] for entry in matching] == ["codex"]

    restored = sessions.open_model(
        str(path), "auto", project_root=str(project),
    )
    assert restored.cache_restored is True
    assert restored.provider == "codex"
    assert [step.body for step in restored.nodes["main"].steps][-1] == "codex old"


def test_explicit_wrong_provider_ignores_valid_same_requested_cache_entry(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    write_jsonl(path, [{**assistant("cached"), "cwd": str(project)}])
    settle(sessions.open_model(
        str(path), "claude", project_root=str(project),
    ))

    with pytest.raises(ValueError, match="is a claude session, not codex"):
        sessions.open_model(
            str(path), "codex", project_root=str(project),
        )


def test_claude_snapshot_does_not_claim_metadata_created_after_poll(tmp_path):
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    project.mkdir()
    write_jsonl(path, [assistant("accepted")])
    model = sessions.open_model(
        str(path), "claude", project_root=str(project),
    )
    settle(model)
    late = tmp_path / "session" / "subagents" / "agent-late.meta.json"
    late.parent.mkdir(parents=True, exist_ok=True)
    late.write_text(json.dumps({"agentType": "worker"}))

    snapshot = claude_snapshot.ClaudeSnapshotCodec().capture(model.raw_model)

    assert os.path.realpath(late) not in {
        claim.path for claim in snapshot.sources if claim.kind == "file"
    }


def test_session_cache_finishes_partial_record_after_restore(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "partial.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("complete")])
    with path.open("a") as stream:
        stream.write('{"type":"assistant","message":{"role":"assistant","content":[')

    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    assert restored.cache_restored is True

    with path.open("a") as stream:
        stream.write('{"type":"text","text":"finished"}]}}\n')
    assert restored.poll() is True
    assert [step.body for step in restored.nodes["main"].steps] == ["complete", "finished"]


def test_session_cache_restores_codex_and_pi_models(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex" / "sessions" / "2026" / "08" / "02" / "rollout.jsonl"
    pi = tmp_path / "pi" / "session.jsonl"
    write_jsonl(codex, codex_records(project))
    write_jsonl(pi, pi_records(project))

    first_codex = sessions.open_model(str(codex), "codex")
    first_pi = sessions.open_model(str(pi), "pi")
    settle(first_codex)
    settle(first_pi)

    documents = [
        json.loads(cache_path(cache_dir, source).read_text())
        for source in (codex, pi)
    ]
    assert all(document["version"] == 3 for document in documents)
    entries = [
        entry
        for document in documents
        for entry in document["entries"].values()
    ]
    assert {entry["provider"] for entry in entries} == {
        "codex", "pi",
    }
    assert all(set(entry) == {
        "provider", "requested", "codec_schema", "sources", "payload",
    } for entry in entries)
    assert all(
        "codex_model._FileState" not in json.dumps(document)
        for document in documents
    )

    restored_codex = sessions.open_model(str(codex), "codex")
    restored_pi = sessions.open_model(str(pi), "pi")
    assert restored_codex.cache_restored is True
    assert restored_pi.cache_restored is True
    assert restored_codex.poll() is False
    assert restored_pi.poll() is False
    assert [step.body for step in restored_codex.nodes["main"].steps][-1] == "codex old"
    assert [step.body for step in restored_pi.nodes["main"].steps][-1] == "pi old"

    with codex.open("a") as stream:
        stream.write(json.dumps({
            "type": "event_msg",
            "timestamp": "2026-08-02T10:01:00Z",
            "payload": {"type": "agent_message", "message": "codex new"},
        }) + "\n")
    with pi.open("a") as stream:
        stream.write(json.dumps({
            "type": "message",
            "timestamp": "2026-08-02T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "pi new"}],
            },
        }) + "\n")
    assert restored_codex.poll() is True
    assert restored_pi.poll() is True
    assert [step.body for step in restored_codex.nodes["main"].steps][-1] == "codex new"
    assert [step.body for step in restored_pi.nodes["main"].steps][-1] == "pi new"


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_restore_with_preopen_suffix_tails_from_saved_offset(
        tmp_path, monkeypatch, provider):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "claude":
        path = tmp_path / "claude" / "session.jsonl"
        records = [assistant("old")]
        suffix = assistant("preopen suffix")
    elif provider == "codex":
        path = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
            / "rollout-root.jsonl"
        )
        records = codex_records(project)
        suffix = {
            "type": "event_msg",
            "timestamp": "2026-08-06T10:01:00Z",
            "payload": {
                "type": "agent_message",
                "message": "preopen suffix",
                "phase": "commentary",
            },
        }
    else:
        path = tmp_path / "pi" / "session.jsonl"
        records = pi_records(project)
        suffix = {
            "type": "message",
            "id": "preopen-suffix",
            "timestamp": "2026-08-06T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "preopen suffix"}],
            },
        }
    write_jsonl(path, records)
    first = sessions.open_model(
        str(path), provider, project_root=str(project),
    )
    settle(first)
    append_jsonl(path, [suffix])

    restored = sessions.open_model(
        str(path), provider, project_root=str(project),
    )

    assert restored.cache_restored is True
    assert restored.poll() is True
    assert [step.body for step in restored.nodes["main"].steps][-1] == "preopen suffix"
    assert restored.poll() is False


@pytest.mark.parametrize("provider", ["codex", "pi"])
def test_snapshot_rejects_restored_nonempty_carry_and_rebuilds_completed_source(
        tmp_path, monkeypatch, provider):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    if provider == "codex":
        path = (
            tmp_path / "codex" / "sessions" / "2026" / "08" / "06"
            / "rollout-root.jsonl"
        )
        records = codex_records(project)
    else:
        path = tmp_path / "pi" / "session.jsonl"
        records = pi_records(project)
    write_jsonl(path, records)
    first = sessions.open_model(str(path), provider, project_root=str(project))
    settle(first)
    expected = node_projection(first)
    saved = cache_path(cache_dir, project)

    def forge_carry(payload):
        if provider == "pi":
            pair = next(
                pair for pair in payload["state"]["$dict"]
                if pair[0] == "_carry"
            )
        else:
            states = encoded_state_field(payload, "_states")["$dict"]
            file_state = states[0][1]["$dict"]
            pair = next(pair for pair in file_state if pair[0] == "carry")
        pair[1] = {"$bytes": "eA=="}

    mutate_cached_snapshot(saved, forge_carry)
    reopened = sessions.open_model(
        str(path), provider, project_root=str(project),
    )

    assert reopened.cache_restored is False
    settle(reopened)
    assert node_projection(reopened) == expected


def test_codex_invalid_utf8_metadata_never_enters_runtime_or_snapshot(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = (
        tmp_path / "codex" / "sessions" / "2026" / "08" / "02"
        / "rollout.jsonl"
    )
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    records = codex_records(project)
    encoded = "".join(json.dumps(record) + "\n" for record in records).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded.replace(b'"codex-root"', b'"bad\xffroot"'))

    with pytest.raises(ValueError):
        sessions.open_model(str(path), "codex", project_root=str(project))
    assert not cache_path(cache_dir, project).exists()

    write_jsonl(path, records)
    recovered = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    settle(recovered)
    cache_file = cache_path(cache_dir, project)
    assert "\ufffd" not in cache_file.read_text()
    restored = sessions.open_model(
        str(path), "codex", project_root=str(project),
    )
    assert restored.cache_restored is True
    assert node_projection(restored) == node_projection(recovered)


def test_session_cache_corrupt_or_unwritable_storage_falls_back(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    project = tmp_path / "project"
    path = tmp_path / "sessions" / "session.jsonl"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    project.mkdir()
    write_jsonl(path, [assistant("source")])

    def refuse_replace(source, destination):
        raise OSError("read only")

    monkeypatch.setattr("quakepro.session_cache.os.replace", refuse_replace)
    model = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(model)
    assert [step.body for step in model.nodes["main"].steps] == ["source"]
    assert not list(cache_dir.rglob("*.tmp"))


def test_overview_members_use_root_scoped_session_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    claude_home = tmp_path / "claude"
    root = tmp_path / "project"
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi"))
    root.mkdir()
    path = Path(claude_project_dir(str(root))) / "session.jsonl"
    record = assistant("overview")
    record["cwd"] = str(root)
    write_jsonl(path, [record])

    first = RunOverview([str(root)])
    settle(first)
    second = RunOverview([str(root)])
    second.poll()
    assert second._members
    assert all(member.cache_restored for _, _, member in second._members.values())


def test_session_cache_restores_workflow_summary_then_tails_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    project.mkdir()
    write_jsonl(path, [assistant("main")])
    workflow = tmp_path / "session" / "subagents" / "workflows" / "wf-run"
    journal = workflow / "journal.jsonl"
    agent = workflow / "agent-worker.jsonl"
    write_jsonl(journal, [
        {"type": "started", "agentId": "worker"},
        {"type": "result", "agentId": "worker", "result": "done"},
    ])
    write_jsonl(agent, [{
        "timestamp": "2026-08-02T10:00:00",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "workflow old"}],
        },
    }])

    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    assert restored.cache_restored is True
    assert restored.nodes["wf:wf-run"].status == "done"
    assert restored.nodes["wfa:wf-run:worker"].result == "done"

    with journal.open("a") as stream:
        stream.write(json.dumps({"type": "started", "agentId": "worker"}) + "\n")
    assert restored.poll() is True
    assert restored.nodes["wf:wf-run"].status == "running"
    assert restored.nodes["wfa:wf-run:worker"].result == ""


def test_session_cache_restores_live_tool_linkage(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    project = tmp_path / "project"
    path = tmp_path / "session.jsonl"
    project.mkdir()
    write_jsonl(path, [{
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"file_path": "/tmp/example"},
            }],
        },
    }])
    first = sessions.open_model(str(path), "claude", project_root=str(project))
    settle(first)
    restored = sessions.open_model(str(path), "claude", project_root=str(project))
    step = restored.nodes["main"].steps[-1]
    assert restored.cache_restored is True
    assert step.status == "running"

    with path.open("a") as stream:
        stream.write(json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "result",
                }],
            },
        }) + "\n")
    assert restored.poll() is True
    assert step.status == "done"
    assert step.output == "result"
