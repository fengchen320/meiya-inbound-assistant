# -*- coding: utf-8 -*-
"""整批发号与分类回落回归（2026-11-24，用户 m12146）。

跑法：python tmp/t_alloc.py

三件事在真实批次里同时出过事：
  1. 一批多单各自从 inner_base 发号 → 建档表里 90005 出现 3 次，
     美亚导入报「内部编号已存在」，那几行整行退回；
  2. 关键词规则推不出分类时直接判「一级分类不能为空」，
     可库里同类物品早有分类（396 条里 76 条推不出，75 条库里有答案）；
  3. 人在弹窗里选过的分类只活在当次 build()，没写回库，
     下一批同样的物品又被拦一次。
"""
import io, json, os, shutil, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb, emit, build_import, openpyxl

OUT = os.path.join(ROOT, "out", "emit", "t_alloc")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
DB = os.path.join(ROOT, "tmp", "t_alloc.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
conn = localdb.connect(DB)

ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-34s %-28s %s" % (label, got, "OK" if good else "WANT " + str(want)))


def save(no, lines, sup="测试供应商甲"):
    d = {"document": {"doc_no": no, "doc_date": "2026-04-01", "supplier_raw": sup},
         "lines": lines}
    p = os.path.join(os.path.join(ROOT, "tmp"), no + "_extracted.json")
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return p


def line(name, spec, lic, mfr="测试厂"):
    return {"product_name": name, "spec": spec, "brand": "测试牌", "license_no": lic,
            "unit": "盒", "quantity": 1, "unit_price": 10.0, "amount": 10.0,
            "lot_no": "L1", "expiry_date": "2028-01-01", "manufacturer": mfr}


# 三份单据，各两件互不相同的新物品。物品名刻意用规则推不出分类的名字（「测试 X」）。
p1 = save("TESTALLOC0001", [line("测试 甲一", "1支/盒", "川械注准20259101"),
                            line("测试 甲二", "2支/盒", "川械注准20259102")])
p2 = save("TESTALLOC0002", [line("测试 乙一", "3支/盒", "川械注准20259103"),
                            line("测试 乙二", "4支/盒", "川械注准20259104")])
p3 = save("TESTALLOC0003", [line("测试 丙一", "5支/盒", "川械注准20259105"),
                            line("测试 丙二", "6支/盒", "川械注准20259106")])

r = emit.emit([p1, p2, p3], os.path.join(OUT, "a"), conn=conn)
rows = r["docs"][0]["products"] + r["docs"][1]["products"] + r["docs"][2]["products"]
codes = [p["内部编号"] for p in rows]
chk("建档项数", len(codes), 6)
chk("内部编号互不相同", len(set(codes)), 6)
chk("没有空号", sorted(c for c in codes if not c), [])

# 建档表里也不许撞号（那才是美亚看到的东西）
wb = openpyxl.load_workbook(os.path.join(OUT, "a", "products.xlsx"))
ws = wb.active
hdr = [c.value for c in ws[2]]
col = hdr.index("内部编号") + 1
xlsx_codes = [ws.cell(row=i, column=col).value for i in range(3, ws.max_row + 1)]
chk("建档表行数", len(xlsx_codes), 6)
chk("建档表内部编号唯一", len(set(xlsx_codes)), 6)

# ── 分类回落本地库：同一身份库里已有分类，规则推不出也应当用库里的 ──
ident = build_import.identity_key("川械注准20259101", "1支/盒", "测试牌")
conn.execute("UPDATE products SET category_l1=? WHERE inner_no=?", ("补牙类", codes[0]))
conn.commit()
r2 = emit.emit([p1], os.path.join(OUT, "b"), conn=conn)
p_first = [x for x in r2["docs"][0]["products"] if x["规格"] == "1支/盒"]
chk("分类回落到库里", (p_first[0]["一级分类"] if p_first else "NOITEM"), "补牙类")

# ── pending 沿用同一个内部编号：再跑一次，号不能变 ──
before = {p["规格"]: p["内部编号"] for p in r["docs"][0]["products"]}
r3 = emit.emit([p1], os.path.join(OUT, "c"), conn=conn)
after = {p["规格"]: p["内部编号"] for p in r3["docs"][0]["products"]}
chk("重跑沿用内部编号", after, before)

# ── remember_product 回填分类：库里为空时要用新建档项的分类补上 ──
conn.execute("UPDATE products SET category_l1='' WHERE inner_no=?", (codes[0],))
conn.commit()
localdb.remember_product({"物品名称": "测试 甲一", "规格": "1支/盒", "品牌": "测试牌",
                          "批准文号": "川械注准20259101", "一级分类": "正畸类",
                          "内部编号": codes[0]}, conn)
got = conn.execute("SELECT category_l1 FROM products WHERE inner_no=?",
                   (codes[0],)).fetchone()[0]
chk("remember 回填分类", got, "正畸类")
n = conn.execute("SELECT count(*) FROM products WHERE inner_no=?", (codes[0],)).fetchone()[0]
chk("remember 没有重复插行", n, 1)

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
