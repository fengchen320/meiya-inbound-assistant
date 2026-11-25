# -*- coding: utf-8 -*-
"""localdb.remember_product 必须自己提交（2026-11-24 血案回归）。

症状：单独调用 remember_product 后，同一进程里 count 是 396，另起一个进程再看
又变回 390 —— 记录随未提交事务在关连接时回滚了。此前看不出问题，是因为
emit.emit() 紧接着写 batches 并 commit，把 insert 一起提交了。
"""
import io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb

DB = os.path.join(ROOT, "tmp", "t_lib_commit.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)

row = {"物品名称": "测试 提交验证", "规格": "1支/盒", "品牌": "测试牌",
       "批准文号": "川械注准20259999", "采购单位": "盒", "拆包数量": 1,
       "内部编号": "90099"}
c1 = localdb.connect(DB)
got = localdb.remember_product(row, c1)
c1.close()

c2 = localdb.connect(DB)
n = c2.execute("SELECT COUNT(*) FROM products").fetchone()[0]
ok = (got is not None) and n == 1
print("inserted", got, "rows_in_new_conn", n, "OK" if ok else "FAIL")
c2.close()
os.remove(DB)
print("ALL_OK", ok)
sys.exit(0 if ok else 1)
