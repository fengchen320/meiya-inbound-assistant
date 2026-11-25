# -*- coding: utf-8 -*-
"""emit.py —— 把一批单据变成「两张表」，不连美亚。

用户 2026-11-23 的新定位（m09105）：
    拉牙e在线 / 梅苗苗 / 照片的商品信息 → 转成美亚的产品库模板和入库模板。

所以这个模块只做三件事：
    1. 逐单查本地库（data/local.db），判断哪些行「已建档可直入」、哪些行「要先建档」
    2. 整批合并出两张表：products.xlsx（建档）+ inbound_all.xlsx（入库）
    3. 出一份 decision.xlsx / decision.json，逐单说明结论与下一步

铁律（2026-11-22 血案，写在 §15 第 106 条）：
    建档表里绝不能有本地库已有的物品 —— 美亚会整批拒绝，同批新物品也建不成。
    build_import.build() 用 identity_key（注册证主号+规格+品牌）严格比对，
    本模块不重复实现这个判断，只负责筛选与合并。
"""

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_import  # noqa: E402
import expiry as expiry_mod  # noqa: E402
import localdb  # noqa: E402
import reject  # noqa: E402

EMIT_ROOT = os.path.join(ROOT, "out", "emit")

# 结论
ST_READY = "ready"                 # 全部行都能直接入
ST_NEED_BUILD = "need_build"       # 有行要先建档
ST_AWAITING = "awaiting_approval"  # 有效期不足，等人点头
ST_EMPTY = "empty"                 # 没有可入的行（全赠品 / 全被禁 / 全被效期挡）
ST_AMBIG = "ambiguous"             # 本地库里同身份有多条档案，必须人指定用哪一条

# 文件名里不能出现的字符（Windows）。供应商名里带「（杭州）」这种全角括号没问题，
# 但真实数据里出现过 "/" 与 "*"，直接用会写出失败。
_BAD_FN = re.compile(r'[\\/:*?"<>|]+')


def _sup_key(s):
    """供应商分组键。空值统一成一个组，而不是每个空供应商各成一组。"""
    return (s or "").strip()


def _sup_slug(s):
    """给供应商起一个能当文件名的短名。中文留着——人要在资源管理器里认表。"""
    t = _BAD_FN.sub("_", (s or "").strip())
    t = re.sub(r"\s+", "", t)
    return t[:24] or "unknown"


def lib_from_db(conn=None):
    """把本地库折成 build_import 要的索引。

    只把「有物品编号」的物品放进 by_ident：没有物品编号的（pending）如果也放进去，
    build() 会当成「已建档」跳过，结果它永远进不了建档表 —— 而它其实还没建。

    第四个元素是本地库独有的两个补充索引：
      cats     身份 -> 库里已有的一级分类（关键词规则没命中时回落到它）
      pending  身份 -> 内部编号（等着建档的物品，重新出表必须沿用同一个号）
    """
    own = conn is None
    conn = conn or localdb.connect()
    try:
        mapping, by_ident, used = {}, {}, set()
        cats, pending, names = {}, {}, {}
        rows = conn.execute(
            "SELECT p.id, p.item_no, p.inner_no, p.license_main, p.spec, p.brand,"
            " p.category_l1, p.manufacturer, p.name FROM products p").fetchall()
        for r in rows:
            inner = (r["inner_no"] or "").strip()
            item_no = (r["item_no"] or "").strip()
            if inner:
                used.add(inner)
            # 键必须是 build_import.identity_key 返回的那个**元组**，不能自己拼字符串。
            # build() 里是 by_ident.get(identity_key(...))，两边序列化方式不一致就会
            # 全部查不到 —— 结果是每一行都被判成「待建档」，建档表里塞满已建档物品，
            # 正好踩 2026-11-22 那个整批被拒的血案。实测踩过一次。
            ident = build_import.identity_key(
                r["license_main"] or "", build_import.clean(r["spec"] or ""),
                build_import.norm_brand(r["brand"] or r["manufacturer"] or "") or "")
            if r["category_l1"]:
                cats.setdefault(ident, r["category_l1"])
            # 物品名索引：同一身份在库里有好几条档案时（历史快照带来的重复建档），
            # 单据上的物品名往往正好能区分它们 —— 实测 24 组歧义身份里 22 组
            # 两条档案的物品名不同（啄木鸟工作尖、大/中/小号托盘），
            # 只有「阿莫西林胶囊」这种同名不同物品编号的才是真的无法消歧。
            if item_no:
                names.setdefault(ident, {})[item_no] = (r["name"] or "").strip()
            if not item_no:
                if inner:
                    pending.setdefault(ident, inner)
                continue
            if inner:
                mapping[inner] = item_no
            by_ident.setdefault(ident, []).append((item_no, inner))
        idx = {"cats": cats, "pending": pending, "names": names}
        return mapping, by_ident, used, idx
    finally:
        if own:
            conn.close()


