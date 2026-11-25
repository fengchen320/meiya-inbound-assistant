# -*- coding: utf-8 -*-
"""ledger.py —— 入库台账：同一份单据只允许真入库一次。

为什么必须有这一层：
    pipeline 是「一条命令跑完」，而人最常见的动作就是「跑一遍看看，不行改改再跑一遍」。
    目前 committed 之后没有任何记录，重跑同一条命令会把同一张随货单再入一次库——
    库存翻倍、账目对不上，而且美亚的入库单列表里**没有源单据号字段**
    （只有系统自己的 RK… 号，实测 25 个字段全看过），事后根本无法按单号反查去重。
    所以去重只能靠自己在提交那一刻记一笔。

三层防护（本模块负责第 1、2 层，第 3 层在 pipeline 里）：
    L1 本地台账（本模块）：doc_no + 明细指纹。快、准、离线可用。
    L2 美亚对账（pipeline.check_meiya_duplicate）：列表接口按「日期+供应商+数量+金额」
       软比对。只能当兜底——没有源单据号，同一天同一供应商两单金额相同就会误判，
       所以它只用于「台账丢了（换机器/删了 config）」时提示人再看一眼，不单独阻断。
    L3 提交前二次确认（pipeline.one_doc）：dry-run 到 commit 之间可能隔了很久，
       中间别的东西也可能改过库存；点「直接入库」前再查一次台账。

为什么记指纹而不只记单号：
    单据会被修订（改数量、换批号、补效期），单号不变但内容变了。只按单号判重会把
    「改过的单据」当成「已入过」直接跳过，人以为入库了其实入的是旧数据。这里两种
    情况分开报：同号同指纹 = 已入库（跳过）；同号不同指纹 = 内容变了（必须人工确认）。

台账是账本不是缓存：只追加，不自动清理，也不因为解析失败就丢掉旧记录。
"""
import hashlib
import io
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "config", "inbound_ledger.json")

# 参与指纹的字段：能唯一确定「这一行货」的最小集合。
# 不放 unit_price：折扣/赠品行价格会变，但数量与批号不变就该算同一批货；
# 放 amount 而不是 price，是因为金额是最终结算值，改价一定会改金额。
_FP_FIELDS = ("product_name", "spec", "lot_no", "quantity", "amount")


def _norm(v):
    """归一化：去所有空白、统一大写。

    OCR 出来的「BCSD-1050 Dynamix 直径5.0mm」和「BCSD-1050Dynamix直径5.0mm」
    是同一行货，空白差异不该被当成不同单据。
    """
    return re.sub(r"\s+", "", str(v if v is not None else "")).upper()


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def norm_doc_no(doc_no):
    """单据号归一化。空单号返回空串（调用方必须自己决定空单号怎么处理）。"""
    return _norm(doc_no)


def fingerprint(lines):
    """明细指纹：每行取 5 个字段拼成一条，排序后再整体 hash。

    排序是必须的：OCR 切行顺序在多行单据上并不稳定，同一份单据两次识别可能行序不同。
    """
    parts = []
    for ln in lines or []:
        parts.append("|".join(_norm(ln.get(k)) for k in _FP_FIELDS))
    parts.sort()
    raw = "\n".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def total_amount(lines):
    s = 0.0
    for ln in lines or []:
        a = _num(ln.get("amount"))
        if a:
            s += a
    return round(s, 2)


def load(path=None):
    p = path or LEDGER
    if not os.path.exists(p):
        return {"version": 1, "records": []}
    try:
        with io.open(p, encoding="utf-8") as f:
            j = json.load(f) or {}
    except Exception:
        # 台账坏了不能装作没事：那等于所有去重瞬间失效。这里保留坏文件再建新的，
        # 让人事后还能把旧记录捞回来。
        bad = p + ".bad." + time.strftime("%Y%m%d_%H%M%S")
        try:
            os.replace(p, bad)
        except OSError:
            pass
        print("[台账] 读不出来（已另存为 %s），本次按空台账继续" % os.path.basename(bad))
        return {"version": 1, "records": []}
    if not isinstance(j.get("records"), list):
        j["records"] = []
    j.setdefault("version", 1)
    return j


