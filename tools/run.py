# -*- coding: utf-8 -*-
"""run.py — 一条命令跑完「读单据 → 生成建档表 + 入库表」。

在此之前，链条是三段手动接的：读表看人、写 extracted.json 靠手敲、跑 build_import
靠记参数。一份单据还行，按月结十几份就会出错——错的地方还很隐蔽（内部编号重号、
物品编号张冠李戴）。这个脚本把三段接成一条，并且强制每一步都留痕。

用法：
    # 单文件
    python tools/run.py --in out/某单据.xlsx
    # 整目录，按单据日期过滤（对应「自定义日期」需求）
    python tools/run.py --in D:/单据 --from 2026-01-01 --to 2026-11-21
    # 只看解析结果，不生成导入表
    python tools/run.py --in xxx.xlsx --dry-run

产物（默认落 out/run/）：
    <doc>_extracted.json      抽取结果（原样留档，含 warnings）
    <doc>_report.json         逐行告警与取号结果（排查用，不是导入文件）
    batch_summary.json        本批汇总：哪些可直接导、哪些要建档、哪些卡住

**逐单的 _products.xlsx / _inbound.xlsx 已停写（2026-11-24）**：
用户原话「模板调整为整体合并建档、入库，一个订单一个订单效率太低了」。
导入文件只剩整批两张，由 emit.emit() 写：
    products.xlsx             所有单的新建档物品合并去重（同一物品只建一次档）
    inbound_<供应商>.xlsx      入库表按供应商分组，一家一张（只有一家时叫 inbound_all.xlsx）

关键设计：
1. 模板靠**表头签名**自动识别，不靠文件名。用户给的文件名是「商品详情.xlsx」这种，
   跟供应商没关系；识别错了整批数据都会错列。
2. 同一批次共用一份产品库快照与一段内部编号空间。分开跑最容易出的错就是两份单据
   各发一个 90001，导入后产品库出现两条撞号档案。
3. 抽不出来就明说。识别不到模板、找不到注册证、批号为空——都进 warnings 并阻断，
   不允许「差不多就导进去」。库存错了要盘点到才发现。
"""

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 禁令先装钩子（no_meiya 在 import 时包住 socket/urllib 出口），必须排在其它本地模块之前。
import no_meiya  # noqa: E402,F401  禁止访问美亚美牙（myyun.com）
import build_import
import emit as emit_mod
import expiry as expiry_mod
import reject as reject_mod
import read_template
import xlsread

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPING_DIR = os.path.join(ROOT, "config", "template_mapping")
DEFAULT_LIBRARY = os.path.join(ROOT, "out", "export_prod_new.xlsx")
OUT_DIR = os.path.join(ROOT, "out", "run")

SHEET_EXT = (".xls", ".xlsx", ".xlsm")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
# 平台拉单（lib_mmm 等）直接产出的抽取结果。让它走同一条下游，而不是为平台
# 单独再写一套建档逻辑——否则「建档表长什么样」会有两个版本，早晚不一致。
EXTRACTED_EXT = ("_extracted.json",)
SUPPORTED_EXT = SHEET_EXT + PHOTO_EXT + EXTRACTED_EXT

# 照片没有表头可读，只能按版式（profile）认。这里给出「文件名关键字 → profile」
# 的默认对应；识别不到就阻断，不猜——照片按错版式解析会静默错列。
PHOTO_PROFILE_HINT = [
    ("随货", "shipping_v1"),
]


def load_mappings():
    """装载所有「读懂别人表」的映射；跳过 purpose=writer_output 的（那是给美亚生成表用的）。"""
    out = []
    for fn in sorted(os.listdir(MAPPING_DIR)):
        if not fn.endswith(".yaml"):
            continue
        mp = read_template.load_mapping(fn)
        if (mp.get("purpose") or "").strip() == "writer_output":
            continue
        out.append(mp)
    return out


