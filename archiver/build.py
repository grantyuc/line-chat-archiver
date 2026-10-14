"""把擷取結果和 txt 組起來：去重 → 對齊 → 取媒體 → 輸出網站，並寫一份報告。"""
import json
import os

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


def build(workdir, device=None, chat_dir=None, stickers=True, photos=True, refresh=False, log=print):
    chat = load_chat(workdir)
    steps, screens = load_capture(workdir)
    report = [f"聊天室：{chat.title}，txt 共 {len(chat.messages)} 則訊息"]
    uniques, pairs, media = [], [(None, 0.0)] * len(chat.messages), None
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
        report.append("對齊結果（對到 / 總數）：")
        for kind, (total, hit) in sorted(info["by_kind"].items(), key=lambda kv: -kv[1][0]):
            report.append(f"    {kind}：{hit} / {total}")
        report.append(f"低信心配對：{len(info['low_confidence'])} 則；畫面上多出來的列：{len(info['extra_rows'])} 列")
        for j in info["extra_rows"][:20]:
            r = rows[j]
            report.append(f"    多出來的列：{r.kind} {r.time} {r.side} {r.text[:40]!r}")
        with open(os.path.join(workdir, "alignment.json"), "w", encoding="utf-8") as f:
            json.dump([{"msg": m.idx, "row": j, "score": round(s or 0, 2),
                        "frame": uniques[j].frame if j is not None else None}
                       for m, (j, s) in zip(chat.messages, pairs)], f)
        log("取得貼圖、照片……")
        media = resolve(workdir, chat, uniques, pairs, device=device, chat_dir=chat_dir,
                        stickers=stickers, photos=photos, refresh=refresh, log=log)
        report.append("媒體來源：")
        report += [f"    {k}：{v}" for k, v in sorted(media["stats"].items())]
    else:
        report.append("還沒有擷取畫面：網站只有文字，媒體顯示為佔位符。")
    site = render(workdir, chat, media, log=log)
    with open(os.path.join(workdir, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    for line in report:
        log(line)
    return site
