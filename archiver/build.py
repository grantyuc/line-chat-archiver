"""把擷取結果和 txt 組起來：去重 → 對齊 → 取媒體 → 輸出網站，並寫一份報告。"""
import bisect
import json
import os
import statistics
from collections import Counter

from .align import align, infer_me, summarize
from .capture import Capture, chat_roi, dump_offset, planned_distance, sessions
from .imgio import imread
from .offset import LOW_SCORE, find_offset, overlap_diff
from .media import expand_albums, resolve
from .render import PAGE_MIN, render
from .screen import parse_dump
from .stitch import stitch
from .theme import sample_theme
from .txt_parser import Chat, classify, set_me


def chat_path(workdir):
    return os.path.join(workdir, "chat.json")


def save_chat(workdir, chat):
    os.makedirs(workdir, exist_ok=True)
    with open(chat_path(workdir), "w", encoding="utf-8") as f:
        json.dump(chat.to_dict(), f, ensure_ascii=False, indent=1)


def load_chat(workdir):
    with open(chat_path(workdir), encoding="utf-8") as f:
        return Chat.from_dict(json.load(f))


def load_capture(workdir):
    """把各段擷取由新到舊串成一串畫面。回傳 (各步紀錄, Screen, 截圖路徑, 各段摘要)。

    每段第一張的位移改成「上一段（較新的那段）最後一張接上它的位移」（join_dy）；
    沒接上時為 None，stitch 會把它當成位移不明。"""
    steps, screens, paths, parts = [], [], [], []
    join = 0
    for session in sessions(workdir):
        cap = Capture(None, workdir, session=session, log=lambda *a: None)
        records = cap.load_steps()
        if not records:
            continue
        for k, s in enumerate(records):
            s = dict(s, session=session)
            if k == 0 and steps:
                s["dy"] = join
            with open(cap.path(s["step"], "xml"), "rb") as f:
                screens.append(parse_dump(f.read()))
            steps.append(s)
            paths.append(cap.image_path(s["step"]))
        join = records[-1].get("join_dy")
        parts.append((session, len(records), records[-1]))
    return steps, screens, paths, parts


MATCH_DIFF = 8.0         # 重新比對位移時，重疊區平均灰階差的上限


def recover_offsets(steps, screens, paths, window=10):
    """擷取時判定不了的位移（None），用存下來的畫面再算一次：先比截圖，再比 dump，
    兩者都要通過整個重疊區的檢查。
    還是不行時用估計值，至少讓之後的列排在對的位置（位移不明時，之後每一列的位置都會差一整步，
    和前面的列交錯，對齊時就會漏掉訊息）。各段擷取之間（更新擷取沒接上）不估計。

    估計值：這一步原本要捲多遠（擷取時記下的 expect，或由上一張畫面重算），加上前後幾步
    「實際位移 − 預定距離」的中位數；舊版擷取每步固定距離，這時改用前後幾步位移的中位數。
    回傳 (各步位移, [(步, 位移, 方法)])。
    """
    offsets = [s["dy"] for s in steps]
    rois = [chat_roi(sc, 420 * sc.width / 1080) for sc in screens]

    def planned(j):
        return steps[j].get("expect") or planned_distance(screens[j - 1], rois[j - 1])

    def estimate(k):
        session = steps[k].get("session")
        near = [j for j in range(max(1, k - window), min(len(steps), k + window + 1))
                if j != k and offsets[j] and offsets[j] > 0 and steps[j].get("session") == session
                and steps[j - 1].get("session") == session]
        if len(near) < 3:
            return None
        residual = [offsets[j] - planned(j) for j in near]
        moved = [offsets[j] for j in near]
        spread = lambda v: statistics.median(abs(x - statistics.median(v)) for x in v)
        if spread(residual) <= spread(moved):
            return int(planned(k) + statistics.median(residual))
        return int(statistics.median(moved))

    notes = []
    for k in range(1, len(steps)):
        if offsets[k] is not None:
            continue
        roi = rois[k - 1]
        view = roi[1] - roi[0]
        same_session = steps[k].get("session") == steps[k - 1].get("session")
        guess = estimate(k) if same_session else None
        a, b = imread(paths[k - 1]), imread(paths[k])
        images = a is not None and b is not None and a.shape == b.shape

        def fits(d):
            """截圖的整個重疊區吻合；沒有截圖時，至少要在合理範圍、接近估計值。"""
            if images:
                return overlap_diff(a, b, roi, d) <= MATCH_DIFF
            return 0 <= d <= view and (guess is None or abs(d - guess) <= view * 0.3)

        dy, how = None, ""
        if images:
            found, score = find_offset(a, b, roi, guess)
            if found is not None and score >= LOW_SCORE and fits(found):
                dy, how = found, "截圖"
        if dy is None:
            found = dump_offset(screens[k - 1], screens[k])
            if found is not None and fits(found):
                dy, how = found, "dump"
        if dy is None and guess is not None:
            dy, how = guess, "估計"
        offsets[k] = dy
        notes.append((k, dy, how))
    return offsets, notes


