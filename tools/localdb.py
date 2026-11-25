# -*- coding: utf-8 -*-
"""本地产品库（SQLite）。取代「从美亚拉产品库快照」这件事。

背景（用户 m09105 / m09127）：
  美亚不允许第三方对接，所以本程序不再读美亚任何数据。
  但「这个物品建过档没有」这个判断仍然必须做，否则会把已建档的物品
  混进建档表 —— 2026-11-22 实测血案：美亚整批拒绝，同批新物品也建不成。

  所以把历史上的产品库快照（390 个物品 / 272 个身份）一次性灌进本库，
  之后就是本地业务：命中 -> 直接出入库表；没命中 -> 出建档表并标 pending。

数据库：data/local.db（SQLite，WAL）。

三种 status：
  built    美亚里确实有这个物品（我们从快照灌进来的，或用户导回后回填的）
  pending  我们已经出过建档表，但还没导进美亚 —— 下一批可以继续用它出库表
  unknown  从单据里长出来的商品，还没建过档（前缀查询用得上，出表不用）
"""

import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "local.db")

SNAPSHOT = os.path.join(ROOT, "out", "pipeline", "library_snapshot.xlsx")

STATUS_BUILT = "built"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"
# documents.state 用：这张单的表已经出过（2026-11-23 起的唯一终态）。
# 与 products.status 是两套值，别混用：一个是「物品建没建档」，一个是「单据出没出表」。
STATUS_EMITTED = "emitted"

INNER_BASE = 10001          # 本地内部编号起点；已占用的会被跳过


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no     TEXT DEFAULT '',          -- 美亚分配的物品编号，本地永远不猜
    inner_no    TEXT DEFAULT '',          -- 内部编号，本地自增分配
    name        TEXT NOT NULL,
    spec        TEXT DEFAULT '',
    brand       TEXT DEFAULT '',
    manufacturer TEXT DEFAULT '',
    license     TEXT DEFAULT '',
    license_main TEXT DEFAULT '',
    category_l1 TEXT DEFAULT '',
    category_l2 TEXT DEFAULT '',
    category_l3 TEXT DEFAULT '',
    unit        TEXT DEFAULT '',
    pack_qty    TEXT DEFAULT '1',
    pack_unit   TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'unknown',
    source      TEXT DEFAULT '',          -- snapshot / manual / doc
    created_at  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT ''
);

