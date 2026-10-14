"""把一次 uiautomator dump 轉成聊天列（Row）。

元件 id 來自實機測試 2（LINE 26.14，Android 14）：
- 聊天列表：chathistory_message_list；它的每個直接子節點是一列。
- 文字：chat_ui_message_text；時間：chat_ui_row_timestamp（下午4:04）
- 已讀：chat_ui_row_read_count（只有自己的訊息有）
- 頭像：chat_ui_row_thumbnail，描述「某某的個人圖片」
- 貼圖：chat_ui_row_sticker_view；照片：chat_ui_row_image；影片：chat_ui_row_video_thumbnail
- 日期條：chat_ui_date_indicator_text（9月2日 週三）
- 蓋在聊天區上的元件：公告列 chat_ui_announcement_*、捲到最新按鈕 chat_ui_scroll_to_bottom_button
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

LINE_PKG = "jp.naver.line.android"
LIST_ID = "chathistory_message_list"
TIME_RE = re.compile(r"(上午|下午|午前|午後)?\s?(\d{1,2}):(\d{2})")


@dataclass
class Row:
    kind: str                      # text/sticker/photo/video/date/unknown
    bounds: tuple                  # 整列在螢幕上的範圍 (x1, y1, x2, y2)
    text: str = ""                 # 訊息文字、日期條文字，或未知列的描述
    time: str = ""                 # HH:MM（24 小時制）
    side: str = ""                 # self / other / center
    sender: str = ""               # 頭像描述取出的名稱（只有一連串訊息的第一則有）
    content: tuple = ()            # 氣泡或媒體的範圍，用來判斷左右
    media: list = field(default_factory=list)   # 貼圖、照片（可多張）、影片的範圍
    avatar: tuple = ()
    date_label: str = ""           # 這一列上方的日期條（日期條也可能和訊息在同一列）
    partial: bool = False          # 有一部分在聊天區外
    ids: list = field(default_factory=list)     # 這一列出現過的元件 id（除錯用）

    def signature(self):
        """跨畫面判斷「是不是同一列」用的特徵。"""
        return (self.kind, self.time, self.side, self.text[:60], len(self.media))

    def to_dict(self):
        return asdict(self)


@dataclass
class Screen:
    width: int
    list_bounds: tuple             # 聊天區範圍
    overlays: list                 # 蓋在聊天區上的元件範圍
    rows: list
    has_line: bool


def _bounds(node):
    nums = list(map(int, re.findall(r"-?\d+", node.get("bounds", ""))))
    return tuple(nums) if len(nums) == 4 else (0, 0, 0, 0)


def _rid(node):
    return node.get("resource-id", "").split("/")[-1]


def to_24h(label):
    m = TIME_RE.search(label or "")
    if not m:
        return ""
    prefix, hour, minute = m.groups()
    hour = int(hour)
    if prefix in ("下午", "午後") and hour != 12:
        hour += 12
    elif prefix in ("上午", "午前") and hour == 12:
        hour = 0
    return f"{hour:02d}:{int(minute):02d}"


def _row(node, width, list_bounds):
    nodes = list(node.iter("node"))
    ids = sorted({_rid(n) for n in nodes if _rid(n)})
    row = Row(kind="unknown", bounds=_bounds(node), ids=ids)
    content = None
    descs = []
    for n in nodes:
        rid, b, desc, text = _rid(n), _bounds(n), n.get("content-desc", ""), n.get("text", "")
        if rid == "chat_ui_date_indicator_text":
            row.date_label = text
        elif rid == "chat_ui_message_text":
            row.kind, row.text = "text", text
        elif rid == "chat_ui_row_text_message" and content is None:
            content = b
        elif rid == "chat_ui_row_timestamp":
            row.time = to_24h(text)
        elif rid == "chat_ui_row_read_count":
            row.side = "self"
        elif rid == "chat_ui_row_thumbnail":
            row.avatar = b
            row.sender = desc.removesuffix("的個人圖片")
        elif rid == "chat_ui_row_sticker_view":
            row.kind, content = "sticker", b
            row.media.append(b)
        elif rid == "chat_ui_row_image" or (
                "照片" in desc and n.get("class", "").endswith("ImageView") and rid != "chat_ui_row_thumbnail"):
            if b not in row.media:
                row.kind = "photo"
                row.media.append(b)
                content = b if content is None else (min(content[0], b[0]), min(content[1], b[1]),
                                                     max(content[2], b[2]), max(content[3], b[3]))
        elif "video_thumbnail" in rid or (desc == "附加影片" and n.get("class", "").endswith("ImageView")):
            if b not in row.media:
                row.kind, content = "video", b
                row.media.append(b)
        elif desc and rid not in ("chat_ui_row_message_share_button", "chat_ui_reaction_list_react_button"):
            descs.append(desc)
        if text and row.kind == "unknown" and rid not in ("chat_ui_row_timestamp", "chat_ui_row_read_count"):
            descs.append(text)
    if row.kind == "unknown" and row.date_label and not row.time:
        row.kind, row.text, row.side = "date", row.date_label, "center"
    elif row.kind == "unknown":
        row.text = " / ".join(dict.fromkeys(d for d in descs if d != row.date_label))[:200]
        if not row.time and not row.avatar:
            row.side = "center"          # 置中的系統訊息（例如收回訊息）
    row.content = content or row.bounds
    if not row.side:
        # 自己的訊息靠右貼齊螢幕邊緣；對方的訊息從頭像右側開始
        row.side = "self" if row.content[2] >= width - 40 and row.content[0] > width * 0.15 else "other"
    top, bottom = list_bounds[1], list_bounds[3]
    row.partial = row.bounds[1] <= top + 1 or row.bounds[3] >= bottom - 1
    return row


def _row_nodes(parent):
    """列表的直接子節點通常就是一列；若某個子節點裡有多個時間或日期條，就往下拆。"""
    for child in parent.findall("node"):
        ids = [_rid(n) for n in child.iter("node")]
        several = ids.count("chat_ui_row_timestamp") > 1 or ids.count("chat_ui_date_indicator_text") > 1
        if several and child.findall("node"):
            yield from _row_nodes(child)
        else:
            yield child


def parse_dump(xml):
    """xml 為 dump 的位元組。回傳 Screen。"""
    root = ET.fromstring(xml)
    all_nodes = list(root.iter("node"))
    has_line = any(n.get("package") == LINE_PKG for n in all_nodes)
    width = max((_bounds(n)[2] for n in all_nodes), default=0)
    lst = next((n for n in all_nodes if _rid(n) == LIST_ID), None)
    if lst is None:
        return Screen(width, (0, 0, width, 0), [], [], has_line)
    list_bounds = _bounds(lst)
    overlays = [_bounds(n) for n in all_nodes
                if "announcement" in _rid(n) or _rid(n) == "chat_ui_scroll_to_bottom_button"]
    rows = [_row(node, width, list_bounds) for node in _row_nodes(lst)]
    rows = [r for r in rows if r.bounds[3] > r.bounds[1]]
    rows.sort(key=lambda r: r.bounds[1])
    return Screen(width, list_bounds, overlays, rows, has_line)
