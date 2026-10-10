import os

from agent.reload import ConfigWatcher


async def test_watcher_calls_back_on_change_and_survives_errors(tmp_path):
    good, bad = tmp_path / "a.yaml", tmp_path / "b.yaml"
    good.write_text("x: 1")
    bad.write_text("y: 1")
    calls = []

    async def on_good():
        calls.append("good")

    async def on_bad():
        raise ValueError("broken yaml")

    w = ConfigWatcher()
    w.watch(good, on_good)
    w.watch(bad, on_bad)
    assert await w.check() == []

    for p in (good, bad):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert set(await w.check()) == {good, bad}  # a failing reload doesn't stop the others
    assert calls == ["good"]
    assert await w.check() == []
