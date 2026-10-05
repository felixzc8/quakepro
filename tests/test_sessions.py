import builtins
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import quakepro.sessions as sessions


JSONL_RECORD_LIMIT = 64 * 1024 * 1024


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def write_oversized_pi_header(path, cwd):
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = (
        b'{"type":"session","version":3,"id":"oversized","cwd":'
        + json.dumps(str(cwd)).encode("utf-8")
        + b',"future":"'
    )
    suffix = b'"}'
    padding = JSONL_RECORD_LIMIT + 1 - len(prefix) - len(suffix)
    assert padding > 0
    chunk = b"x" * (1024 * 1024)
    with path.open("wb") as stream:
        stream.write(prefix)
        while padding:
            part = min(padding, len(chunk))
            stream.write(chunk[:part])
            padding -= part
        stream.write(suffix + b"\n")
    assert path.stat().st_size == JSONL_RECORD_LIMIT + 2


def claude_turn():
    return {"type": "assistant", "message": {"role": "assistant", "content": []}}


def codex_meta(thread_id, cwd="/work/quakepro", session_id=None, parent=None):
    payload = {
        "id": thread_id,
        "session_id": session_id or thread_id,
        "cwd": cwd,
        "thread_source": "subagent" if parent else "user",
        "source": ({"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
                   if parent else "cli"),
        "model_provider": "openai",
    }
    if parent:
        payload["parent_thread_id"] = parent
        payload["forked_from_id"] = parent
        payload["agent_path"] = "/root/worker"
    return {"type": "session_meta", "payload": payload}


def set_homes(monkeypatch, tmp_path):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    # Keep auto-detection off the developer's own pi sessions.
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    return claude, codex


def root_provider_dirs(tmp_path, monkeypatch, root):
    claude, codex = set_homes(monkeypatch, tmp_path)
    pi = tmp_path / "pi-sessions"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(pi))
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(root)))
    return claude / "projects" / slug, codex / "sessions", pi


def provider_rows(discovery):
    return [
        (
            result.provider,
            result.path,
            None if result.error is None else (result.error.error_type, result.error.message),
        )
        for result in discovery.providers
    ]


def nested_value(containers):
    value = 0
    for _ in range(containers):
        value = [value]
    return value


def test_detect_provider_from_content(tmp_path):
    claude = tmp_path / "claude.jsonl"
    codex = tmp_path / "rollout.jsonl"
    write_jsonl(claude, [claude_turn()])
    write_jsonl(codex, [codex_meta("root")])

    assert sessions.detect_provider(str(claude)) == "claude"
    assert sessions.detect_provider(str(codex)) == "codex"


def test_detect_provider_recognizes_claude_session_prologue(tmp_path):
    path = tmp_path / "claude.jsonl"
    write_jsonl(path, [{"type": "queue-operation", "sessionId": "session-1"}])
    assert sessions.detect_provider(str(path)) == "claude"


@pytest.mark.parametrize("content", ["", "not-json\n", '{"type":"mystery"}\n'])
def test_detect_provider_rejects_unknown_or_malformed(tmp_path, content):
    path = tmp_path / "unknown.jsonl"
    path.write_text(content)
    with pytest.raises(ValueError):
        sessions.detect_provider(str(path))


def test_latest_codex_ranks_group_by_child_but_returns_root(tmp_path, monkeypatch):
    _, codex = set_homes(monkeypatch, tmp_path)
    day = codex / "sessions" / "2026" / "07" / "09"
    old_root = day / "rollout-old.jsonl"
    root = day / "rollout-root.jsonl"
    child = day / "rollout-child.jsonl"
    write_jsonl(old_root, [codex_meta("old", cwd="/work/other")])
    write_jsonl(root, [codex_meta("root")])
    write_jsonl(child, [codex_meta("child", session_id="root", parent="root")])
    os.utime(old_root, (300, 300))
    os.utime(root, (100, 100))
    os.utime(child, (400, 400))

    assert sessions.latest_session(provider="codex") == str(root)


def test_codex_user_fork_is_a_root_not_a_subagent(tmp_path, monkeypatch):
    _, codex = set_homes(monkeypatch, tmp_path)
    day = codex / "sessions" / "2026" / "07" / "09"
    original = day / "rollout-original.jsonl"
    fork = day / "rollout-fork.jsonl"
    write_jsonl(original, [codex_meta("original")])
    record = codex_meta("fork")
    record["payload"]["forked_from_id"] = "original"
    write_jsonl(fork, [record])
    os.utime(original, (100, 100))
    os.utime(fork, (200, 200))

    assert sessions.latest_session(provider="codex") == str(fork)