def detect_mapping(path, mappings):
    """按表头签名匹配模板。返回 (mapping, 得分, 明细)。

    评分只看「yaml 里声明的列名在表头里出现的比例」，不看顺序：
    顺序会变，列名相对稳定。
    """
    best, best_score, detail = None, -1.0, []
    try:
        _, _, header, data = xlsread.read_rows(
            path, sheet=None, header_row=1)
    except Exception as e:  # 读不了就先按每个模板自己的 header_row 试
        header, data = [], []
        detail.append("一次读表失败：%s" % e)

    for mp in mappings:
        hr = int(mp.get("header_row") or 1)
        try:
            _, _, h, d = xlsread.read_rows(path, sheet=mp.get("sheet"), header_row=hr)
        except Exception as e:
            detail.append("%s 读取失败：%s" % (mp.get("template_id"), e))
            continue
        srcs = [c["src"] for c in (mp.get("columns") or [])]
        if not srcs:
            continue
        hit = sum(1 for s in srcs if read_template._header_index(h, s) >= 0)
        score = hit / float(len(srcs))
        detail.append("%s: %.0f%% (%d/%d)" % (mp.get("template_id"), score * 100, hit, len(srcs)))
        if score > best_score:
            best, best_score = mp, score
    return best, best_score, detail


def _doc_stem(path, doc):
    """给产物起名：优先用单据号，其次文件名。单据号能天然去重。"""
    no = (doc.get("doc_no") or "").strip()
    if no:
        return "".join(ch for ch in no if ch.isalnum() or ch in "-_")
    return os.path.splitext(os.path.basename(path))[0]


def _mtime_date(path):
    return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))


def _guess_profile(path):
    """按文件名关键字猜照片版式。猜不到返回 None，由调用方阻断。"""
    low = os.path.basename(path).lower()
    for key, pid in PHOTO_PROFILE_HINT:
        if key.lower() in low:
            return pid
    # 兜底：只有唯一一个 profile 时才敢用（多份版式混跑必须显式指定）
    try:
        files = [f for f in os.listdir(os.path.join(ROOT, "config", "photo_profiles"))
                 if f.endswith(".json")]
    except OSError:
        return None
    if len(files) == 1:
        return files[0][:-5]
    return None


# 平台接口里「没有批号」有三种写法：空串、"/"、字面 "N/A"（牙e 的奥美科托槽就是这种）。
# 只判空串会把它当成有批号，结果是提示里不报、审批单上也看不到这一行。
_LOT_EMPTY = {"", "/", "n/a", "na", "none", "null", "无", "-", "—"}


def _lot_ok(v):
    return str(v or "").strip().lower() not in _LOT_EMPTY


def _ymd(v):
    """把各种写法的日期压成 YYYYMMDD 用于比较。

    单据日期有三个来源，格式各不相同：yaml 抽出来的 "20260630"、模板给的
    "2026-06-30"、以及取不到时回退的文件修改时间。直接按字符串比较会在
    跨格式时给出错误结果（"2026-11-21" < "20260101" 为真），所以统一压成
    8 位数字再比。
    """
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[:8]


# 单据来源的中文名。界面上只看单据号是分不清「牙e 拉的」和「照片识别的」——
# 两者的出错方式完全不同（前者是模板/接口问题，后者是 OCR 问题），排查时要先看这个。
_ORIGIN_LABEL = {
    "yae": "牙e在线",
    "mmm": "梅苗苗",
    "photo": "照片",
}


def _origin_label(doc, tpl_id, path):
    """来源标签：优先取抽取时写下的 platform:xxx / photo:xxx，其次按路径猜。"""
    raw = str((doc or {}).get("source_sheet_origin") or "").strip()
    tpl = str(tpl_id or "")
    if raw.startswith("platform:"):
        return _ORIGIN_LABEL.get(raw.split(":", 1)[1], raw.split(":", 1)[1])
    if raw.startswith("photo:") or tpl.startswith("photo:"):
        return _ORIGIN_LABEL["photo"]
    if tpl.startswith("platform:"):
        return _ORIGIN_LABEL.get(tpl.split(":", 1)[1], tpl.split(":", 1)[1])
    # 平台抽取结果里 source_sheet 会是 platform:yae；老的落盘文件没有 origin 字段，
    # 但路径里通常还留着 out/pull/<平台>/，这是最后的线索。
    low = str(path or "").replace("\\", "/").lower()
    for k, v in _ORIGIN_LABEL.items():
        if "/pull/%s/" % k in low:
            return v
    return "本地文件"