def save(j, path=None):
    p = path or LEDGER
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)   # 原子替换：中途断电不会留下半个 JSON


def check(doc_no, lines=None, path=None):
    """查台账。返回 (status, record)。

    status:
        "new"     没有这个单号的记录 —— 可以入库
        "dup"     同单号 + 同指纹 —— 已经入过库，必须跳过
        "changed" 同单号 + 不同指纹 —— 单据被改过，必须人工确认
        "noid"    单据号为空 —— 无法判重，调用方必须阻断
    """
    key = norm_doc_no(doc_no)
    if not key:
        return "noid", None
    j = load(path)
    fp = fingerprint(lines) if lines is not None else None
    hits = [r for r in j["records"] if norm_doc_no(r.get("doc_no")) == key]
    if not hits:
        return "new", None
    if fp is None:
        return "dup", hits[-1]
    same = [r for r in hits if r.get("fingerprint") == fp]
    if same:
        return "dup", same[-1]
    return "changed", hits[-1]


def record(doc_no, lines, extra=None, path=None):
    """记一笔。只在「真的点了直接入库」之后调用 —— dry-run 不算入库。"""
    key = norm_doc_no(doc_no)
    if not key:
        raise ValueError("单据号为空，不能记台账（否则下次无法判重）")
    j = load(path)
    rec = {
        "doc_no": doc_no,
        "fingerprint": fingerprint(lines),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lines": len(lines or []),
        "amount": total_amount(lines),
    }
    rec.update(extra or {})
    j["records"].append(rec)
    save(j, path)
    return rec


def confirm(doc_no, note="", path=None):
    """人点了「确认入库」：把台账里这一单标成「已在美亚入库」。

    为什么要有这一步（用户 2026-11-25）：
        以前台账是「出表那一刻就记一笔」，含义是「已出过表」。但程序不操作美亚，
        表出出来了人却可能压根没导进美亚 —— 台账说有、库存里没有，月底一对账
        差一批货，而且查不出是哪一批（美亚入库单没有源单据号）。所以现在把
        「出表」和「确认入库」分成两个动作：出表记 emitted_at，人导完回来说一声
        才记 confirmed_at。
    为什么是软确认而不是补一条新记录：
        判重靠「单据号 + 明细指纹」，台账里同一单只该有一条；补新记录会让
        check() 拿到两条同号记录（虽然同指纹仍是 dup，但 first_at / 金额会读到最后
        那条），对账时也要去重。就地打标记最省事，历史记录也还看得见。
    返回 (记录, 是否是第一次确认)。找不到这一单返回 (None, False)。
    """
    key = norm_doc_no(doc_no)
    if not key:
        raise ValueError("单据号为空，不能确认入库")
    j = load(path)
    hit = None
    for r in j["records"]:
        if norm_doc_no(r.get("doc_no")) == key:
            hit = r          # 同一单被重出过表时留最后一条，与 check() 口径一致
    if hit is None:
        return None, False
    first = not hit.get("confirmed_at")
    hit["confirmed_at"] = hit.get("confirmed_at") or first_now()
    hit["confirmed_by"] = "ui"
    if note:
        hit["confirm_note"] = note
    save(j, path)
    return hit, first


def unconfirm(doc_no, path=None):
    """撤销「确认入库」（点错了 / 导进美亚又被退回）。返回被撤销的记录或 None。

    保留 confirmed_at 的历史没必要：这个标记的全部意义就是「现在到底入了没有」，
    留一个「曾经确认过」会让人对着一批不存在的库存对账。
    """
    key = norm_doc_no(doc_no)
    if not key:
        raise ValueError("单据号为空")
    j = load(path)
    hit = None
    for r in j["records"]:
        if norm_doc_no(r.get("doc_no")) == key:
            hit = r
    if hit is None or not hit.get("confirmed_at"):
        return None
    hit.pop("confirmed_at", None)
    hit.pop("confirmed_by", None)
    hit.pop("confirm_note", None)
    save(j, path)
    return hit


