# -*- coding: utf-8 -*-
"""统一把 .xls / .xlsx 读成二维字符串数组。

两条路，按顺序试：
  - BIFF8 的 .xls 先用 **xlrd** 直接读（已装 xlrd 2.0.2，只读 .xls 仍然支持）。
    这是现在的主路径：纯 Python、无外部进程、单文件约 20ms。
  - xlrd 不可用才退回「Excel COM 转存成临时 xlsx + openpyxl」。
    COM 这条路对本机所有 .xls 都能读通，但每次要起一个 PowerShell 子进程去开
    Excel——实测连续转 30 个文件时会在第 8 个左右卡住（Excel 进程没退出，
    后续 saveAs 排队），整批拉单因此挂死。所以它只当兜底。
  - openpyxl 只能读 xlsx（OOXML），读 BIFF8 的 .xls 会直接报错。

关键点：美亚「批量导出」下载的文件后缀是 .xls，内容其实是 xlsx（PK 头），
所以要**先嗅探文件头**再决定走哪条路，不能只看扩展名。
"""

import os
import shutil
import subprocess
import sys
import tempfile

__all__ = ["sniff", "to_xlsx", "read_sheet", "read_rows"]

_MAGIC_ZIP = b"PK\x03\x04"
_MAGIC_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_xls2xlsx.ps1")


def sniff(path):
    """返回 'xlsx' / 'xls' / 'unknown'，按文件头而不是扩展名判断。"""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(_MAGIC_ZIP):
        return "xlsx"
    if head.startswith(_MAGIC_OLE):
        return "xls"
    return "unknown"


_PS1_BODY = r'''param([string]$Src, [string]$Dst)
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$xl.DisplayAlerts = $false
try {
    $wb = $xl.Workbooks.Open($Src, 0, $true)
    $wb.SaveAs($Dst, 51)
    $wb.Close($false)
    Write-Output ("OK sheets=" + $wb.Worksheets.Count)
} finally {
    $xl.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($xl) | Out-Null
}
'''


def _scratch_dir():
    """临时文件放工作区内。

    不用 tempfile.mkdtemp() 的原因：某些环境下对系统 temp 的写入会被安全策略
    拒绝（实测抛 PermissionError，路径形如 \\Temp\\xxx_xls_...\\src.xls），
    而程序工作区一定是可写的。
    """
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "xlsread")
    os.makedirs(d, exist_ok=True)
    return d


def to_xlsx(path, workdir=None):
    """确保返回一个可被 openpyxl 打开的 xlsx 路径；必要时用 Excel COM 转换。"""
    kind = sniff(path)
    if kind == "xlsx":
        # 扩展名与真实格式不符时（美亚「批量导出」下的是 .xls，内容其实是 PK 头 xlsx；
        # 反过来本仓库的 in_template.xls 也是 xlsx 内容），openpyxl 只认扩展名，会直接
        # 报 InvalidFileException，所以必须复制成 .xlsx 再交给它。
        if path.lower().endswith((".xlsx", ".xlsm")):
            return path
        d = workdir or _scratch_dir()
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, "sniffed_as_xlsx.xlsx")
        shutil.copyfile(path, dst)
        return dst
    if kind == "unknown":
        raise ValueError("无法识别的文件格式（既不是 OOXML 也不是 BIFF8）：%s" % path)

    # COM 对中文路径 / 中文文件名不稳定（实测会报「无法找到文件」），先复制成 ASCII 名
    tmpdir = workdir or _scratch_dir()
    os.makedirs(tmpdir, exist_ok=True)
    src = os.path.join(tmpdir, "src.xls")
    dst = os.path.join(tmpdir, "dst.xlsx")
    shutil.copyfile(path, src)

    os.makedirs(os.path.dirname(_PS1), exist_ok=True)
    with open(_PS1, "w", encoding="utf-8-sig") as f:
        f.write(_PS1_BODY)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _PS1, src, dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if not os.path.exists(dst):
        raise RuntimeError("Excel COM 转换失败：%s\n%s" % (r.stdout, r.stderr))
    return dst


def _read_sheet_xlrd(path, sheet=None):
    """用 xlrd 直接读 BIFF8 .xls。读不了返回 None（交给 COM 兜底）。"""
    try:
        import xlrd
    except ImportError:
        return None
    try:
        book = xlrd.open_workbook(path, on_demand=True)
    except Exception:
        return None
    try:
        names = book.sheet_names()
        if not names:
            return None
        idx = names.index(sheet) if sheet and sheet in names else 0
        sh = book.sheet_by_index(idx)
        out = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                if v is None:
                    row.append("")
                elif isinstance(v, float):
                    # xlrd 把所有数字都当 float 读；整数值要还原成 "1" 而不是 "1.0"，
                    # 否则批号 20260506 会变成 "20260506.0"，跟文本批号对不上。
                    row.append(str(int(v)) if v == int(v) else repr(v))
                else:
                    row.append(str(v).strip())
            out.append(row)
        return sh.name, out
    finally:
        try:
            book.release_resources()
        except Exception:
            pass


def read_sheet(path, sheet=None, workdir=None):
    """读取一个 sheet，返回 (sheet名, 二维列表)。单元格一律转成字符串并 strip。"""
    if sniff(path) == "xls":
        got = _read_sheet_xlrd(path, sheet)
        if got:
            return got
    import openpyxl
    x = to_xlsx(path, workdir)
    wb = openpyxl.load_workbook(x, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.worksheets[0]
        out = []
        for row in ws.iter_rows(values_only=True):
            out.append(["" if v is None else str(v).strip() for v in row])
        return ws.title, out
    finally:
        wb.close()


def read_rows(path, sheet=None, header_row=None, workdir=None):
    """读取并把第 header_row 行当表头，返回 (单据头区行, 表头列表, 数据行列表)。

    header_row 从 1 开始计数。返回的数据行已去掉尾部全空行。
    """
    name, grid = read_sheet(path, sheet, workdir)
    if header_row is None:
        header_row = 1
    if header_row - 1 > len(grid):
        raise ValueError("header_row=%d 超出表格行数 %d" % (header_row, len(grid)))
    head_block = grid[: header_row - 1]
    header = grid[header_row - 1]
    data = grid[header_row:]
    while data and not any(c for c in data[-1]):
        data.pop()
    return name, head_block, header, data