def _load_extracted(path):
    """读平台拉下来的抽取结果。字段缺失一律当空，交给 build_import 去阻断，
    不在这里补默认值——补了就等于替用户编数据。"""
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    doc = j.get("document") or {}
    lines = j.get("lines") or []
    warns = list(j.get("warnings") or [])
    tpl_id = doc.get("source_sheet") or "extracted"
    return doc, lines, warns, tpl_id


def process_one(path, mapping, out_dir, library, inner_base, extra_used, dry_run=False,
                d_from=None, d_to=None, profile=None):
    if path.lower().endswith(EXTRACTED_EXT):
        doc, lines, warns, tpl_id = _load_extracted(path)
    elif profile:
        # 照片通道：输出结构与表格通道完全一致，因此下游 build_import 一行都不用改。
        import ocr_photo
        doc, lines, warns = ocr_photo.extract(path, profile)
        tpl_id = "photo:" + profile
    else:
        doc, lines, warns = read_template.read(
            path, mapping["template_id"] if isinstance(mapping, dict) else mapping)
        tpl_id = mapping.get("template_id") if isinstance(mapping, dict) else mapping
    stem = _doc_stem(path, doc)
    rec = {
        "source": os.path.abspath(path),
        "template_id": tpl_id,
        "origin": _origin_label(doc, tpl_id, path),
        "doc_no": doc.get("doc_no"),
        "doc_date": doc.get("doc_date") or _mtime_date(path),
        "line_count": len(lines),
        "read_warnings": warns,
        "stem": stem,
        # 行名一并记下来：界面的「禁止入库」是按行打的标记，而它只拿得到
        # pipeline_summary.json。不写在这里，界面上标一次要回去读抽取结果文件，
        # 而那份文件在 tmp 清理后就没了（历史批次正是这种情况）。
        "line_names": [ln.get("product_name") for ln in lines if ln.get("product_name")],
    }
    os.makedirs(out_dir, exist_ok=True)
    ex_path = os.path.join(out_dir, stem + "_extracted.json")
    with open(ex_path, "w", encoding="utf-8") as f:
        json.dump({"document": doc, "lines": lines, "warnings": warns}, f, ensure_ascii=False, indent=1)
    rec["extracted"] = ex_path

    # 日期过滤必须在生成导入表**之前**：被过滤掉的单据若也生成了 inbound.xlsx，
    # 用户看到文件就会直接导，等于日期范围形同虚设。抽取结果照样留档备查。
    dd = _ymd(rec["doc_date"])
    if d_from and dd and dd < _ymd(d_from):
        rec["skipped"] = "早于 --from"
        return rec, set()
    if d_to and dd and dd > _ymd(d_to):
        rec["skipped"] = "晚于 --to"
        return rec, set()

    if dry_run or not lines:
        return rec, set()

    base = os.path.join(out_dir, stem)
    # 产品库在本地 data/local.db（用户 2026-11-23 改的定位：不再读美亚）。
    # --library 给了 xlsx 就用它（老命令、单测），没给才折本地库。
    # 走 lib= 而不是 library_path=：lib_from_db 只把「有物品编号」的物品放进
    # by_ident，pending 的留在外面，这样它下一批还能进建档表。
    lib = build_import.load_library(library) if library else emit_mod.lib_from_db()
    d, rows, bwarns, products = build_import.build(
        ex_path, library, inner_base=inner_base, override_path=None,
        extra_used=extra_used, lib=lib)
    # 逐单的 _inbound.xlsx / _products.xlsx 已经停写（用户 2026-11-24 的要求：
    # 「模板调整为整体合并建档、入库，一个订单一个订单效率太低了」）。
    # 这里只出**整批**的两张表，由 emit.emit() 统一写：
    #   products.xlsx   所有单的新建档物品合并去重（同一物品只建一次档）
    #   inbound_*.xlsx  按供应商分组，一家一张（供应商是入库单表头字段，混了就错账）
    # 逐单还留着的是 _extracted.json（原始留档）与 _report.json（逐行告警，排查用），
    # 它们不是用来导入的文件，所以不会让人误以为「一单要导一次」。
    # 老批次目录里遗留的旧单表也要清掉，否则下次打开目录还会看见它们。
    for _fn in ("_inbound.xlsx", "_products.xlsx"):
        _old = base + _fn
        if os.path.exists(_old):
            os.remove(_old)
    prod_path = ""
    with open(base + "_report.json", "w", encoding="utf-8") as f:
        json.dump({"doc": d, "rows": len(rows), "warnings": bwarns, "products": products},
                  f, ensure_ascii=False, indent=1)

    # 只把本次新分配的号算进「已占用」。产品库里 301 条原有内部编号也会出现在
    # used 里，若拿 len(used) 当偏移，第二份单据的起点会被顶到几百号之外，
    # 号段立刻不连续——看上去只是难看，实际是日后对账时找不到号的原因。
    # 可提交性：只有「物品编号有值 + 一级分类在允许字面值内 + 批号非空」三者全中的行
    # 才允许进美亚。少一项都会被服务端整行拒绝，与其让用户导进去看到红字，不如在
    # 本地就把它挑出来——这也是「抽不出来就明说」的落地。
    allowed_l1 = set(build_import.ALLOWED_L1)
    blockers = []
    for p in products:
        why = []
        if not p.get("一级分类"):
            why.append("一级分类未推断出，必须人工填写")
        elif p["一级分类"] not in allowed_l1:
            why.append("一级分类「%s」不是分类树字面值" % p["一级分类"])
        if not p.get("拆包数量") or not p.get("拆包单位"):
            why.append("拆包数量/单位缺失")
        if why:
            # 规格必须一起带上：界面上的处置是按「单据号 + 物品名 + 规格」记的
            # （同名不同规格很常见：牙胶尖 04-30# / 04-35#，它们可能属于不同分类）。
            # 只给物品名，界面就没法把人的选择写回到正确的那一行上。
            blockers.append({"物品名称": p.get("物品名称"), "规格": p.get("规格"),
                             "原因": why})
    # 采购数量必须 > 0：数量 0 的行美亚整行拒绝（实测 MS260127163114015050），
    # 而且拒绝后表格只留「暂无数据」占位行，看上去「有 1 行」—— 不在这里拦，
    # 就会一路走到入库表、点完「直接入库」什么也没提交，单据还记成已入库。
    ready_rows = 0
    not_ready = []
    lot_missing = []      # 批号缺失（不阻断，只提示）
    short_expiry = []     # 效期缺失/不足一年（阻断，等用户审批）
    approved_expiry = []  # 已经过人工审批、本次放行的效期问题行
    rejected_now = []     # 被「禁止入库」拦下的行
    doc_date = (d or {}).get("doc_date")
    for r in rows:
        try:
            q = float(r[5] or 0)
        except (TypeError, ValueError):
            q = 0.0
        why = []
        # 「禁止入库」是人工永久判定：这东西不该进库存（赠品、样品、客户自备料…），
        # 与效期无关，也不该再问人一次。放在最前面，因为它的原因最容易被别的
        # 提示盖掉（比如同一行既缺效期又不该入，说「效期不足」会让人去补效期）。
        if reject_mod.is_rejected(rec.get("doc_no"), r[0]):
            why.append("已被标记「禁止入库」（人工判定这东西不该进库存），"
                       "如需撤销请到结果里取消标记")
            rejected_now.append(r[0])
        if not (r[1] or "").strip():
            why.append("物品编号为空（还没建档或取号失败）")
        if not (r[9] or "").strip():
            why.append("一级分类为空")
        if q <= 0:
            why.append("采购数量是 %g，美亚会整行拒绝" % q)
        # 批号：**缺了不再阻断**（用户 2026-11-23 定下的新规则：有有效期、无批号可入库）。
        # 批号只进提示，因为它是追溯用的，缺了还能靠收货记录倒查。
        if not _lot_ok(r[12]):
            lot_missing.append(r[0])
        # 有效期：缺失或距单据日期不足一年 → 坚决不入库，标红等人点头。
        # 这是安全项不是追溯项：不知道效期就不知道这批还能不能用。
        ex = expiry_mod.check(r[14], doc_date)
        if not ex["ok"]:
            # 已经人工审批过的（同一单号 + 同一物品 + 同一有效期）直接放行。
            # 键里带有效期是有意的：效期变了就是换了一批货，旧审批不能沿用。
            if expiry_mod.is_approved(rec.get("doc_no"), r[0], r[14]):
                approved_expiry.append({"物品名称": r[0], "有效期": r[14],
                                        "原因": ex["msg"]})
            else:
                why.append(ex["msg"])
                short_expiry.append({"物品名称": r[0], "原因": ex["msg"],
                                     "code": ex["code"], "days": ex["days"],
                                     "有效期": r[14]})
        if not why:
            ready_rows += 1
        else:
            # 逐行记下「为什么不能提交」，pipeline 直接拿它当 blocked_build 的原因。
            # 只记在警告字符串里的话，界面只能显示一整段文字，看不出是哪一行。
            not_ready.append({"物品名称": r[0], "原因": why})

    new_used = {p["内部编号"] for p in products if p.get("内部编号")} - set(extra_used or ())
    ready = ready_rows  # 口径统一：能提交的行必须同时有物品编号与一级分类
    rec.update({
        # 这两项留空是有意的：整批合并之后，一张单没有「自己的」入库表 / 建档表。
        # 真有表的地方在 emit 的返回值里（products_file / inbound_groups），
        # pipeline 会把它们挂到每一单的 detail.tables 上，界面从那里拿。
        "inbound": "",
        "products": prod_path,
        "report": base + "_report.json",
        "rows": len(rows),
        "rows_ready": ready_rows,
        "products_blocked": blockers,
        "can_submit": bool(rows) and ready_rows == len(rows),
        # 「待建档」只数「没有物品编号」的行。以前写的是 len(rows) - ready_rows，
        # 会把「数量为 0 / 一级分类缺失」这类不能提交的行也算成待建档 ——
        # 实测 MS260127163114015050：物品编号 10357 早就有了，只是数量 0，
        # 却被算成待建档 1 行，pipeline 于是要一份根本不存在的 products.xlsx，
        # 最后报 no_products_file，把「数量为 0」这个真原因盖掉了。
        "rows_pending_build": sum(1 for r in rows if not (r[1] or "").strip()),
        "rows_not_ready": len(rows) - ready,
        "rows_not_ready_detail": not_ready,
        # 批号缺失：只提示。用户 2026-11-23 明确「有有效期、无批号可入库」。
        "rows_lot_missing": lot_missing,
        # 效期缺失/不足一年：阻断，且**必须标红等用户审批**才能放行。
        # 单独列一个字段，界面据此画红并给「放行」按钮，pipeline 据此给 awaiting_approval。
        "rows_short_expiry": short_expiry,
        "expiry_blocked": bool(short_expiry),
        # 审批过、本次放行的行：入库照常进行，但结果里要留痕，
        # 否则日后有人问「这批货效期明明不足一年怎么进来的」就答不上来。
        "rows_expiry_approved": approved_expiry,
        "rows_rejected": rejected_now,
        # doc_date 也带上：审批时必须让人看到「是按哪天的单算的」，否则
        # 同一批货晚一个月跑，判定结果会变，人会以为程序算错了。
        "doc_date_for_expiry": doc_date,
        # 被跳过的非医疗赠品（食品、水果等）单独列出来：它们既不在入库表里，
        # 也不会出现在「不能提交的行」里，只在警告串里——不说清楚，人会以为
        # 程序漏读了一行。
        "rows_skipped_gift": [w for w in bwarns if w.startswith("[非医疗赠品]")],
        # 整张单全是赠品（食品、水果…）时入库表是空的。这不是数据问题，
        # 更不是「建档卡住」——要单独给一个状态，否则它会掉进 blocked_build，
        # 人被引去补一级分类、补批号，而那张单根本不该入库。
        "all_gift_skipped": bool(bwarns) and not rows
        and all(w.startswith("[非医疗赠品]") for w in bwarns),
        "build_warnings": bwarns,
    })
    return rec, new_used


