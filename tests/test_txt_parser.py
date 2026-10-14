from archiver.txt_parser import parse, to_24h

SAMPLE = (
    "﻿[LINE] 與小明的聊天記錄\r\n"
    "儲存日期：2026/10/14 下午5:27\r\n"
    "\r\n"
    "2021/3/26（週五）\r\n"
    "22:41\t小明\t未接來電\r\n"
    "22:41\t小明\t急找\r\n"
    "23:36\t我\t通話時間 0:10\r\n"
    "\r\n"
    "2021/4/24（週六）\r\n"
    "10:26\t小明\t早午餐推薦\r\n"
    "0960 000 000\r\n"
    "https://maps.app.goo.gl/abc\r\n"
    "14:52\t我\t[貼圖]\r\n"
    "14:53\t我\t第一段\r\n"
    "\r\n"
    "第二段\r\n"
    "19:08\t小明\t[] 100台灣台北市中正區某路1號 : https://maps.google.com/maps?q=25,121\r\n"
    "19:09\t小明\t[照片]\r\n"
    "22:50\t小明\t出租資訊：\thttps://example.com/x\r\n"
    "22:51\t\t小明已收回訊息\r\n"
    "22:52\t我\t[收到NT$ 80的轉帳。]\r\n"
    "\r\n"
    "2021/4/25（週日）\r\n"
    "08:00\t我\t2021/4/24（週六）\r\n"
    "\r\n"
)


def test_header_and_self():
    chat = parse(SAMPLE)
    assert chat.title == "小明"
    assert chat.partner == "小明"
    assert chat.saved_at == "2026/10/14 下午5:27"
    assert chat.me == "我"
    assert [m.is_self for m in chat.messages[:3]] == [False, False, True]


def test_dates_times_and_kinds():
    msgs = parse(SAMPLE).messages
    assert msgs[0].date == "2021-03-26" and msgs[0].time == "22:41"
    assert msgs[0].kind == "call"
    assert msgs[2].kind == "call"
    assert msgs[3].date == "2021-04-24"
    kinds = {m.body.split("\n")[0][:6]: m.kind for m in msgs}
    assert kinds["[貼圖]"] == "sticker"
    assert kinds["[照片]"] == "photo"
    assert kinds["[] 100"] == "location"
    assert kinds["小明已收回訊"] == "unsend"
    assert kinds["[收到NT$"] == "other"


def test_multiline_and_blank_lines():
    msgs = parse(SAMPLE).messages
    assert msgs[3].body == "早午餐推薦\n0960 000 000\nhttps://maps.app.goo.gl/abc"
    assert msgs[5].body == "第一段\n\n第二段"


def test_tab_inside_body_and_empty_sender():
    msgs = parse(SAMPLE).messages
    assert msgs[8].body == "出租資訊：\thttps://example.com/x"
    assert msgs[9].sender == "" and msgs[9].kind == "unsend"


def test_date_text_inside_message_is_not_a_date_line():
    msgs = parse(SAMPLE).messages
    last = msgs[-1]
    assert last.date == "2021-04-25"
    assert last.body == "2021/4/24（週六）"
    assert len(msgs) == 12


def test_twelve_hour_clock():
    assert to_24h("下午", "4", "04") == "16:04"
    assert to_24h("上午", "12", "05") == "00:05"
    assert to_24h("下午", "12", "30") == "12:30"
    assert to_24h(None, "9", "07") == "09:07"
