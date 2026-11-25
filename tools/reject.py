# -*- coding: utf-8 -*-
"""reject.py — 「禁止入库」：人工判定某一行永久不进库存。

与「效期审批」的区别：
  审批是「这次能用，放行」，下次同单据同效期仍会被拦；
  禁止是「这东西不该入」，之后**无论效期多好都不再进表**，也不需要再问人。

真实场景（用户 2026-11-23 提出）：赠品、样品、客户自备料、退回重发的货——
它们长得像耗材，规则也拦不住（效期正常、有批号），但入库存只会把库存搞乱。

审批键与 expiry.approve 一致：单据号 + 物品名，**但不带有效期**——
这是有意的：禁止的是「这个物品不进库存」这件事本身，与这批货的效期无关。
"""
import datetime
import hashlib
import json
import os
import re

REJECTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "inbound_rejects.json")


def _key(doc_no, item):
    raw = "%s|%s" % (str(doc_no or "").strip(),
                     re.sub(r"\s+", "", str(item or "")))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load(path=None):
    p = path or REJECTS
    if not os.path.exists(p):
        return {"version": 1, "records": []}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f) or {}
    except (ValueError, OSError):
        return {"version": 1, "records": []}
    j.setdefault("records", [])
    return j


def save(j, path=None):
    p = path or REJECTS
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def is_rejected(doc_no, item, path=None):
    k = _key(doc_no, item)
    return any(r.get("key") == k for r in load(path)["records"])


def reject(doc_no, item, reason="", by="", path=None):
    j = load(path)
    k = _key(doc_no, item)
    j["records"] = [r for r in j["records"] if r.get("key") != k]
    rec = {"key": k, "doc_no": doc_no, "item": item, "reason": reason, "by": by,
           "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    j["records"].append(rec)
    save(j, path)
    return rec


def unreject(doc_no, item, path=None):
    j = load(path)
    k = _key(doc_no, item)
    before = len(j["records"])
    j["records"] = [r for r in j["records"] if r.get("key") != k]
    if len(j["records"]) == before:
        return False
    save(j, path)
    return True


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "list":
        j = load()
        print("禁止入库 %d 条 -> %s" % (len(j["records"]), REJECTS))
        for r in j["records"]:
            print("  %s  %s  %s  (%s)" % (r.get("doc_no"), r.get("item"),
                                          r.get("reason"), r.get("at")))
    elif a and a[0] == "add" and len(a) >= 3:
        print(reject(a[1], a[2], reason=(a[3] if len(a) > 3 else ""), by="cli"))
    elif a and a[0] == "del" and len(a) >= 3:
        print("已移除" if unreject(a[1], a[2]) else "没有这条记录")
    else:
        print("用法：python tools/reject.py list")
        print("     python tools/reject.py add <单据号> <物品名称> [备注]")
        print("     python tools/reject.py del <单据号> <物品名称>")
