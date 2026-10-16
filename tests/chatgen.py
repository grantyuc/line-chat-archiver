"""產生一段測試用聊天：同時輸出 LINE 匯出 txt 和模擬手機要顯示的內容。"""
import datetime
import random

WEEK = "一二三四五六日"


def make_chat(days=4, per_day=25, seed=1, partner="威廷", me="我", albums=False, voices=False):
    rng = random.Random(seed)
    lines = [f"[LINE] 與{partner}的聊天記錄", "儲存日期：2026/10/14 下午5:27", ""]
    items = []
    for d in range(days):
        date = datetime.date(2026, 9, 1) + datetime.timedelta(days=d)
        wd = WEEK[date.weekday()]
        lines.append(f"{date.year}/{date.month}/{date.day}（週{wd}）")
        items.append({"kind": "date", "text": f"{date.month}月{date.day}日 週{wd}"})
        for i in range(per_day):
            hhmm = f"{9 + i // 6:02d}:{(i * 7) % 60:02d}"
            is_self = rng.random() < 0.5
            sender = me if is_self else partner
            kind = rng.choices(["text", "sticker", "photo", "video"], [0.75, 0.12, 0.1, 0.03])[0]
            if albums and i % 9 == 4:
                count = 3 + (i + d) % 2          # 一次傳 3 或 4 張
                lines.extend(f"{hhmm}\t{sender}\t[照片]" for _ in range(count))
                items.append({"kind": "album", "count": count, "text": "", "time": hhmm, "self": is_self,
                              "sender": sender})
                continue
            if voices and rng.random() < 0.1:
                lines.append(f"{hhmm}\t{sender}\t[語音訊息]")
                items.append({"kind": "voice", "text": "", "time": hhmm, "self": is_self, "sender": sender,
                              "seconds": rng.randint(2, 75)})
                continue
            text = ""
            if kind == "text":
                text = rng.choice(["好", "哈哈", "OK", f"第{d}天第{i}則訊息"])
                if rng.random() < 0.1:
                    text += "\n第二行"
                body = text
            else:
                body = {"sticker": "[貼圖]", "photo": "[照片]", "video": "[影片]"}[kind]
            first, *rest = body.split("\n")
            lines.append(f"{hhmm}\t{sender}\t{first}")
            lines.extend(rest)
            items.append({"kind": kind, "text": text, "time": hhmm, "self": is_self, "sender": sender})
        lines.append("")
    return "\r\n".join(lines) + "\r\n", items
