import os

import pytest

from archiver.align import align
from archiver.capture import Capture
from archiver.media import resolve
from archiver.screen import parse_dump
from archiver.stitch import stitch
from archiver.txt_parser import parse
from tests.chatgen import make_chat
from archiver.media import CHATS, STICKERS as STICKERS_DIR
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
            assert m["source"] == "cache", (msg.idx, it.get("animated"))
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
    album = NS(row=NS(media=[(0, 0, 1, 1)] * 3, more_media=False))
    single = NS(row=NS(media=[(0, 0, 1, 1)], more_media=False))
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


def test_old_sticker_index_is_rebuilt(tmp_path):
    import numpy as np

    from archiver.media import StickerLibrary

    device = FakeChat(make_chat(days=1, per_day=10, seed=2)[1])
    cache = tmp_path / "cache"
    StickerLibrary(device, str(cache), log=lambda *a: None)
    # 換成舊版格式的索引（沒有 version 欄位）
    np.savez(cache / "sticker_index.npz", ids=np.array(["1"]), bgr=np.zeros((1, 2)), alpha=np.zeros((1, 2)))
    lib = StickerLibrary(device, str(cache), log=lambda *a: None)
    assert len(lib.owner) >= 40
    assert "version" in np.load(cache / "sticker_index.npz").files


def test_carousel_album_visible_photos_matched_and_hidden_ones_inferred(tmp_path):
    txt, items = make_chat(days=3, per_day=20, seed=4, albums=True)
    chat = parse(txt)
    device = FakeChat(items)
    work = tmp_path / "work"
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    albums = [u.row for u in uniques if u.row.more_media]
    assert albums and all(r.kind == "photo" and len(r.media) == 2 for r in albums)
    pairs = align(chat.messages, [u.row for u in uniques])
    data = resolve(str(work), chat, uniques, pairs, device=device, log=lambda *a: None)

    expected = []                        # 每則訊息應該對到的快取編號
    for it in device.items:
        if it["kind"] == "date":
            continue
        if it["kind"] == "album":
            expected += it["numbers"]
        else:
            expected.append(it.get("number"))
    assert len(expected) == len(chat.messages)
    inferred = 0
    for msg, number in zip(chat.messages, expected):
        if msg.kind != "photo":
            continue
        media = data["media"].get(str(msg.idx))
        assert media and media[0].get("number") == number, (msg.idx, media)
        inferred += bool(media[0].get("inferred"))
    assert inferred == sum(it["count"] - 2 for it in device.items if it["kind"] == "album")


def test_infer_hidden():
    from archiver.media import infer_hidden
    numbers = [100, 101, 102, 103, 110, 120]
    assert infer_hidden(numbers, {100, 101}, 101) == 102
    assert infer_hidden(numbers, {100, 101, 102}, 101) == 103
    assert infer_hidden(numbers, {100, 101}, 101, before=102) is None    # 快取中缺了這張
    assert infer_hidden(numbers, set(), 120) is None


def test_photo_without_thumb_is_found_between_its_neighbours(tmp_path):
    txt, items = make_chat(days=3, per_day=25, seed=11)
    chat = parse(txt)
    device = FakeChat(items)
    photos = [it for it in device.items if it["kind"] == "photo"]
    assert len(photos) >= 3
    lost = photos[len(photos) // 2]
    del device.files[f"{CHATS}/{CHAT_ID}/messages/{lost['number']}.thumb"]       # 快取中只剩大圖
    work = tmp_path / "work"
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    pairs = align(chat.messages, [u.row for u in uniques])
    data = resolve(str(work), chat, uniques, pairs, device=device, chat_dir=CHAT_ID, log=lambda *a: None)

    msg = [m for m, it in zip(chat.messages, [i for i in device.items if i["kind"] != "date"]) if it is lost][0]
    media = data["media"][str(msg.idx)][0]
    assert media["number"] == lost["number"] and media["source"] in ("plain", "original")
    assert os.path.exists(os.path.join(work, "site", media["src"]))
    assert "photo:screenshot" not in data["stats"]


def test_media_summary_lists_screenshot_fallbacks():
    from types import SimpleNamespace as NS

    from archiver.build import media_summary
    msgs = [NS(idx=1, kind="photo", date="2026-01-30", time="13:58", sender="威廷"),
            NS(idx=2, kind="sticker", date="2026-02-01", time="09:00", sender="我"),
            NS(idx=3, kind="photo", date="2026-02-02", time="10:00", sender="我")]
    media = {"media": {"1": [{"source": "screenshot", "src": "media/crops/1_0.png",
                              "tried": [[1234, "thumb/plain", 0.41]]}],
                       "2": [{"source": "screenshot", "src": "media/crops/2_0.png"}],
                       "3": [{"source": "plain", "src": "media/photos/9_t.jpg"}]}}
    text = "\n".join(media_summary(NS(messages=msgs), media))
    assert "photo：共 2 則，快取 1、截圖裁切 1" in text
    assert "photo 2026-01-30 13:58 威廷：site/media/crops/1_0.png" in text
    assert "1234（thumb/plain）0.41" in text
    assert "sticker 2026-02-01 09:00 我：site/media/crops/2_0.png" in text
