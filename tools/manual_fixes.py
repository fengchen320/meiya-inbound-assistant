# -*- coding: utf-8 -*-
"""manual_fixes.py —— 人工处置记录（弹窗里选了分类 / 补了拆包 / 这批先不入）。

为什么单独一个文件，而不是塞进 config/expiry_approvals.json：
    效期审批的键是「单据号 + 物品名 + 有效期」，换了效期就是换了货，旧审批必须作废；
    这里记的是「这个物品的一级分类是 X」「这批先不入」——与效期无关。
    混在一个文件里，一次效期审批的写入会把分类记录整条覆盖掉（两者键不同，
    只留一份 records 数组时后写的必然赢），表现为「我选好的分类过一会儿又变回没选」。

键里为什么带规格：同一张单里同名不同规格的物品很常见（牙胶尖 04-30# / 04-35#），
只按名字记会把两条一起改掉，而它们确实可能属于不同分类。
"""
import hashlib
import json
import os
import re

FIXES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "manual_fixes.json")


def key(doc_no, item, spec):
    """和 expiry/reject 同一种做法：把三个字段拍平成一个短键，只用于查表。"""
    raw = "%s|%s|%s" % (str(doc_no or "").strip(),
                       re.sub(r"\s+", "", str(item or "")),
                       re.sub(r"\s+", "", str(spec or "")).lower())
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load(path=None):
    p = path or FIXES
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
    p = path or FIXES
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def index(path=None):
    """一次读盘、按行查。build() 对每一行都要查，逐行 open() 会让一千行的单慢得没道理。"""
    return {r.get("key"): r for r in load(path).get("records") or []}


def find(doc_no, item, spec, path=None, idx=None):
    k = key(doc_no, item, spec)
    if idx is None:
        idx = index(path)
    return idx.get(k)


def set_fix(doc_no, item, spec, l1=None, pack_qty=None, pack_unit=None,
            skip=None, by="", reason="", path=None):
    """写一条处置。已有记录按字段合并，不清空没提到的字段。

    合并而不是覆盖的理由：人可能先补了拆包数量，过一会儿才选分类。
    覆盖式写入会让前一次的补充静默消失，而界面上看不出来（弹窗只为还没解决的项出现，
    已经解决的那条不会再展示，于是「我明明填过」无从对证）。
    """
    j = load(path)
    k = key(doc_no, item, spec)
    old = next((r for r in j["records"] if r.get("key") == k), None)
    rec = dict(old or {})
    rec.update({"key": k, "doc_no": str(doc_no or "").strip(),
                "item": str(item or "").strip(), "spec": str(spec or "").strip(),
                "by": by, "reason": reason,
                "at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    if l1 is not None:
        rec["l1"] = str(l1 or "").strip()
    if pack_qty is not None:
        rec["pack_qty"] = pack_qty
    if pack_unit is not None:
        rec["pack_unit"] = str(pack_unit or "").strip()
    if skip is not None:
        rec["skip"] = bool(skip)
    j["records"] = [r for r in j["records"] if r.get("key") != k] + [rec]
    save(j, path)
    return rec


def clear(doc_no, item, spec, path=None):
    j = load(path)
    k = key(doc_no, item, spec)
    n = len(j["records"])
    j["records"] = [r for r in j["records"] if r.get("key") != k]
    save(j, path)
    return n - len(j["records"])


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "list":
        js = load()
        print("人工处置 %d 条 -> %s" % (len(js["records"]), FIXES))
        for r in js["records"]:
            print("  %s %s %s l1=%s pack=%s/%s skip=%s %s"
                  % (r.get("doc_no"), r.get("item"), r.get("spec"), r.get("l1"),
                     r.get("pack_qty"), r.get("pack_unit"), r.get("skip"), r.get("at")))
    else:
        print("用法：python tools/manual_fixes.py list")
