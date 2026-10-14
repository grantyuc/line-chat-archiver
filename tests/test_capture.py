import os

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
    assert all(not u.row.partial for u in uniques)
    assert all(s["method"] in ("start", "image") for s in steps)
    # 每步捲的距離依畫面而定，平均遠多於半個聊天區
    moving = [s["dy"] for s in steps[1:] if s["dy"]][:-1]     # 最後一次碰到頂端，只捲一部分
    view = 2143 - 278
    assert max(moving) <= int(view * 0.75) + 4
    assert sum(moving) / len(moving) >= view * 0.6
    assert cap.overshoot == 0 and set(cap.timing) >= {"拖曳", "dump", "截圖", "比對", "存檔"}
    assert os.path.exists(cap.image_path(0)) and cap.image_path(0).endswith(".jpg")


def test_fast_mode_shows_every_tall_photo_whole(tmp_path):
    _, items = make_chat(days=3, seed=9)
    photos = [it for it in items if it["kind"] == "photo"]
    assert len(photos) >= 4
    for k, it in enumerate(photos):
        it["height"] = (1500, 1200, 900, 700)[k % 4]            # 最高的占聊天區八成
    device = FakeChat(items, fast=True)
    cap, steps = run_capture(tmp_path, device)
    screens, offsets = load(cap, steps)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == [] and got(uniques) == expected(items)
    tall = [u for u in uniques if u.row.kind == "photo"]
    assert len(tall) == len(photos)
    assert all(not u.row.partial and u.row.bounds[1] >= 278 + 126 for u in tall)   # 也不在浮動日期條底下


def oldest(items):
    from types import SimpleNamespace as NS

    from archiver.capture import oldest_texts
    texts = oldest_texts([NS(kind=it["kind"], body=it["text"]) for it in items if it["kind"] != "date"])
    assert texts
    return texts


def test_drag_blocked_by_a_row_is_retried_elsewhere(tmp_path):
    _, items = make_chat(days=3, seed=4, albums=True)
    device = FakeChat(items, fast=True)
    device.sticky = ("album",)
    cap, steps = run_capture(tmp_path, device, oldest=oldest(items))
    assert steps[-1]["done"] and device.offset == 0
    screens, offsets = load(cap, steps)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == [] and len(uniques) == len(items)


def test_slow_loading_is_not_taken_for_the_top(tmp_path):
    _, items = make_chat(days=3)
    device = FakeChat(items, fast=True)
    device.wall, device.wall_left = device.offset // 2, 5          # 捲到一半要等 5 次才載入更舊的
    cap, steps = run_capture(tmp_path, device, oldest=oldest(items))
    assert steps[-1]["done"] and device.offset == 0
    screens, offsets = load(cap, steps)
    assert got(stitch(screens, offsets)[0]) == expected(items)


def test_stuck_capture_stops_without_done_and_resumes(tmp_path):
    _, items = make_chat(days=3)
    device = FakeChat(items, fast=True)
    device.wall = device.offset // 2                                # 永遠捲不過去
    cap, steps = run_capture(tmp_path, device, oldest=oldest(items))
    assert not steps[-1].get("done") and device.offset == device.wall
    device.wall = None                                              # 使用者處理好手機畫面
    cap, steps = run_capture(tmp_path, device, oldest=oldest(items))
    assert steps[-1]["done"] and device.offset == 0
    screens, offsets = load(cap, steps)
    assert got(stitch(screens, offsets)[0]) == expected(items)


def test_false_done_without_txt_is_rechecked_on_the_next_run(tmp_path):
    _, items = make_chat(days=3)
    device = FakeChat(items, fast=True)
    device.wall = device.offset // 2
    cap, steps = run_capture(tmp_path, device)                      # 沒有 txt 可以對照：誤判到頂
    assert steps[-1]["done"] and device.offset > 0
    device.wall = None
    cap, steps = run_capture(tmp_path, device)
    assert steps[-1]["done"] and device.offset == 0
    screens, offsets = load(cap, steps)
    assert got(stitch(screens, offsets)[0]) == expected(items)


def test_dump_offset_uses_the_uncut_edge_of_partial_rows():
    from archiver.capture import dump_offset
    from tests.xmlgen import hierarchy, text_row

    LIST = (0, 278, 1080, 2143)
    long_text = "這是一則很長很長的訊息"
    # 上一步：長訊息完整；這一步往下捲了 1100px，它的下緣被切掉，畫面上沒有任何一列兩邊都完整
    prev = parse_dump(hierarchy(LIST, [text_row(278, 900, "更早", "09:00", False),
                                       text_row(950, 1700, long_text, "09:01", False),
                                       text_row(1750, 2143, "OK", "09:02", True)]))
    cur = parse_dump(hierarchy(LIST, [text_row(278, 1500, "上面新出現的", "08:59", False),
                                      text_row(2050, 2143, long_text, "09:01", False)]))
    assert not any(not r.partial for r in cur.rows if r.text == long_text)
    assert dump_offset(prev, cur) == 1100

    # 只有一則常見的短訊息對得上時，不能當證據
    cur_ok = parse_dump(hierarchy(LIST, [text_row(278, 1500, "上面新出現的", "08:59", False),
                                         text_row(1600, 2143, "OK", "09:02", True)]))
    assert dump_offset(prev, cur_ok) is None