-- 一个身份可能对应多条产品（产品库允许同证同规格多条，甚至换过品牌）。
-- 所以主键是 (ident, product_id) 而不是 ident：漏掉这些条目会让「已建档」
-- 判断退化成「没建档」，于是把它们又塞进建档表 —— 就是 2026-11-22 整批被拒那个血案。
CREATE TABLE IF NOT EXISTS product_ident (
    ident       TEXT NOT NULL,
    product_id  INTEGER NOT NULL,
    PRIMARY KEY (ident, product_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    alias       TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no      TEXT NOT NULL UNIQUE,
    origin      TEXT DEFAULT '',
    doc_date    TEXT DEFAULT '',
    supplier    TEXT DEFAULT '',
    amount      REAL DEFAULT 0,
    fingerprint TEXT DEFAULT '',
    state       TEXT DEFAULT '',
    batch_id    INTEGER,
    created_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS doc_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL,
    product_id  INTEGER,
    name        TEXT DEFAULT '',
    spec        TEXT DEFAULT '',
    brand       TEXT DEFAULT '',
    license     TEXT DEFAULT '',
    qty         REAL DEFAULT 0,
    price       REAL DEFAULT 0,
    amount      REAL DEFAULT 0,
    lot         TEXT DEFAULT '',
    expiry      TEXT DEFAULT '',
    unit        TEXT DEFAULT '',
    status      TEXT DEFAULT '',          -- built / need_build
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT DEFAULT '',
    out_dir     TEXT DEFAULT '',
    counts      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS emissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER,
    doc_no      TEXT DEFAULT '',
    products_file TEXT DEFAULT '',
    inbound_file  TEXT DEFAULT '',
    at          TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ident_pid ON product_ident(product_id);
CREATE INDEX IF NOT EXISTS idx_lines_doc ON doc_lines(doc_id);
CREATE INDEX IF NOT EXISTS idx_status ON products(status);
"""


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    p = path or DB_PATH
    if p != ":memory:":
        os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path=None):
    """建表。幂等。"""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── 身份 ────────────────────────────────────────────────────────────
def identity(license_no, spec, brand):
    """与 build_import.identity_key 完全一致的三元组，序列化成字符串做唯一键。

    为什么不直接存三个列做主键：产品库历史上存在「同名同规格不同品牌」和
    「同注册证不同规格」两种情况，三元组才是真正的建档身份；拼成一个字符串
    比复合主键更省事，也便于日志里一眼看出是哪一条。
    """
    from build_import import split_license, clean, norm_brand
    main, _ = split_license(license_no)
    return "%s|%s|%s" % (main or "",
                         clean(spec or ""),
                         norm_brand(brand) or "")


def lookup_all(ident, conn=None):
    """按身份查所有已建档物品，返回 list（可能多条 = 歧义）。"""
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            "SELECT p.* FROM product_ident i JOIN products p ON p.id = i.product_id "
            "WHERE i.ident = ? ORDER BY p.id", (ident,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def lookup(ident, conn=None):
    """按身份查已建档物品。唯一命中返回 dict，多条命中返回 None（歧义），没有也 None。

    为什么要区分「歧义」和「没有」：歧义时自动取号会把货入到错的物品上，
    必须留空阻断让人挑；而「没有」是要去建档表。两者处置完全不同。
    """
    hits = lookup_all(ident, conn)
    return hits[0] if len(hits) == 1 else None


def lookup_line(line, conn=None):
    """按单据行查。单据行的键是 license_no / spec / brand。"""
    return lookup(identity(line.get("license_no") or line.get("license_no_raw"),
                           line.get("spec"), line.get("brand")), conn)


# ── 取号 ────────────────────────────────────────────────────────────
def next_inner_no(count=1, conn=None):
    """分配 count 个本地内部编号。返回列表。

    为什么要本地发号：内部编号是美亚建档表里的必填列，而美亚分配规则我们
    看不见也猜不到（快照里是 99000+ 这种段）。本地维护一段自增号，
    导入美亚后如果它改号了，用户把新导出的产品库导回来，我们再回填。
    """
    own = conn is None
    conn = conn or connect()
    try:
        used = {r[0] for r in conn.execute(
            "SELECT inner_no FROM products WHERE inner_no != ''").fetchall()}
        out, n = [], INNER_BASE
        while len(out) < count:
            s = str(n)
            if s not in used:
                out.append(s)
            n += 1
        return out
    finally:
        if own:
            conn.close()


def put_pending(name, spec, brand, license_no, category, conn=None):
    """把一个「本地库没有」的物品写成 pending 并返回它的行。

    关键：pending 状态意味着「我们出过建档表，但还没导进美亚」。
    它在**下一批**可以当已建档用（用户可能已经导了），但如果这次还没导，
    它必须继续出现在建档表里 —— 所以调用方要按 batch 决定是否重复列出，
    见 emit.py 的 _should_emit_product()。
    """
    own = conn is None
    conn = conn or connect()
    try:
        ident = identity(license_no, spec, brand)
        row = lookup(ident, conn)
        if row:
            return row
        inner = next_inner_no(1, conn)[0]
        l1, l2, l3, unit, pkg_qty, pkg_unit = (category or (None,) * 6)[:6]
        cur = conn.execute(
            "INSERT INTO products (name, spec, brand, manufacturer, license, license_main,"
            " category_l1, category_l2, category_l3, unit, pack_qty, pack_unit,"
            " inner_no, status, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name or "", spec or "", brand or "", (category or [None] * 7)[6] or "",
             license_no or "", "", l1 or "", l2 or "", l3 or "",
             unit or "", pkg_qty or "1", pkg_unit or "", inner,
             STATUS_PENDING, "doc", _now(), _now()))
        conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id) VALUES (?,?)",
                     (ident, cur.lastrowid))
        conn.commit()
        return dict(conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        if own:
            conn.close()


def remember_product(p, conn=None):
    """把 build_import 生成的一条建档项记成 pending（内部编号沿用它的分配结果）。

    为什么要记：不记的话，下一批同样的物品又要重新发一个内部编号，
    用户先导了 A 批的建档表、再导 B 批的，同一物品就有两个内部编号，
    产品库里出现两条档案 —— 之后按身份查就是「歧义」，整条链都得人工介入。
    """
    own = conn is None
    conn = conn or connect()
    try:
        from build_import import split_license, clean, norm_brand
        lic = p.get("批准文号") or ""
        lic_main, _ = split_license(lic)
        ident = "%s|%s|%s" % (lic_main, clean(p.get("规格") or ""),
                              norm_brand(p.get("品牌") or "") or "")
        hit = conn.execute(
            "SELECT p.id, p.category_l1 FROM product_ident i JOIN products p ON p.id=i.product_id"
            " WHERE i.ident=? ORDER BY p.id LIMIT 1", (ident,)).fetchone()
        if hit:
            # 身份已经记过（上一次出建档表时记的 pending），不能重复插一行；
            # 但**分类要回填**：人在弹窗里选的一级分类只存在于当次的 build()
            # 里，不写回库，下一批同样的物品又从关键词规则开始猜，猜不出就报
            # 「一级分类不能为空」——实测就是这么被美亚退回的。
            if p.get("一级分类") and not (hit["category_l1"] or ""):
                conn.execute("UPDATE products SET category_l1=?, updated_at=? WHERE id=?",
                             (p.get("一级分类"), _now(), hit["id"]))
                conn.commit()
            return None
        cur = conn.execute(
            "INSERT INTO products (name, spec, brand, manufacturer, license, license_main,"
            " category_l1, category_l2, category_l3, unit, pack_qty, pack_unit, inner_no,"
            " status, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.get("物品名称") or "", p.get("规格") or "", p.get("品牌") or "",
             p.get("生产厂家") or "", lic, lic_main,
             p.get("一级分类") or "", p.get("二级分类") or "", p.get("三级分类") or "",
             p.get("采购单位") or "", str(p.get("拆包数量") or "1"),
             p.get("拆包单位") or "", str(p.get("内部编号") or ""),
             STATUS_PENDING, "emit", _now(), _now()))
        conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id) VALUES (?,?)",
                     (ident, cur.lastrowid))
        # 必须在这里提交：不提交时只有「调用方后面又写了别的并 commit」才碰巧落盘
        # （emit.emit() 正好接着写 batches 并提交，所以一直看不出问题）；
        # 单独调用一次这个函数，记录会在关连接时随未提交事务一起回滚 ——
        # 实测「INSERTED 6，下一句统计就只剩 390，pending 0」。
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def stats(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        d = {"products": 0, "idents": 0, "built": 0, "pending": 0, "unknown": 0,
             "docs": 0, "batches": 0}
        d["products"] = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        d["idents"] = conn.execute("SELECT COUNT(*) FROM product_ident").fetchone()[0]
        for st in (STATUS_BUILT, STATUS_PENDING, STATUS_UNKNOWN):
            d[st] = conn.execute(
                "SELECT COUNT(*) FROM products WHERE status=?", (st,)).fetchone()[0]
        d["docs"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        d["batches"] = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        return d
    finally:
        if own:
            conn.close()


# ── 种子：历史产品库快照 ──────────────────────────────────────────
def seed(path=None, conn=None, replace=False):
    """把历史产品库快照灌进本地库。只做一次（幂等：已有 built 记录就跳过）。

    快照列序（实测 out/pipeline/library_snapshot.xlsx，第 2 行表头、第 3 行起数据）：
      0=物品编号 1=物品名称 2=内部编号 3=物品类型 4=药品通用名 5=规格 6=品牌
      7=生产厂家 8=采购单位 9=拆包数量 10=拆包单位 11=批准文号 12=国家医保编码
      13=一级供应商 14=是否高值耗材 15=是否启用
    """
    import openpyxl
    src = path or SNAPSHOT
    if not os.path.exists(src):
        return {"ok": False, "error": "快照不存在：%s" % src}

    own = conn is None
    conn = conn or connect()
    try:
        if replace:
            conn.execute("DELETE FROM product_ident")
            conn.execute("DELETE FROM products WHERE source='snapshot'")
            conn.commit()
        existed = conn.execute(
            "SELECT COUNT(*) FROM products WHERE source='snapshot'").fetchone()[0]
        if existed:
            return {"ok": True, "skipped": True, "already": existed}

        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if len(rows) < 3:
            return {"ok": False, "error": "快照行数不足：%d" % len(rows)}

        from build_import import split_license, clean, norm_brand
        n_p, n_i, n_dup = 0, 0, 0
        for r in rows[2:]:
            if not r or len(r) < 12:
                continue
            g = lambda i: ("" if i >= len(r) or r[i] is None else str(r[i]).strip())
            name = g(1)
            if not name:
                continue
            item_no, inner = g(0), g(2)
            lic_main, _ = split_license(g(11))
            ident = "%s|%s|%s" % (lic_main, clean(g(5)), norm_brand(g(6)) or "")
            # 同身份多条也要全收：它们是「已建档」的证据，丢掉就会重复建档
            dup = conn.execute("SELECT 1 FROM product_ident WHERE ident=?",
                               (ident,)).fetchone()
            if dup:
                n_dup += 1
            cur = conn.execute(
                "INSERT INTO products (item_no, inner_no, name, spec, brand, manufacturer,"
                " license, license_main, category_l1, unit, pack_qty, pack_unit,"
                " status, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_no, inner, name, g(5), g(6), g(7), g(11), lic_main, g(3),
                 g(8), g(9) or "1", g(10), STATUS_BUILT, "snapshot", _now(), _now()))
            # 快照里「物品类型」就是一级分类；二级三级历史数据没存，留空
            conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id)"
                         " VALUES (?,?)", (ident, cur.lastrowid))
            n_p += 1
            n_i += 1
        conn.commit()
        return {"ok": True, "products": n_p, "idents": n_i, "dup_ident": n_dup,
                "rows_total": len(rows) - 2}
    finally:
        if own:
            conn.close()


def _read_sheet_rows(path):
    """读一张表的所有行，返回 list[tuple]。xlsx/xlsm 用 openpyxl，.xls 用 xlrd。

    为什么要分两个库：美亚「产品库 → 批量导出」在不同账号/浏览器下给的是 .xls
    （Excel 97-2003 老格式），openpyxl 对它直接抛 InvalidFileException，页面
    上就是一大段英文异常（实测用户 2026-11-24 报的就是这个）。而 xlrd 2.x
    反过来只支持 .xls、不支持 .xlsx，两者正好互补。

    **这不是读美亚数据**：只看本地磁盘上的文件，不发任何网络请求。
    """
    p = str(path or "")
    with open(p, "rb") as fh:
        head = fh.read(8)
    # 认文件头，不认扩展名。实测用户传的「产品库.xls」其实是 xlsx
    # （文件头 50 4B 03 04 = PK 压缩包），openpyxl 只看扩展名就抛
    # InvalidFileException；反过来若真给了 Excel 97-2003（D0 CF 11 E0），
    # openpyxl 也读不了，必须走 xlrd。两种都必须按内容判，否则页面报的
    # 永远是那句和事实相反的英文异常。
    if head[:4] == b"PK\x03\x04":
        import openpyxl
        # 必须传文件对象而不是路径：openpyxl 只看扩展名，名字叫 .xls 就直接
        # 抛「does not support the old .xls file format」——哪怕内容是 xlsx。
        fh = open(p, "rb")
        try:
            wb = openpyxl.load_workbook(fh, data_only=True, read_only=True)
        finally:
            pass
        try:
            ws = wb[wb.sheetnames[0]]
            return list(ws.iter_rows(values_only=True))
        finally:
            wb.close()
            fh.close()
    if head[:4] == b"\xd0\xcf\x11\xe0":
        try:
            import xlrd
        except ImportError:
            raise RuntimeError("这个表是 Excel 97-2003 老格式（.xls），需要 xlrd 才能读，"
                               "当前环境没装。可以先用 Excel 打开「另存为」xlsx 再传。")
        book = xlrd.open_workbook(p)
        sheet = book.sheet_by_index(0)
        return [tuple(sheet.row_values(r)) for r in range(sheet.nrows)]
    raise RuntimeError("这个文件不是 Excel（文件头 %s）。请确认选的是美亚导出的"
                       "产品库表格，或在 Excel 里另存为 xlsx。" % head[:4].hex())


def import_export(path, conn=None, dry_run=False):
    """把用户从美亚导出的产品库 xlsx 合并进本地库。

    **这不是「读取美亚数据」**：文件是用户自己在美亚界面点「批量导出」拿到的，
    程序只读本地磁盘上的这个文件，一个网络请求都不发（no_meiya 也拦着）。

    合并规则（**按可靠度依次试，前面的命中就不用后面的**）：
      ① 物品编号（美亚发的那个号，唯一且稳定）
      ② 内部编号（我们自己发的号，建档表里带出去的那个）
      ③ 身份 identity = 注册证主号+规格+品牌
      ④ 都没有 → 新增一条，status=built（美亚里既然有，就是已建档）
    返回 {"added": n, "updated": m, "skipped": k}

    为什么不能只按身份匹（血案）：实测用户 2026-11-24 传回来的 396 行导出表，
    只按身份算会「新增 297 条」，而本地库本来就有这些物品 —— 身份匹配失败，
    因为快照里 297 条的规格列是空的、品牌填的是供应商名。导进去等于凭空
    多出 297 条重复档案，之后按身份查全是歧义。物品编号是美亚发的，
    内部编号是我们发的，两个都比「注册证+规格+品牌」可靠得多。

    为什么要这一步：本地库的种子是 2026-11-22 那份快照，之后新建的档
    （比如 2 月那批 10382 义获嘉粘接剂、10384 豆瓣成形片）本地根本不知道。
    不导回来，它们每一批都会被判成「待建档」，人就得反复手工挑。
    """
    src = path
    if not os.path.exists(src):
        return {"ok": False, "error": "文件不存在：%s" % src}
    own = conn is None
    conn = conn or connect()
    try:
        from build_import import split_license, clean, norm_brand
        try:
            rows = _read_sheet_rows(src)
        except Exception as e:
            # 把 openpyxl / xlrd 的英文异常翻成人能看懂的一句话。
            # 直接抛出去时页面上显示的是一整段 InvalidFileException，
            # 用户不知道该做什么（实测 2026-11-24）。
            return {"ok": False, "file": os.path.abspath(src),
                    "error": "这个表格读不了：%s" % e}
        if len(rows) < 3:
            return {"ok": False, "error": "行数不足：%d" % len(rows)}
        # 表头可能在第 1 行（用户手工整理过）也可能在第 2 行（美亚原始导出）
        hdr_row = 0
        for i in range(min(3, len(rows))):
            vals = [str(v).strip() if v is not None else "" for v in (rows[i] or [])]
            if "物品名称" in vals and "规格" in vals:
                hdr_row = i
                break
        hdr = [str(v).strip() if v is not None else "" for v in (rows[hdr_row] or [])]
        col = {k: hdr.index(k) for k in
               ("物品编号", "内部编号", "物品名称", "规格", "品牌", "生产厂家", "批准文号")
               if k in hdr}
        if "物品名称" not in col or "规格" not in col:
            return {"ok": False, "error": "表头缺列：%s" % (hdr[:12],)}

        added = updated = skipped = 0
        for r in rows[hdr_row + 1:]:
            if not r:
                continue
            g = lambda k: ("" if col.get(k) is None or col[k] >= len(r) or r[col[k]] is None
                           else str(r[col[k]]).strip())
            name = g("物品名称")
            if not name:
                continue
            item_no, inner = g("物品编号"), g("内部编号")
            lic_main, _ = split_license(g("批准文号"))
            ident = "%s|%s|%s" % (lic_main, clean(g("规格")),
                                  norm_brand(g("品牌")) or norm_brand(g("生产厂家")) or "")
            # ① 物品编号。只认「库里那条自己还没号」或「号一样」的，避免把
            #    一件物品的号写到另一件上（历史上出现过手工填错号的表）。
            pid = None
            if item_no:
                row = conn.execute("SELECT id FROM products WHERE item_no=?"
                                   " ORDER BY id LIMIT 1", (item_no,)).fetchone()
                if row:
                    pid = row[0]
            # ② 内部编号：建档表带出去的号，用户拿去美亚建档后原样带回来。
            #    这一条能把「刚建完的档」准确接回原来那条 pending 记录。
            if pid is None and inner:
                row = conn.execute("SELECT id FROM products WHERE inner_no=?"
                                   " ORDER BY id LIMIT 1", (inner,)).fetchone()
                if row:
                    pid = row[0]
            # ③ 身份：注册证+规格+品牌。前三者都对不上时才可能走到这里。
            if pid is None:
                hits = conn.execute(
                    "SELECT p.id FROM product_ident i JOIN products p ON p.id=i.product_id"
                    " WHERE i.ident=? ORDER BY p.id", (ident,)).fetchall()
                if hits:
                    pid = hits[0][0]
            if pid is not None:
                if not item_no:
                    skipped += 1        # 库里已有，导出表这行没号，没什么可回填的
                    continue
                if not dry_run:
                    conn.execute(
                        "UPDATE products SET item_no=?, inner_no=COALESCE(NULLIF(?,''), inner_no),"
                        " status=?, updated_at=? WHERE id=?",
                        (item_no, inner, STATUS_BUILT, _now(), pid))
                    # 身份没登记过的补登记：靠 ① ② 命中的行原来可能没有 ident 行
                    conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id)"
                                 " VALUES (?,?)", (ident, pid))
                updated += 1
                continue
            if dry_run:
                added += 1
                continue
            cur = conn.execute(
                "INSERT INTO products (item_no, inner_no, name, spec, brand, manufacturer,"
                " license, license_main, category_l1, unit, pack_qty, pack_unit,"
                " status, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_no, inner or next_inner_no(1, conn)[0], name, g("规格"), g("品牌"),
                 g("生产厂家"), g("批准文号"), lic_main, "", "", "1", "",
                 STATUS_BUILT, "export", _now(), _now()))
            conn.execute("INSERT OR IGNORE INTO product_ident (ident, product_id) VALUES (?,?)",
                         (ident, cur.lastrowid))
            added += 1
        if not dry_run:
            conn.commit()
        return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
                "header_row": hdr_row + 1}
    finally:
        if own:
            conn.close()


# ── 迁移：历史台账 → documents 表 ────────────────────────────────────
def migrate_ledger(path=None, conn=None):
    """把 config/inbound_ledger.json 的历史记录搬进 documents 表。

    为什么一定要搬：台账原本记的是「这张单已经真入库」，2026-11-23 定位改成
    「只出表」之后，它记的是「这张单已出过表」。两者在判重上等价（判重键一直是
    「单据号 + 明细指纹」），但只有摆进 documents 表，界面的历史才会完整 ——
    否则「上个月那张单去哪了」只能去 JSON 里翻。

    为什么标 state='emitted' 而不是新造一个「已入库」：对外的语义现在就这一个，
    多一个状态会让人以为还有别的出路。真正的出处写在 note 里（供应商字段后面
    带了 RK 号就一并留着），需要追溯时看 note。
    """
    own = conn is None
    conn = conn or connect()
    try:
        import ledger as ledger_mod
        j = ledger_mod.load(path)
        recs = j.get("records") or []
        added = updated = 0
        for r in recs:
            doc_no = (r.get("doc_no") or "").strip()
            if not doc_no:
                continue
            note_sup = r.get("supplier") or ""
            rk = r.get("meiya_order_no")
            if rk:
                note_sup = (note_sup + " / " if note_sup else "") + "历史入库单 " + str(rk)
            row = conn.execute("SELECT id FROM documents WHERE doc_no=?",
                               (doc_no,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE documents SET amount=?, fingerprint=?, state=?, supplier=?"
                    " WHERE id=?",
                    (float(r.get("amount") or 0), r.get("fingerprint") or "",
                     STATUS_EMITTED, note_sup, row["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO documents (doc_no, origin, doc_date, supplier, amount,"
                    " fingerprint, state, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (doc_no, "历史台账", "", note_sup, float(r.get("amount") or 0),
                     r.get("fingerprint") or "", STATUS_EMITTED,
                     r.get("at") or _now()))
                added += 1
        conn.commit()
        return {"ok": True, "total": len(recs), "added": added, "updated": updated}
    finally:
        if own:
            conn.close()


# ── CLI ─────────────────────────────────────────────────────────────
def _main(argv):
    cmd = argv[0] if argv else "stats"
    if cmd == "init":
        init()
        print("DB 已建：%s" % DB_PATH)
        return 0
    if cmd == "seed":
        init()
        r = seed(replace="--replace" in argv)
        print(r)
        return 0 if r.get("ok") else 1
    if cmd == "stats":
        st = stats()
        print("本地库 %s" % DB_PATH)
        print("  物品 %d 条（已建档 %d / 待导入 %d / 未建档 %d）  身份 %d 个"
              % (st["products"], st["built"], st["pending"], st["unknown"], st["idents"]))
        print("  批量出表 %d 次  单据 %d 张" % (st["batches"], st["docs"]))
        return 0
    if cmd == "import":
        if len(argv) < 2:
            print("用法：localdb.py import <产品库导出的 xlsx> [--dry-run]")
            return 2
        init()
        r = import_export(argv[1], dry_run="--dry-run" in argv)
        print(r)
        return 0 if r.get("ok") else 1
    if cmd == "migrate":
        # python tools/localdb.py migrate [--dry-run 不做]
        init()
        r = migrate_ledger()
        print(r)
        return 0 if r.get("ok") else 1
    if cmd == "find":
        q = argv[1] if len(argv) > 1 else ""
        conn = connect()
        rows = conn.execute(
            "SELECT item_no, inner_no, name, spec, brand, category_l1, status FROM products"
            " WHERE name LIKE ? OR spec LIKE ? OR brand LIKE ? LIMIT 40",
            ("%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q)).fetchall()
        conn.close()
        print("命中 %d 条" % len(rows))
        for r in rows:
            print("  %-8s %-7s %-40s %-18s %-12s %s"
                  % (r["item_no"] or "-", r["inner_no"] or "-", r["name"][:40],
                     (r["spec"] or "")[:18], (r["brand"] or "")[:12], r["status"]))
        return 0
    print("用法：localdb.py [init|seed [--replace]|stats|find <关键词>]")
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
