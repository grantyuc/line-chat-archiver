from archiver.screen import parse_dump, to_24h
from tests.xmlgen import date_row, hierarchy, media_row, node, text_row

LIST = (0, 278, 1080, 2143)


def test_rows_kinds_sides_and_times():
    xml = hierarchy(LIST, [
        date_row(278, 340, "9月2日 週三"),
        text_row(360, 450, "在看到你", "20:24", False, sender="威廷"),
        text_row(470, 560, "真是棒", "20:59", True),
        media_row(600, 990, "sticker", "21:00", False),
        media_row(1010, 1850, "photo", "21:01", True, x_span=(671, 1059)),
        media_row(1870, 2143, "video", "21:02", False),
    ], overlays=[node("chat_ui_scroll_to_bottom_button", "android.widget.ImageView", (964, 2027, 1059, 2122))])
    screen = parse_dump(xml)
    assert screen.has_line and screen.width == 1080 and screen.list_bounds == LIST
    assert screen.overlays == [(964, 2027, 1059, 2122)]
    kinds = [r.kind for r in screen.rows]
    assert kinds == ["date", "text", "text", "sticker", "photo", "video"]
    date, other, mine, sticker, photo, video = screen.rows
    assert date.text == "9月2日 週三" and date.side == "center"
    assert other.text == "在看到你" and other.time == "20:24" and other.side == "other" and other.sender == "威廷"
    assert mine.side == "self" and mine.time == "20:59"
    assert sticker.side == "other" and sticker.media == [(114, 600, 567, 990)]
    assert photo.side == "self"
    assert date.partial and video.partial and not mine.partial


def test_album_row_has_several_photos():
    boxes = [(114, 600, 400, 886), (408, 600, 694, 886), (114, 894, 400, 1180)]
    screen = parse_dump(hierarchy(LIST, [media_row(600, 1180, "photo", "10:00", False, boxes=boxes)]))
    assert screen.rows[0].kind == "photo"
    assert screen.rows[0].media == boxes


def test_nested_container_is_split_into_rows():
    inner = [text_row(400, 490, "一", "09:00", False), text_row(500, 590, "二", "09:01", False)]
    wrapper = node("wrapper", bounds=(0, 400, 1080, 590), children=inner)
    screen = parse_dump(hierarchy(LIST, [wrapper]))
    assert [r.text for r in screen.rows] == ["一", "二"]


def test_unknown_row_keeps_descriptions():
    row = node("chat_ui_row_swipeable_framelayout", bounds=(0, 400, 1080, 520), children=[
        node("chat_ui_row_file_message", "android.widget.LinearLayout", (114, 400, 700, 520), desc="報告.pdf 1.2MB"),
        node("chat_ui_row_timestamp", "android.widget.TextView", (713, 470, 816, 507), text="下午3:15"),
    ])
    screen = parse_dump(hierarchy(LIST, [row]))
    assert screen.rows[0].kind == "unknown"
    assert screen.rows[0].text == "報告.pdf 1.2MB"
    assert screen.rows[0].time == "15:15"


def test_not_line_screen():
    xml = b"<?xml version='1.0' ?><hierarchy><node package=\"com.android.systemui\" bounds=\"[0,0][1080,2340]\" /></hierarchy>"
    screen = parse_dump(xml)
    assert not screen.has_line and screen.rows == []


def test_to_24h():
    assert to_24h("下午4:04") == "16:04"
    assert to_24h("上午12:30") == "00:30"
    assert to_24h("") == ""
