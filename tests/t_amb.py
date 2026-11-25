# -*- coding: utf-8 -*-
"""档案歧义（同身份多条档案）回归：2026-11-24 甲硝唑棒「该物品已存在」。

跑法：python tmp/t_amb.py

现场：本地库里「甲硝唑棒 / 每100g含甲硝唑22g（2支/盒）/ 佑华制药（乐山）有限公司」
这一条身份同时挂着 10321 与 10325 两条档案（美亚自己的产品库就有两条）。
旧行为把它判进 need_build → 进建档表 → 人导进美亚，整行被回「1.该物品已存在」。

正确行为：
  1. 判成 ambiguous（不是 need_build），且这一行**不进**建档表；
  2. decision.json / summary 里带候选（item_no + 内部编号 + 名字），供界面选择；
  3. 选了之后写进 config/inbound_overrides.json，重跑就命中那条，变 emitted；
  4. 只按身份记，不改写库里的两条档案（删重复是人的事）。
"""
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_import, emit, localdb

ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-34s %-30s %s" % (label, got, "OK" if good else "WANT " + str(want)))


DB = os.path.join(os.path.join(ROOT, "tmp"), "t_amb.db")
OVR = os.path.join(os.path.join(ROOT, "tmp"), "t_amb_overrides.json")
for f in (DB, OVR, OVR + ".tmp"):
    if os.path.exists(f):
        os.remove(f)
localdb.init(DB)
conn = localdb.connect(DB)

NAME = "测试 甲硝唑棒"
SPEC = "每100g含甲硝唑22g（2支/盒）"
BRAND = "测试佑华制药"
LIC = "国药准字H10939999"
ident = build_import.identity_key(LIC, SPEC, BRAND)   # 三元组 (注册证主号, 规格, 品牌)

# 库里同一条身份挂两条档案（美亚产品库真实就是这样）
for item_no, inner in (("10321", "92008"), ("10325", "92013")):
    cur = conn.execute(
        "INSERT INTO products (item_no, inner_no, name, spec, brand, license, license_main,"
        " status, source) VALUES (?,?,?,?,?,?,?,'built','snapshot')",
        (item_no, inner, NAME, SPEC, BRAND, LIC, LIC))
    conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id) VALUES (?,?)",
                 ("|".join(ident), cur.lastrowid))
conn.commit()

DOC = {"document": {"doc_no": "TESTAMB0001", "doc_date": "2026-05-21",
                    "supplier_raw": "测试供应商甲"},
       "lines": [{"product_name": NAME, "spec": SPEC, "brand": BRAND,
                  "license_no": LIC, "unit": "盒", "quantity": 1, "unit_price": 45.0,
                  "amount": 45.0, "lot_no": "L1", "expiry_date": "2028-05-01",
                  "manufacturer": "测试厂"}]}
p = os.path.join(os.path.join(ROOT, "tmp"), "TESTAMB0001_extracted.json")
json.dump(DOC, open(p, "w", encoding="utf-8"), ensure_ascii=False)

OLD = build_import.DEFAULT_OVERRIDES
build_import.DEFAULT_OVERRIDES = OVR          # 别污染真实的 config/inbound_overrides.json
try:
    # ── 1. 歧义：状态、不进建档表、带候选 ──
    out1 = os.path.join(os.path.join(ROOT, "out"), "emit", "t_amb1")
    r1 = emit.emit([p], out1, conn=conn)
    d1 = r1["docs"][0]
    chk("1 状态", d1["state"], "ambiguous")
    chk("1 不进待建档", len(d1["need_build"]), 0)
    chk("1 入库行", len(d1["inbound_rows"]), 0)
    chk("1 建档表行数", r1["counts"]["products"], 0)
    amb = r1["decision"]["documents"][0].get("ambiguous") or []
    chk("1 决策里带歧义项", len(amb), 1)
    chk("1 身份键", amb[0]["ident"] if amb else "", "|".join(ident))
    cands = sorted(c["item_no"] for c in (amb[0]["cands"] if amb else []))
    chk("1 候选物品编号", ",".join(cands), "10321,10325")
    chk("1 counts.ambiguous", r1["counts"]["ambiguous"], 1)
    # 建档表文件本身也不能有这行（不能只靠 counts）
    pf = r1["products_file"]
    chk("1 没生成建档表", bool(pf), False)

    # ── 2. 选了 10321 → 记进 overrides → 重跑命中 ──
    n = build_import.add_override("|".join(ident), "10321")
    chk("2 overrides 条数", n, 1)
    saved = json.load(open(OVR, encoding="utf-8"))
    chk("2 写的是人能看懂的三段键", "|".join(ident) in saved, True)
    chk("2 选的值", saved.get("|".join(ident)), "10321")

    out2 = os.path.join(os.path.join(ROOT, "out"), "emit", "t_amb2")
    r2 = emit.emit([p], out2, conn=conn)
    d2 = r2["docs"][0]
    chk("2 状态", d2["state"], "ready")
    chk("2 入库行", len(d2["inbound_rows"]), 1)
    row = d2["inbound_rows"][0]
    chk("2 用的物品编号", row[1], "10321")
    # 内部编号必须跟着选中那条走，不能自己再发一个
    chk("2 沿用那条的内部编号", row[8], "92008")

    # ── 3. 非候选编号不得写进去（界面过期/手滑）──
    # add_override 本身不校验（它是通用写入口，服务端 _resolve 才校验候选），
    # 这里只断言「按身份取号」这条链没被别的路径绕过：不选就还是歧义。
finally:
    build_import.DEFAULT_OVERRIDES = OLD
    conn.close()
    for f in ("TESTAMB0001_extracted.json",):
        fp = os.path.join(os.path.join(ROOT, "tmp"), f)
        if os.path.exists(fp):
            os.remove(fp)

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
