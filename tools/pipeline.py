# -*- coding: utf-8 -*-
"""pipeline.py —— 一条命令跑完「读单据 → 判断建不建档 → 出两张表」。

2026-11-23 定位变了（用户决定）：美亚不允许第三方对接，所以本程序**不再操作美亚**，
只做「把牙e在线 / 梅苗苗 / 照片的商品信息，变成美亚能导入的产品库模板与入库模板」。

于是这一层只剩三段：
    第 0 步  拉单（可选）：把平台订单拉成 *_extracted.json
    第 1 步  抽取：run.py 读表/OCR → *_extracted.json
    第 2 步  出表：emit.py 查本地库 → products.xlsx + inbound_all.xlsx + decision.json

状态机（每份单据一个）：
    need_build        本地库里没有这些物品 → 必须先导入建档表，建完再重新出表
    awaiting_approval 有效期为空或距单据日期不足一年 → 标红等用户点头（expiry.py）
    blocked_build     行本身不完整（分类推断不出 / 数量为 0）→ 停下交人工
    skipped_gift      整单明细都是赠品（食品、饮料、水果）→ 不出表
    all_rejected      整单明细都被人工标记「禁止入库」→ 不出表
    duplicate         这张单以前已经出过表（台账里同号同明细）→ 本次跳过
    empty             没有可入的行（全部被上面的规则筛掉）
    emitted           已出表（可直接把 inbound_all.xlsx 导进美亚）

用法：
    python tools/pipeline.py --in tmp/batch2
    python tools/pipeline.py --pull yae --pull-from 2026-01-01 --pull-to 2026-01-31
    python tools/pipeline.py --in tmp/batch2 --approve-expiry
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# 禁令必须先装钩子：no_meiya 在 import 那一刻就把 socket.create_connection /
# socket.connect / urllib.request.urlopen 三层出口包住，所以它必须排在任何其它
# 本地模块之前。晚一步装，中间这几行 import 就有机会连出去。
import no_meiya  # noqa: E402,F401  禁止访问美亚美牙（myyun.com）
import emit as emit_mod  # noqa: E402
import pacing  # noqa: E402  拉单限速（用户要求「禁止爬虫类，会导致封禁」）
import expiry as expiry_mod  # noqa: E402
import ledger  # noqa: E402

OUT_DIR = os.path.join(ROOT, "out", "pipeline")
# 拉单要用 CDP 读牙e / 梅苗苗的登录态。这两个平台跟美亚无关，允许。
CDP_PORT = os.environ.get("PLATFORM_CDP_PORT", "9335")


def node_bin():
    """找 node：环境变量 > DSH 运行时自带 > PATH 里的 node。"""
    for c in (os.environ.get("DSH_NODE"),
              "node"):
        if c == "node" or (c and os.path.exists(c)):
            return c
    return "node"


NODE = node_bin()


# ── 结构化进度（界面用）─────────────────────────────────────────────
# 一行一条 "EVT {json}"，普通 print 保持原样不动。
# 为什么不让人话日志兼作进度：解析「产品库快照 302 条 -> …」这种句子，
# 改一个字前端就瞎了；EVT 是契约，人话是给人看的，两者混在一起两边都不牢。
EVT_ON = False


def evt(kind, **kw):
    # 第一个形参不能叫 name：调用处是 evt("step", name="login", …)，
    # 同名会撞成 "got multiple values for argument 'name'"（实测踩过）。
    if not EVT_ON:
        return
    kw["e"] = kind
    kw["t"] = time.strftime("%H:%M:%S")
    print("EVT " + json.dumps(kw, ensure_ascii=False), flush=True)


def sh(cmd, cwd=ROOT, timeout=600, on_line=None):
    """跑外部命令并原样回传输出 —— 失败时必须能看到子进程原文。

    on_line 给了就边跑边吐行（界面要逐行实时），否则等跑完一次性返回。
    流式分支必须自己管超时：subprocess.run 的 timeout 在流式下拿不到。
    """
    if on_line is None:
        p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    p = subprocess.Popen(cmd, cwd=cwd, shell=isinstance(cmd, str),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    buf = []
    deadline = time.time() + timeout
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            buf.append(line)
            try:
                on_line(line)
            except Exception:
                pass  # 回调出错不能把子进程带崩
            if time.time() > deadline:
                p.kill()
                buf.append("[超时] 子进程被终止（%ds）" % timeout)
                break
    finally:
        p.wait()
    return p.returncode, "\n".join(buf)


def python_bin():
    """优先用装了 openpyxl 的解释器。"""
    for c in (os.environ.get("DSH_PYTHON"),
              sys.executable):
        if c and os.path.exists(c):
            return c
    return sys.executable


# ── 第 0 步：平台拉单 ────────────────────────────────────────────────
def pull_platform(platform, d_from, d_to, out_dir=None, status=-1):
    """把平台订单拉成 extracted.json，让后面的链条不必知道「单据从哪来」。

    为什么要单独一步而不是塞进 run.py：run.py 的职责是「把单据读成结构化数据」，
    它面对的是文件；平台拉单面对的是网络与登录态。混在一起会让 run.py 依赖 CDP，
    而 run.py 现在在纯离线环境（如另一台机器只是补跑解析）也能用。
    """
    out = out_dir or os.path.join(ROOT, "out", "pull", platform)
    os.makedirs(out, exist_ok=True)
    if platform == "mmm":
        import lib_mmm
        rc, log = sh([NODE, os.path.join(ROOT, "tools", "cdp_token_mmm.mjs"),
                      "--port", CDP_PORT], timeout=120)
        if "TOKEN_SAVED_MMM" not in log:
            tail = (log.strip().splitlines() or [""])[-1]
            return None, None, "梅苗苗登录态不可用：%s" % tail
        try:
            got, skipped = lib_mmm.pull(d_from, d_to, out, status=status)
        except lib_mmm.MmmAuthError as e:
            return None, None, "梅苗苗登录态失效：%s" % e
        except lib_mmm.MmmError as e:
            return None, None, "梅苗苗拉单失败：%s" % e
        for s in skipped:
            print("  跳过 %s（%s）：%s" % (s["orderNo"], s.get("status"), s["why"]))
        print("  " + pacing.summary())   # 让人看得见限速在工作，而不是以为程序卡了
        return out, got, None
    if platform == "yae":
        import lib_yae
        rc, log = sh([NODE, os.path.join(ROOT, "tools", "cdp_token_yae.mjs"),
                      "--port", CDP_PORT], timeout=120)
        if "TOKEN_SAVED_YAE" not in log:
            tail = (log.strip().splitlines() or [""])[-1]
            return None, None, "牙e在线登录态不可用：%s" % tail
        try:
            # 牙e 的导出端点每单一次请求，几十单约 20s；给足超时但不要无限等
            got, skipped = lib_yae.pull(d_from, d_to, out)
        except lib_yae.YaeAuthError as e:
            return None, None, "牙e在线登录态失效：%s" % e
        except lib_yae.YaeError as e:
            return None, None, "牙e在线拉单失败：%s" % e
        for s in skipped:
            print("  跳过 %s：%s" % (s["orderNo"], s["why"]))
        print("  " + pacing.summary())   # 同上：限速是特性，要让它可见
        return out, got, None
    return None, None, "未知平台 %s" % platform


# ── 第 1 步：抽取 ───────────────────────────────────────────────────
def run_extract(src, out_dir, inner_base, profile=None, photo_plan=None, on_line=None):
    """跑 run.py 做抽取。

    无论调用方要不要 EVT，这里的输出都必须**逐行实时**流出去：
    抽取要走 PaddleOCR，三张照片约 3 分钟。原先不传 on_line 时用 subprocess.run
    把输出全部缓冲到底，界面上 3 分钟一个字都不动，人以为程序卡死（实测被当成 bug）。
    """
    os.makedirs(out_dir, exist_ok=True)
    cmd = [python_bin(), os.path.join(ROOT, "tools", "run.py"),
           "--in", src, "--out", out_dir, "--inner-base", str(inner_base)]
    if profile:
        cmd += ["--profile", profile]
    if photo_plan:
        # 落盘再传路径而不是塞进命令行：照片可能有几十张，JSON 长了会撞 Windows
        # 命令行长度上限（32k），而且路径里的引号在命令行里没法安全转义。
        pp = os.path.join(out_dir, "_photo_plan.json")
        with open(pp, "w", encoding="utf-8") as f:
            json.dump(photo_plan, f, ensure_ascii=False)
        cmd += ["--photo-plan", pp]
    buf = []

    def _sink(ln):
        buf.append(ln)
        if on_line:
            on_line(ln)
        else:
            print("  " + ln)

    rc, log = sh(cmd, on_line=_sink)
    log = log or "".join(x + "\n" for x in buf)
    summary_path = os.path.join(out_dir, "batch_summary.json")
    summary = {}
    if os.path.exists(summary_path):
        with open(summary_path, encoding="utf-8") as f:
            summary = json.load(f)
    return rc, log, summary


def _doc_lines(rec):
    """从抽取结果里读明细行，给台账算指纹用。读不到返回 None。"""
    ex = rec.get("extracted") or ""
    if not ex or not os.path.exists(ex):
        return None
    try:
        with open(ex, encoding="utf-8") as f:
            j = json.load(f)
    except (ValueError, OSError):
        return None
    return j.get("lines") or []


def _lib_count():
    """本地库里已建档（有物品编号）的物品数：出表前打一行，让人知道判断依据有多厚。"""
    try:
        import localdb
        return localdb.stats().get("built", 0)
    except Exception:
        return 0


# ── 第 2 步：把 emit 的结论翻成界面认识的状态 ─────────────────────────
_STATE_MAP = {
    emit_mod.ST_READY: "emitted",
    emit_mod.ST_NEED_BUILD: "need_build",
    emit_mod.ST_AMBIG: "ambiguous",
    emit_mod.ST_AWAITING: "awaiting_approval",
    emit_mod.ST_EMPTY: "empty",
}


def step_for(rec, dec, em, use_ledger=True, force=False):
    """把一份单据的抽取结果 + emit 的结论，合成界面要的那一条 step。

    为什么要这一层而不是让界面直接读 decision.json：
    界面还要展示「这单是怎么来的、读了几行、被跳过了什么、表在哪」——
    这些一半在 run.py 的 rec 里、一半在 emit 的 dec 里，散着让人自己拼早晚拼错。
    """
    step = {"source": rec.get("source"), "stem": rec.get("stem"),
            "origin": rec.get("origin") or dec.get("origin") or "",
            "doc_no": dec.get("doc_no") or rec.get("doc_no") or "",
            "doc_date": dec.get("doc_date") or rec.get("doc_date") or "",
            "extracted": rec.get("extracted"),
            # 供应商放在顶层：界面的「表」列要用它区分「这张入库表是谁的」，
            # 而它以前只在 emitted 分支的 detail 里，别的状态拿不到。
            "supplier": (dec.get("supplier") or rec.get("supplier_raw") or "").strip(),
            "line_names": dec.get("line_names") or rec.get("line_names") or []}

    # tables 必须放在所有分支之前算：emit 在判重之前就跑完了，所以哪怕这一单
    # 被判重跳过 / 被规则筛掉，这批的入库表也已经出好。不带 tables，人就拿着
    # 「表」列全是「—」的结果页，不知道这批的表在哪（实测被报成「没有入库表」）。
    # 这张单的入库表是哪一张：按供应商对号入座。
    # 一行一家供应商时，每单的「表」列就该只指向自己那张表 —— 全部指向同一张
    # 会让人以为「一单一张表」（用户抱怨「一个订单一个订单」正是这么来的），
    # 而指向别家的表更糟：导进去货就挂错了供应商。
    groups = em.get("inbound_groups") or []
    sup = (dec.get("supplier") or rec.get("supplier_raw") or "").strip()
    my_in = ""
    for g in groups:
        if (g.get("supplier") or "").strip() == sup and sup:
            my_in = g.get("file") or ""
            break
    if not my_in and len(groups) == 1:
        my_in = groups[0].get("file") or ""
    if not my_in:
        my_in = em.get("inbound_file") or ""
    tables = {"products": em.get("products_file") or "",
              "inbound": my_in,
              # 整批全部入库表（一家一张）：界面的批次行要全列出来，
              # 否则另一家的表就在旁边却没人知道该导它。
              "inbound_groups": groups,
              "decision": os.path.join(em.get("out_dir") or "", "decision.json")}

    # 整单全赠品 / 全被禁：emit 给的是 empty（没有可入行），但要说得更具体，
    # 否则人看到「没有可入的行」会去查分类，其实是赠品或人工禁入。
    if rec.get("all_gift_skipped"):
        step["state"] = "skipped_gift"
        step["detail"] = {"hint": "这张单的明细全是赠品（食品、饮料、水果等非耗材非药品），"
                                  "按规定不出表也不入库",
                          "gifts": rec.get("rows_skipped_gift") or []}
        return step
    if rec.get("rows_rejected") and len(rec.get("rows_rejected") or []) >= int(rec.get("rows") or 0) > 0:
        step["state"] = "all_rejected"
        step["detail"] = {"rows": rec.get("rows"), "items": rec.get("rows_rejected"),
                          "hint": "这张单的所有行都被标记为「禁止入库」，本次不出表。"}
        return step
    # 行本身不完整（一级分类推断不出 / 数量 0）：不是「要建档」，是数据要人补。
    blocked = rec.get("products_blocked") or []
    if blocked and _STATE_MAP.get(dec.get("state")) != "awaiting_approval":
        step["state"] = "blocked_build"
        # detail 是 list 而不是 dict：这里要按物品逐条列，界面才能逐条给下拉框。
        # 规格一并带上——界面按「单据号 + 物品名 + 规格」记处置，
        # 少了规格，同名不同规格的两行会被当成同一行一起改（实测牙胶尖 04-30#/04-35#）。
        step["detail"] = [{"物品名称": b.get("物品名称"), "规格": b.get("规格"),
                           "原因": b.get("原因")}
                          for b in blocked]
        return step

    # ── 判重（已出过表就别再出一遍）──────────────────────────────────
    # 定位变了，台账的含义也跟着变：以前记「已入库」，现在记「已出过表」。
    # 判重仍然必要：同一张单重复出表 → 人可能把 inbound_all.xlsx 导两遍 → 库存翻倍。
    lines = _doc_lines(rec)
    doc_no = step["doc_no"]
    if use_ledger and doc_no:
        st, old = ledger.check(doc_no, lines)
        if st == "dup" and not force:
            step["state"] = "duplicate"
            step["detail"] = {"doc_no": doc_no, "first_at": (old or {}).get("at"),
                              "amount": (old or {}).get("amount"),
                              "tables": tables,
                              "hint": "这张单以前已经出过表（台账里有同号同明细的记录），"
                                      "本次跳过，避免同一批耗材被导两遍。"
                                      "同一批的入库表还是出好了，就在右边「本批入库表」链接里。"
                                      "若确实要再出一次，加 --force。"}
            return step
        if st == "changed" and not force:
            step["state"] = "ledger_conflict"
            step["detail"] = {"doc_no": doc_no, "first_at": (old or {}).get("at"),
                              "tables": tables,
                              "hint": "单据号相同但明细变了（数量/批号/金额）。"
                                      "请确认是「单据修订」还是「另一张同号单」，"
                                      "确认后加 --force 重新出表。"}
            return step

    state = _STATE_MAP.get(dec.get("state"), "empty")
    step["state"] = state
    if state == "emitted":
        step["detail"] = {"rows": len(dec.get("inbound_rows") or []),
                          "tables": tables,
                          "warn": dec.get("warnings") or []}
        if dec.get("supplier"):
            step["detail"]["supplier"] = dec.get("supplier")
        if rec.get("rows_skipped_gift"):
            step["detail"]["gift"] = rec.get("rows_skipped_gift")
        if rec.get("rows_lot_missing"):
            step["detail"]["lot_missing"] = rec.get("rows_lot_missing")
        if rec.get("rows_expiry_approved"):
            step["detail"]["expiry_approved"] = rec.get("rows_expiry_approved")
        # emit 侧也算了一遍审批放行（run.py 与 emit 两条路径），这里把出表这一步
        # 实际放行的行带上：界面要能让用户看见「我点的那几条真的进去了」。
        if dec.get("approved"):
            step["detail"]["approved"] = dec.get("approved")
        if rec.get("rows_rejected"):
            step["detail"]["rejected"] = rec.get("rows_rejected")
        if not doc_no:
            # 没有单据号就没法判重，这次出表照出，但要留一句：下次重跑会再出一遍。
            step["detail"]["warn_docno"] = ("这张单没有单据号，无法判重："
                                            "下次重跑还会再出一次表，导入前请自己确认。")
    elif state == "ambiguous":
        # 本地库同一身份有多条档案（美亚里本来就有两条同名同规格的条目）。
        # 这一类的出口是「选一条」，不是「导建档表」：再建一次必被回
        # 「该物品已存在」整行退回（实测 2026-11-24 甲硝唑棒 10321/10325）。
        step["detail"] = {"items": dec.get("ambiguous") or [],
                          "tables": tables,
                          "hint": "这些物品在产品库里有多条同身份档案，程序无从判断该入哪一条。"
                                  "请在弹窗里选一条（会记进 config/inbound_overrides.json），"
                                  "或先在产品库删掉重复档案再重跑。"}
    elif state == "need_build":
        # items 里已经有 物品名称/规格/内部编号（emit.plan 给的），界面的处置清单
        # 要按这三项拼键，所以原样带过去，不要在这里裁剪字段。
        step["detail"] = {"items": dec.get("need_build") or [],
                          "tables": tables,
                          "hint": "这些物品本地库里没有，必须先导入 products.xlsx 建档；"
                                  "建完档（或从美亚界面导出最新产品库后用 "
                                  "python tools/localdb.py import <xlsx> 导入）再重新出表，"
                                  "它们就会进入库表。"}
    elif state == "awaiting_approval":
        step["detail"] = {"rows": dec.get("total"),
                          "doc_date": step["doc_date"],
                          "items": dec.get("approval") or [],
                          "tables": tables,
                          # 这一单里要新建档的物品本轮被压住了（审批通过才出建档表）。
                          # 不说清楚，人看到建档表里少了这几项会以为程序漏了。
                          "products_held": dec.get("products_held") or 0,
                          "hint": "这些行的有效期为空或距单据日期不足一年，按规定不出表，"
                                  "这单要新建档的物品也先不出（审批通过后再出）。"
                                  "确认要用就点「同意入库」，或运行 "
                                  "python tools/expiry.py approve <单据号> <物品名称> <有效期>。"}
        if blocked:
            # 同一张单里可能还有别的阻断，一并带上，否则人批完效期又撞下一个。
            step["detail"]["other_blockers"] = blocked
    else:  # empty
        step["detail"] = {"rows": dec.get("total"), "tables": tables,
                          "hint": "这张单没有可入的行（明细全部被赠品/禁入/效期规则筛掉）。",
                          "rejected": dec.get("rejected") or []}
    return step


def _remember_src(out_dir, src):
    """把本批实际读取的数据目录写进 <out>/_cmd.json 的 src 字段。

    只补一个字段，不动 cmd：cmd 是「重新从平台拉一遍」的原始命令，留着是为了
    人能看懂这一批当初怎么来的；重跑该走哪条路，由 src 决定（见 ui._resume）。
    写失败不影响主流程 —— 最坏情况是重跑退回老行为。
    """
    path = os.path.join(out_dir, "_cmd.json")
    try:
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
        data["src"] = os.path.abspath(src) if src else ""
        data["src_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def main():
    # line_buffering=True 不是可有可无的：重定向到文件/管道时 TextIOWrapper 默认是
    # 块缓冲（8KB），进度行会一直压在缓冲里——界面看起来「卡住不动」，日志文件是空的。
    # PYTHONUNBUFFERED=1 救不了这一层，它只影响 Python 启动时的 sys.stdout。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=None,
                    help="单据文件或目录；用 --pull 时可省略")
    ap.add_argument("--pull", choices=["mmm", "yae"], default=None,
                    help="第 0 步从电商平台按日期区间拉单（mmm=梅苗苗，yae=牙e在线）")
    ap.add_argument("--pull-from", dest="pull_from", default=None)
    ap.add_argument("--pull-to", dest="pull_to", default=None)
    ap.add_argument("--out", dest="out", default=OUT_DIR)
    ap.add_argument("--inner-base", type=int, default=92000)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--pull-out", dest="pull_out", default=None,
                    help="平台拉单的落盘目录，默认 out/pull/<平台>")
    ap.add_argument("--pull-status", dest="pull_status", type=int, default=-1,
                    help="平台订单状态过滤：-1 全部 / 见平台 statusString")
    ap.add_argument("--approve-expiry", dest="approve_expiry", action="store_true",
                    help="把本批「效期不足」的行记为已审批并重跑（用户已经同意入库）")
    ap.add_argument("--approve-note", dest="approve_note", default="",
                    help="审批备注（跟 --approve-expiry 一起用，会写进 config/expiry_approvals.json）")
    ap.add_argument("--force", action="store_true",
                    help="忽略台账判重，强制再出一次表")
    ap.add_argument("--no-ledger", action="store_true",
                    help="不查台账也不记账（只用于排查判重逻辑本身）")
    ap.add_argument("--ledger-stats", action="store_true",
                    help="只打印台账统计后退出")
    ap.add_argument("--photo-plan", dest="photo_plan", default=None,
                    help="照片版式计划 JSON，键是照片绝对路径、值是版式 id 或 null")
    # 下面三个只为兼容老命令行与界面旧参数：本程序已不连美亚，给了也一律忽略。
    ap.add_argument("--supplier", default=None, help="(已废弃：不再连美亚，忽略)")
    ap.add_argument("--supplier-auto", action="store_true",
                    help="(已废弃：不再连美亚，忽略)")
    ap.add_argument("--no-create-supplier", action="store_true",
                    help="(已废弃：不再连美亚，忽略)")
    ap.add_argument("--commit", action="store_true",
                    help="(已废弃：本程序不再操作美亚，出表即完成，导入由人来做)")
    ap.add_argument("--evt", action="store_true",
                    help="额外输出 EVT {json} 结构化进度行（界面解析用，人看可以忽略）")
    a = ap.parse_args()
    global EVT_ON
    EVT_ON = bool(a.evt)
    if a.ledger_stats:
        n, last = ledger.stats()
        print("台账 %s：%d 条" % (ledger.LEDGER, n))
        if last:
            print("最近一条：%s  %s  %s 行  %s 元"
                  % (last.get("at"), last.get("doc_no"), last.get("lines"), last.get("amount")))
        return 0
    if a.commit:
        # 老脚本会带 --commit（以前是「真点直接入库」）。现在没有可点的按钮了，
        # 明说一句，否则人以为程序漏做了最后一步。
        print("提示：--commit 已无意义 —— 本程序不再操作美亚，出表就是终点，"
              "导入两张表由人来点。")
    photo_plan = None
    if a.photo_plan:
        with open(a.photo_plan, encoding="utf-8") as f:
            photo_plan = {os.path.abspath(k): v for k, v in (json.load(f) or {}).items()}
    if not a.src and not a.pull:
        ap.error("必须给 --in 或 --pull")
    if a.pull and not (a.pull_from and a.pull_to):
        ap.error("--pull 必须同时给 --pull-from 与 --pull-to（平台接口按日期区间拉单）")

    os.makedirs(a.out, exist_ok=True)

    src = a.src
    if a.pull:
        print("第 0 步：从 %s 拉单 %s ~ %s" % (a.pull, a.pull_from, a.pull_to))
        pdir, got, err = pull_platform(a.pull, a.pull_from, a.pull_to, a.pull_out, a.pull_status)
        if err:
            print(err)
            # 这里必须多打一句「接下来怎么办」：界面里这段话是用户唯一能看到的
            # 现场。登录态失败时子进程 0.5 秒就结束了，日志只剩一行错误，
            # 看起来就像「点了没反应」（用户就是这么报的）。
            print("  [怎么办] 拉单要靠浏览器里的登录态。请在上方「平台拉单」里点"
                  "「打开浏览器」→「去登录」，在那个窗口里登录 %s，登录成功后再点一次「开始」。"
                  % ("梅苗苗" if a.pull == "mmm" else "牙e在线"))
            print("  [说明] 出表本身不依赖登录：手上的单据文件（本地文件 / 照片）随时可以跑。")
            return 4
        print("  拉到 %d 单 -> %s" % (len(got or []), pdir))
        if not got:
            print("  该区间没有订单，结束。")
            return 0
        src = pdir
        # 把「这一批到底读的是哪个目录」记进 _cmd.json。拉单下载的 extracted.json
        # 本来就落在 out/pull/<平台>/，处置完重跑没有理由再去平台上拉一遍 ——
        # 那一遍既慢、又要求登录态、还可能拉到已经变了的订单（用户 2026-11-24 报的
        # 「审批后点保存继续会一直重新拉取」就是这么来的）。
        # 界面用这个字段把重跑命令换成 --in <src>，而不是原样重放 --pull。
        _remember_src(a.out, src)

    evt("step", name="extract", status="run", src=src)
    # 说一句「会慢」而不是让人自己猜：照片走 PaddleOCR，单张实测 60–75 秒，
    # 一张照片的第一行输出要等模型加载完才出来，这期间界面上只有 paddle 的
    # 环境噪声，很容易被当成卡死（用户已经报过一次「卡住不动」）。
    print("开始抽取（照片走 OCR，单张约 1 分钟；下面出现文件名就说明在动）")
    rc, log, summary = run_extract(src, a.out, a.inner_base, a.profile,
                                   photo_plan=photo_plan,
                                   on_line=(lambda ln: evt("line", text=ln)) if EVT_ON else None)
    docs = summary.get("documents") or []
    evt("step", name="extract", status="ok", docs=len(docs),
        blocked=len(summary.get("blocked") or []))
    if not docs:
        print(log[-1500:])
        print("没有可处理单据。blocked=%s"
              % json.dumps(summary.get("blocked"), ensure_ascii=False))
        return 2

    # --approve-expiry：把本批所有「效期不足」的行记为已审批，再照常往下跑。
    # 为什么做成批处理而不是逐行确认：这些行都是从同一批单据里检测出来的，
    # 用户看过界面上的清单后一次性点头，逐行点会逼人点十几次，
    # 而每多点一次就多一次点错的机会。
    if a.approve_expiry:
        n_ok = 0
        for rec in docs:
            for it in (rec.get("rows_short_expiry") or []):
                expiry_mod.approve(rec.get("doc_no"), it.get("物品名称"),
                                   it.get("有效期"),
                                   reason=a.approve_note or "用户在界面上同意入库",
                                   by="ui")
                n_ok += 1
        print("已登记效期审批 %d 行 -> %s" % (n_ok, expiry_mod.APPROVALS))
        evt("step", name="approve_expiry", status="ok", rows=n_ok)
        if n_ok:
            # 必须重跑抽取：rows_short_expiry 是 run.py 里算的，
            # 审批只改了配置文件，不重算的话这些行仍然是「不能提交」。
            print("重跑抽取以应用审批…")
            rc, log, summary = run_extract(src, a.out, a.inner_base, a.profile,
                                           photo_plan=photo_plan,
                                           on_line=(lambda ln: evt("line", text=ln)) if EVT_ON else None)
            docs = summary.get("documents") or []

    # ── 第 2 步：整批出表 ────────────────────────────────────────────
    # 整批合并（用户选的）：同一物品只进一次建档表，入库表全部行并为一张。
    # 一张单出一份表最省事，但十几单就是十几个文件，导入时漏一个谁也不知道。
    paths = []
    for rec in docs:
        ex = rec.get("extracted")
        if ex and os.path.exists(ex):
            paths.append(ex)
    evt("step", name="emit", status="run", docs=len(paths))
    print("开始出表（本地库已建档 %d 个物品，判断哪些要新建档）…" % _lib_count())
    em = emit_mod.emit(paths, out_dir=a.out, note="pipeline 出表")
    decs = {os.path.abspath(d["path"]): d for d in (em.get("docs") or [])}
    c = em.get("counts") or {}
    evt("step", name="emit", status="ok", counts=c,
        products=em.get("products_file"), inbound=em.get("inbound_file"))
    print("  出表完成：可直接入 %d 行 / 待建档 %d 行 / 待审批 %d 行"
          % (c.get("inbound_rows", 0), c.get("need_build", 0), c.get("awaiting", 0)))
    print("  建档表：%s" % (em.get("products_file") or "（本批无需建档）"))
    if not em.get("inbound_groups"):
        print("  入库表：（本批没有可直接入的行）")
    else:
        # 一家供应商一张表：只打一句「入库表：路径」会让人以为只有一张，
        # 而另一家的表就在旁边却没被认出来。
        for g in em["inbound_groups"]:
            print("  入库表：%s（%s，%d 行）"
                  % (g.get("file"), g.get("supplier") or "供应商未识别", g.get("rows") or 0))

    results = []
    for rec in docs:
        ex = os.path.abspath(rec.get("extracted") or "")
        dec = decs.get(ex) or {}
        evt("doc", stem=rec.get("stem"), status="run")
        r = step_for(rec, dec, em, use_ledger=not a.no_ledger, force=a.force)
        # 记账的时机只有这一个：表真的出出来了（state == emitted）。
        # 反过来写（先记再出表）会把失败的单据记成已出表，那比不记更危险：
        # 下次重跑会被判「已出过表」跳过，人以为导过了，其实什么也没有。
        if r.get("state") == "emitted" and not a.no_ledger and r.get("doc_no"):
            try:
                lines = _doc_lines(rec)
                rec2 = ledger.record(r["doc_no"], lines or [], {
                    "supplier": (r.get("detail") or {}).get("supplier") or rec.get("supplier_raw"),
                    "rows": (r.get("detail") or {}).get("rows"),
                    "source": rec.get("source"),
                    # 注意这句的措辞：它只说明「表出出来了」，不说明货进了库存
                    # （用户 2026-11-25 起两者分开记）。写「已出表」会让人在台账里
                    # 看成「已入库」，而美亚那边可能一个字都没录。
                    "note": "已出表（还没确认导入美亚）",
                })
                r["ledger"] = {"doc_no": rec2["doc_no"], "at": rec2["at"],
                               "fingerprint": rec2["fingerprint"]}
            except (ValueError, OSError) as e:
                # 台账写失败不能让人以为没事：表已经出了，但下次判重会失效。
                r["ledger_error"] = str(e)
                print("  [台账] 记账失败：%s（这次表已出，但下次无法判重，请手工补记）" % e)
        evt("doc", stem=r.get("stem"), status=r.get("state"),
            rows=((r.get("detail") or {}) if isinstance(r.get("detail"), dict) else {}).get("rows"))
        results.append(r)

    dst = os.path.join(a.out, "pipeline_summary.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "commit": False,
                   "documents": results,
                   "emission": {"out_dir": em.get("out_dir"),
                                "products_file": em.get("products_file"),
                                "inbound_file": em.get("inbound_file"),
                                "inbound_groups": em.get("inbound_groups") or [],
                                "counts": c}}, f,
                  ensure_ascii=False, indent=1)
    print("")
    for r in results:
        print("  %-30s %-18s %s" % (os.path.basename(r.get("source") or "?"),
                                    r.get("state"), r.get("origin") or ""))
    print("汇总 -> %s" % dst)
    # duplicate 不算「坏」：它是判重生效的结果，正是我们要的行为。
    # ambiguous 同样不进 bad：它是「库里有多条档案，等人挑一条」，
    # 与 need_build 一样是流程中的一个正常待办，不是程序出错。
    # 算进 bad 的话界面会显示 rc=1 红字，人以为跑失败了，转头去查日志。
    # ambiguous 也不进 bad：它是「库里有多条档案，等人挑一条」，和 need_build
    # 一样是流程里的正常待办，不是程序出错。算进 bad 会让界面显示 rc=1 红字，
    # 人以为跑失败了，转头去查一个并不存在的错误。
    # need_build 也不进 bad：首次导入时它就是正常环节（建完档再出一次表就过了）。
    # awaiting_approval 同理：它等用户点头，不是失败。把它们算进 bad 会让退出码
    # 永远是 1，而人一旦习惯忽略退出码，真正的失败就没人看了。
    bad = [r for r in results
           if r.get("state") not in ("emitted", "ready", "committed", "duplicate",
                                     "skipped_gift", "awaiting_approval",
                                     "all_rejected", "need_build", "ambiguous")]
    evt("done", total=len(results), bad=len(bad), dst=dst)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
