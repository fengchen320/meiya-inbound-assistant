# -*- coding: utf-8 -*-
import sys, io, datetime
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import expiry as E

cases = [
    # (有效期, 单据日期, 期望 ok, 期望 code)
    ("2028-12-15", "2026-02-02", True, "ok"),
    ("", "2026-02-02", False, "no_expiry"),
    ("1900-01-01", "2026-02-02", False, "no_expiry"),
    ("/", "2026-02-02", False, "no_expiry"),
    (None, "2026-02-02", False, "no_expiry"),
    ("2026-09-18", "2026-02-02", False, "short_expiry"),   # 约 7.5 个月
    ("2027-01-01", "2026-02-02", False, "short_expiry"),   # 334 天
    ("2027-02-02", "2026-02-02", True, "ok"),              # 恰好 365 天
    ("2026-01-01", "2026-02-02", False, "short_expiry"),   # 已过期
    ("20271223", "2026-02-02", True, "ok"),
    ("1766448000000", "2026-02-02", False, "short_expiry"),  # 2025-12-23 已过期
]
bad = 0
for exp, dd, want_ok, want_code in cases:
    r = E.check(exp, dd)
    ok = (r["ok"] == want_ok) and (r["code"] == want_code)
    if not ok: bad += 1
    print(("OK  " if ok else "FAIL"), repr(exp), dd, "->", r["code"], r["ok"], r["msg"][:40])
# 缺单据日期时用 today
r = E.check("2028-01-01", None, today=datetime.date(2026, 1, 1))
print("NO_DOC_DATE", r["code"], r["ok"])
print("ALL_OK", bad == 0)