def test_latest_codex_project_filter_uses_root_cwd(tmp_path, monkeypatch):
    _, codex = set_homes(monkeypatch, tmp_path)
    day = codex / "sessions" / "2026" / "07" / "09"
    quakepro = day / "rollout-quakepro.jsonl"
    other = day / "rollout-other.jsonl"
    write_jsonl(quakepro, [codex_meta("quakepro", cwd="/src/quakepro")])
    write_jsonl(other, [codex_meta("other", cwd="/src/other")])
    os.utime(quakepro, (100, 100))
    os.utime(other, (200, 200))

    assert sessions.latest_session("quakepro", "codex") == str(quakepro)
    assert sessions.latest_session("missing", "codex") is None


def test_latest_claude_keeps_path_substring_filter(tmp_path, monkeypatch):
    claude, _ = set_homes(monkeypatch, tmp_path)
    alpha = claude / "projects" / "-src-alpha" / "a.jsonl"
    beta = claude / "projects" / "-src-beta" / "b.jsonl"
    write_jsonl(alpha, [claude_turn()])
    write_jsonl(beta, [claude_turn()])
    os.utime(alpha, (100, 100))
    os.utime(beta, (200, 200))

    assert sessions.latest_session("alpha", "claude") == str(alpha)


def test_latest_claude_skips_malformed_newest_candidate(tmp_path, monkeypatch):
    claude, _ = set_homes(monkeypatch, tmp_path)
    valid = claude / "projects" / "project" / "valid.jsonl"
    malformed = claude / "projects" / "project" / "malformed.jsonl"
    write_jsonl(valid, [claude_turn()])
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text('{"type":"assistant"')
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert sessions.latest_session(provider="claude") == str(valid)
    assert sessions.latest_session(provider="auto") == str(valid)


def test_empty_claude_candidate_can_win_without_weakening_explicit_files(
        tmp_path, monkeypatch):
    claude, codex = set_homes(monkeypatch, tmp_path)
    old_claude = claude / "projects" / "project" / "old.jsonl"
    empty_claude = claude / "projects" / "project" / "new.jsonl"
    codex_root = codex / "sessions" / "2026" / "07" / "09" / "rollout-root.jsonl"
    write_jsonl(old_claude, [claude_turn()])
    empty_claude.parent.mkdir(parents=True, exist_ok=True)
    empty_claude.write_text("")
    write_jsonl(codex_root, [codex_meta("root")])
    os.utime(old_claude, (100, 100))
    os.utime(codex_root, (200, 200))
    os.utime(empty_claude, (300, 300))

    discovered = sessions.latest_session(provider="claude")
    assert discovered == str(empty_claude)
    assert sessions.open_model(discovered, "claude").path == str(empty_claude)
    assert sessions.latest_session(provider="auto") == str(empty_claude)
    with pytest.raises(ValueError, match="empty or malformed"):
        sessions.open_model(str(empty_claude), "claude")


def test_auto_returns_empty_claude_tail_only_without_a_valid_candidate(tmp_path, monkeypatch):
    claude, _ = set_homes(monkeypatch, tmp_path)
    empty = claude / "projects" / "project" / "session.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("")

    discovered = sessions.latest_session(provider="auto")
    assert discovered == str(empty)
    assert sessions.open_model(discovered).provider == "claude"


def test_auto_prefers_newer_empty_claude_file_over_old_valid_session(tmp_path, monkeypatch):
    claude, _ = set_homes(monkeypatch, tmp_path)
    valid = claude / "projects" / "project" / "valid.jsonl"
    empty = claude / "projects" / "project" / "empty.jsonl"
    write_jsonl(valid, [claude_turn()])
    empty.write_text("")
    os.utime(valid, (100, 100))
    os.utime(empty, (200, 200))

    assert sessions.latest_session(provider="auto") == str(empty)


def test_auto_prefers_newer_codex_group_over_empty_claude_file(tmp_path, monkeypatch):
    claude, codex = set_homes(monkeypatch, tmp_path)
    empty = claude / "projects" / "project" / "empty.jsonl"
    root = codex / "sessions" / "2026" / "07" / "09" / "rollout-root.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("")
    write_jsonl(root, [codex_meta("root")])
    os.utime(empty, (100, 100))
    os.utime(root, (200, 200))

    assert sessions.latest_session(provider="auto") == str(root)


def test_auto_compares_claude_file_and_codex_group_activity(tmp_path, monkeypatch):
    claude, codex = set_homes(monkeypatch, tmp_path)
    claude_path = claude / "projects" / "-src-project" / "session.jsonl"
    day = codex / "sessions" / "2026" / "07" / "09"
    root = day / "rollout-root.jsonl"
    child = day / "rollout-child.jsonl"
    write_jsonl(claude_path, [claude_turn()])
    write_jsonl(root, [codex_meta("root")])
    write_jsonl(child, [codex_meta("child", session_id="root", parent="root")])
    os.utime(claude_path, (300, 300))
    os.utime(root, (100, 100))
    os.utime(child, (400, 400))

    assert sessions.latest_session(provider="auto") == str(root)


