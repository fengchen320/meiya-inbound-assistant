# -*- coding: utf-8 -*-
"""生产企业名称补全：解决纸质单据印刷截断导致厂家识别失败。

对应文档：docs/dental-inbound-prompt.md §7.4

三条铁律（不可违反）：
1. 原文永远单独保存到 manufacturer_raw，不覆盖、不丢弃（§15 第 1、3 条）；
2. 补全必须有据可依，按 注册证号 → 别名精确 → 别名前缀补全 逐级降置信度；
3. 补不出来就保留原文并标 needs_review，禁止臆造，禁止静默"看起来像"的猜测。

为什么注册证优先：注册证号是确定性主键，与纸张印刷质量、OCR 分辨率无关。
只要单据上有注册证号，生产厂家就能唯一确定——这是本问题的根治手段。
"""
import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALIAS_PATH = os.path.join(ROOT, "config", "manufacturer_aliases.json")

# 公司后缀残片：出现在末尾且缺句点，提示可能被列宽截断（弱证据，仅用于提示）
_SUFFIX_HINTS = ("ltd", "co", "inc", "corp", "industries", "industrial",
                 "company", "limited", "gmbh", r"s\.a", r"b\.v")


def _norm(s):
    """归一化：NFKC 全角转半角、小写、去标点、合并空格。只用于比对，不用于展示。"""
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", str(s)).lower()
    t = re.sub(r"[.,;:()\uFF08\uFF09\[\]\u3010\u3011'\"\u00b7\-]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _lickey(s):
    """注册证号比较键：NFKC、小写、去掉**所有**空白。

    为什么要单独一个键、而不复用 _norm()：证号里没有标点语义，空格却可能出现在号中间
    （「晋晋中械备 20250013」是**一个**号，空格不是分隔符）。_norm() 会把空白合并成一个空格
    而不是删掉，两边写法稍有差异就判不等。
    """
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", str(s)).lower()
    t = re.sub(r"\s+", "", t)
    # 末尾的「号」是写法差异，不是证号的一部分：单据写「鲁日械备20170014号」，
    # 别名表写「鲁日械备20170014」，两边必须能对上，否则同一条要登记两遍。
    return t[:-1] if t.endswith("号") else t


# 复合注册证的分隔符。斜杠/逗号是明确的「两个号」；顿号、分号见过同义写法，一并认。
_LIC_SEP = re.compile(r"[/,;\uff0c\u3001\uff1b]+")
# 什么样子算「一个证号」——用来判断空格两侧到底是两个号还是一个号里夹了空格。
_LIC_MARK = re.compile(r"(械注[进准]|械备|卫消|药准|械字)")


def _lic_tokens(raw):
    """把单据上的注册证值拆成一个个号（已删空白、已小写）。

    单据上的注册证经常是**复合值**，实测三种写法（out/ 下 911 行有证数据里 96 行是复合号）：
      - 斜杠：国械注进20173637237/国械注进20172177237（DMG Luxatemp）
      - 空格：国械注进20182170121 国械注进20163632246
      - 逗号：鲁械注准20182170360,鲁日械备20170014号
    lib_norm.split_license() 只拆「原注册证号：…」括号写法，不拆上面这些分隔符，所以整串原样传进来。

    空格要小心：`晋晋中械备 20250013`、`浙械注准 20252171279` 里的空格是**排版**，切开就成了两个
    都不成立的号。所以只有当空格两侧**都**像证号（够长且有「械注进/械注准/械备…」字样）时才拆。
    """
    if not raw:
        return []
    t = unicodedata.normalize("NFKC", str(raw)).lower().strip()
    out = []
    for part in _LIC_SEP.split(t):
        part = part.strip()
        if not part:
            continue
        toks = part.split()
        if len(toks) > 1 and all(len(x) >= 6 and _LIC_MARK.search(x) for x in toks):
            out.extend(toks)
        else:
            out.append(part)
    return [re.sub(r"\s+", "", x) for x in out if re.sub(r"\s+", "", x)]


def _lic_hit(lic_raw, alias_raw):
    """单据上的注册证值里，是否**恰有一个号**等于这个别名证号。

    必须是整号相等，不能做子串包含：不同注册体系（进/准/备）后半段数字长度相同，
    `2016217412` 做子串会命中 `20162174128`。整号比对把这类误判全堵掉。
    """
    if not alias_raw:
        return False
    want = _lickey(alias_raw)
    if not want:
        return False
    return any(_lickey(tok) == want for tok in _lic_tokens(lic_raw))


def load_aliases(path=ALIAS_PATH):
    """读取别名表。文件缺失时返回空表（降级为 raw_only，不抛异常）。"""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("aliases", [])


def truncation_evidence(raw, aliases=None):
    """判断原文是否疑似被窗口/列宽截断，返回 (bool, 说明)。"""
    t = _norm(raw)
    if not t:
        return False, None
    aliases = aliases if aliases is not None else load_aliases()
    for a in aliases:
        for m in a.get("matches", []):
            mn = _norm(m)
            if len(t) >= 8 and mn.startswith(t) and mn != t:
                return True, ("原文「%s」是「%s」的严格前缀，判定为印刷/列宽截断"
                              % (raw, a["canonical"]))
    last = t.split(" ")[-1] if t else ""
    for h in _SUFFIX_HINTS:
        if re.fullmatch(h + r"\.?", last):
            return True, "原文以公司后缀残片「%s」结尾且缺句点，疑似截断" % last
    return False, None


def resolve(raw, license_no=None, aliases=None):
    """补全生产厂家。返回 dict，字段见 out 初始化。

    method 取值与置信度：
      license_exact  0.99  注册证号命中，最可靠，可直接自动通过
      alias_exact    0.96  别名表精确命中
      prefix_fill    0.85  判定为截断后补全，须人工确认（B/C 级）
      raw_only       0.50  未命中，保留原文，须人工确认
      none           0.00  既无原文也无注册证
    """
    aliases = aliases if aliases is not None else load_aliases()
    raw = (raw or "").strip() or None
    lic = (license_no or "").strip() or None

    out = {
        "manufacturer_raw": raw,
        "manufacturer": None,
        "manufacturer_cn": None,
        "method": "none",
        "confidence": 0.0,
        "needs_review": True,
        "truncated": False,
        "note": None,
    }

    # ① 注册证精确命中：与印刷质量无关，最可靠
    # 用 _lic_hit 而不是 lic in [...]：单据上的证号常是复合值（斜杠/空格/逗号连写多个号），
    # 整串永远不等于表里任何一个号。实测 96 行复合号在修前全部漏到 raw_only。
    if lic:
        for a in aliases:
            if any(_lic_hit(lic, x) for x in a.get("license_nos", [])):
                out.update(
                    manufacturer=a["canonical"],
                    manufacturer_cn=a.get("canonical_cn"),
                    method="license_exact",
                    confidence=0.99,
                    needs_review=False,
                    note=None if raw else "单据未提供生产厂家，已按注册证号补全",
                )
                return out

    if not raw:
        out["note"] = "既无生产厂家原文也无注册证号，必须人工录入"
        return out

    n = _norm(raw)

    # ② 别名精确命中
    for a in aliases:
        for m in a.get("matches", []):
            if _norm(m) == n:
                out.update(manufacturer=a["canonical"],
                           manufacturer_cn=a.get("canonical_cn"),
                           method="alias_exact", confidence=0.96, needs_review=False)
                return out

    # ③ 前缀补全（截断）
    best, best_len = None, 0
    for a in aliases:
        for m in a.get("matches", []):
            mn = _norm(m)
            if len(n) >= 8 and mn.startswith(n) and mn != n and len(n) > best_len:
                best, best_len = a, len(n)
    if best:
        trunc, ev = truncation_evidence(raw, aliases)
        out.update(
            manufacturer=best["canonical"],
            manufacturer_cn=best.get("canonical_cn"),
            method="prefix_fill",
            confidence=0.85,
            needs_review=True,
            truncated=True,
            note="%s；已按「%s」补全，须人工确认" % (ev or "疑似截断", best["canonical"]),
        )
        return out

    # ④ 兜底：保留原文，交人工
    trunc, ev = truncation_evidence(raw, aliases)
    out.update(manufacturer=raw, method="raw_only", confidence=0.5,
               needs_review=True, truncated=bool(trunc),
               note=(ev + "；未在别名表中命中，保留原文，请补录 config/manufacturer_aliases.json")
               if ev else "未在别名表中命中，保留原文，请人工确认并补录别名表")
    return out


def suggest_alias_row(raw, license_no=None):
    """为 raw_only 的结果生成一条待补录的别名草稿（人工填 canonical 后粘回配置）。"""
    return {
        "canonical": "<请填写法定全称>",
        "canonical_cn": None,
        "country": None,
        "brands": [],
        "license_nos": [license_no] if license_no else [],
        "matches": [_norm(raw)] if raw else [],
        "source": "needs-review",
        "note": "由单据原文「%s」自动生成，需人工核准法定名称后启用" % (raw or ""),
    }
