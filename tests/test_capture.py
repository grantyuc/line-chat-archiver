from archiver.capture import Capture
from archiver.screen import parse_dump
from archiver.stitch import stitch
from tests.chatgen import make_chat
from tests.fakechat import FakeChat


def run_capture(workdir, device, **kw):
    cap = Capture(device, str(workdir), idle_wait=0, log=lambda *a: None, **kw)
    return cap, cap.run()


def load(cap, steps):
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    return screens, [s["dy"] for s in steps]


def expected(items):
    return [(it["kind"], it["text"] if it["kind"] in ("text", "date") else "", it.get("time", ""),
             "center" if it["kind"] == "date" else ("self" if it["self"] else "other")) for it in items]


def got(uniques):
    return [(u.row.kind, u.row.text if u.row.kind in ("text", "date") else "", u.row.time, u.row.side)
            for u in uniques]


def test_capture_and_stitch_recover_every_row(tmp_path):
    _, items = make_chat()
    device = FakeChat(items)
    cap, steps = run_capture(tmp_path, device)
    assert steps[-1]["done"]
    assert device.offset == 0
    screens, offsets = load(cap, steps)
    assert all(s["method"] in ("start", "image") for s in steps)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == []
    assert got(uniques) == expected(items)
    assert all(not u.row.partial for u in uniques)
    others = [u.row for u in uniques if u.row.side == "other"]
    assert all(r.sender == "威廷" for r in others)


def test_offsets_match_the_real_scroll(tmp_path):
    _, items = make_chat(days=2)
    device = FakeChat(items)
    cap, steps = run_capture(tmp_path, device)
    moving = [s["dy"] for s in steps[1:] if s["dy"]]
    # 模擬手機每步捲動固定距離（最後一步碰到頂端會比較短），估計值誤差應在 2px 內
    full = moving[:-1]
    assert full and max(full) - min(full) <= 4


def test_resume_after_interruption(tmp_path):
    _, items = make_chat()
    device = FakeChat(items)
    cap, steps = run_capture(tmp_path, device, max_steps=5)
    assert not steps[-1].get("done")
    cap2, steps2 = run_capture(tmp_path, device)
    assert steps2[-1]["done"]
    assert any(s["method"].startswith("resume") for s in steps2)
    screens, offsets = load(cap2, steps2)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == []
    assert got(uniques) == expected(items)


def test_fast_mode_drag_without_fling(tmp_path):
    _, items = make_chat(days=3)
    device = FakeChat(items, fast=True)
    cap, steps = run_capture(tmp_path, device)
    assert steps[-1]["done"] and device.offset == 0
    screens, offsets = load(cap, steps)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == []
    assert got(uniques) == expected(items)
    # 拖曳一半聊天區、沒有慣性：每步位移固定
    moving = [s["dy"] for s in steps[1:] if s["dy"]][:-1]     # 最後一次碰到頂端，只捲一部分
    assert max(moving) - min(moving) <= 4
    assert min(moving) >= int((2143 - 278) * 0.5) - 4
