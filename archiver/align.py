"""模組 3：把擷取到的聊天列（由舊到新）對到 txt 的訊息（由舊到新）。

用動態規劃做序列對齊，配對分數依據：
- 類型：文字對文字、貼圖對貼圖、照片對照片、影片對影片；系統訊息對系統訊息；檔案、位置等對「未知」列。
- 文字相似度（文字訊息）。只有 LINE 表情的文字訊息（txt 寫成「(笑)」）畫面上是表情圖，對沒有文字的未知列。
- 時間：畫面上每則訊息都有時間，和 txt 相同加分、不同扣分。
- 左右側：自己的訊息在右側。
- 日期：列上方最近的日期條（例如「9月2日 週三」）和 txt 的日期一致加分。
txt 有、畫面沒擷取到的訊息，以及畫面上多出來的列（例如「以下為尚未閱讀的訊息」）都允許跳過。
"""
import re
from difflib import SequenceMatcher

MEDIA = ("sticker", "photo", "video")
NON_BUBBLE = ("file", "voice", "contact", "location", "call", "unsend", "system", "other", "album", "note")
SKIP = -1.0                  # 跳過一則訊息或一列的代價
DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
# 只有 LINE 表情的訊息：txt 寫成「(笑)」「(smirk Moon)」，畫面上是放大的表情圖，沒有文字
EMOJI_ONLY_RE = re.compile(r"^(\([^()\n]{1,24}\)\s*){1,3}$")


def norm(text):
    return re.sub(r"\s+", " ", text.replace("​", "")).strip()


def text_similarity(a, b):
    a, b = norm(a), norm(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if b.endswith("…") and a.startswith(b[:-1]):
        return 0.95
    sm = SequenceMatcher(None, a, b, autojunk=False)
    if sm.quick_ratio() < 0.5:
        return sm.quick_ratio() * 0.5
    return sm.ratio()


def row_days(rows):
    """每一列所在的日期 (月, 日)，取自它上方最近的日期條；不明時為 None。"""
    days, current = [], None
    for r in rows:
        label = r.date_label or (r.text if r.kind == "date" else "")
        if label:
            m = DATE_RE.search(label)
            current = (int(m.group(1)), int(m.group(2))) if m else None
        days.append(current)
    return days


def pair_score(msg, row, day, use_side=True):
    """訊息和列配對的分數；不可能配對時回傳 None。"""
    if msg.kind == "text":
        if row.kind == "unknown" and not row.text.strip() and EMOJI_ONLY_RE.match(msg.body.strip()):
            score = 1.5
        elif row.kind != "text":
            return None
        else:
            score = 6 * text_similarity(msg.body, row.text) - 2
    elif msg.kind in MEDIA:
        if row.kind != msg.kind:
            return None
        score = 3.0
    elif msg.kind == "call" and row.kind == "call":
        score = 3.0
    elif row.kind == "system":
        if msg.kind not in ("system", "unsend"):
            return None
        score = 6 * text_similarity(msg.body, row.text) - 2
    elif msg.kind in NON_BUBBLE:
        if row.kind == "unknown":
            score = 1.5
        elif row.kind == "text":
            score = 6 * text_similarity(msg.body, row.text) - 3
        else:
            return None
    else:
        return None
    if row.time:
        score += 1.0 if row.time == msg.time else -1.5
    if day:
        month, dd = int(msg.date[5:7]), int(msg.date[8:10])
        score += 0.5 if day == (month, dd) else -2.0
    if use_side and row.side in ("self", "other") and msg.sender:
        score += 0.5 if (row.side == "self") == msg.is_self else -3.0
    return score


def align(messages, rows, use_side=True, band=None):
    """回傳 pairs：與 messages 等長的清單，每項為 (列索引或 None, 分數)。rows 中的日期列不參與配對。"""
    cand = [j for j, r in enumerate(rows) if r.kind != "date"]
    days = row_days(rows)
    n, m = len(messages), len(cand)
    if band is None:
        # 小聊天室做完整比對；大聊天室只在對角線附近搜尋（兩邊數量大致相同）
        band = m + n if n * m <= 250_000 else abs(n - m) + 150
    neg = float("-inf")
    # score[i][j]：前 i 則訊息和前 j 列的最佳分數；以字典存帶狀範圍
    score = [dict() for _ in range(n + 1)]
    back = [dict() for _ in range(n + 1)]
    score[0][0] = 0.0
    for j in range(1, min(m, band) + 1):
        score[0][j] = j * SKIP
        back[0][j] = "row"
    for i in range(1, n + 1):
        center = round(i * m / n) if n else 0
        lo, hi = max(0, center - band), min(m, center + band)
        for j in range(lo, hi + 1):
            best, how = neg, None
            if j in score[i - 1]:
                best, how = score[i - 1][j] + SKIP, "msg"
            if j - 1 in score[i]:
                s = score[i][j - 1] + SKIP
                if s > best:
                    best, how = s, "row"
            if j >= 1 and j - 1 in score[i - 1]:
                ps = pair_score(messages[i - 1], rows[cand[j - 1]], days[cand[j - 1]], use_side)
                if ps is not None:
                    s = score[i - 1][j - 1] + ps
                    if s > best:
                        best, how = s, "pair"
            if how:
                score[i][j], back[i][j] = best, how
    # 回溯
    i, j = n, max(score[n], key=lambda k: score[n][k] + (m - k) * SKIP)
    pairs = [(None, 0.0)] * n
    while i > 0:
        how = back[i].get(j)
        if how == "pair":
            ps = pair_score(messages[i - 1], rows[cand[j - 1]], days[cand[j - 1]], use_side)
            pairs[i - 1] = (cand[j - 1], ps)
            i, j = i - 1, j - 1
        elif how == "msg":
            i -= 1
        elif how == "row":
            j -= 1
        else:
            i -= 1
    return pairs


def infer_me(messages, rows, pairs):
    """群組聊天：用對到右側列的訊息發話者，推斷哪個名字是自己。"""
    counts = {}
    for msg, (j, _) in zip(messages, pairs):
        if j is not None and rows[j].side == "self" and msg.sender:
            counts[msg.sender] = counts.get(msg.sender, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def summarize(messages, rows, pairs):
    """回傳對齊統計：各類型訊息的配對數、低分配對、沒對到的列。"""
    by_kind = {}
    low = []
    used = set()
    for msg, (j, s) in zip(messages, pairs):
        total, hit = by_kind.get(msg.kind, (0, 0))
        by_kind[msg.kind] = (total + 1, hit + (j is not None))
        if j is not None:
            used.add(j)
            if s < 2:
                low.append(msg.idx)
    extra = [j for j, r in enumerate(rows) if r.kind != "date" and j not in used]
    return {"by_kind": by_kind, "low_confidence": low, "extra_rows": extra}
