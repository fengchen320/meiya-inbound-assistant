# -*- coding: utf-8 -*-
"""生产厂家超长截断（美亚「生产厂家最多支持输入50个字」）回归。

现场 m13286：DMG Luxatemp 那条厂家 53 字，整份建档表被美亚退回。
规矩：本地库存全称（证据），只有写 xlsx 那一刻截断到 50 字且末尾带「…」。
"""
import io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import openpyxl
import build_import as bi

ok = True


def ck(name, cond, extra=""):
    global ok
    print(("OK   " if cond else "FAIL ") + name + (" " + str(extra) if extra else ""))
    if not cond:
        ok = False


LONG = "德国DMG化学医药集团公司DMG Chemisch-Pharmazeutische Fabrik GmbH"
ck("夹具长度是 53", len(LONG) == 53, len(LONG))

t = bi.fit_cell("生产厂家", LONG)
ck("截断后 <= 50", len(t) <= 50, len(t))
ck("截断后以 … 结尾", t.endswith(bi.ELLIPSIS), t)
ck("没把 GmbH 切成 Gmb", not t.endswith("Gmb"), t)

ck("没登记的列原样返回", bi.fit_cell("物品名称", LONG) == LONG)
ck("短值原样返回", bi.fit_cell("生产厂家", "奥美科公司") == "奥美科公司")
ck("None 原样返回", bi.fit_cell("生产厂家", None) is None)
ck("金额数字原样返回", bi.fit_cell("生产厂家", 12345) == 12345)

# 写表实测：表里是截断值，调用方手里那份（本地库来源）必须还是全称
out = os.path.join(ROOT, "out", "emit", "t_mfrlen", "products.xlsx")
os.makedirs(os.path.dirname(out), exist_ok=True)
row = {"内部编号": "99001", "物品名称": "DMG Luxatemp", "一级分类": "修复类", "规格": "76g",
       "采购单位": "盒", "拆包数量": 1, "拆包单位": "盒", "生产厂家": LONG}
bi.write_products([dict(row)], out)
wb = openpyxl.load_workbook(out)
ws = wb.worksheets[0]
hdr = [ws.cell(row=2, column=c).value for c in range(1, ws.max_column + 1)]
cell = ws.cell(row=3, column=hdr.index("生产厂家") + 1).value
ck("表里厂家 <= 50", len(str(cell or "")) <= 50, len(str(cell or "")))
ck("表里厂家带 …", str(cell or "").endswith(bi.ELLIPSIS), cell)
ck("入参没被就地改掉", row["生产厂家"] == LONG, row["生产厂家"])

# 模板示例行必须已被清掉（前 2 行之外不该有别的数据）
ck("只有 1 行数据", ws.max_row == 3, ws.max_row)
os.remove(out)

print("ALL_OK" if ok else "SOME_FAILED")
sys.exit(0 if ok else 1)