def test_environment_homes_are_read_at_call_time(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_path = first / "projects" / "one" / "one.jsonl"
    second_path = second / "projects" / "two" / "two.jsonl"
    write_jsonl(first_path, [claude_turn()])
    write_jsonl(second_path, [claude_turn()])

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(first))
    assert sessions.latest_session(provider="claude") == str(first_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(second))
    assert sessions.latest_session(provider="claude") == str(second_path)


def test_open_model_validates_override_and_opens_codex_root_or_child(tmp_path):
    root = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    write_jsonl(root, [codex_meta("root")])
    write_jsonl(child, [codex_meta("child", session_id="root", parent="root")])

    assert sessions.open_model(str(root)).path == str(root)
    assert sessions.open_model(str(child), "codex").path == str(child)
    with pytest.raises(ValueError, match="not claude"):
        sessions.open_model(str(root), "claude")


def test_open_model_marks_existing_model_as_claude(tmp_path):
    path = tmp_path / "claude.jsonl"
    write_jsonl(path, [claude_turn()])
    model = sessions.open_model(str(path), "claude")
    assert model.provider == "claude"
    assert model.path == str(path)


def test_open_model_passes_matching_explicit_snapshot_codec_for_each_provider(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    claude = tmp_path / "claude.jsonl"
    codex = tmp_path / "codex.jsonl"
    pi = tmp_path / "pi.jsonl"
    root.mkdir()
    write_jsonl(claude, [{**claude_turn(), "cwd": str(root)}])
    write_jsonl(codex, [codex_meta("codex", cwd=str(root))])
    write_jsonl(pi, [{
        "type": "session",
        "version": 3,
        "id": "pi",
        "cwd": str(root),
    }])
    calls = []

    def record_open(path, cache_root, codec, build):
        model = build()
        calls.append((
            model.provider,
            os.path.realpath(path),
            os.path.realpath(cache_root),
            codec.provider,
            codec.schema_version,
            type(codec).__module__,
        ))
        return model

    monkeypatch.setattr(sessions, "open_cached_model", record_open)

    sessions.open_model(str(claude), "claude", project_root=str(root))
    sessions.open_model(str(codex), "codex", project_root=str(root))
    sessions.open_model(str(pi), "pi", project_root=str(root))

    assert calls == [
        ("claude", os.path.realpath(claude), os.path.realpath(root),
         "claude", 4, "quakepro.claude_snapshot"),
        ("codex", os.path.realpath(codex), os.path.realpath(root),
         "codex", 6, "quakepro.codex_snapshot"),
        ("pi", os.path.realpath(pi), os.path.realpath(root),
         "pi", 7, "quakepro.pi_snapshot"),
    ]


def test_session_cache_root_uses_requested_path_without_metadata_read(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache root opened transcript")
        ),
        raising=False,
    )

    assert sessions._session_cache_root("/tmp/huge.jsonl", "claude", None) == os.path.realpath(
        "/tmp/huge.jsonl"
    )


def test_session_cache_root_uses_explicit_root_without_checking_it(tmp_path):
    root = tmp_path / "not-created"

    assert sessions._session_cache_root(
        "/tmp/session.jsonl", "codex", str(root),
    ) == os.path.realpath(root)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        sessions.latest_session(provider="other")