def is_confirmed(doc_no, path=None):
    """这一单确认入库了吗。老记录没有 confirmed_at 字段 → False。

    老台账（2026-11-25 之前）的语义就是「已经导进美亚了」（那时程序会点直接入库），
    但它们的 note 是「已出表」还是老文案并不可靠，所以一律当未确认 —— 人会在台账上
    看到它们「未确认」，点一次确认就补上了。宁可多问一次，也不能替人担保库存。
    """
    key = norm_doc_no(doc_no)
    if not key:
        return False
    for r in load(path)["records"]:
        if norm_doc_no(r.get("doc_no")) == key:
            return bool(r.get("confirmed_at"))
    return False


def backfill_confirmed(path=None, only_before=None, note=""):
    """把**已有的**台账记录统一标成「已入库」（一次性迁移）。

    为什么需要（用户 2026-11-25）：
        分出「已出表 / 已入库」两个标记之后，2026-11-25 之前的 64 条记录全都没有
        confirmed_at，台账里一片「未确认」，而它们在当时的流程里都是真入过库的
        （那时程序会点「直接入库」，带 RK 号的 15 条就是证据）。让用户对着这些
        老账一条条点确认既没意义、也没法点（旧记录对应的批次早就翻过去了）。
        所以一次性补上，并写明这是迁移而不是人当场确认的。

    参数：
        only_before  只补 at 早于这个时间的记录（"YYYY-MM-DD HH:MM:SS"），None = 全部
        note         写进 confirm_note 的说明（默认自带一句迁移说明）
    返回补了几条。
    """
    j = load(path)
    stamp = note or "迁移：2026-11-25 之前的台账记录按当时流程已实际入库"
    n = 0
    for r in j["records"]:
        if r.get("confirmed_at"):
            continue
        if only_before and str(r.get("at") or "") >= only_before:
            continue
        # confirmed_at 用记录时间而不是现在：这一单是那天入的库，
        # 写成今天会让人以为昨天才补录入，对账时又对不上。
        r["confirmed_at"] = r.get("at") or first_now()
        r["confirmed_by"] = "migrate"
        r["confirm_note"] = stamp
        n += 1
    if n:
        save(j, path)
    return n


def first_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def set_order_no(doc_no, order_no, path=None):
    """把美亚的 RK 单号补到台账记录上（入库那一刻常常拿不到）。

    为什么要有这个函数：真入库后回查 RK 号有一次被 601001 挡住，
    台账里 meiya_order_no 就留了空。空着的记录判重仍然有效（判重键是
    单据号 + 明细指纹），但人想在美亚页面搜那一张单就搜不到 ——
    所以允许事后按金额/数量配上再补写。
    """
    key = norm_doc_no(doc_no)
    if not key:
        raise ValueError("单据号为空")
    j = load(path)
    n = 0
    for r in j["records"]:
        if norm_doc_no(r.get("doc_no")) == key:
            r["meiya_order_no"] = order_no
            n += 1
    if n:
        save(j, path)
    return n


def stats(path=None):
    j = load(path)
    return len(j["records"]), (j["records"][-1] if j["records"] else None)


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap_argv = list(sys.argv[1:] if argv is None else argv)
    if not ap_argv or ap_argv[0] in ("-h", "--help"):
        print(__doc__)
        print("用法：")
        print("  python tools/ledger.py list          列出全部已入库记录")
        print("  python tools/ledger.py check <单号>   查这个单号入过没有")
        return 0
    cmd = ap_argv[0]
    if cmd == "list":
        j = load()
        print("台账 %s：%d 条" % (LEDGER, len(j["records"])))
        for r in j["records"]:
            print("  %s  %s  %s 行  %s 元  %s" % (
                r.get("at"), r.get("doc_no"), r.get("lines"),
                r.get("amount"), r.get("meiya_order_no") or ""))
        return 0
    if cmd == "backfill":
        # python tools/ledger.py backfill <单据号> <RK 单号>
        if len(ap_argv) < 3:
            print("用法：python tools/ledger.py backfill <单据号> <RK 单号>")
            return 2
        n = set_order_no(ap_argv[1], ap_argv[2])
        print("已补写 %d 条：%s -> %s" % (n, ap_argv[1], ap_argv[2]))
        return 0 if n else 1
    if cmd == "check":
        if len(ap_argv) < 2:
            print("要给单号")
            return 2
        st, rec = check(ap_argv[1])
        print("status", st)
        if rec:
            print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 0
    print("未知命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
