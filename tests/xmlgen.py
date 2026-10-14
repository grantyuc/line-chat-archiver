"""產生和實機 LINE 結構相同的 uiautomator XML，給測試用。"""
from xml.sax.saxutils import quoteattr

PKG = "jp.naver.line.android"


def node(rid="", cls="android.widget.FrameLayout", bounds=(0, 0, 0, 0), text="", desc="", children=(), scrollable=False):
    rid_attr = f"{PKG}:id/{rid}" if rid else ""
    b = f"[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]"
    attrs = (f'index="0" text={quoteattr(text)} resource-id={quoteattr(rid_attr)} class={quoteattr(cls)} '
             f'package="{PKG}" content-desc={quoteattr(desc)} scrollable="{str(scrollable).lower()}" bounds="{b}"')
    inner = "".join(children)
    return f"<node {attrs}>{inner}</node>" if inner else f"<node {attrs} />"


def hierarchy(list_bounds, rows, width=1080, height=2340, overlays=()):
    lst = node("chathistory_message_list", "androidx.recyclerview.widget.RecyclerView", list_bounds,
               children=rows, scrollable=True)
    root = node("content", bounds=(0, 0, width, height), children=[lst, *overlays])
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            f'<hierarchy rotation="0">{root}</hierarchy>').encode()


def h12(hhmm):
    h, m = map(int, hhmm.split(":"))
    prefix = "上午" if h < 12 else "下午"
    return f"{prefix}{(h - 1) % 12 + 1}:{m:02d}"


def text_row(y1, y2, text, hhmm, self_side, sender="", width=1080):
    x1, x2 = (width - 300, width - 21) if self_side else (114, 500)
    kids = [node("chat_ui_row_text_message", "android.widget.LinearLayout", (x1, y1, x2, y2), desc=text,
                 children=[node("chat_ui_message_text", "android.widget.TextView", (x1 + 14, y1 + 13, x2 - 14, y2 - 13), text=text)])]
    ts = (x1 - 110, y2 - 48, x1 - 7, y2 - 11) if self_side else (x2 + 13, y2 - 48, x2 + 116, y2 - 11)
    kids.append(node("chat_ui_row_timestamp", "android.widget.TextView", ts, text=h12(hhmm)))
    if self_side:
        kids.append(node("chat_ui_row_read_count", "android.widget.TextView", (ts[0], ts[1] - 40, ts[2], ts[1] - 3), text="已讀"))
    elif sender:
        kids.append(node("chat_ui_row_thumbnail", "android.widget.ImageView", (21, y1, 100, y1 + 79), desc=f"{sender}的個人圖片"))
    return node("chat_ui_row_swipeable_framelayout", bounds=(0, y1, width, y2), children=kids)


def media_row(y1, y2, kind, hhmm, self_side, sender="", width=1080, x_span=(114, 567), boxes=None):
    x1, x2 = (width - 21 - (x_span[1] - x_span[0]), width - 21) if self_side else x_span
    if kind == "sticker":
        kids = [node("chat_ui_row_sticker_view", "android.widget.ImageView", (x1, y1, x2, y2), desc="貼圖")]
    elif kind == "photo":
        boxes = boxes or [(x1, y1, x2, y2)]
        kids = [node("chat_ui_row_image", "android.widget.ImageView", b, desc="夾帶照片") for b in boxes]
    else:
        kids = [node("chat_ui_row_video_thumbnail", "android.widget.ImageView", (x1, y1, x2, y2), desc="附加影片")]
    ts = (x1 - 110, y2 - 48, x1 - 7, y2 - 11) if self_side else (x2 + 13, y2 - 48, x2 + 116, y2 - 11)
    kids.append(node("chat_ui_row_timestamp", "android.widget.TextView", ts, text=h12(hhmm)))
    if self_side:
        kids.append(node("chat_ui_row_read_count", "android.widget.TextView", (ts[0], ts[1] - 40, ts[2], ts[1] - 3), text="已讀"))
    elif sender:
        kids.append(node("chat_ui_row_thumbnail", "android.widget.ImageView", (21, y1, 100, y1 + 79), desc=f"{sender}的個人圖片"))
    return node("chat_ui_row_swipeable_framelayout", bounds=(0, y1, width, y2), children=kids)


def date_row(y1, y2, label, width=1080):
    return node("chat_ui_row_date", bounds=(0, y1, width, y2),
                children=[node("chat_ui_date_indicator_text", "android.widget.TextView", (446, y1 + 10, 635, y2 - 10), text=label)])