def test_sessions_for_root_returns_one_ordered_result_per_provider(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)

    claude = claude_dir / "claude.jsonl"
    write_jsonl(claude, [{**claude_turn(), "cwd": str(root)}])
    codex = codex_dir / "2026" / "08" / "04" / "rollout-codex.jsonl"
    write_jsonl(codex, [codex_meta("codex", cwd=str(root))])
    pi = pi_dir / "pi.jsonl"
    write_jsonl(pi, [{"type": "session", "version": 3, "id": "pi", "cwd": str(root)}])

    discovery = sessions.sessions_for_root(str(root))

    assert type(discovery).__name__ == "RootDiscovery"
    assert discovery.root == str(root)
    assert provider_rows(discovery) == [
        ("claude", str(claude), None),
        ("codex", str(codex), None),
        ("pi", str(pi), None),
    ]


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_discovery_cache_never_reopens_accepted_prefix_or_rescans_on_append(
    tmp_path, monkeypatch, provider,
):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(
        tmp_path, monkeypatch, root,
    )
    now = datetime.now(timezone.utc)
    if provider == "claude":
        path = claude_dir / "session.jsonl"
        records = [{**claude_turn(), "cwd": str(root)}]
    elif provider == "codex":
        path = (
            codex_dir / f"{now.year:04d}" / f"{now.month:02d}"
            / f"{now.day:02d}" / "rollout.jsonl"
        )
        records = [codex_meta("root", cwd=str(root))]
    else:
        path = pi_dir / "session.jsonl"
        records = [{
            "type": "session", "version": 3, "id": "pi", "cwd": str(root),
        }]
    write_jsonl(path, records)

    prefix_reads = []
    if provider == "claude":
        real_reader = sessions._read_bounded_claude_records

        def read_prefix(candidate):
            prefix_reads.append(os.path.abspath(candidate))
            return real_reader(candidate)

        monkeypatch.setattr(
            sessions, "_read_bounded_claude_records", read_prefix,
        )
    else:
        real_reader = sessions.read_admission_result

        def read_prefix(candidate, candidate_provider):
            prefix_reads.append(os.path.abspath(candidate))
            return real_reader(candidate, candidate_provider)

        monkeypatch.setattr(sessions, "read_admission_result", read_prefix)

    cache = sessions.DiscoveryCache()
    first = sessions.sessions_for_root(str(root), cache)
    assert first.providers[("claude", "codex", "pi").index(provider)].path == str(path)
    assert prefix_reads == [str(path)]

    scanned = []
    real_scandir = os.scandir

    def track_scandir(directory):
        scanned.append(os.path.abspath(directory))
        return real_scandir(directory)

    monkeypatch.setattr(sessions.os, "scandir", track_scandir)
    prefix_reads.clear()
    assert sessions.sessions_for_root(str(root), cache) == first
    assert prefix_reads == []
    assert scanned == []

    with path.open("ab") as stream:
        stream.write(b"{}\n")
    assert sessions.sessions_for_root(str(root), cache) == first
    assert prefix_reads == []
    assert scanned == []


def test_root_discovery_preserves_symlink_spelling_for_provider_directories(
        tmp_path, monkeypatch):
    import pi_fixture
    from quakepro.pi_model import pi_session_dir
    from quakepro.session_identity import claude_project_dir

    _, codex_home = set_homes(monkeypatch, tmp_path)
    real_root = tmp_path / "real-work"
    real_root.mkdir()
    linked_root = tmp_path / "linked-work"
    linked_root.symlink_to(real_root, target_is_directory=True)
    root = str(linked_root.absolute())
    target = str(real_root.absolute())

    claude_dir = Path(claude_project_dir(root))
    claude = claude_dir / "claude.jsonl"
    write_jsonl(claude, [{**claude_turn(), "cwd": target}])
    pi_dir = Path(pi_session_dir(root))
    pi_dir.mkdir(parents=True)
    pi = pi_dir / "pi.jsonl"
    pi_fixture.write_session(pi, [pi_fixture.header(cwd=target)])

    discovery = sessions.sessions_for_root(root)

    assert discovery.root == root
    assert provider_rows(discovery) == [
        ("claude", str(claude), None),
        ("codex", None, None),
        ("pi", str(pi), None),
    ]
    assert sessions.discovery_observation_paths(root) == frozenset({
        str(claude_dir),
        str(codex_home / "sessions"),
        str(pi_dir),
    })


def test_sessions_for_root_treats_missing_provider_directories_as_healthy(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    root_provider_dirs(tmp_path, monkeypatch, root)

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", None, None),
    ]


def test_sessions_for_root_tolerates_bad_candidates_and_uses_next_valid(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)

    claude = claude_dir / "100-valid.jsonl"
    write_jsonl(claude, [{**claude_turn(), "cwd": str(root)}])
    write_jsonl(claude_dir / "200-other-root.jsonl", [
        {**claude_turn(), "cwd": str(other)},
    ])
    (claude_dir / "300-malformed.jsonl").write_text('{"type":"assistant"')
    (claude_dir / "400-incomplete.jsonl").write_text("")

    codex = codex_dir / "2026" / "08" / "04" / "100-valid.jsonl"
    write_jsonl(codex, [codex_meta("codex", cwd=str(root))])
    write_jsonl(codex.parent / "200-other-root.jsonl", [
        codex_meta("other", cwd=str(other)),
    ])
    write_jsonl(codex.parent / "300-child.jsonl", [
        codex_meta("child", cwd=str(root), session_id="codex", parent="codex"),
    ])
    (codex.parent / "400-malformed.jsonl").write_text("not json\n")

    pi = pi_dir / "100-valid.jsonl"
    write_jsonl(pi, [{"type": "session", "version": 3, "id": "pi", "cwd": str(root)}])
    write_jsonl(pi_dir / "200-other-root.jsonl", [
        {"type": "session", "version": 3, "id": "other", "cwd": str(other)},
    ])
    (pi_dir / "300-malformed.jsonl").write_text("not json\n")
    (pi_dir / "400-incomplete.jsonl").write_text("")

    for rank, path in enumerate((
        claude,
        claude_dir / "200-other-root.jsonl",
        claude_dir / "300-malformed.jsonl",
        claude_dir / "400-incomplete.jsonl",
        codex,
        codex.parent / "200-other-root.jsonl",
        codex.parent / "300-child.jsonl",
        codex.parent / "400-malformed.jsonl",
        pi,
        pi_dir / "200-other-root.jsonl",
        pi_dir / "300-malformed.jsonl",
        pi_dir / "400-incomplete.jsonl",
    ), start=1):
        os.utime(path, (rank, rank))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", str(claude), None),
        ("codex", str(codex), None),
        ("pi", str(pi), None),
    ]


