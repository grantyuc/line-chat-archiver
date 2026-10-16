import os
import re

from archiver.build import build, save_chat
from archiver.capture import Capture
from archiver.txt_parser import parse
from tests.chatgen import make_chat
from tests.fakechat import FakeChat


def test_end_to_end_site(tmp_path):
    work = str(tmp_path / "waiting")
    txt, items = make_chat(days=3, per_day=20, seed=5, partner="威廷(Waiting)")
    chat = parse(txt)
    save_chat(work, chat)
    device = FakeChat([dict(it, sender="威廷(Waiting)") if not it.get("self") and it["kind"] != "date" else it
                       for it in items])
    Capture(device, work, idle_wait=0, log=lambda *a: None).run()
    site = build(work, device=device, log=lambda *a: None)

    pages = sorted(f for f in os.listdir(site) if re.fullmatch(r"\d{4}-\d{2}\.html", f))
    assert pages == ["2026-09.html"]
    page = open(os.path.join(site, pages[0]), encoding="utf-8").read()
    assert page.count('class="msg ') == len(chat.messages)
    for src in re.findall(r'(?:src|href)="(media/[^"]+)"', page):
        assert os.path.exists(os.path.join(site, src)), src
    assert "media/crops/" not in page                     # 所有媒體都從快取取得
    assert 'class="avatar" loading="lazy" src="media/avatars/' in page
    index = open(os.path.join(site, "index.html"), encoding="utf-8").read()
    assert "威廷(Waiting)" in index and 'id="q"' in index
    assert os.path.exists(os.path.join(site, "assets", "search-index.js"))
    report = open(os.path.join(work, "report.txt"), encoding="utf-8").read()
    for kind in ("text", "photo", "sticker"):
        total = sum(1 for m in chat.messages if m.kind == kind)
        assert f"{kind}：{total} / {total}" in report
    photos = sum(1 for m in chat.messages if m.kind == "photo")
    assert f"photo：共 {photos} 則，快取 {photos}" in report


def test_text_only_site_without_capture(tmp_path):
    work = str(tmp_path / "textonly")
    txt, _ = make_chat(days=2, per_day=5)
    save_chat(work, parse(txt))
    site = build(work, device=None, log=lambda *a: None)
    page = open(os.path.join(site, "2026-09.html"), encoding="utf-8").read()
    assert 'class="missing"' in page or "[照片]" in page or "[貼圖]" in page or 'class="bubble"' in page


def test_unsend_notice_goes_after_the_previous_matched_message(tmp_path):
    from archiver.build import previous_message, unsend_notices
    from archiver.render import render
    from archiver.screen import Row

    rows = [Row("text", (0, 0, 1, 1), text="a"), Row("system", (0, 0, 1, 1), text="您已收回訊息", side="center"),
            Row("text", (0, 0, 1, 1), text="b"), Row("unknown", (0, 0, 1, 1), text="以下為尚未閱讀的訊息", side="center")]
    pairs = [(0, 4.0), (2, 4.0)]
    notices = unsend_notices(rows, pairs, [1, 3])
    assert notices == [(1, "您已收回訊息")]
    assert previous_message(pairs, 1) == 0
    assert previous_message(pairs, 0) is None

    txt, _ = make_chat(days=1, per_day=3)
    chat = parse(txt)
    site = render(str(tmp_path), chat, notices={chat.messages[0].idx: ["您已收回訊息"]}, log=lambda *a: None)
    page = open(os.path.join(site, "2026-09.html"), encoding="utf-8").read()
    first = page.index(f'id="m{chat.messages[0].idx}"')
    notice = page.index("您已收回訊息")
    second = page.index(f'id="m{chat.messages[1].idx}"')
    assert first < notice < second


def test_group_months():
    from archiver.render import group_months
    sizes = [("2021-01", 5), ("2021-02", 800), ("2021-03", 40), ("2021-04", 30), ("2021-05", 600), ("2021-06", 3)]
    # 3 則併入 600 → 5 則併入 800 → 30 則和較小的鄰居 40 合併 → 70 則併入 603
    assert group_months(sizes, 300) == [["2021-01", "2021-02"], ["2021-03", "2021-04", "2021-05", "2021-06"]]
    assert group_months(sizes, 0) == [[m] for m, _ in sizes]
    assert group_months([("2021-01", 1), ("2021-02", 1)], 300) == [["2021-01", "2021-02"]]


