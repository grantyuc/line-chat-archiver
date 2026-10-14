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

    rows = [Row("text", (0, 0, 1, 1), text="a"), Row("unknown", (0, 0, 1, 1), text="您已收回訊息", side="center"),
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