def main():
    # line_buffering=True 不是可有可无的：重定向到文件/管道时 TextIOWrapper 默认是
    # 块缓冲（8KB），进度行会一直压在缓冲里——界面看起来「卡住不动」，日志文件是空的。
    # PYTHONUNBUFFERED=1 救不了这一层，它只影响 Python 启动时的 sys.stdout。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    # 不设 required=True：--probe 只试识别一张照片，本来就没有「输入目录」。
    # 缺 --in 的校验放在 probe 分支之后（见 ap.error）。
    ap.add_argument("--in", dest="src", default=None, help="单据文件或目录")
    ap.add_argument("--out", dest="out", default=OUT_DIR)
    # 产品库现在在本地 data/local.db（emit.py lib_from_db）。--library 只为兼容
    # 老命令与单测保留：给了就用那份 xlsx 当产品库，不给就走本地库。
    ap.add_argument("--library", dest="library", default=None,
                    help="(可选) 产品库 xlsx 快照；不给则用本地 data/local.db")
    ap.add_argument("--inner-base", dest="inner_base", type=int, default=92000)
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--profile", default=None,
                    help="照片版式 id（如 shipping_v1）；不给则按文件名关键字猜，猜不到就阻断")
    ap.add_argument("--photo-plan", dest="photo_plan", default=None,
                    help="照片版式计划 JSON：{\"绝对路径\": \"版式id\" 或 null}。"
                         "null 表示用户明确不让识别这张；给了计划的照片优先于 --profile 与文件名猜测")
    ap.add_argument("--probe", default=None, metavar="PHOTO",
                    help="只识别不落盘：打印一张照片的抽取结果 JSON 后退出（界面上的「试识别」用）")
    a = ap.parse_args()

    # 试识别只跑识别、不写任何文件：拍歪了、版式选错了，在这一步就能看见，
    # 而不是等建档表生成完才发现整批列错位。
    if a.probe:
        if not a.profile:
            ap.error("--probe 必须同时给 --profile（试识别不接受猜测版式）")
        import ocr_photo
        doc, lines, warns = ocr_photo.extract(a.probe, a.profile)
        print(json.dumps({"document": doc, "lines": lines, "warnings": warns},
                         ensure_ascii=False))
        return 0

    if not a.src:
        ap.error("必须给 --in（单据文件或目录）")

    mappings = load_mappings()
    if not mappings:
        print("没有可用的模板映射（config/template_mapping/*.yaml）")
        return 2

    # 照片版式计划：{"绝对路径": "版式id" | null}。
    # 为什么不让界面直接传 --profile：一批照片可能来自不同供应商、不同版式，
    # 而 --profile 是全批一个值；混着传就会把 A 家的单据按 B 家版式解析。
    photo_plan = None
    if a.photo_plan:
        with open(a.photo_plan, encoding="utf-8") as f:
            raw = json.load(f)
        # 键统一成绝对路径，否则界面传相对路径、目录扫描给绝对路径，两边对不上，
        # 计划会被静默忽略（用户看到的就是「我明明选了版式它还是报版式不符」）。
        photo_plan = {os.path.abspath(k): v for k, v in (raw or {}).items()}

    srcs = []
    if os.path.isdir(a.src):
        inp = os.path.abspath(a.src)
        outp = os.path.abspath(a.out)
        for fn in sorted(os.listdir(a.src)):
            if not (fn.lower().endswith(SUPPORTED_EXT) and not fn.startswith("~$")):
                continue
            full = os.path.join(a.src, fn)
            # 本脚本自己会往 out 目录写 <stem>_extracted.json。输入目录与输出目录
            # 相同时那些产物会被当成新单据再读一遍，越跑越多。
            if fn.lower().endswith(EXTRACTED_EXT) and os.path.abspath(os.path.dirname(full)) == outp:
                continue
            # 历史上本脚本会往输入目录旁边写 <单号>_inbound.xlsx / <单号>_products.xlsx
            # （逐单出表时代）。这些是**给人导入美亚的成品**，不是供应商发来的单据；
            # 停写逐单表之后它们仍躺在老目录里，于是被当成新单据去识别模板，
            # 全部识别失败 → 「本批 6 份，阻断 6 份」，真正该跑的单反而被埋了。
            # 用户手上的历史目录不会自己变干净，所以这里必须跳过，而不是靠人去删。
            low = fn.lower()
            if low.endswith(("_inbound.xlsx", "_products.xlsx")):
                continue
            # 整批出表之后目录里还会多出 products.xlsx 与 inbound[_供应商].xlsx
            # （2026-11-24 起逐单表停写，只剩这两类整批表）。它们同样是给人导入
            # 美亚的成品，放在输入目录里会被当成新单据去识别模板 —— 实测一批
            # 2 单却能「阻断 2 份」，全是这两张表。
            if low == "products.xlsx":
                continue
            if low.startswith("inbound") and low.endswith(".xlsx"):
                continue
            srcs.append(full)
    else:
        srcs = [a.src]

    results, used, blocked = [], set(), []
    for p in srcs:
        is_photo = p.lower().endswith(PHOTO_EXT)
        is_extracted = p.lower().endswith(EXTRACTED_EXT)
        profile = None
        mp = None
        if is_extracted:
            pass  # 平台抽取结果自带 document/lines，不需要模板也不需要版式
        elif is_photo:
            # 计划优先于一切：界面上每张照片人工选过版式（或明确不选），
            # 就不能再让文件名关键字把它改回去。
            key = os.path.abspath(p)
            in_plan = photo_plan is not None and key in photo_plan
            if in_plan:
                profile = photo_plan[key]
                if not profile:
                    blocked.append({"source": key,
                                    "reason": "这张照片在版式计划里被标为「不识别」，跳过",
                                    "candidates": []})
                    continue
            else:
                profile = a.profile or _guess_profile(p)
            if not profile:
                blocked.append({"source": key,
                                "reason": "照片无法识别所属版式，请在 config/photo_profiles/ 里新增 profile "
                                          "或用 --profile 指定；禁止按猜测的版式解析",
                                "candidates": [n for _k, n in PHOTO_PROFILE_HINT]})
                continue
        else:
            mp, score, detail = detect_mapping(p, mappings)
            if not mp or score < 0.6:
                blocked.append({"source": os.path.abspath(p),
                                "reason": "模板识别置信度不足（最高 %.0f%%），拒绝按猜测解析" % (max(score, 0) * 100),
                                "candidates": detail})
                continue
        rec, nu = process_one(p, mp, a.out, a.library, a.inner_base + len(used), used,
                             a.dry_run, a.d_from, a.d_to, profile=profile)
        used |= nu
        results.append(rec)
        print("  %-28s %-16s %2d 行  待建档 %d" % (
            os.path.basename(p), rec.get("template_id") or "-",
            rec.get("rows", rec.get("line_count", 0)),
            rec.get("rows_pending_build", 0)))

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "library_snapshot": os.path.abspath(a.library) if a.library else None,
        "documents": results,
        "blocked": blocked,
        "inner_numbers_used": sorted(used),
        "note": "入库表的物品编号只对已建档物品有值；rows_pending_build>0 的单据必须先导 products.xlsx 建档，"
                "再【批量导出】产品库重跑本批，否则入库表不能提交。",
    }
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "batch_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("")
    print("本批 %d 份，阻断 %d 份；汇总 -> %s" % (
        len(results), len(blocked), os.path.join(a.out, "batch_summary.json")))
    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
