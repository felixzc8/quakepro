import json

import pytest

import quakepro.provider_admission as provider_admission
import quakepro.session_identity as session_identity
import quakepro.sessions as sessions


def write_meta(path, thread_id, session_id=None, parent=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": thread_id,
        "session_id": session_id or thread_id,
        "cwd": str(path.parent),
        "thread_source": "subagent" if parent else "user",
    }
    if parent:
        payload["parent_thread_id"] = parent
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )


def test_root_identity_uses_exact_hook_path_without_catalog_scan(tmp_path, monkeypatch):
    path = tmp_path / "sessions" / "2026" / "07" / "26" / "root.jsonl"
    write_meta(path, "root")

    def fail_scan(*args, **kwargs):
        raise AssertionError("root resolution scanned history")

    monkeypatch.setattr(session_identity, "index_codex_sessions", fail_scan)
    identity = session_identity.resolve_codex_session(str(path))

    assert identity.root_path == str(path)
    assert identity.group_id == "root"
    assert identity.metadata == {str(path): identity.requested_meta}


def test_child_identity_scans_local_day_for_root(tmp_path):
    day = tmp_path / "sessions" / "2026" / "07" / "26"
    root = day / "root.jsonl"
    child = day / "child.jsonl"
    write_meta(root, "root")
    write_meta(child, "child", session_id="root", parent="root")

    identity = session_identity.resolve_codex_session(str(child))

    assert identity.root_path == str(root)
    assert identity.group_id == "root"


def test_codex_public_identity_helpers_deny_later_metadata_after_wrong_first_record(
    tmp_path,
):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps({"type": "event_msg", "payload": {}}) + "\n"
        + json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "later",
                "session_id": "later",
                "cwd": str(tmp_path),
                "thread_source": "user",
            },
        }) + "\n",
        encoding="utf-8",
    )

    assert session_identity.codex_meta(str(path)) is None
    assert session_identity.first_codex_meta(str(path)) == {}
    assert session_identity.hook_transcript_matches(str(path), "later") is False
    with pytest.raises(ValueError):
        session_identity.detect_provider(str(path))
    for provider in ("auto", "codex"):
        with pytest.raises(ValueError):
            sessions.open_model(str(path), provider)


def test_codex_identity_helpers_do_not_reopen_admitted_metadata(
    tmp_path, monkeypatch,
):
    path = tmp_path / "rollout.jsonl"
    write_meta(path, "root")
    admission_calls = []
    real_admission = provider_admission.read_admission

    def track_admission(candidate, provider):
        admission_calls.append((candidate, provider))
        return real_admission(candidate, provider)

    def fail_reopen(*args, **kwargs):
        raise AssertionError("identity helper reopened admitted metadata")

    monkeypatch.setattr(
        session_identity, "read_admission", track_admission, raising=False,
    )
    monkeypatch.setattr(session_identity, "records", fail_reopen)

    assert session_identity.codex_meta(str(path))["id"] == "root"
    assert session_identity.first_codex_meta(str(path))["id"] == "root"
    assert session_identity.detect_provider(str(path)) == "codex"
    assert session_identity.hook_transcript_matches(str(path), "root") is True

    assert admission_calls
    assert all(call == (str(path), "codex") for call in admission_calls)


def test_codex_meta_cannot_mix_admitted_record_with_rewritten_bytes(
    tmp_path, monkeypatch,
):
    path = tmp_path / "rollout.jsonl"
    write_meta(path, "stable")
    real_admission = provider_admission.read_admission

    def mutate_after_admission(candidate, provider):
        admitted = real_admission(candidate, provider)
        write_meta(path, "replacement")
        return admitted

    def fail_reopen(*args, **kwargs):
        raise AssertionError("identity helper reopened rewritten metadata")

    monkeypatch.setattr(
        session_identity, "read_admission", mutate_after_admission, raising=False,
    )
    monkeypatch.setattr(session_identity, "records", fail_reopen)

    assert session_identity.codex_meta(str(path))["id"] == "stable"
    assert json.loads(path.read_text(encoding="utf-8"))["payload"]["id"] == (
        "replacement"
    )