def test_pi_deep_first_record_is_skipped_by_discovery_and_latest_session(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    malformed = pi_dir / "200-deep.jsonl"
    write_jsonl(valid, [{
        "type": "session", "version": 3, "id": "valid", "cwd": str(root),
    }])
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text(
        "[" * 10_000 + "0" + "]" * 10_000 + "\n",
        encoding="utf-8",
    )
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]
    assert sessions.latest_session(str(root), provider="pi") == str(valid)


def test_oversized_pi_first_record_cannot_hide_valid_latest_session(
        tmp_path, monkeypatch):
    from quakepro.pi_model import PiModel, newest_pi_session, pi_header

    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    oversized = pi_dir / "200-oversized.jsonl"
    write_jsonl(valid, [{
        "type": "session", "version": 3, "id": "valid", "cwd": str(root),
    }])
    write_oversized_pi_header(oversized, root)
    os.utime(valid, (100, 100))
    os.utime(oversized, (200, 200))

    invalid = PiModel(str(oversized))
    assert invalid.poll() is False
    assert pi_header(str(oversized)) == {}
    assert newest_pi_session(str(root)) == str(valid)
    assert sessions.latest_session(str(root), provider="pi") == str(valid)
    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]


def test_pi_root_discovery_uses_shared_admission_reader(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "valid.jsonl"
    write_jsonl(valid, [{
        "type": "session", "version": 3, "id": "valid", "cwd": str(root),
    }])

    def forbidden_local_open(*_args, **_kwargs):
        raise AssertionError("sessions used local first-line reader")

    monkeypatch.setattr(sessions, "open", forbidden_local_open, raising=False)

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]


def test_sessions_for_root_skips_claude_candidate_with_list_type(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, _, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = claude_dir / "100-valid.jsonl"
    malformed = claude_dir / "200-malformed.jsonl"
    write_jsonl(valid, [{**claude_turn(), "cwd": str(root)}])
    write_jsonl(malformed, [{
        "type": [],
        "cwd": str(root),
        "message": {"role": "assistant", "content": []},
    }])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", str(valid), None),
        ("codex", None, None),
        ("pi", None, None),
    ]


def test_sessions_for_root_skips_codex_candidate_with_nul_cwd(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    day = codex_dir / "2026" / "08" / "04"
    valid = day / "100-valid.jsonl"
    malformed = day / "200-malformed.jsonl"
    write_jsonl(valid, [codex_meta("valid", cwd=str(root))])
    write_jsonl(malformed, [codex_meta("malformed", cwd="/invalid\x00cwd")])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", str(valid), None),
        ("pi", None, None),
    ]


