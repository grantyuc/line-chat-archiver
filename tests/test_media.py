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


def test_stale_cache_listing_is_refreshed_for_missed_photos(tmp_path):
    txt, items = make_chat(days=3, per_day=25, seed=11)
    chat = parse(txt)
    device = FakeChat(items)
    photos = [it for it in device.items if it["kind"] == "photo"]
    late = photos[len(photos) // 2]
    folder = f"{CHATS}/{CHAT_ID}/messages/{late['number']}"
    saved = {k: device.files.pop(k) for k in list(device.files) if k.startswith(folder)}
    work = tmp_path / "work"
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    pairs = align(chat.messages, [u.row for u in uniques])
    msg = [m for m, it in zip(chat.messages, [i for i in device.items if i["kind"] != "date"]) if it is late][0]

    first = resolve(str(work), chat, uniques, pairs, device=device, chat_dir=CHAT_ID, log=lambda *a: None)
    assert first["media"][str(msg.idx)][0]["source"] == "screenshot"
    device.files.update(saved)                 # 之後 LINE 才把這張下載到快取；本機的快取清單已經過時
    data = resolve(str(work), chat, uniques, pairs, device=device, chat_dir=CHAT_ID, log=lambda *a: None)
    assert data["media"][str(msg.idx)][0]["number"] == late["number"]


def test_chat_dir_found_even_when_the_oldest_photos_left_the_cache(tmp_path):
    txt, items = make_chat(days=8, per_day=25, seed=11)
    chat = parse(txt)
    device = FakeChat(items)
    photos = [it for it in device.items if it["kind"] == "photo"]
    for it in photos[:8]:                                   # 最早的 8 張（舊版只試這幾張）已經不在快取裡
        for suffix in (".thumb", "", ".original"):
            device.files.pop(f"{CHATS}/{CHAT_ID}/messages/{it['number']}{suffix}", None)
    assert len(photos) >= 12
    work = tmp_path / "work"
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    pairs = align(chat.messages, [u.row for u in uniques])
    data = resolve(str(work), chat, uniques, pairs, device=device, log=lambda *a: None)
    assert data["chat_dir"] == CHAT_ID
    assert data["stats"].get("photo:screenshot", 0) == 8


def capture_and_resolve(tmp_path, chat, device, **kw):
    work = tmp_path / "work"
    cap = Capture(device, str(work), idle_wait=0, log=lambda *a: None)
    steps = cap.run()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    uniques, _ = stitch(screens, [s["dy"] for s in steps])
    pairs = align(chat.messages, [u.row for u in uniques])
    return work, resolve(str(work), chat, uniques, pairs, device=device, log=lambda *a: None, **kw)


def test_voice_messages_get_their_audio_files(tmp_path):
    txt, items = make_chat(days=4, per_day=25, seed=13, voices=True)
    chat = parse(txt)
    device = FakeChat(items)
    voices = [it for it in device.items if it["kind"] == "voice"]
    assert len(voices) >= 6
    # 有兩則的語音檔已經不在快取裡：其餘的仍要對到正確的檔案（靠前後照片的編號和長度）
    for it in voices[1:3]:
        del device.files[f"{CHATS}/{CHAT_ID}/messages/voice_{it['number']}.aac"]
    work, data = capture_and_resolve(tmp_path, chat, device)
    msg_items = [it for it in device.items if it["kind"] != "date"]
    got = {}
    for msg, it in zip(chat.messages, msg_items):
        if it["kind"] == "voice":
            media = data["media"].get(str(msg.idx))
            got[it["number"]] = media[0]["number"] if media else None
    assert all(n == got_n for n, got_n in got.items() if got_n is not None)       # 對到的都對
    assert sum(1 for v in got.values() if v) >= len(voices) - 2
    played = next(m for m in data["media"].values() if m[0]["type"] == "audio")[0]
    assert os.path.exists(os.path.join(work, "site", played["src"]))


def test_video_and_voice_files_in_other_formats(tmp_path):
    from archiver.build import media_summary
    from tests.fakechat import m4a
    txt, items = make_chat(days=4, per_day=25, seed=2, voices=True)
    chat = parse(txt)
    device = FakeChat(items)
    videos = [it for it in device.items if it["kind"] == "video"]
    voices = [it for it in device.items if it["kind"] == "voice"]
    assert len(videos) >= 3 and len(voices) >= 3
    folder = f"{CHATS}/{CHAT_ID}/messages/"
    del device.files[folder + videos[0]["number"]]                                  # 只有縮圖
    device.files[folder + videos[1]["number"]] = b"\x00\x00\x00\x08wide" + bytes(2000)  # MOV：第一個區塊不是 ftyp
    device.files[folder + videos[2]["number"]] = b"LINEENC1" + bytes(2000)           # 認不得的格式
    device.files[f"{folder}voice_{voices[0]['number']}.aac"] = m4a(voices[0]["seconds"])  # 副檔名是 aac，其實是 M4A
    device.files[f"{folder}voice_{voices[1]['number']}.aac"] = b""                         # 沒下載到的語音
    work, data = capture_and_resolve(tmp_path, chat, device)
    got = {m[0].get("number"): m[0] for m in data["media"].values()}
    assert got[videos[0]["number"]]["type"] == "image" and got[videos[0]["number"]]["source"] == "thumb"
    assert got[videos[1]["number"]]["type"] == "video"
    assert got[videos[2]["number"]]["type"] == "image" and got[videos[2]["number"]]["unknown"][0][0] == "plain"
    voice = got[voices[0]["number"]]
    assert voice["src"].endswith(".m4a") and voice["format"] == "mp4" and voice["seconds"] == voices[0]["seconds"]
    assert voices[1]["number"] not in got
    assert all(got[it["number"]]["seconds"] == it["seconds"] for it in voices[2:] if it["number"] in got)
    report = "\n".join(media_summary(chat, data))
    assert "只有縮圖（不能播放） 2" in report and "認不得格式：1 則" in report and "LINEENC1".encode().hex(" ") in report
    assert "檔案是空的 1" in report and "mp4 1" in report


def test_audio_seconds_reads_adts_and_m4a(tmp_path):
    from archiver.media import audio_seconds
    from tests.fakechat import adts, m4a
    for name, data, want in (("a.aac", adts(12), 12), ("b.aac", m4a(7.5), 7.5), ("c.aac", b"", None)):
        path = tmp_path / name
        path.write_bytes(data)
        got = audio_seconds(str(path))
        assert (got is None) if want is None else abs(got - want) < 0.1


def test_assign_voices():
    from archiver.media import assign_voices
    # 則數和檔數一樣：依序
    assert assign_voices([3, 8], {}, [500, 520]) == {3: 500, 8: 520}
    # 前後照片的編號把語音分段：段內則數和檔數一樣就依序
    anchors = {1: 100, 5: 200, 9: 300}
    assert assign_voices([2, 3, 7], anchors, [110, 120, 250, 260]) == {2: 110, 3: 120}
    # 段內數量不同：用長度配對
    assert assign_voices([7], anchors, [250, 260], seconds={7: 30}, file_seconds={250: 5.0, 260: 30.4}) == {7: 260}


def test_voice_order_is_checked_against_lengths():
    from archiver.media import assign_voices
    # 數量一樣，但第一則的檔案不在、多了一個別的檔：長度不合就不照順序硬配
    got = assign_voices([3, 8], {}, [490, 520], seconds={3: 10, 8: 40}, file_seconds={490: 55.0, 520: 40.2})
    assert got == {8: 520}


def test_sticker_with_typed_text_uses_the_screenshot(tmp_path):
    txt, items = make_chat(days=3, per_day=25, seed=11)
    chat = parse(txt)
    stickers = [it for it in items if it["kind"] == "sticker"]
    stickers[1]["custom_text"] = "HELLO"                  # 這一張是訊息貼圖，上面有自己輸入的字
    device = FakeChat(items)
    work, data = capture_and_resolve(tmp_path, chat, device)
    msg_items = [it for it in device.items if it["kind"] != "date"]
    sources = {}
    for msg, it in zip(chat.messages, msg_items):
        if it["kind"] == "sticker":
            sources[bool(it.get("custom_text"))] = sources.get(bool(it.get("custom_text")), []) + \
                [data["media"][str(msg.idx)][0]["source"]]
    assert sources[True] == ["custom"]
    assert set(sources[False]) == {"cache"}


def test_animated_webp_is_detected(tmp_path):
    from PIL import Image

    from archiver.imgio import is_animated, sniff
    frames = [Image.new("RGB", (40, 30), (i * 40, 0, 0)) for i in range(3)]
    moving, still = str(tmp_path / "a.webp"), str(tmp_path / "s.webp")
    frames[0].save(moving, save_all=True, append_images=frames[1:], duration=100, loop=0)
    frames[0].save(still)
    assert sniff(moving) == sniff(still) == "webp"
    assert is_animated(moving, "webp") and not is_animated(still, "webp")