def test_sparse_months_share_a_page(tmp_path):
    from archiver.render import render
    lines = ["[LINE] 與威廷的聊天記錄", "儲存日期：2026/10/14 下午5:27", ""]
    for month, count in ((1, 2), (2, 3), (3, 400), (4, 1)):
        lines.append(f"2026/{month}/2（週一）")
        lines += [f"10:{i % 60:02d}\t威廷\t{month}月第{i}則" for i in range(count)]
        lines.append("")
    chat = parse("\r\n".join(lines))
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "2026-01.html").write_text("舊的分頁")
    site = render(str(tmp_path), chat, log=lambda *a: None)

    pages = sorted(f for f in os.listdir(site) if f.endswith(".html") and f != "index.html")
    assert pages == ["2026-01_2026-04.html"]          # 舊的 2026-01.html 已移除
    index = open(os.path.join(site, "index.html"), encoding="utf-8").read()
    for name in pages:
        assert f'href="{name}"' in index
    search = open(os.path.join(site, "assets", "search-index.js"), encoding="utf-8").read()
    assert "2026-01.html" not in search and all(name in search for name in pages)
    page = open(os.path.join(site, pages[0]), encoding="utf-8").read()
    assert "2026/1/2" in page and "1月第0則" in page


def test_report_counts_album_photos_as_matched(tmp_path):
    work = str(tmp_path / "albums")
    txt, items = make_chat(days=3, per_day=20, seed=4, albums=True)
    chat = parse(txt)
    save_chat(work, chat)
    device = FakeChat(items)
    Capture(device, work, idle_wait=0, log=lambda *a: None).run()
    build(work, device=device, log=lambda *a: None)
    report = open(os.path.join(work, "report.txt"), encoding="utf-8").read()
    photos = sum(1 for m in chat.messages if m.kind == "photo")
    shared = sum(it["count"] - 1 for it in device.items if it["kind"] == "album")
    assert f"photo：{photos} / {photos}（其中 {shared} 則和同一批照片共用畫面上的一列）" in report
    assert "沒對到畫面的訊息：0 則" in report


def split_at_day(items, day):
    """items 中第 day 個日期條（從 0 算）之前的部分。"""
    dates = [i for i, it in enumerate(items) if it["kind"] == "date"]
    return items[:dates[day]]


def test_update_capture_joins_the_previous_capture(tmp_path):
    from archiver.capture import next_update
    work = str(tmp_path / "update")
    txt, items = make_chat(days=4, per_day=20, seed=8)
    chat = parse(txt)
    save_chat(work, chat)
    old = FakeChat(split_at_day(items, 3), fast=True)                  # 第一次擷取時只有前三天
    newest = old.offset                                                 # 第一次擷取的第一張畫面的位置
    Capture(old, work, idle_wait=0, log=lambda *a: None).run()

    device = FakeChat(items, fast=True)                                 # 之後多了第四天的訊息
    session, join = next_update(work)
    assert session == "001"
    logs = []
    Capture(device, work, idle_wait=0, session=session, join=join, max_steps=1, log=logs.append).run()
    session2, join2 = next_update(work)                                 # 中斷了：接續同一段
    assert session2 == "001"
    steps = Capture(device, work, idle_wait=0, session=session2, join=join2, log=logs.append).run()
    assert steps[-1].get("join_dy") is not None and device.offset > 0   # 接上就停，沒有一路捲到頂
    # 每天的時間都一樣（不同天同一時間的貼圖、照片），不能因此接錯位置
    assert abs(steps[-1]["join_dy"] - (device.offset - newest)) <= 2
    assert next_update(work)[0] == "002"

    build(work, device=device, log=lambda *a: None)
    report = open(os.path.join(work, "report.txt"), encoding="utf-8").read()
    for kind in ("text", "photo", "sticker"):
        total = sum(1 for m in chat.messages if m.kind == kind)
        assert f"{kind}：{total} / {total}" in report, report
    assert "更新擷取 001：" in report and "已接上上一段" in report
    assert "沒對到畫面的訊息：0 則" in report


def test_update_with_few_new_messages_joins_on_the_first_frame(tmp_path):
    from archiver.capture import next_update
    work = str(tmp_path / "few")
    txt, items = make_chat(days=2, per_day=20, seed=3)
    save_chat(work, parse(txt))
    Capture(FakeChat(items[:-2], fast=True), work, idle_wait=0, log=lambda *a: None).run()
    session, join = next_update(work)
    steps = Capture(FakeChat(items, fast=True), work, idle_wait=0, session=session, join=join,
                    log=lambda *a: None).run()
    assert len(steps) == 1 and steps[0]["join_dy"] is not None


