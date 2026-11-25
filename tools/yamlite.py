# -*- coding: utf-8 -*-
"""极简 YAML 子集解析器（零依赖）。

为什么不用 PyYAML：本机两套解释器各缺一半能力——runtime python 3.12 有 openpyxl
但没 pyyaml，系统 python 3.14 有 pip 但没 openpyxl。template_mapping/*.yaml 只用
到「标量 / 列表 / 一层嵌套 / 行内列表」，写 90 行自解析比引依赖可靠，也方便离线部署。

支持的子集（够用即可，多一个特性就多一处出错）：
    key: value            -> {"key": "value"}
    key:                  -> 起始一个嵌套块（下一行缩进更深）
      - src: A            -> 列表项，项内可继续 key: value
        dst: [x, y]       -> 行内列表
    # 注释                -> 丢弃（只认行首或空白后的 #）

不支持并会显式报错：Tab 缩进、多行字符串、锚点、流式 map。
解析失败一律抛异常，不静默返回半截数据——导入表错一格就是一整批错账。
"""

import json
import re
import sys

__all__ = ["load", "loads"]

_INLINE_LIST = re.compile(r"^\[(.*)\]$")
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _scalar(v):
    v = v.strip()
    if not v:
        return ""
    m = _INLINE_LIST.match(v)
    if m:
        inner = m.group(1).strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    v = re.sub(r"\s+#.*$", "", v)  # 行尾注释；前面必须有空白，避免误伤 URL 里的 #
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if v in ("null", "Null", "~"):
        return None
    if _INT.match(v):
        return int(v)
    if _FLOAT.match(v):
        return float(v)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _bare(v):
    """去掉行尾注释，只留真正的值部分（用于判断「有没有值」）。"""
    return re.sub(r"\s+#.*$", "", v)


def loads(text):
    rows = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body = raw.lstrip(" ")
        if body.startswith("\t"):
            raise ValueError("检测到 Tab 缩进，本解析器只支持空格：%r" % raw)
        rows.append((len(raw) - len(body), body.rstrip()))
    if not rows:
        return {}

    pos = [0]

    def parse_dict(indent, node=None):
        node = {} if node is None else node
        while pos[0] < len(rows):
            ind, line = rows[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError("缩进比预期更深（第 %d 条）：%r" % (pos[0] + 1, line))
            if line.startswith("- "):
                break
            pos[0] += 1
            k, _, v = line.partition(":")
            k = k.strip()
            # 行尾注释要与「值」区分开：`key:      # 说明` 是嵌套块的起始，
            # 不是字符串值 "# 说明"。直接 v.strip() 判断会把后者判成有值，
            # 于是下一个更深缩进的行就报「缩进比预期更深」。
            if _bare(v).strip():
                node[k] = _scalar(v)
            else:
                if pos[0] < len(rows) and rows[pos[0]][0] > ind:
                    node[k] = parse_node(rows[pos[0]][0])
                else:
                    node[k] = None
        return node

    def parse_list(indent):
        items = []
        while pos[0] < len(rows):
            ind, line = rows[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError("缩进比预期更深（第 %d 条）：%r" % (pos[0] + 1, line))
            if not line.startswith("- "):
                break
            pos[0] += 1
            rest = line[2:].strip()
            if ":" not in rest:
                items.append(_scalar(rest))
                continue
            k, _, v = rest.partition(":")
            item = {}
            if _bare(v).strip():
                item[k.strip()] = _scalar(v)
            elif pos[0] < len(rows) and rows[pos[0]][0] > ind:
                item[k.strip()] = parse_node(rows[pos[0]][0])
            else:
                item[k.strip()] = None
            # 同一列表项其余的 key: value（缩进更深、且不是新列表项）
            if pos[0] < len(rows) and rows[pos[0]][0] > ind and not rows[pos[0]][1].startswith("- "):
                parse_dict(rows[pos[0]][0], item)
            items.append(item)
        return items

    def parse_node(indent):
        return parse_list(indent) if rows[pos[0]][1].startswith("- ") else parse_dict(indent)

    out = parse_node(rows[0][0])
    if pos[0] != len(rows):
        raise ValueError("解析未消费完，剩余：%r" % (rows[pos[0]:][:3],))
    return out


def load(path):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())


if __name__ == "__main__":
    sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print(json.dumps(load(sys.argv[1]), ensure_ascii=False, indent=1))