def dump_path(workdir, step):
    """這一步的 dump 檔，相對於工作目錄（給 inspect 用）。"""
    cap = Capture(None, workdir, session=step.get("session"), log=lambda *a: None)
    return os.path.relpath(cap.path(step["step"], "xml"), workdir)


def step_label(step):
    """「第 123 步」或「更新 001 第 12 步」，用來在 capture 目錄裡找到那張畫面。"""
    session = step.get("session")
    return (f"更新 {session} " if session else "") + f"第 {step['step']} 步"


def unsend_notices(rows, pairs, extra_rows):
    """txt 不含收回通知，但畫面上有（置中的「某某已收回訊息」）。回傳 [(列索引, 文字)]。"""
    return [(j, rows[j].text) for j in extra_rows
            if rows[j].kind in ("system", "unknown") and rows[j].side == "center" and "收回" in rows[j].text]


def previous_message(pairs, row_index):
    """畫面上位於 row_index 之前、最接近的那則已對到的訊息索引。"""
    matched = sorted((j, i) for i, (j, _) in enumerate(pairs) if j is not None)
    k = bisect.bisect_left(matched, (row_index, -1))
    return matched[k - 1][1] if k else None


LIST_LIMIT = 200         # 報告中逐則列出改用截圖的媒體，最多幾則


def media_summary(chat, media):
    """每則貼圖、照片、影片最後拿到什麼：快取、依編號推斷、截圖，或沒有圖。"""
    lines = ["每則媒體訊息的結果："]
    for kind in ("photo", "video", "sticker"):
        msgs = [m for m in chat.messages if m.kind == kind]
        if not msgs:
            continue
        got = Counter()
        for m in msgs:
            items = media["media"].get(str(m.idx))
            if not items:
                got["沒有圖"] += 1
            elif items[0].get("inferred"):
                got["依編號推斷"] += 1
            elif items[0]["source"] == "screenshot":
                got["截圖裁切"] += 1
            elif items[0]["source"] == "custom":
                got["訊息貼圖或拼拼樂（用截圖）"] += 1
            else:
                got["快取"] += 1
        order = ("快取", "依編號推斷", "訊息貼圖或拼拼樂（用截圖）", "截圖裁切", "沒有圖")
        lines.append(f"    {kind}：共 {len(msgs)} 則，" + "、".join(f"{k} {got[k]}" for k in order if got[k]))
    voices = [m for m in chat.messages if m.kind == "voice"]
    if voices:
        played = sum(1 for m in voices if media["media"].get(str(m.idx)))
        lines.append(f"    voice：共 {len(voices)} 則，可以播放 {played}、沒有檔案 {len(voices) - played}")
    listed = 0
    for m in chat.messages:
        items = media["media"].get(str(m.idx))
        if m.kind not in ("photo", "video", "sticker") or (items and items[0]["source"] != "screenshot"):
            continue
        listed += 1
        if listed > LIST_LIMIT:
            continue
        where = f"site/{items[0]['src']}" if items else "畫面上沒有"
        lines.append(f"    {m.kind} {m.date} {m.time} {m.sender}：{where}")
        if items and "tried" in items[0]:
            tried = "、".join(f"{n}（{v}）{s:.2f}" for n, v, s in items[0]["tried"]) or "沒有"
            lines.append(f"        前後已對到的照片之間的快取編號（有哪些檔案）一致比例：{tried}")
    if listed > LIST_LIMIT:
        lines.append(f"    ……其餘 {listed - LIST_LIMIT} 則略")
    return lines


