"""模組 5：輸出 LINE 風格的靜態網站。

- 依月份分頁（YYYY-MM.html），避免單一頁面太大；訊息少的月份和相鄰月份合併成一頁
  （YYYY-MM_YYYY-MM.html）。首頁有分頁清單和全文搜尋。
- 文字都是原生 DOM 文字，可以反白、Ctrl+F；跨月份的搜尋用首頁的搜尋框。
- 搜尋資料存成 search-index.js 用 <script> 載入，直接用 file:// 開啟也能用。
- 照片用縮圖顯示（loading="lazy"），點開看大圖。
"""
import html
import json
import os
import re
import shutil
from collections import OrderedDict

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
WEEK = "一二三四五六日"
URL_RE = re.compile(r"https?://[^\s<>\"']+")
ICONS = {"file": "📄", "voice": "🎤", "contact": "👤", "location": "📍", "call": "📞", "other": "🎁",
         "album": "🖼", "note": "📝"}
LABELS = {"photo": "照片", "video": "影片", "sticker": "貼圖"}
PAGE_MIN = 300                      # 少於這麼多則的頁和相鄰的頁合併
PAGE_RE = re.compile(r"\d{4}-\d{2}(_\d{4}-\d{2})?\.html")


def linkify(text):
    out, last = [], 0
    for m in URL_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        out.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(url)}</a>')
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def weekday(date):
    import datetime
    y, m, d = map(int, date.split("-"))
    return WEEK[datetime.date(y, m, d).weekday()]


def group_months(sizes, page_min=PAGE_MIN):
    """把相鄰月份合併成頁。sizes 為 [(月份, 訊息數)]，依時間排序。
    反覆把最小的頁和較小的相鄰頁合併，直到每頁至少 page_min 則或只剩一頁。
    回傳 [[月份, …], …]。page_min=0 時每月一頁。"""
    pages = [[month] for month, _ in sizes]
    counts = [n for _, n in sizes]
    while len(pages) > 1:
        i = min(range(len(pages)), key=counts.__getitem__)
        if counts[i] >= page_min:
            break
        if i == 0:
            j = 1
        elif i == len(pages) - 1:
            j = i - 1
        else:
            j = i - 1 if counts[i - 1] <= counts[i + 1] else i + 1
        a, b = min(i, j), max(i, j)
        pages[a:b + 1] = [pages[a] + pages[b]]
        counts[a:b + 1] = [counts[a] + counts[b]]
    return pages


def page_label(months):
    return months[0] if len(months) == 1 else f"{months[0]}～{months[-1]}"


def page_name(months):
    return f"{months[0]}.html" if len(months) == 1 else f"{months[0]}_{months[-1]}.html"


def _media_html(msg, items):
    if not items:
        return f'<div class="missing">[{LABELS.get(msg.kind, msg.kind)}]</div>'
    parts = []
    for it in items:
        src = html.escape(it["src"])
        if it["type"] == "sticker":
            parts.append(f'<img class="sticker" loading="lazy" src="{src}" alt="貼圖">')
        elif it.get("anim") and it["type"] == "video":      # 被 LINE 轉成 MP4 的動圖：像 GIF 一樣自動循環播放
            parts.append(f'<video class="anim" muted loop playsinline preload="none" poster="{src}" '
                         f'src="{html.escape(it["full"])}"></video>')
        elif it.get("anim"):                                  # GIF／APNG：直接顯示原檔才會動
            full = html.escape(it["full"])
            parts.append(f'<a class="photo" href="{full}"><img loading="lazy" src="{full}" alt="動圖"></a>')
        elif it["type"] == "video" and it.get("full"):
            parts.append(f'<video controls preload="none" poster="{src}" src="{html.escape(it["full"])}"></video>')
        else:
            full = html.escape(it.get("full") or it["src"])
            parts.append(f'<a class="photo" href="{full}"><img loading="lazy" src="{src}" alt="{LABELS.get(msg.kind, "照片")}"></a>')
    return "".join(parts)


def _message_html(msg, media, avatar, show_head):
    side = "self" if msg.is_self else "other"
    t = f"<time>{msg.time}</time>"
    if msg.kind in ("unsend", "system"):
        text = msg.body if msg.sender == "" or msg.body.startswith(msg.sender) else f"{msg.sender} {msg.body}"
        return f'<div class="notice" id="m{msg.idx}"><span>{html.escape(text)}</span></div>'
    if msg.kind in ("sticker", "photo", "video"):
        content = _media_html(msg, media)
    else:
        icon = ICONS.get(msg.kind)
        attr = f' class="bubble icon" data-icon="{icon}"' if icon else ' class="bubble"'
        content = f"<div{attr}>{linkify(msg.body)}</div>"
    head = ""
    if side == "other":
        if show_head and avatar:
            head = f'<img class="avatar" loading="lazy" src="{html.escape(avatar)}" alt="">'
        else:
            head = '<span class="avatar blank"></span>'
    name = f'<div class="name">{html.escape(msg.sender)}</div>' if side == "other" and show_head else ""
    return (f'<div class="msg {side}" id="m{msg.idx}">{head}<div class="body">{name}'
            f'<div class="line">{content}{t}</div></div></div>')