def test_sessions_for_root_skips_codex_candidate_with_invalid_utf8_metadata(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    day = codex_dir / "2026" / "08" / "04"
    valid = day / "100-valid.jsonl"
    invalid = day / "200-invalid.jsonl"
    write_jsonl(valid, [codex_meta("valid", cwd=str(root))])
    encoded = (json.dumps(codex_meta("invalid", cwd=str(root))) + "\n").encode()
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(encoded.replace(b'"invalid"', b'"bad\xffid"'))
    os.utime(valid, (100, 100))
    os.utime(invalid, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", str(valid), None),
        ("pi", None, None),
    ]


def test_sessions_for_root_skips_pi_candidate_with_nul_cwd(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    malformed = pi_dir / "200-malformed.jsonl"
    write_jsonl(valid, [{"type": "session", "version": 3, "id": "valid", "cwd": str(root)}])
    write_jsonl(malformed, [
        {"type": "session", "version": 3, "id": "malformed", "cwd": "/invalid\x00cwd"},
    ])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]


def test_sessions_for_root_skips_codex_candidate_with_list_id(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    day = codex_dir / "2026" / "08" / "04"
    valid = day / "100-valid.jsonl"
    malformed = day / "200-malformed.jsonl"
    write_jsonl(valid, [codex_meta("valid", cwd=str(root))])
    malformed_meta = codex_meta("malformed", cwd=str(root))
    malformed_meta["payload"]["id"] = ["malformed"]
    malformed_meta["payload"].pop("session_id")
    write_jsonl(malformed, [malformed_meta])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", str(valid), None),
        ("pi", None, None),
    ]


def test_sessions_for_root_skips_codex_candidate_with_list_thread_source(
        tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    day = codex_dir / "2026" / "08" / "04"
    valid = day / "100-valid.jsonl"
    malformed = day / "200-malformed.jsonl"
    write_jsonl(valid, [codex_meta("good", cwd=str(root))])
    write_jsonl(malformed, [{
        "type": "session_meta",
        "payload": {
            "id": "bad",
            "session_id": "bad",
            "parent_thread_id": "good",
            "cwd": str(root),
            "thread_source": [],
            "source": "cli",
        },
    }])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", str(valid), None),
        ("pi", None, None),
    ]


def test_sessions_for_root_skips_pi_candidate_with_list_id(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    malformed = pi_dir / "200-malformed.jsonl"
    write_jsonl(valid, [{"type": "session", "version": 3, "id": "valid", "cwd": str(root)}])
    write_jsonl(malformed, [
        {"type": "session", "version": 3, "id": ["malformed"], "cwd": str(root)},
    ])
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]


@pytest.mark.parametrize(
    "case",
    ["missing-version", "string-version", "boolean-version"],
)
def test_sessions_for_root_uses_runtime_pi_header_version_schema(
        tmp_path, monkeypatch, case):
    from quakepro.pi_model import PiModel, pi_header

    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    older = pi_dir / "100-valid.jsonl"
    newest = pi_dir / "200-invalid-version.jsonl"
    valid_header = {
        "type": "session", "version": 3, "id": "valid", "cwd": str(root),
    }
    write_jsonl(older, [valid_header])
    invalid_header = {
        "type": "session", "version": 3, "id": "newest", "cwd": str(root),
    }
    if case == "missing-version":
        invalid_header.pop("version")
    elif case == "string-version":
        invalid_header["version"] = "3"
    else:
        invalid_header["version"] = True
    write_jsonl(newest, [invalid_header])
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))

    invalid = PiModel(str(newest))
    assert invalid.poll() is False
    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(older), None),
    ]

    write_jsonl(newest, [{
        "type": "session", "version": 3, "id": "recovered", "cwd": str(root),
    }])
    os.utime(newest, (300, 300))
    recovered = PiModel(str(newest))
    assert recovered.poll() is True
    assert (recovered.provider, recovered.path, recovered.cwd) == (
        "pi", str(newest), str(root),
    )
    assert pi_header(str(newest))["id"] == "recovered"
    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(newest), None),
    ]


@pytest.mark.parametrize("field", ["id", "parentSession"])
@pytest.mark.parametrize(
    ("length", "newest_is_valid"),
    [(65_536, True), (65_537, False)],
)
def test_sessions_for_root_enforces_pi_decoded_identity_limit(
        tmp_path, monkeypatch, field, length, newest_is_valid):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    older = pi_dir / "100-valid.jsonl"
    newest = pi_dir / "200-boundary.jsonl"
    write_jsonl(older, [
        {"type": "session", "version": 3, "id": "valid", "cwd": str(root)},
    ])
    header = {
        "type": "session",
        "version": 3,
        "id": "boundary",
        "cwd": str(root),
        field: "x" * length,
    }
    write_jsonl(newest, [header])
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))

    expected = newest if newest_is_valid else older
    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(expected), None),
    ]


def test_sessions_for_root_skips_pi_candidate_with_invalid_utf8(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    invalid = pi_dir / "200-invalid.jsonl"
    write_jsonl(valid, [
        {"type": "session", "version": 3, "id": "valid", "cwd": str(root)},
    ])
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(
        b'{"type":"session","version":3,"id":"bad\xff","cwd":'
        + json.dumps(str(root)).encode()
        + b"}\n"
    )
    os.utime(valid, (100, 100))
    os.utime(invalid, (200, 200))

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, None),
        ("pi", str(valid), None),
    ]