def test_unknown_offsets_are_recovered_at_build(tmp_path, monkeypatch):
    import archiver.build as B
    from archiver.build import load_capture, recover_offsets
    from archiver.stitch import stitch
    work = str(tmp_path / "recover")
    txt, items = make_chat(days=3, per_day=20, seed=5)
    chat = parse(txt)
    save_chat(work, chat)
    device = FakeChat(items, fast=True)
    Capture(device, work, idle_wait=0, log=lambda *a: None).run()
    steps, screens, paths, _ = load_capture(work)
    truth = [s["dy"] for s in steps]
    broken = [3, 4, 7]
    for k in broken:                                # 擷取時判定不了的步
        steps[k]["dy"] = None
        steps[k].pop("expect", None)                # 舊版擷取沒有記下預定距離
    offsets, notes = recover_offsets(steps, screens, paths)
    assert all(abs(offsets[k] - truth[k]) <= 2 for k in broken), ([offsets[k] for k in broken], notes)
    uniques, warnings = stitch(screens, offsets)
    assert warnings == [] and len(uniques) == len(items)

    # dump 和截圖都比不出來時，用估計值：誤差要在去重的容許範圍內
    monkeypatch.setattr(B, "dump_offset", lambda *a, **k: None)
    offsets, notes = recover_offsets(steps, screens, ["/沒有這個檔案"] * len(paths))
    assert {h for _, _, h in notes} == {"估計"}
    assert all(abs(offsets[k] - truth[k]) <= 20 for k in broken)


def test_nearby_match_accepts_a_clear_best_below_the_full_threshold():
    from archiver.media import PhotoLibrary
    lib = object.__new__(PhotoLibrary)
    lib.entries = {str(n): {"dir": "u1", "thumb": ("x", 1)} for n in (10, 11, 12)}
    lib.thumb_path = lambda n: __file__                          # 存在的檔案即可
    scores = {"10": 0.0, "11": 0.81, "12": 0.05}                # 影片縮圖：蓋著播放鍵，只到 0.8 左右
    lib.verify = lambda image, path, _s=iter(["10", "11", "12"]): scores[next(_s)]
    number, tried = lib.match_nearby(None, "u1", 9, 13, set())
    assert number == "11"
    scores.update({"10": 0.78, "11": 0.81})                     # 兩張都接近：分不出來，不採用
    lib.verify = lambda image, path, _s=iter(["10", "11", "12"]): scores[next(_s)]
    assert lib.match_nearby(None, "u1", 9, 13, set())[0] is None


def test_emoji_only_and_note_messages_align():
    from archiver.align import align
    from archiver.screen import Row
    txt = "\r\n".join(["[LINE] 與C的聊天記錄", "儲存日期：2026/10/14 下午5:27", "",
                       "2025/3/20（週四）", "20:17\tC\t早", "20:18\tC\tC已建立記事本。", "23:39\tC\t(笑)",
                       "23:40\t我\t(smirk Moon)", "23:41\tC\t晚", ""])
    chat = parse(txt, me="我")
    assert [m.kind for m in chat.messages] == ["text", "note", "text", "text", "text"]
    rows = [Row("text", (0, 0, 1, 1), text="早", time="20:17", side="other"),
            Row("unknown", (0, 0, 1, 1), text="Double tap to open / 03/23 分享稿", time="20:18", side="other"),
            Row("unknown", (0, 0, 1, 1), time="23:39", side="other"),
            Row("unknown", (0, 0, 1, 1), time="23:40", side="self"),
            Row("text", (0, 0, 1, 1), text="晚", time="23:41", side="other")]
    assert [j for j, _ in align(chat.messages, rows)] == [0, 1, 2, 3, 4]


def test_animated_photos_play_in_the_page(tmp_path):
    from archiver.render import render
    txt, _ = make_chat(days=1, per_day=3)
    chat = parse(txt)
    m1, m2 = chat.messages[0], chat.messages[1]
    m1.kind = m2.kind = "photo"
    media = {"media": {str(m1.idx): [{"type": "image", "src": "media/photos/1_t.jpg", "full": "media/photos/1.gif",
                                      "source": "plain", "anim": True}],
                       str(m2.idx): [{"type": "video", "src": "media/photos/2_t.jpg", "full": "media/photos/2.mp4",
                                      "source": "plain", "anim": True}]}, "avatars": {}}
    site = render(str(tmp_path), chat, media, log=lambda *a: None)
    page = open(os.path.join(site, "2026-09.html"), encoding="utf-8").read()
    assert '<img loading="lazy" src="media/photos/1.gif"' in page
    assert '<video class="anim" muted loop playsinline' in page and "controls" not in page