def plan(doc_paths, conn=None, today=None):
    """逐单出计划。不写任何文件，纯计算（便于单测）。"""
    own = conn is None
    conn = conn or localdb.connect()
    lib = lib_from_db(conn)
    # 整批共用一份发号状态：每份单据各自从 inner_base 起发号，一批里就会出现
    # 重复的内部编号（实测 5 单一次跑，建档表里 90005 出现 3 次），美亚报
    # 「内部编号已存在」并把那几行整行退回。
    alloc = {"used": set(lib[2]), "cursor": build_import.ALLOC_BASE}
    try:
        docs = []
        for p in doc_paths:
            with open(p, encoding="utf-8") as f:
                j = json.load(f)
            doc = j.get("document") or {}
            doc_no = str(doc.get("doc_no") or "").strip()
            doc_date = doc.get("doc_date") or ""
            d, rows, warns, products = build_import.build(p, lib=lib, alloc=alloc)
            inbound, need_build, approval, rejected, approved = [], [], [], [], []
            ambiguous = d.get("_ambiguous") or []
            # 歧义行（本地库同身份多条档案）也满足「物品编号为空」，但**绝不能**
            # 并进 need_build：need_build 的出口是 «导建档表»，而歧义行在美亚里
            # 本来就有档案，导建档表只会被回「该物品已存在」整行拒绝
            # （实测 2026-11-24 甲硝唑棒 10321/10325）。它们的出口是 «选一条»。
            amb_keys = {(a.get("物品名称") or "", a.get("规格") or "") for a in ambiguous}
            for r in rows:
                if reject.is_rejected(doc_no, r[0]):
                    rejected.append({"物品名称": r[0], "原因": "已被标记「禁止入库」"})
                    continue
                ex = expiry_mod.check(r[14], doc_date)
                if not ex["ok"]:
                    # 已经人工审批过的（同单号 + 同物品 + 同有效期）直接放行。
                    # run.py 那条路径一直有这道判断，出表这条漏了 —— 后果是：
                    # 人在弹窗里点了「同意入库」，config/expiry_approvals.json 也写了，
                    # 但出表这一步照样判 awaiting_approval，弹窗又弹出来，
                    # 点一次跑一轮，永远出不来（用户 2026-11-24 报的无限循环）。
                    if expiry_mod.is_approved(doc_no, r[0], r[14]):
                        approved.append({"物品名称": r[0], "有效期": r[14],
                                         "原因": ex["msg"]})
                    else:
                        approval.append({"物品名称": r[0], "有效期": r[14],
                                         "原因": ex["msg"]})
                        continue
                if not (r[1] or "").strip():
                    if ((r[0] or ""), (r[2] or "")) in amb_keys:
                        continue     # 已记在 ambiguous 里，由界面让人选档案
                    need_build.append({"物品名称": r[0], "规格": r[2], "内部编号": r[8]})
                    continue
                inbound.append(r)
            if approval:
                state = ST_AWAITING
            elif not inbound and not need_build and not ambiguous:
                state = ST_EMPTY
            elif ambiguous:
                # 歧义优先于「待建档」：一单里两类同时出现时，先让人把档案选掉，
                # 再谈建档 —— 否则人会导出建档表、被美亚退回，回头发现还要选档案。
                state = ST_AMBIG
            elif need_build:
                state = ST_NEED_BUILD
            else:
                state = ST_READY
            docs.append({
                "path": p, "doc_no": doc_no, "doc_date": doc_date,
                "supplier": doc.get("supplier_raw") or "",
                # 来源必须在出表时就记下来：以后翻历史批次，只有单据号时分不清
                # 是牙e 拉的、梅苗苗拉的还是照片识别的，而三者出错方式完全不同。
                "origin": _origin(doc),
                "line_names": [ln.get("product_name") for ln in (j.get("lines") or [])
                               if ln.get("product_name")],
                "total": len(rows), "inbound_rows": inbound, "need_build": need_build,
                "ambiguous": ambiguous,
                "approval": approval, "rejected": rejected, "approved": approved,
                "products": products, "warnings": warns, "state": state,
            })
        return docs
    finally:
        if own:
            conn.close()


