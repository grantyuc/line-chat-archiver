"""從截圖取樣聊天室的配色（聊天背景、自己和對方的泡泡），網頁的淺色模式就用這些顏色，看起來和手機上一樣。"""
import numpy as np

from .imgio import imread


def _hex(color):
    b, g, r = (int(round(c)) for c in color)
    return f"#{r:02x}{g:02x}{b:02x}"


def _luma(color):
    b, g, r = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def sample_theme(screens, paths, samples=30):
    """回傳 CSS 變數 {名稱: 顏色}；取樣不到的省略，全部取樣不到時回傳 {}。

    - 背景：聊天區最右緣的一條（自己的泡泡離右緣還有一段距離），避開底端的按鈕。
    - 泡泡：完整的文字訊息，泡泡內側邊緣的一小塊（文字從更裡面才開始）。外側邊緣有 14px 是放尾巴的
      地方，泡泡中段那裡是透明的（看到的是背景），所以對方的泡泡取右緣、自己的泡泡取左緣。
    """
    picks = sorted({round(i * (len(paths) - 1) / max(1, samples - 1)) for i in range(samples)}) if paths else []
    bg, mine, theirs = [], [], []
    for k in picks:
        img = imread(paths[k])
        if img is None:
            continue
        screen = screens[k]
        h, w = img.shape[:2]
        top, bottom = screen.list_bounds[1], screen.list_bounds[3]
        strip = img[top + 150:max(top + 151, bottom - 300), max(0, w - 14):max(1, w - 6)]
        if strip.size:
            bg.append(np.median(strip.reshape(-1, 3), axis=0))
        for r in screen.rows:
            if r.kind != "text" or r.partial or r.side not in ("self", "other"):
                continue
            x1, y1, x2, y2 = r.content
            mid = (y1 + y2) // 2
            inner = (x1 + 4, x1 + 12) if r.side == "self" else (x2 - 12, x2 - 4)
            patch = img[max(0, mid - 6):mid + 6, inner[0]:inner[1]]
            if patch.size and x2 - x1 > 40 and y2 - y1 > 40:
                (mine if r.side == "self" else theirs).append(np.median(patch.reshape(-1, 3), axis=0))
    theme = {}
    if bg:
        color = np.median(bg, axis=0)
        theme["--bg"] = _hex(color)
        theme["--meta-text"] = "#24324a" if _luma(color) > 140 else "#e8eef8"     # 時間、日期的字色
    if mine:
        theme["--self"] = _hex(np.median(mine, axis=0))
    if theirs:
        color = np.median(theirs, axis=0)
        theme["--other"] = _hex(color)
        theme["--text"] = "#111111" if _luma(color) > 140 else "#eef1f6"
    return theme


def theme_css(theme):
    """淺色模式（沒有切到深色時）套用截圖的配色。"""
    if not theme:
        return ""
    body = " ".join(f"{k}: {v};" for k, v in theme.items())
    return f':root:not([data-theme="dark"]) {{ {body} }}\n'
