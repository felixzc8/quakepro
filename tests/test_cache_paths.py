import json
import stat

from quakepro.sessions import open_model


def _source(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({
        "type": "session", "version": 3, "id": "cache-path-smoke",
        "cwd": str(tmp_path), "timestamp": "2026-10-02T12:00:00Z",
    }) + "\n")
    return path


def _settle(model):
    while model.poll():
        pass


def test_cache_base_symlink_never_redirects_writes(tmp_path, monkeypatch):
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mode = outside.stat().st_mode
    cache = tmp_path / "cache"
    cache.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache))

    model = open_model(str(source), "pi")
    _settle(model)

    assert model.cache_restored is False
    assert "main" in model.nodes
    assert not list(outside.iterdir())
    assert outside.stat().st_mode == original_mode


def test_cache_base_is_owner_only_before_writing(tmp_path, monkeypatch):
    source = _source(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o755)
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(cache))

    _settle(open_model(str(source), "pi"))

    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    snapshots = list((cache / "session-state-v3").glob("*.json"))
    assert snapshots
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshots)
    assert open_model(str(source), "pi").cache_restored is True