_ORIGIN_NAMES = {"yae": "牙e在线", "mmm": "梅苗苗", "photo": "照片"}


def _origin(doc):
    """从抽取结果的 source_sheet 推来源。yae/mmm/photo 之外统一叫「本地文件」。"""
    raw = str(doc.get("source_sheet_origin") or doc.get("source_sheet") or "")
    if raw.startswith("platform:"):
        k = raw.split(":", 1)[1]
        return _ORIGIN_NAMES.get(k, k)
    if raw.startswith("photo:"):
        return _ORIGIN_NAMES["photo"]
    return "本地文件"


def _merge_products(docs):
    """跨单合并建档项。同一身份只出一次，否则建档表里自己跟自己重复。"""
    seen, out = set(), []
    for d in docs:
        for p in d.get("products") or []:
            k = (p.get("批准文号") or "", (p.get("规格") or "").lower(),
                 (p.get("品牌") or "").strip())
            if k in seen:
                continue
            seen.add(k)
            q = dict(p)
            q["_doc"] = d.get("doc_no")
            out.append(q)
    return out


def _doc_state(state):
    """把「行级结论」折成 documents.state 用的「单据级状态」。

    只有 ST_READY（全部行都能直接入）才算 emitted —— 入库表里真的有这张单的行。
    need_build / awaiting_approval / empty 的单据这次并没有把它的入库行交出去
    （待建档的要先导建档表、待审批的要人点头、empty 一行都没有），如果也记成
    emitted，下一批 ledger.check 就会判 duplicate 把它跳过，那些行永远出不来。
    """
    return localdb.STATUS_EMITTED if state == ST_READY else state