def build(workdir, device=None, chat_dir=None, stickers=True, photos=True, refresh=False, page_min=PAGE_MIN,
          log=print):
    chat = load_chat(workdir)
    for m in chat.messages:                       # 訊息類型依目前的規則重新判斷，不必重新 parse
        m.kind = classify(m.sender, m.body)
    steps, screens, paths, parts = load_capture(workdir)
    report = [f"聊天室：{chat.title}，txt 共 {len(chat.messages)} 則訊息"]
    uniques, pairs, media, notices = [], [(None, 0.0)] * len(chat.messages), None, []
    if steps:
        offsets, recovered = recover_offsets(steps, screens, paths)
        uniques, warnings = stitch(screens, offsets)
        rows = [u.row for u in uniques]
        report.append(f"擷取：{len(steps)} 步，去重後 {len(rows)} 列"
                      + ("" if steps and steps[-1].get("done") else "（尚未捲到最舊的訊息）"))
        if recovered:
            how = Counter(h for _, d, h in recovered if d is not None)
            left = sum(1 for _, d, _ in recovered if d is None)
            report.append(f"擷取時判定不了位移的步數：{len(recovered)} 步；重新比對 dump {how['dump']} 步、"
                          f"截圖 {how['截圖']} 步，用估計值 {how['估計']} 步，仍不明 {left} 步")
            for k, d, h in recovered:
                if h == "估計":
                    report.append(f"    {step_label(steps[k])}：用估計值 {d}px，前後的訊息可能重複出現")
        for session, count, last in parts:
            if session is None:
                continue
            state = (f"已接上上一段（位移 {last['join_dy']}px）" if last.get("join_dy") is not None
                     else "尚未接上上一段：兩段之間的訊息可能重複或遺漏，請再執行 capture --update 接續")
            report.append(f"    更新擷取 {session}：{count} 步，{state}")
        report += ["警告：" + w for w in warnings]
        if not chat.me:
            first = align(chat.messages, rows, use_side=False)
            set_me(chat, infer_me(chat.messages, rows, first))
            save_chat(workdir, chat)
            report.append(f"推斷自己的名稱：{chat.me or '（無法判斷）'}")
        log("對齊畫面與 txt……")
        pairs = align(chat.messages, rows)
        info = summarize(chat.messages, rows, pairs)
        # 一次傳多張的照片在畫面上只有一列，對齊只配給其中一則；其餘的由同一列取得
        slots = expand_albums(chat.messages, uniques, pairs)
        shared = Counter(m.kind for m, (j, _), s in zip(chat.messages, pairs, slots) if j is None and s)
        report.append("對齊結果（對到 / 總數）：")
        for kind, (total, hit) in sorted(info["by_kind"].items(), key=lambda kv: -kv[1][0]):
            note = f"（其中 {shared[kind]} 則和同一批照片共用畫面上的一列）" if shared[kind] else ""
            report.append(f"    {kind}：{hit + shared[kind]} / {total}{note}")
        missed = [i for i, s in enumerate(slots) if s is None]
        report.append(f"沒對到畫面的訊息：{len(missed)} 則")
        last_seen = None
        seen_before = []                          # 每則訊息之前最近一則對到的列在哪一步
        for s in slots:
            seen_before.append(last_seen)
            if s is not None:
                last_seen = s[0]
        for i in missed[:30]:
            m, j = chat.messages[i], seen_before[i]
            near = f"（前一則對到的在{step_label(steps[uniques[j].frame])}）" if j is not None else ""
            report.append(f"    {m.date} {m.time} {m.sender} {m.kind} {m.body[:20]!r}{near}")
        notices = unsend_notices(rows, pairs, info["extra_rows"])
        inserted = {j for j, _ in notices}
        others = [j for j in info["extra_rows"] if j not in inserted]
        report.append(f"低信心配對：{len(info['low_confidence'])} 則；畫面上多出來的列：{len(info['extra_rows'])} 列"
                      f"（其中 {len(inserted)} 則收回通知已插入網頁；其餘 {len(others)} 列）")
        for j in others[:30]:
            r = rows[j]
            report.append(f"    多出來的列（{step_label(steps[uniques[j].frame])}）：{r.kind} {r.time} {r.side} {r.text[:40]!r}")
        with open(os.path.join(workdir, "alignment.json"), "w", encoding="utf-8") as f:
            json.dump([{"msg": m.idx, "row": j, "score": round(s or 0, 2),
                        "frame": uniques[j].frame if j is not None else None,
                        "where": step_label(steps[uniques[j].frame]) if j is not None else None,
                        "xml": dump_path(workdir, steps[uniques[j].frame]) if j is not None else None,
                        "row_kind": rows[j].kind if j is not None else None}
                       for m, (j, s) in zip(chat.messages, pairs)], f, ensure_ascii=False)
        log("取得貼圖、照片……")
        media = resolve(workdir, chat, uniques, pairs, device=device, chat_dir=chat_dir, frame_paths=paths,
                        stickers=stickers, photos=photos, refresh=refresh, log=log)
        report.append("媒體來源：")
        report += [f"    {k}：{v}" for k, v in sorted(media["stats"].items())]
        report += media_summary(chat, media)
    else:
        report.append("還沒有擷取畫面：網站只有文字，媒體顯示為佔位符。")
    after = {}
    for j, text in notices:
        i = previous_message(pairs, j)
        if i is not None:
            after.setdefault(chat.messages[i].idx, []).append(text)
    theme = sample_theme(screens, paths) if steps else {}
    site = render(workdir, chat, media, notices=after, page_min=page_min, theme=theme, log=log)
    with open(os.path.join(workdir, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    for line in report:
        log(line)
    return site
