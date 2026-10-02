import asyncio
import json

from quakepro.app import QuakePro
from quakepro.sessions import open_model


def test_native_events_refresh_tui_after_complete_record(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    path = tmp_path / "session.jsonl"

    def message(entry_id, text):
        return {
            "type": "message", "id": entry_id, "parentId": None,
            "timestamp": "2026-10-02T12:00:01Z",
            "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": text}],
                "timestamp": 1_784_000_000_000,
            },
        }

    path.write_text(
        json.dumps({
            "type": "session", "version": 3, "id": "native-smoke",
            "cwd": str(tmp_path), "timestamp": "2026-10-02T12:00:00Z",
        }) + "\n" + json.dumps(message("initial", "initial text")) + "\n"
    )

    async def exercise():
        app = QuakePro(str(path), model=open_model(str(path), "pi"))
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.25)
            assert any(
                step.body == "initial text" for step in app.model.nodes["main"].steps
            )
            record = json.dumps(message("appended", "native update")) + "\n"
            split = len(record) // 2
            with path.open("a") as stream:
                stream.write(record[:split])
            await pilot.pause(0.35)
            assert not any(
                step.body == "native update" for step in app.model.nodes["main"].steps
            )

            with path.open("a") as stream:
                stream.write(record[split:])
            expected = path.read_bytes()
            before = path.stat()

            async def await_update():
                while not any(
                    step.body == "native update"
                    for step in app.model.nodes["main"].steps
                ):
                    assert not app._observation_error
                    await pilot.pause(0.05)

            await asyncio.wait_for(await_update(), timeout=8)
            assert not app._observation_error
            await pilot.press("q")

        after = path.stat()
        assert path.read_bytes() == expected
        assert after.st_ino == before.st_ino
        assert after.st_mode == before.st_mode
        assert after.st_mtime_ns == before.st_mtime_ns

    asyncio.run(exercise())