def test_voice_message_has_a_player(tmp_path):
    from archiver.render import render
    txt, _ = make_chat(days=1, per_day=3)
    chat = parse(txt)
    m = chat.messages[0]
    m.kind = "voice"
    v = chat.messages[1]
    v.kind = "video"
    media = {"media": {str(m.idx): [{"type": "audio", "src": "media/voice/1.aac", "source": "cache", "seconds": 67}],
                       str(v.idx): [{"type": "image", "src": "media/photos/2_t.jpg", "full": "media/photos/2.jpg",
                                     "source": "thumb", "number": "2"}]}, "avatars": {}}
    site = render(str(tmp_path), chat, media, log=lambda *a: None)
    page = open(os.path.join(site, "2026-09.html"), encoding="utf-8").read()
    # 按播放前就要看得到長度（preload="none" 時播放器顯示 0:00）
    assert '<audio controls preload="metadata" src="media/voice/1.aac"></audio><span class="length">1:07</span>' in page
    # 手機上沒有影片檔：縮圖標明無法播放
    assert 'class="photo video-still" href="media/photos/2.jpg"' in page


def test_site_uses_colors_from_the_screenshots_and_rechecks_kinds(tmp_path):
    import json
    import subprocess
    import sys
    work = str(tmp_path / "theme")
    txt, items = make_chat(days=2, per_day=20, seed=5)
    chat = parse(txt)
    chat.messages[3].body, chat.messages[3].kind = "威廷已建立記事本。", "text"     # 舊版 parse 存成 text
    save_chat(work, chat)
    device = FakeChat(items, fast=True)
    Capture(device, work, idle_wait=0, log=lambda *a: None).run()
    site = build(work, device=device, log=lambda *a: None)
    css = open(os.path.join(site, "assets", "theme.css"), encoding="utf-8").read()
    # 模擬手機：背景 BGR (231,236,240)、自己的泡泡 (140,230,150)、對方的泡泡白色（截圖是 JPEG，容許一點誤差）
    import re
    colors = dict(re.findall(r"(--[a-z-]+): #([0-9a-f]{6})", css))
    near = lambda name, rgb: all(abs(int(colors[name][i * 2:i * 2 + 2], 16) - c) <= 4 for i, c in enumerate(rgb))
    assert near("--bg", (240, 236, 231)) and near("--self", (150, 230, 140)) and near("--other", (255, 255, 255))
    assert 'data-theme="dark"' in css                      # 只套用在淺色模式
    page = open(os.path.join(site, "2026-09.html"), encoding="utf-8").read()
    assert 'href="assets/theme.css"' in page and 'class="theme"' in page and 'id="d2026-09-01"' in page
    assert "window.DAY_INDEX" in open(os.path.join(site, "assets", "search-index.js"), encoding="utf-8").read()
    aligned = {a["msg"]: a for a in json.load(open(os.path.join(work, "alignment.json"), encoding="utf-8"))}
    assert all(a["xml"] and os.path.exists(os.path.join(work, a["xml"])) for a in aligned.values() if a["row"] is not None)

    word = next(m.body for m in chat.messages if m.body.startswith("第"))
    out = subprocess.run([sys.executable, "main.py", "find", word, "--work", work],
                         capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__))).stdout
    assert "main.py inspect" in out and "capture" in out


def test_line_emoji_combo_stickers_and_reactions(tmp_path):
    import json
    import re
    work = str(tmp_path / "extras")
    txt, items = make_chat(days=4, per_day=25, seed=21, extras=True)
    chat = parse(txt)
    kinds = {it["kind"] for it in items}
    assert {"emoji", "combo"} <= kinds and any(it.get("reaction") for it in items)
    save_chat(work, chat)
    device = FakeChat(items, fast=True)
    Capture(device, work, idle_wait=0, log=lambda *a: None).run()
    site = build(work, device=device, log=lambda *a: None)
    report = open(os.path.join(work, "report.txt"), encoding="utf-8").read()
    assert "沒對到畫面的訊息：0 則" in report, report[:1500]
    media = json.load(open(os.path.join(work, "media.json"), encoding="utf-8"))
    assert set(media["emoji"]) == {"(hahaha)", "(笑)"}
    msg_items = [it for it in device.items if it["kind"] != "date"]
    for msg, it in zip(chat.messages, msg_items):
        got = media["media"].get(str(msg.idx))
        if it["kind"] == "combo":
            assert got[0]["source"] == "combo"
        if it["kind"] == "emoji":
            assert [g["name"] for g in got] == re.findall(r"\([^()]+\)", it["text"])
        if it.get("reaction"):
            assert [r["name"] for r in media["reactions"][str(msg.idx)]] == [it["reaction"]]
        else:
            assert str(msg.idx) not in media["reactions"]
    page = "".join(open(os.path.join(site, f), encoding="utf-8").read() for f in os.listdir(site) if f.startswith("2026"))
    assert '<div class="bubble">' in page and 'class="emoji" src="media/emoji/' in page    # 文字裡的 (hahaha) 換成圖
    assert '<div class="emoji-only">' in page and '<div class="reactions">' in page
