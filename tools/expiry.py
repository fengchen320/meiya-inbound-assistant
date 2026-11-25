# -*- coding: utf-8 -*-
"""expiry.py — 有效期是否够用的判定（2026-11-23 用户定下的新规则）。

规则原话（用户 m09219）：
  有有效期、无批号可入库；
  无有效期（或有效期距下单日期不足一年）坚决不能入库，需标红等待用户审批，
  用户同意入库才能放行。

为什么把「有效期」提到比「批号」更硬的位置：
  批号是追溯用的，缺了还能靠收货记录倒查；
  有效期是**安全**用的——没有效期就不知道这批耗材还能不能用，
  近效期（不足一年）则可能还没用完就过期，进了库存等于给临床埋雷。
  所以：批号缺失只提示，效期缺失/不足一年必须拦下等人点头。
"""
import datetime
import hashlib
import json
import os
import re

# Excel 空日期会写成 1900-01-01（序列号 0 的显示值），必须当成「没有效期」
_EMPTY = {"", "1900-01-01", "1900/1/1", "1899-12-30", "无", "none", "null", "/"}

# 「距下单日期不足一年」的阈值。写 365 而不是 366/360：
# 用 365 天算出来的边界与「一年」的自然理解一致，也不会把闰年单据误判成够用。
MIN_DAYS = 365


def _to_date(v):
    """把各种写法的日期压成 date；认不出来返回 None。

    平台接口有时给 13 位毫秒时间戳、有时给 2027-12-23、有时给 20271223；
    这三种在本项目里都真实出现过（goodsList.expiryDate / 导出模板 / OCR）。
    """
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.lower() in _EMPTY:
        return None
    if re.fullmatch(r"\d{13}", s):
        return datetime.date.fromtimestamp(int(s) / 1000)
    m = re.fullmatch(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def check(expiry, doc_date=None, min_days=MIN_DAYS, today=None):
    """判定一条明细的有效期是否够用。

    返回 dict：
      ok        True 表示可以入库
      code      ok | no_expiry | short_expiry
      days      距单据日期的天数（no_expiry 时为 None）
      msg       给人看的一句话（拦下时直接进 blocked 原因列）
    """
    ed = _to_date(expiry)
    if ed is None:
        return {"ok": False, "code": "no_expiry", "days": None,
                "msg": "有效期为空，禁止入库（没有效期就不知道还能不能用）"}
    base = _to_date(doc_date) or (today or datetime.date.today())
    days = (ed - base).days
    if days < 0:
        return {"ok": False, "code": "short_expiry", "days": days,
                "msg": "有效期 %s 在单据日期之前（已过期 %d 天），禁止入库" % (ed.isoformat(), -days)}
    if days < min_days:
        return {"ok": False, "code": "short_expiry", "days": days,
                "msg": "有效期 %s 距单据日期只有 %d 天（不足一年），"
                       "原则上不入库，需要你确认后才能放行" % (ed.isoformat(), days)}
    return {"ok": True, "code": "ok", "days": days, "msg": ""}

# ── 审批（用户同意入库才能放行）───────────────────────────────────────
# 存在 config/expiry_approvals.json，按「单据号 + 物品名 + 有效期」三件套做键。
# 为什么把有效期也算进键：审批的是「这批货只剩 X 个月」这件事，换了效期就是换了货，
# 旧审批不能自动沿用，否则一次点「放行」等于长期豁免。
APPROVALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "expiry_approvals.json")


def _key(doc_no, item, expiry):
    raw = "%s|%s|%s" % (str(doc_no or "").strip(),
                        re.sub(r"\s+", "", str(item or "")),
                        str(expiry or "").strip())
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_approvals(path=None):
    p = path or APPROVALS
    if not os.path.exists(p):
        return {"version": 1, "records": []}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f) or {}
    except (ValueError, OSError):
        return {"version": 1, "records": []}
    j.setdefault("records", [])
    return j


def save_approvals(j, path=None):
    p = path or APPROVALS
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def is_approved(doc_no, item, expiry, path=None):
    k = _key(doc_no, item, expiry)
    return any(r.get("key") == k for r in load_approvals(path)["records"])


def approve(doc_no, item, expiry, reason="", by="", path=None):
    """记一条审批。重复审批覆盖旧记录，不产生重复行。"""
    j = load_approvals(path)
    k = _key(doc_no, item, expiry)
    j["records"] = [r for r in j["records"] if r.get("key") != k]
    rec = {"key": k, "doc_no": doc_no, "item": item, "expiry": expiry,
           "reason": reason, "by": by,
           "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    j["records"].append(rec)
    save_approvals(j, path)
    return rec


def approvals_for(doc_no, path=None):
    return [r for r in load_approvals(path)["records"] if r.get("doc_no") == doc_no]


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "list":
        j = load_approvals()
        print("效期审批 %d 条 -> %s" % (len(j["records"]), APPROVALS))
        for r in j["records"]:
            print("  %s  %s  有效期 %s  审批于 %s" % (r.get("doc_no"), r.get("item"),
                                                  r.get("expiry"), r.get("at")))
    elif a and a[0] == "approve" and len(a) >= 4:
        print(approve(a[1], a[2], a[3], reason=(a[4] if len(a) > 4 else "")))
    else:
        print("用法：python tools/expiry.py list")
        print("     python tools/expiry.py approve <单据号> <物品名称> <有效期> [备注]")
