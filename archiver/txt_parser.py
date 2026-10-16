"""解析 LINE 手機版「傳送聊天記錄」匯出的 txt（繁體中文介面）。

格式規則（實機測試 3 確認）：
- UTF-8 有 BOM、CRLF 換行；前兩行是 `[LINE] 與某某的聊天記錄` 和 `儲存日期：…`。
- 日期行：`2021/3/26（週五）`，月日不補零，括號為全形；前一行是空行。
- 訊息行：`時間<Tab>發話者<Tab>內容`，時間為 `22:41`（也接受 `下午10:41`）。
  內容本身可能含 Tab，所以只切前兩個。
- 多行訊息不加引號：第二行起沒有時間戳，訊息內部也可能有空行。
"""
import re
from dataclasses import asdict, dataclass, field

HEADER_RE = re.compile(r"^\[LINE\]\s*(與)?(.+?)的聊天記錄\s*$")
DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})\s*[（(](?:週|星期)?[日一二三四五六天][）)]\s*$")
MSG_RE = re.compile(r"^(上午|下午|午前|午後)?\s?(\d{1,2}):(\d{2})\t")

PLACEHOLDERS = {
    "[貼圖]": "sticker",
    "[照片]": "photo",
    "[影片]": "video",
    "[檔案]": "file",
    "[語音訊息]": "voice",
    "[聯絡資訊]": "contact",
    "[相簿]": "album",
    "[記事本]": "note",
}
CALL_RE = re.compile(r"^(☎\s*)?(通話時間\s*\d+:\d{2}(:\d{2})?|未接來電|無人接聽|您已結束通話|已取消通話|通話已取消)$")
UNSEND_RE = re.compile(r"已收回訊息$")
LOCATION_RE = re.compile(r"^\[\]\s.+\s:\shttps?://")
NOTE_RE = re.compile(r"已建立記事本。?$")
BRACKET_RE = re.compile(r"^\[[^\]]+\]$")


@dataclass
class Message:
    idx: int
    date: str            # YYYY-MM-DD
    time: str            # HH:MM（24 小時制）
    sender: str
    body: str
    kind: str = "text"   # text/sticker/photo/video/file/voice/contact/location/call/unsend/system/other…
    is_self: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Chat:
    title: str
    partner: str          # 一對一聊天的對方名稱；群組為空字串
    saved_at: str
    messages: list
    me: str = ""

    def to_dict(self):
        return {"title": self.title, "partner": self.partner, "saved_at": self.saved_at,
                "me": self.me, "messages": [m.to_dict() for m in self.messages]}

    @classmethod
    def from_dict(cls, data):
        msgs = [Message(**m) for m in data["messages"]]
        return cls(data["title"], data["partner"], data["saved_at"], msgs, data.get("me", ""))


def to_24h(prefix, hour, minute):
    hour = int(hour)
    if prefix in ("下午", "午後") and hour != 12:
        hour += 12
    elif prefix in ("上午", "午前") and hour == 12:
        hour = 0
    return f"{hour:02d}:{int(minute):02d}"


def classify(sender, body):
    text = body.strip()
    if text in PLACEHOLDERS:
        return PLACEHOLDERS[text]
    if not sender:
        return "unsend" if UNSEND_RE.search(text) else "system"
    if NOTE_RE.search(text) and "\n" not in text and len(text) <= 60:
        return "note"
    if UNSEND_RE.search(text) and "\n" not in text and len(text) <= 40:
        return "unsend"
    if CALL_RE.match(text):
        return "call"
    if LOCATION_RE.match(text):
        return "location"
    if BRACKET_RE.match(text):
        return "other"
    return "text"


def is_date_line(line):
    return DATE_RE.match(line) is not None


def parse(text, me=""):
    """把匯出檔的全文解析成 Chat。me 為自己的顯示名稱；一對一聊天可省略，會自動推斷。"""
    lines = text.lstrip("\ufeff").splitlines()
    title = partner = saved_at = ""
    start = 0
    if lines and (m := HEADER_RE.match(lines[0])):
        title = m.group(2)
        partner = m.group(2) if m.group(1) else ""
        start = 1
    if len(lines) > start and lines[start].startswith("儲存日期"):
        saved_at = lines[start].split("：", 1)[-1].strip()
        start += 1

    messages = []
    date = None
    for i in range(start, len(lines)):
        line = lines[i]
        prev_blank = i == start or not lines[i - 1].strip()
        if prev_blank and (d := DATE_RE.match(line)):
            date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
            continue
        m = MSG_RE.match(line)
        if m and date:
            parts = line.split("\t", 2)
            body = parts[2] if len(parts) == 3 else ""
            sender = parts[1] if len(parts) == 3 else ""
            messages.append(Message(len(messages), date, to_24h(*m.groups()), sender, body))
            continue
        if not messages:
            continue
        if not line.strip():
            # 空行：接著是日期行就是分隔，否則屬於多行訊息的一部分
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if i + 1 >= len(lines) or is_date_line(nxt):
                continue
            messages[-1].body += "\n"
            continue
        messages[-1].body += "\n" + line

    for msg in messages:
        msg.body = msg.body.rstrip("\n")
        msg.kind = classify(msg.sender, msg.body)

    chat = Chat(title, partner, saved_at, messages)
    set_me(chat, me)
    return chat


def set_me(chat, me=""):
    """決定哪個發話者是自己。一對一聊天：對方以外出現最多的發話者。"""
    if not me and chat.partner:
        counts = {}
        for msg in chat.messages:
            if msg.sender and msg.sender != chat.partner:
                counts[msg.sender] = counts.get(msg.sender, 0) + 1
        me = max(counts, key=counts.get) if counts else ""
    chat.me = me
    for msg in chat.messages:
        msg.is_self = bool(me) and msg.sender == me


def parse_file(path, me=""):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-16")
    return parse(text, me)
