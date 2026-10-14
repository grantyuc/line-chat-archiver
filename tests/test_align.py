from archiver.align import align, infer_me, summarize
from archiver.screen import Row
from archiver.txt_parser import parse, set_me
from tests.chatgen import make_chat


def rows_from(items):
    rows = []
    for it in items:
        if it["kind"] == "date":
            rows.append(Row("date", (0, 0, 1080, 60), text=it["text"], side="center"))
            continue
        rows.append(Row(it["kind"], (0, 0, 1080, 90), text=it["text"], time=it["time"],
                        side="self" if it["self"] else "other"))
    return rows


def test_every_message_finds_its_row():
    txt, items = make_chat(days=5, per_day=30)
    chat = parse(txt)
    rows = rows_from(items)
    pairs = align(chat.messages, rows)
    msg_rows = [j for j, r in enumerate(rows) if r.kind != "date"]
    assert [j for j, _ in pairs] == msg_rows


def test_missing_rows_extra_rows_and_repeated_texts():
    txt, items = make_chat(days=3, per_day=30, seed=7)
    chat = parse(txt)
    rows = rows_from(items)
    msg_rows = [j for j, r in enumerate(rows) if r.kind != "date"]
    truth = dict(zip(range(len(msg_rows)), msg_rows))
    # 漏擷取 3 則、多一列「以下為尚未閱讀的訊息」
    missing = {5, 40, 41}
    divider = Row("unknown", (0, 0, 1080, 60), text="以下為尚未閱讀的訊息", side="center")
    kept = [(j, r) for j, r in enumerate(rows) if j not in {truth[i] for i in missing}]
    kept.insert(30, (None, divider))
    new_rows = [r for _, r in kept]
    old_index = [j for j, _ in kept]
    pairs = align(chat.messages, new_rows)
    for i, (j, _) in enumerate(pairs):
        if i in missing:
            assert j is None
        else:
            assert old_index[j] == truth[i], (i, chat.messages[i].body)
    info = summarize(chat.messages, new_rows, pairs)
    assert new_rows.index(divider) in info["extra_rows"]


def test_infer_me_for_group_chat():
    txt, items = make_chat(days=2, per_day=20, seed=3)
    txt = txt.replace("[LINE] 與威廷的聊天記錄", "[LINE] 讀書會的聊天記錄")
    chat = parse(txt)
    assert chat.partner == "" and chat.me == ""
    rows = rows_from(items)
    pairs = align(chat.messages, rows, use_side=False)
    set_me(chat, infer_me(chat.messages, rows, pairs))
    assert chat.me == "我"