def test_sessions_for_root_skips_unreadable_candidate(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    codex_dir.mkdir(parents=True)
    pi_dir.mkdir(parents=True)
    valid = claude_dir / "valid.jsonl"
    unreadable = claude_dir / "unreadable.jsonl"
    write_jsonl(valid, [{**claude_turn(), "cwd": str(root)}])
    write_jsonl(unreadable, [{**claude_turn(), "cwd": str(root)}])
    os.utime(valid, (100, 100))
    os.utime(unreadable, (200, 200))
    real_open = builtins.open

    def guarded_open(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(str(unreadable)):
            raise PermissionError("candidate unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", str(valid), None),
        ("codex", None, None),
        ("pi", None, None),
    ]


@pytest.mark.parametrize("failed_provider", ["claude", "codex", "pi"])
def test_sessions_for_root_scopes_enumeration_error_to_one_provider(
        tmp_path, monkeypatch, failed_provider):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    for directory in (claude_dir, codex_dir, pi_dir):
        directory.mkdir(parents=True)
    blocked = {
        "claude": claude_dir,
        "codex": codex_dir,
        "pi": pi_dir,
    }[failed_provider]
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(str(blocked)):
            raise PermissionError("blocked " + failed_provider)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    discovery = sessions.sessions_for_root(str(root))

    assert provider_rows(discovery) == [
        (
            provider,
            None,
            ("PermissionError", "blocked " + provider) if provider == failed_provider else None,
        )
        for provider in ("claude", "codex", "pi")
    ]


def test_sessions_for_root_clips_enumeration_error_fields(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    for directory in (claude_dir, codex_dir, pi_dir):
        directory.mkdir(parents=True)
    error_class = type("E" * 140, (OSError,), {})
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(str(pi_dir)):
            raise error_class("m" * 2010)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    pi_result = sessions.sessions_for_root(str(root)).providers[2]

    assert pi_result.path is None
    assert pi_result.error.error_type == "E" * 128
    assert pi_result.error.message == "m" * 2000


def test_codex_recursive_enumeration_error_discards_partial_candidates(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    claude_dir, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    for directory in (claude_dir, pi_dir):
        directory.mkdir(parents=True)
    codex = codex_dir / "2026" / "08" / "04" / "rollout-valid.jsonl"
    write_jsonl(codex, [codex_meta("codex", cwd=str(root))])
    blocked = codex_dir / "2026" / "blocked"
    blocked.mkdir(parents=True)
    real_scandir = os.scandir

    def guarded_scandir(path):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(str(blocked)):
            raise PermissionError("blocked nested codex directory")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    assert provider_rows(sessions.sessions_for_root(str(root))) == [
        ("claude", None, None),
        ("codex", None, ("PermissionError", "blocked nested codex directory")),
        ("pi", None, None),
    ]


def test_root_discovery_rejects_missing_or_misordered_providers():
    healthy_claude = sessions.ProviderDiscovery("claude", None, None)
    healthy_pi = sessions.ProviderDiscovery("pi", None, None)

    with pytest.raises(TypeError):
        sessions.RootDiscovery("/work/root", (healthy_claude, healthy_pi, healthy_pi))


def test_discovery_observation_paths_honor_provider_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)

    assert sessions.discovery_observation_paths("/work/alpha") == frozenset({
        str(tmp_path / "claude" / "projects" / "-work-alpha"),
        str(tmp_path / "codex" / "sessions"),
        str(tmp_path / "pi" / "sessions" / "--work-alpha--"),
    })

    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi-override"))
    assert sessions.discovery_observation_paths("/work/alpha") == frozenset({
        str(tmp_path / "claude" / "projects" / "-work-alpha"),
        str(tmp_path / "codex" / "sessions"),
        str(tmp_path / "pi-override"),
    })


@pytest.mark.parametrize(
    "first_record",
    [
        pytest.param(
            {"type": "session_meta", "payload": {"id": 7}},
            id="invalid-codex-identity",
        ),
        pytest.param(
            {"type": "session", "version": 3, "id": "pi", "cwd": "/work"},
            id="foreign-provider",
        ),
        pytest.param(
            {"type": "event_msg", "payload": {}},
            id="non-session",
        ),
    ],
)
def test_codex_discovery_never_uses_later_metadata_after_first_record_denial(
    tmp_path, monkeypatch, first_record,
):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, _ = root_provider_dirs(tmp_path, monkeypatch, root)
    day = codex_dir / "2026" / "08" / "07"
    valid = day / "100-valid.jsonl"
    denied = day / "200-denied.jsonl"
    write_jsonl(valid, [codex_meta("valid", cwd=str(root))])
    write_jsonl(denied, [
        first_record,
        codex_meta("later", cwd=str(root)),
    ])
    os.utime(valid, (100, 100))
    os.utime(denied, (200, 200))

    discovery = sessions.sessions_for_root(str(root))

    assert discovery.providers[1].path == str(valid)
    assert sessions.latest_session(str(root), provider="codex") == str(valid)


def test_latest_codex_does_not_follow_linked_directory_cycle(
    tmp_path, monkeypatch,
):
    import quakepro.provider_admission as provider_admission

    root = tmp_path / "work"
    root.mkdir()
    _, codex_home = set_homes(monkeypatch, tmp_path)
    sessions_dir = codex_home / "sessions"
    valid = sessions_dir / "2026" / "08" / "07" / "rollout.jsonl"
    write_jsonl(valid, [codex_meta("root", cwd=str(root))])
    cycle = sessions_dir / "cycle"
    cycle.symlink_to(sessions_dir, target_is_directory=True)
    discovered = sessions.sessions_for_root(str(root)).providers[1].path
    real_reader = provider_admission.read_admission
    scanned = []

    def track_admission(path, *args, **kwargs):
        scanned.append(os.path.abspath(path))
        return real_reader(path, *args, **kwargs)

    monkeypatch.setattr(sessions, "read_admission", track_admission, raising=False)
    latest = sessions.latest_session(str(root), provider="codex")

    assert latest == discovered == str(valid)
    assert scanned == [os.path.abspath(valid)]


@pytest.mark.parametrize("api", ["latest", "root"])
def test_codex_discovery_ranks_with_admitted_stamp_after_candidate_vanishes(
    tmp_path, monkeypatch, api,
):
    import quakepro.provider_admission as provider_admission

    root = tmp_path / "work"
    root.mkdir()
    _, codex_home = set_homes(monkeypatch, tmp_path)
    path = (
        codex_home / "sessions" / "2026" / "08" / "07" / "rollout.jsonl"
    )
    write_jsonl(path, [codex_meta("root", cwd=str(root))])
    real_reader = provider_admission.read_admission

    def vanish_after_admission(candidate, provider):
        admitted = real_reader(candidate, provider)
        Path(candidate).unlink()
        return admitted

    monkeypatch.setattr(
        sessions, "read_admission", vanish_after_admission, raising=False,
    )

    if api == "latest":
        discovered = sessions.latest_session(str(root), provider="codex")
    else:
        discovered = sessions.sessions_for_root(str(root)).providers[1].path

    assert discovered == str(path)
    assert not path.exists()


@pytest.mark.parametrize("provider", ["codex", "pi"])
@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(128, True, id="depth-128"),
        pytest.param(129, False, id="depth-129"),
    ],
)
def test_provider_discovery_uses_shared_nesting_boundary(
    tmp_path, monkeypatch, provider, depth, accepted,
):
    root = tmp_path / "work"
    root.mkdir()
    _, codex_dir, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    directory = codex_dir / "2026" / "08" / "07" if provider == "codex" else pi_dir
    fallback = directory / "100-valid.jsonl"
    boundary = directory / "200-boundary.jsonl"
    if provider == "codex":
        fallback_record = codex_meta("fallback", cwd=str(root))
        boundary_record = codex_meta("boundary", cwd=str(root))
    else:
        fallback_record = {
            "type": "session", "version": 3, "id": "fallback", "cwd": str(root),
        }
        boundary_record = {
            "type": "session", "version": 3, "id": "boundary", "cwd": str(root),
        }
    boundary_record["future"] = nested_value(depth - 1)
    write_jsonl(fallback, [fallback_record])
    write_jsonl(boundary, [boundary_record])
    os.utime(fallback, (100, 100))
    os.utime(boundary, (200, 200))
    expected = str(boundary if accepted else fallback)

    discovery = sessions.sessions_for_root(str(root))

    provider_index = 1 if provider == "codex" else 2
    assert discovery.providers[provider_index].path == expected
    assert sessions.latest_session(str(root), provider=provider) == expected


def test_pi_file_symlink_cannot_win_any_discovery_api(tmp_path, monkeypatch):
    from quakepro.pi_model import newest_pi_session, pi_session_files

    root = tmp_path / "work"
    root.mkdir()
    _, _, pi_dir = root_provider_dirs(tmp_path, monkeypatch, root)
    valid = pi_dir / "100-valid.jsonl"
    external = tmp_path / "external" / "300-external.jsonl"
    linked = pi_dir / "300-linked.jsonl"
    header = {"type": "session", "version": 3, "id": "pi", "cwd": str(root)}
    write_jsonl(valid, [header])
    write_jsonl(external, [{**header, "id": "external"}])
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(external)
    os.utime(valid, (100, 100))
    os.utime(external, (300, 300))

    discovery = sessions.sessions_for_root(str(root))

    assert pi_session_files() == [str(valid)]
    assert newest_pi_session(str(root)) == str(valid)
    assert sessions.latest_session(str(root), provider="pi") == str(valid)
    assert discovery.providers[2].path == str(valid)
