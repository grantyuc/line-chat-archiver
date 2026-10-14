"""把擷取結果和 txt 組起來：去重 → 對齊 → 取媒體 → 輸出網站，並寫一份報告。"""
import bisect
import json
import os
from collections import Counter

from .align import align, infer_me, summarize
from .capture import Capture
from .media import resolve
from .render import render
from .screen import parse_dump
from .stitch import stitch
from .txt_parser import Chat, set_me


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
    cap = Capture(None, workdir, log=lambda *a: None)
    steps = cap.load_steps()
    screens = []
    for s in steps:
        with open(cap.path(s["step"], "xml"), "rb") as f:
            screens.append(parse_dump(f.read()))
    return steps, screens


def unsend_notices(rows, pairs, extra_rows):
    """txt 不含收回通知，但畫面上有（置中的「某某已收回訊息」）。回傳 [(列索引, 文字)]。"""
    return [(j, rows[j].text) for j in extra_rows
            if rows[j].kind == "unknown" and rows[j].side == "center" and "收回" in rows[j].text]


def previous_message(pairs, row_index):
    """畫面上位於 row_index 之前、最接近的那則已對到的訊息索引。"""
    matched = sorted((j, i) for i, (j, _) in enumerate(pairs) if j is not None)
    k = bisect.bisect_left(matched, (row_index, -1))
    return matched[k - 1][1] if k else None


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
            else:
                got["快取"] += 1
        order = ("快取", "依編號推斷", "截圖裁切", "沒有圖")
        lines.append(f"    {kind}：共 {len(msgs)} 則，" + "、".join(f"{k} {got[k]}" for k in order if got[k]))
    return lines


def build(workdir, device=None, chat_dir=None, stickers=True, photos=True, refresh=False, log=print):
    chat = load_chat(workdir)
    steps, screens = load_capture(workdir)
    report = [f"聊天室：{chat.title}，txt 共 {len(chat.messages)} 則訊息"]
    uniques, pairs, media, notices = [], [(None, 0.0)] * len(chat.messages), None, []
    if steps:
        uniques, warnings = stitch(screens, [s["dy"] for s in steps])
        rows = [u.row for u in uniques]
        report.append(f"擷取：{len(steps)} 步，去重後 {len(rows)} 列"
                      + ("" if any(s.get("done") for s in steps) else "（尚未捲到最舊的訊息）"))
        report += ["警告：" + w for w in warnings]
        if not chat.me:
            first = align(chat.messages, rows, use_side=False)
            set_me(chat, infer_me(chat.messages, rows, first))
            save_chat(workdir, chat)
            report.append(f"推斷自己的名稱：{chat.me or '（無法判斷）'}")
        log("對齊畫面與 txt……")
        pairs = align(chat.messages, rows)
        info = summarize(chat.messages, rows, pairs)
        report.append("對齊結果（對到 / 總數；一次傳多張的照片在畫面上只有一列，只會算到其中一則）：")
        for kind, (total, hit) in sorted(info["by_kind"].items(), key=lambda kv: -kv[1][0]):
            report.append(f"    {kind}：{hit} / {total}")
        missed = [m for m, (j, _) in zip(chat.messages, pairs) if j is None]
        report.append(f"沒對到畫面的訊息：{len(missed)} 則")
        for m in missed[:30]:
            report.append(f"    {m.date} {m.time} {m.sender} {m.kind} {m.body[:20]!r}")
        notices = unsend_notices(rows, pairs, info["extra_rows"])
        inserted = {j for j, _ in notices}
        report.append(f"低信心配對：{len(info['low_confidence'])} 則；畫面上多出來的列：{len(info['extra_rows'])} 列"
                      f"（其中 {len(inserted)} 則收回通知已插入網頁）")
        for j in info["extra_rows"][:30]:
            r = rows[j]
            mark = "（已插入網頁）" if j in inserted else ""
            report.append(f"    多出來的列：{r.kind} {r.time} {r.side} {r.text[:40]!r}{mark}")
        with open(os.path.join(workdir, "alignment.json"), "w", encoding="utf-8") as f:
            json.dump([{"msg": m.idx, "row": j, "score": round(s or 0, 2),
                        "frame": uniques[j].frame if j is not None else None}
                       for m, (j, s) in zip(chat.messages, pairs)], f)
        log("取得貼圖、照片……")
        media = resolve(workdir, chat, uniques, pairs, device=device, chat_dir=chat_dir,
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
    site = render(workdir, chat, media, notices=after, log=log)
    with open(os.path.join(workdir, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    for line in report:
        log(line)
    return site
