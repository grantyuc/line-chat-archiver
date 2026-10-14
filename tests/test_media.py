import os

import pytest

from archiver.align import align
from archiver.capture import Capture
from archiver.media import resolve
from archiver.screen import parse_dump
from archiver.stitch import stitch
from archiver.txt_parser import parse
from tests.chatgen import make_chat
from archiver.media import STICKERS as STICKERS_DIR
from tests.fakechat import CHAT_ID, FakeChat


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    txt, items = make_chat(days=3, per_day=25, seed=11)
    chat = parse(txt)
    device = FakeChat(items)
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    pairs = align(chat.messages, [u.row for u in uniques])
    return work, chat, items, device, uniques, pairs


def test_photos_videos_and_stickers_come_from_cache(pipeline):
    work, chat, items, device, uniques, pairs = pipeline
    data = resolve(str(work), chat, uniques, pairs, device=device, log=lambda *a: None)
    assert data["chat_dir"] == CHAT_ID
    msg_items = [it for it in device.items if it["kind"] != "date"]
    checked = 0
    for msg, it in zip(chat.messages, msg_items):
        if it["kind"] not in ("photo", "video", "sticker"):
            continue
        media = data["media"][str(msg.idx)]
        assert len(media) == 1
        m = media[0]
        if it["kind"] == "sticker":
            assert m["source"] == "cache", msg.idx
            assert m["src"].endswith(f"_{it['sticker_id']}.png")
        else:
            assert m["number"] == it["number"], msg.idx
            assert m["type"] == ("video" if it["kind"] == "video" else "image")
            expected = "original" if it["kind"] == "photo" and it["idx"] % 3 == 0 else "plain"
            assert m["source"] == expected
        assert os.path.exists(os.path.join(work, "site", m["src"]))
        checked += 1
    assert checked >= 10
    assert data["avatars"] and all(os.path.exists(os.path.join(work, "site", p)) for p in data["avatars"].values())


def test_without_device_media_are_screenshot_crops(pipeline, tmp_path):
    work, chat, items, device, uniques, pairs = pipeline
    data = resolve(str(work), chat, uniques, pairs, device=None, log=lambda *a: None)
    sources = {m["source"] for media in data["media"].values() for m in media}
    assert sources == {"screenshot"}


def test_album_row_is_shared_by_consecutive_photo_messages():
    from types import SimpleNamespace as NS

    from archiver.media import expand_albums
    msgs = [NS(kind="photo", sender="威廷", time="10:00"), NS(kind="photo", sender="威廷", time="10:00"),
            NS(kind="photo", sender="威廷", time="10:00"), NS(kind="photo", sender="威廷", time="10:05")]
    album = NS(row=NS(media=[(0, 0, 1, 1)] * 3))
    single = NS(row=NS(media=[(0, 0, 1, 1)]))
    pairs = [(0, 5.0), (None, 0), (None, 0), (1, 5.0)]
    assert expand_albums(msgs, [album, single], pairs) == [(0, 0), (0, 1), (0, 2), (1, 0)]


def test_sticker_cache_pulled_into_missing_dir_and_misplaced_packs_adopted(tmp_path):
    from archiver.media import StickerLibrary

    device = FakeChat(make_chat(days=1, per_day=10, seed=2)[1])
    fresh = tmp_path / "fresh" / "cache"            # 上層目錄也不存在
    lib = StickerLibrary(device, str(fresh), log=lambda *a: None)
    assert len(lib.entries) >= 40 and (fresh / "stickers" / "2000").is_dir()

    # 舊版留下的配置：貼圖組目錄直接在 cache 底下。應該直接搬，不再拉。
    old = tmp_path / "old" / "cache"
    old.mkdir(parents=True)
    device.pull(STICKERS_DIR, str(old / "x"))
    for pack in os.listdir(old / "x"):
        os.replace(old / "x" / pack, old / pack)
    os.rmdir(old / "x")

    class NoPull:
        def pull_dir(self, *a):
            raise AssertionError("不應該重新拉")

    lib2 = StickerLibrary(NoPull(), str(old), log=lambda *a: None)
    assert set(lib2.entries) == set(lib.entries)
    assert not any(d.isdigit() for d in os.listdir(old))
