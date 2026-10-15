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