def _page(title, body, extra_head=""):
    return ("<!doctype html>\n<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title><link rel=\"stylesheet\" href=\"assets/style.css\">{extra_head}"
            f"</head><body>{body}<script src=\"assets/app.js\"></script></body></html>\n")


def render(workdir, chat, media=None, notices=None, page_min=PAGE_MIN, log=print):
    """輸出到 <工作目錄>/site/。media 為 media.resolve 的結果（可為 None）；
    notices 為 {訊息索引: [置中通知文字]}，插在該則訊息之後（例如收回通知）；
    page_min：訊息少於這麼多則的月份和相鄰月份合併成一頁（0 為每月一頁）。"""
    site = os.path.join(workdir, "site")
    os.makedirs(os.path.join(site, "assets"), exist_ok=True)
    for name in os.listdir(site):                      # 上次輸出的分頁可能和這次分法不同
        if PAGE_RE.fullmatch(name):
            os.remove(os.path.join(site, name))
    for name in ("style.css", "app.js"):
        shutil.copyfile(os.path.join(TEMPLATES, name), os.path.join(site, "assets", name))
    media = media or {"media": {}, "avatars": {}}
    by_msg = media.get("media", {})
    avatars = media.get("avatars", {})
    title = chat.title or "LINE 聊天記錄"

    months = OrderedDict()
    for msg in chat.messages:
        months.setdefault(msg.date[:7], []).append(msg)
    groups = group_months([(m, len(v)) for m, v in months.items()], page_min)
    pages = [(page_name(g), page_label(g), [msg for m in g for msg in months[m]]) for g in groups]

    search = []
    for k, (name, label, msgs) in enumerate(pages):
        out, last_date, last_key = [], None, None
        for msg in msgs:
            if msg.date != last_date:
                y, m, d = msg.date.split("-")
                out.append(f'<div class="day"><span>{int(y)}/{int(m)}/{int(d)}（週{weekday(msg.date)}）</span></div>')
                last_date, last_key = msg.date, None
            key = (msg.sender, msg.is_self)
            show_head = key != last_key
            last_key = key if msg.kind not in ("unsend", "system") else None
            out.append(_message_html(msg, by_msg.get(str(msg.idx), []), avatars.get(msg.sender), show_head))
            for text in (notices or {}).get(msg.idx, []):
                out.append(f'<div class="notice"><span>{html.escape(text)}</span></div>')
                last_key = None
            if msg.kind == "text" or msg.kind in ICONS:
                search.append([name, msg.idx, msg.date, msg.time, msg.sender, msg.body])
        prev_link = f'<a href="{pages[k - 1][0]}">‹ {pages[k - 1][1]}</a>' if k > 0 else "<span></span>"
        next_link = f'<a href="{pages[k + 1][0]}">{pages[k + 1][1]} ›</a>' if k + 1 < len(pages) else "<span></span>"
        body = (f'<header class="bar"><a href="index.html">☰ 目錄</a><h1>{html.escape(title)}　{label}</h1>'
                f'<a href="index.html">🔍 搜尋</a></header>'
                f'<main class="chat">{"".join(out)}</main><nav class="pager">{prev_link}{next_link}</nav>')
        with open(os.path.join(site, name), "w", encoding="utf-8") as f:
            f.write(_page(f"{title} {label}", body))

    with open(os.path.join(site, "assets", "search-index.js"), "w", encoding="utf-8") as f:
        f.write("window.SEARCH_INDEX = " + json.dumps(search, ensure_ascii=False) + ";\n")

    cards = "".join(f'<a href="{name}">{label}<small>{len(msgs)} 則</small></a>' for name, label, msgs in pages)
    first, last = (chat.messages[0].date, chat.messages[-1].date) if chat.messages else ("", "")
    body = (f'<header class="bar"><h1>{html.escape(title)}</h1></header><main>'
            f'<p class="summary">{first} ～ {last}，共 {len(chat.messages)} 則訊息</p>'
            '<div class="search"><input id="q" type="search" placeholder="搜尋全部訊息" autocomplete="off"></div>'
            f'<div id="results" class="results"></div><div class="months">{cards}</div></main>')
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(_page(title, body, '<script src="assets/search-index.js"></script>'))
    log(f"網站輸出到 {os.path.join(site, 'index.html')}（{len(months)} 個月份，{len(pages)} 頁）")
    return site
