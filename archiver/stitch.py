"""把每一步 dump 出來的列合併成一份不重複、由舊到新的清單。

座標：第 k 步之前累計往下捲了 G_k 像素，螢幕上的 y 對應到整段對話的 y - G_k。
越舊的訊息 y 越小。同一列在相鄰兩步之間的位置差應該就是那一步的位移，
所以只拿上一步的列來比對，累計誤差不會影響去重。

部分在畫面外的列，dump 的範圍會被裁掉、也可能少了時間或文字，
所以只比對沒被裁掉的那一邊，以及雙方都有的欄位。兩次觀察沒有共同的完整邊時
（例如比畫面還高、上下都被裁掉的長文字訊息），改看露出來的部分有沒有重疊。
"""
from dataclasses import dataclass, field

TOLERANCE = 40      # 同一列在相鄰兩步之間，位置可以差多少像素


@dataclass(eq=False)          # 以物件本身比較，不比欄位
class Unique:
    row: object                 # 最佳觀察（盡量是完整出現的那一次）
    frame: int                  # 最佳觀察在哪一步
    room: float = 0             # 最佳觀察離聊天區上下緣的距離
    gy1: float = None           # 上緣（整段對話座標）；被裁掉時為 None
    gy2: float = None
    frames: list = field(default_factory=list)
    seen: tuple = None          # 最近一次露出來的範圍（整段對話座標）

    def key(self):
        if self.gy1 is not None:
            return self.gy1
        if self.gy2 is not None:
            return self.gy2 - (self.row.bounds[3] - self.row.bounds[1])
        return self.seen[0]     # 上下都被裁掉：真正的上緣在露出來的部分之上，排序時用它即可


def compatible(a, b):
    if a.kind != b.kind and "unknown" not in (a.kind, b.kind):
        return False
    if a.time and b.time and a.time != b.time:
        return False
    if a.kind == b.kind == "text" and a.text and b.text and a.text != b.text:
        return False
    if a.kind == b.kind == "date" and a.text != b.text:
        return False
    return True


def better(new, old, new_room=0, old_room=0):
    """new 這次觀察比 old 好嗎？完整出現 > 部分；同樣完整時取資訊較多者，
    再取離聊天區上下緣較遠者（頂端有浮動日期條、公告，底端有按鈕，可能蓋住一部分）。"""
    if old.partial != new.partial:
        return old.partial
    if (old.kind == "unknown") != (new.kind == "unknown"):
        return old.kind == "unknown"
    if bool(old.time) != bool(new.time):
        return not old.time
    return new_room > old_room


def stitch(screens, offsets):
    """screens：每一步的 Screen；offsets：每一步相對上一步的位移（第 0 步為 0；None 表示不明）。

    回傳 (由舊到新的 Unique 清單, 警告訊息清單)。
    """
    uniques, warnings = [], []
    shift = 0.0
    prev = []                   # 上一步出現過的 Unique
    for k, (screen, dy) in enumerate(zip(screens, offsets)):
        top, bottom = screen.list_bounds[1], screen.list_bounds[3]
        if dy is None:
            warnings.append(f"第 {k} 步位移不明，這一步的訊息無法和上一步對齊，可能重複或遺漏")
            prev = []
        else:
            shift += dy
        current = []
        for row in screen.rows:
            top_cut = row.bounds[1] <= top + 1
            bottom_cut = row.bounds[3] >= bottom - 1
            gy1 = None if top_cut else row.bounds[1] - shift
            gy2 = None if bottom_cut else row.bounds[3] - shift
            seen = (row.bounds[1] - shift, row.bounds[3] - shift)
            match, best_gap = None, TOLERANCE + 1
            for u in prev:
                if u in current or not compatible(u.row, row):
                    continue
                gaps = []
                if gy1 is not None and u.gy1 is not None:
                    gaps.append(abs(gy1 - u.gy1))
                if gy2 is not None and u.gy2 is not None:
                    gaps.append(abs(gy2 - u.gy2))
                overlap = min(seen[1], u.seen[1]) - max(seen[0], u.seen[0])
                if not gaps and overlap > TOLERANCE and (row.kind != "text" or row.text):
                    gaps.append(TOLERANCE)  # 沒有共同的邊：露出來的部分重疊（文字列的文字已由 compatible 確認相同）
                if gaps and min(gaps) < best_gap:
                    match, best_gap = u, min(gaps)
            room = min(row.bounds[1] - top, bottom - row.bounds[3])
            if match is None:
                match = Unique(row, k, room, gy1, gy2, seen=seen)
                uniques.append(match)
            else:
                if better(row, match.row, room, match.room):
                    match.row, match.frame, match.room = row, k, room
                if match.gy1 is None and gy1 is not None:
                    match.gy1 = gy1
                if match.gy2 is None and gy2 is not None:
                    match.gy2 = gy2
                match.seen = seen
            match.frames.append(k)
            current.append(match)
        prev = current
    uniques.sort(key=Unique.key)
    fill_senders(uniques)
    return uniques, warnings


def fill_senders(uniques):
    """頭像只出現在一連串訊息的第一則；把名稱往後補到同一側的後續訊息。"""
    sender = ""
    for u in uniques:
        r = u.row
        if r.side == "other":
            if r.sender:
                sender = r.sender
            elif sender:
                r.sender = sender
        elif r.side == "self":
            sender = ""