def emit(doc_paths, out_dir=None, conn=None, note=""):
    """出表。返回 dict（含文件路径与统计）。"""
    own = conn is None
    conn = conn or localdb.connect()
    try:
        docs = plan(doc_paths, conn)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = out_dir or os.path.join(EMIT_ROOT, stamp)
        os.makedirs(dest, exist_ok=True)

        # 待审批的单据这一轮先不出建档表（用户 2026-11-25 要求：审批之后才建档）。
        # 理由不只是顺序好看：建档表里一旦有这家的新物品，人顺手导进美亚，
        # 那批货就"有了档案"，而审批还没点头 —— 反过来等审批通过再建，
        # 人看到的一定是"这批已经能入库"的东西，不会建出一批用不上的档案。
        held_docs = [d for d in docs if d["state"] == ST_AWAITING]
        held_ids = {id(d) for d in held_docs}
        # 记在 doc 上而不是只在 decision.json 里算：pipeline 的 awaiting 分支
        # 与单测都要能直接读到「这一单压住了几个建档项」。
        for d in docs:
            d["products_held"] = len(d.get("products") or []) if id(d) in held_ids else 0
        products = _merge_products([d for d in docs if id(d) not in held_ids])
        # 仍然要把待审批的那些物品记进本地库（pending）：下一轮审批通过后重新出表，
        # 它们必须沿用同一批内部编号，否则同一样东西会拿到两个号。
        products_all = _merge_products(docs)
        inbound_rows = []
        for d in docs:
            inbound_rows.extend(d["inbound_rows"])

        prod_file = os.path.join(dest, "products.xlsx")
        if products:
            build_import.write_products(products, prod_file)
        elif os.path.exists(prod_file):
            os.remove(prod_file)
            prod_file = ""
        else:
            prod_file = ""

        # ── 入库表按**供应商**分组 ────────────────────────────────────
        # 美亚入库单模板的 15 列里没有「供应商」：它是**表头**字段，人导入前自己填。
        # 所以把两家供应商的行并进同一张表，人就只能填一个供应商，另一半的行
        # 会挂到错误的供应商名下 —— 而且在美亚那边看起来是导入成功，最难发现。
        # 「整体合并」的正确含义 therefore 是：同一供应商的所有单合并成一张，
        # 跨供应商分文件。一批里通常只有一家，所以多数时候仍然只有一张表。
        groups, by_sup = [], {}
        for d in docs:
            if not d["inbound_rows"]:
                continue
            k = _sup_key(d.get("supplier"))
            g = by_sup.get(k)
            if g is None:
                g = {"supplier": k, "rows": [], "doc_nos": [], "doc_dates": []}
                by_sup[k] = g
                groups.append(g)
            g["rows"].extend(d["inbound_rows"])
            g["doc_nos"].append(d["doc_no"] or os.path.basename(d["path"]))
            g["doc_dates"].append(d.get("doc_date") or "")

        # 上一轮留在同一目录里的入库表必须清掉：pipeline 复用同一个 out 目录，
        # 留着旧的 inbound_*.xlsx，人下次会照着旧表导一遍（库存翻倍）。
        for fn in os.listdir(dest):
            if re.match(r"^inbound(_[^/\\]*)?\.xlsx$", fn):
                try:
                    os.remove(os.path.join(dest, fn))
                except OSError:
                    pass

        used_fn = set()
        for g in groups:
            base = "inbound_all" if len(groups) == 1 else "inbound_" + _sup_slug(g["supplier"])
            fn, n = base + ".xlsx", 2
            while fn in used_fn:      # 两家供应商 slug 撞车时不能互相覆盖
                fn, n = "%s_%d.xlsx" % (base, n), n + 1
            used_fn.add(fn)
            g["file"] = os.path.join(dest, fn)
            g["rows_count"] = len(g["rows"])
            build_import.write_inbound(g["rows"], {"supplier_raw": g["supplier"]}, g["file"])

        # 一家供应商时沿用旧字段（inbound_file 指向唯一的表）：老批次、老脚本、
        # 对账都按这个字段找表，凭空改名等于把已有的东西弄坏。
        # 多家时 inbound_file 置空，改由 inbound_groups 指明每张表是谁的。
        in_file = groups[0]["file"] if len(groups) == 1 else ""

        # 本次要建的档记进本地库（pending）：下一批同样的物品就不用再发新号，
        # 也不会被认成「已建档」而漏掉建档表。
        # 用 products_all 而不是 products：待审批那几单的物品这一轮虽然没进建档表，
        # 号也已经发出来了，必须记下来，否则下一轮会重新发一批新号（同一物品两条档案）。
        for p in products_all:
            localdb.remember_product(p, conn)

        # 批次落库
        counts = {
            "docs": len(docs),
            "inbound_rows": len(inbound_rows),
            "need_build": sum(len(d["need_build"]) for d in docs),
            # 歧义行单独计数：它不是「要去建档」，是「库里有多条档案要人挑一条」，
            # 混进 need_build 会让人误以为导张建档表就好了（那正是上一版的 bug）。
            "ambiguous": sum(len(d.get("ambiguous") or []) for d in docs),
            "products": len(products),
            # 因为待审批而压住没进建档表的物品数：界面要说清「这些不是没有，
            # 是等审批」，否则人看见建档表少了几个物品会以为漏了。
            "products_held": len(products_all) - len(products),
            "awaiting": sum(len(d["approval"]) for d in docs),
            # 入库表是几张、分别是谁的：界面要按这个把链接摆出来，
            # 而不是让程序另外猜一遍（猜错就是把 A 家的表当 B 家的导）。
            "inbound_files": len(groups),
        }
        inbound_groups = [{"supplier": g["supplier"], "file": g["file"],
                           "rows": g["rows_count"], "docs": g["doc_nos"],
                           "dates": g["doc_dates"]} for g in groups]
        cur = conn.execute(
            "INSERT INTO batches (created_at, out_dir, counts) VALUES (?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), dest,
             json.dumps(counts, ensure_ascii=False)))
        batch_id = cur.lastrowid
        for d in docs:
            conn.execute(
                "INSERT OR REPLACE INTO documents"
                " (doc_no, origin, doc_date, supplier, amount, state, batch_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (d["doc_no"] or os.path.basename(d["path"]), d.get("origin") or "",
                 d["doc_date"],
                 d["supplier"], 0, _doc_state(d["state"]), batch_id,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            conn.execute(
                "INSERT INTO emissions (batch_id, doc_no, products_file, inbound_file, at)"
                " VALUES (?,?,?,?,?)",
                (batch_id, d["doc_no"], prod_file, in_file,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

        dec = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "batch_id": batch_id, "out_dir": dest, "note": note,
            "counts": counts,
            "files": {"products": prod_file, "inbound": in_file},
            "inbound_groups": inbound_groups,
            "documents": [{
                "doc_no": d["doc_no"], "doc_date": d["doc_date"],
                "origin": d.get("origin") or "",
                "supplier": d["supplier"], "state": d["state"],
                "lines": d["total"], "inbound": len(d["inbound_rows"]),
                "need_build": d["need_build"], "ambiguous": d.get("ambiguous") or [],
                "approval": d["approval"],
                "rejected": d["rejected"],
                # 这单的建档项这一轮被压住了（等审批）：界面/报告要说明白，
                # 否则「本地库没有它但建档表里也没有」看起来像程序漏了一行。
                "products_held": d.get("products_held") or 0,
            } for d in docs],
        }
        with open(os.path.join(dest, "decision.json"), "w", encoding="utf-8") as f:
            json.dump(dec, f, ensure_ascii=False, indent=1)

        return {"ok": True, "out_dir": dest, "products_file": prod_file,
                "inbound_file": in_file, "inbound_groups": inbound_groups,
                "counts": counts, "docs": docs, "decision": dec}
    finally:
        if own:
            conn.close()


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="把一批单据变成两张可导入美亚的表（不连美亚）")
    ap.add_argument("--in", dest="src", required=False, help="单据目录（含 *_extracted.json）")
    ap.add_argument("--files", nargs="*", default=None, help="直接指定 extracted json")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    paths = []
    if a.files:
        paths = [os.path.abspath(p) for p in a.files]
    elif a.src:
        src = os.path.abspath(a.src)
        for fn in sorted(os.listdir(src)):
            if fn.endswith("_extracted.json"):
                paths.append(os.path.join(src, fn))
    if not paths:
        print("没有可处理的 *_extracted.json")
        return 2

    r = emit(paths, a.out)
    c = r["counts"]
    print("出表完成 -> %s" % r["out_dir"])
    print("  %d 份单据：可直接入 %d 行 / 待建档 %d 行 / 待审批 %d 行 / 档案歧义 %d 行"
          % (c["docs"], c["inbound_rows"], c["need_build"], c["awaiting"],
             c.get("ambiguous") or 0))
    if c.get("products_held"):
        print("  （其中 %d 个新物品属于待审批的单据，按规矩等审批通过后才进建档表）"
              % c["products_held"])
    print("  建档表：%s" % (r["products_file"] or "（本批无需建档）"))
    if not r.get("inbound_groups"):
        print("  入库表：（本批没有可直接入的行）")
    else:
        for g in r["inbound_groups"]:
            print("  入库表：%s（%s，%d 行）"
                  % (g["file"], g["supplier"] or "供应商未识别", g["rows"]))
    for d in r["docs"]:
        print("  %-26s %-16s 入 %2d / 建档 %2d" % (
            d["doc_no"], d["state"], len(d["inbound_rows"]), len(d["need_build"])))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
