# -*- coding: utf-8 -*-
"""
build_import.py — 把抽取出的 JSON 生成两阶段导入文件
阶段一：产品库建档表（28 列，物品编号留空由系统分配）
阶段二：入库表（15 列，物品编号必须来自产品库导出映射）
用法：
  python build_import.py <extracted.json> <outBase> [library_export.xlsx]
"""
import json, sys, os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_norm import clean, norm_date, split_license, split_package, norm_brand, unit_ok, money2
from lib_mfr import resolve as resolve_mfr
import expiry as expiry_mod
import manual_fixes
import openpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

TEMPLATE = os.path.join(ROOT, "docs", "fixtures", "in_template.xls")
PRODUCT_TEMPLATE = os.path.join(ROOT, "docs", "fixtures", "product_template_v2.xlsx")

HEADERS = ["物品名称","物品编号","规格","品牌","采购单位","采购数量","采购单价（含税）","税率","内部编号",
           "一级分类","二级分类","三级分类","批次号","生产日期","有效期至"]

# 一级分类字面值只能取美亚分类树实测值（7 个，全部带「类」后缀）。
# 实测来源：2026-11-19 产品库【批量导出】297 条记录，一级分类去重后恰好这 7 个字面值。
ALLOWED_L1 = {"药品类", "种植类", "正畸类", "补牙类", "修复类", "一次性耗材", "牙周类"}

# 美亚对建档表的个别列有**字面长度上限**，超一个字整行被拒（实测 2026-11-25：
# 「1.生产厂家最多支持输入50个字」，DMG Luxatemp 那条 53 字，整行退回）。
# 这里只登记**实测过**的上限。没有证据的列一律不动：凭猜一个数字去截断
# 物品名称/规格/品牌，会把本来能过的数据改坏，而且改完没人看得出来。
MAX_LEN = {"生产厂家": 50}
ELLIPSIS = "\u2026"


def fit_cell(header, value):
    """按 MAX_LEN 截断单元格文本。不该截的原值原样返回（含 None/数字）。

    截断点优先落在空格或标点处，别把「GmbH」切成「Gmb」；找不到像样的边界
    （边界落在前 60% 以内就当没有）才硬切。末尾补「…」——台账上看到半截
    厂家名，必须一眼能看出是长度限制导致的，而不是字打错了。
    """
    n = MAX_LEN.get(header)
    if not n or value is None:
        return value
    s = str(value)
    if len(s) <= n:
        return value  # 没超长的原样返回（数字仍是数字，别把 12345 变成 "12345"）
    head = s[: n - 1]
    cut = max(head.rfind(ch) for ch in " \u3000\u3001\uff0c,;\uff1b/|\uff08(\u00b7-\uff0d")
    if cut >= n * 0.6:
        head = head[:cut]
    head = head.rstrip(" \u3000\u3001\uff0c,;\uff1b/|\uff08(\u00b7-\uff0d")
    return (head + ELLIPSIS)[:n]

# 关键词 → 一级分类。规则不是拍脑袋，是从这 297 条真实记录反推：
#   车针 29 条全部「修复类」；含「锉」20 条「修复类」；牙胶 10 条「修复类」；
#   树脂 24 条「补牙类」；弓丝 16 条「正畸类」；托槽/橡皮圈「正畸类」；
#   L/M/S/儿童托盘 4 条「一次性耗材」（不是修复类）；
#   牙线/排龈膏/派丽奥/工作尖/美白剂 → 「牙周类」。
# 顺序有意义：先药品，再专科耗材，最后泛用耗材，避免「片」「胶囊」这类宽词误吞。
CATEGORY_RULES = [
    (r"注射液|胶囊|缓释片|片剂|阿替卡因|利多卡因|肾上腺素|布洛芬|头孢|替硝|甲硝唑|阿莫西林|地塞米松|葡萄糖|氯化钠|阿托品|尼可刹米|麻药|麻醉|注射针", "药品类"),
    # 「骨粉/骨填充/缝合线」在真实产品库里都归「种植类」（实测产品库快照：
    # 0.5cc骨粉 / 0.25cc骨粉 / 缝合线 均为一等级分类「种植类」），牙e在线的一月订单里
    # 盖氏 Bio-Oss骨填充材料、金环可吸收性外科缝线因此曾被判「一级分类未推断出」。
    # 「手术刀片」在真实产品库是「种植类」（实测：手术刀片11/12/15号 共 4 条全是种植类）。
    # 2026-02 牙e 订单 SC260226092949015050 的「联辉 手术刀片（T10A碳钢）」因此曾判未推断出。
    (r"种植|implant|骨粉|骨填充|植骨|人工骨|缝合|缝线|手术刀|刀片", "种植类"),
    (r"橡皮圈|正畸|托槽|弓丝|牵引|颊面管|结扎|分牙|保持器|牵引钩", "正畸类"),
    # 「根管/氢氧化钙/失活」必须排在「消毒」之前：氢氧化钙根管消毒剂在真实产品库里
    # 是「修复类」，被后面的一次性耗材「消毒」规则先吞掉就归错类了（实测踩过）。
    # 「生物陶瓷型材料」「根管充填材料」也是修复类（牙e在线的捷科顿 SP生物陶瓷型材料实测踩过：
    # 三个规则都不命中，整单被阻断）。必须排在「正畸」之后——「陶瓷托槽」要先被正畸的「托槽」抓走。
    # 「成形片/豆瓣片」「明胶海绵」「代型材料/压膜片」在真实产品库都是「修复类」
    # （实测：成形片中号/大号 10245/10246、豆瓣片 10242、明胶海绵 10234 全为修复类）。
    # 2026-02 牙e 订单里的「牙医公园 M10豆瓣成形片套装」「金光 代型材料/真空成型片/压膜片」
    # 「湃生/艾瑞金 胶原蛋白海绵」都因此曾判未推断出，整单被阻断。
    (r"根管|氢氧化钙|MTA|拔髓|纸尖|失活|樟脑|甲醛|丁香油|碘甘油|牙胶|糊剂|生物陶瓷|陶瓷型材料|充填材料|暂封|"
     r"成形片|成型片|豆瓣|代型|压膜|胶原|海绵", "修复类"),
    (r"牙线|排龈|排齦|工作尖|派丽奥|美白剂|洁牙|龈下", "牙周类"),
    # 「窝沟封闭」在真实产品库是「补牙类」（实测：窝沟封闭（皓齿）→ 补牙类）。
    # 「封闭」这个宽词放这里安全：种植体的「封闭螺丝」会先被上面的「种植」抓走。
    (r"树脂|粘接|粘接剂|酸蚀|充填|脱敏|氟保护|窝沟|封闭", "补牙类"),
    # 「笔筒/定制」是真出现过的赠品（牙e在线 定制笔筒），归泛用耗材，不让它整单阻断。
    # 「含漱液/漱口水」「口镜」也都是泛用耗材（实测：朗力生物1%次氯酸钠消毒液 → 一次性耗材）。
    (r"托盘|消毒|灭菌|酒精|碘伏|双氧水|过氧化氢|多酶|棉|手套|口罩|帽子|注射器|隔离套|吸唾管|湿巾|包装袋|器械盘|洗手液|放置盒|牙套盒|笔筒|定制|"
     r"漱|口镜", "一次性耗材"),
    # 「纤维桩」在真实产品库是「修复类」（实测：纤维桩1.2 / 纤维桩1.4 / 纤维桩辅助1.0）。
    # 「水门汀」必须挂在最后这条修复类上，不能往前挪：松风 BeautiCem 树脂水门汀、
    # 齿科粘接用树脂水门汀在真实库里都是「补牙类」，靠上面的「树脂」「粘接」先命中；
    # 挂到前面会把这两条已有正确答案改错。实测库里「聚羧酸锌」「丁香油水门汀」都是修复类，
    # 而科尔「聚羧酸锌水门汀/无酸水门汀」到 2026-11-24 仍推不出、整单被 blocked_build 挡住
    # （美亚报「一级分类不能为空」）。
    (r"车针|钻头|钻针|磨头|锉|抛光|拔牙|印模|硅橡胶|藻酸盐|自凝|蜡|咬合|美白|冠|桥|义齿|纤维桩|桩核|水门汀", "修复类"),
]

# 实测：产品库 297 条记录「是否高值耗材」全部为「否」，含全部 37 条种植类。
# 本租户并未启用高值耗材标识，因此默认填「否」并告警，绝不按品类擅自标「是」
# （高值耗材会触发唯一码管理，标错会改变入库流程）。
HIGH_VALUE_L1 = set()


def infer_category(name, spec):
    """按关键词把物品归到美亚一级分类；未命中或不合法返回 None，由人工填。

    二级/三级分类一律留空：实测产品库 297 条记录的二级、三级分类 100% 为空，
    即当前租户并不使用这两级分类，臆造一个分类名只会增加导入被拒的风险。
    """
    text = f"{name or ''} {spec or ''}"
    for pat, l1 in CATEGORY_RULES:
        if re.search(pat, text, re.I):
            return (l1 if l1 in ALLOWED_L1 else None), "", ""
    return None, "", ""


# 赠品里会出现根本不是耗材/药品的东西（用户实测：商家送过食品、水果）。
# 这类行不能入库：进了库存就成了「药品/耗材」，出库时永远平不掉，还会污染
# 分类统计。规则只收「明确是吃的喝的」这类词，宁可漏也不误伤牙科物品。
# 关键保护：命中本表后还要再确认「分类规则也没命中」——否则「葡萄糖」会因为
# 含「葡萄」被当成水果扔掉（它有「注射液」等词，会先被分类规则抓走）。
NON_MEDICAL_RULES = (
    r"水果|苹果|香蕉|橙子|桔子|橘子|西瓜|葡萄|哈密瓜|草莓|芒果|猕猴桃|荔枝|柚子|柠檬",
    r"零食|饼干|糖果|巧克力|薯片|坚果|瓜子|辣条|魔芋|方便面|面包|蛋糕|月饼|粽子|火腿肠|香肠",
    r"饮料|矿泉水|纯净水|可乐|雪碧|果汁|牛奶|酸奶|咖啡|茶叶|大米|食用油|酱油|食醋",
)

# 「拆开单位」：产品库里这类物品的采购单位一律是整包（袋/盒/双/箱），
# 数量按整包数填。单据上若是散装只数，直接把数填进去就会把 100 支当成 100 袋。
PIECE_UNITS = {"只", "支", "个", "张", "粒", "条", "根", "片", "枚", "毫升", "克"}


def is_non_medical(name, spec):
    """判断这一行是不是「非耗材/非药品的赠品」。命中分类规则的一律不算。"""
    text = f"{name or ''} {spec or ''}"
    if infer_category(name, spec)[0]:
        return False
    return any(re.search(p, text) for p in NON_MEDICAL_RULES)

def product_key(line):
    main, _ = split_license(line.get("license_no") or line.get("license_no_raw"))
    return identity_key(main, clean(line.get("spec")), norm_brand(line.get("brand")) or "")


def identity_key(license_main, spec, brand):
    """建档身份 = 注册证主号 + 归一化规格 + 归一化品牌，与 product_key 一致。"""
    return (license_main or "", (spec or "").strip().lower(), norm_brand(brand) or "")


def _norm_name(s):
    """物品名归一：去空白（含全角空格）、转小写。

    只用于「同一身份多条档案时按名字消歧」的比对，绝不改写写进表里的值。
    历史快照里有 44 组同身份多档案（啄木鸟工作尖、大/中/小号托盘、棉棒大小号），
    它们物品名不同而注册证与规格都为空，单据上的物品名是唯一能区分的信息。
    """
    return re.sub(r"[\s\u3000]+", "", (s or "")).lower()


def load_library(path):
    """读取美亚产品库【批量导出】的结果。

    返回 (内部编号->物品编号, 身份->物品编号, 已占用内部编号集合)。

    为什么要两个索引：真实流程是「单据先抽出来 → 建档 → 系统分配物品编号」，
    脚本不可能预先知道内部编号；只有回头【批量导出】后才知道。所以优先按身份
    （注册证主号+规格+品牌）反查物品编号，内部编号只是已建库条目命中时的便捷键。
    """
    mapping, by_ident, used = {}, {}, set()  # by_ident: 身份 -> [(物品编号, 内部编号), ...]
    if not path or not os.path.exists(path):
        return mapping, by_ident, used
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    # 第 2 行是表头（第 1 行是合并的标题/须知）
    hdr = [str(v).strip() if v is not None else "" for v in rows[1]]
    try:
        idx = {k: hdr.index(k) for k in
               ("物品编号", "内部编号", "规格", "品牌", "批准文号", "生产厂家")}
    except ValueError:
        wb.close()
        return mapping, by_ident, used
    for r in rows[2:]:
        if not r or len(r) <= max(idx.values()):
            continue
        item_no = str(r[idx["物品编号"]]).strip() if r[idx["物品编号"]] is not None else ""
        inner = str(r[idx["内部编号"]]).strip() if r[idx["内部编号"]] is not None else ""
        if inner:
            used.add(inner)
        if not item_no:
            continue
        if inner:
            mapping[inner] = item_no
        lic, _ = split_license(r[idx["批准文号"]])
        k = identity_key(lic, r[idx["规格"]], r[idx["品牌"]] or r[idx["生产厂家"]])
        # 同时记下库内该条目的内部编号：身份命中说明这个物品已经建过档，
        # 入库表就应当沿用库里的内部编号，不能另发一个新号造成同一物品两个内部编号。
        # 同一身份可能已被重复建档（产品库允许同名同规格多条），全部收下，
        # 由 build 判断是否歧义；歧义时不许自动取号，否则会入错物品。
        by_ident.setdefault(k, []).append((item_no, inner))
    wb.close()
    return mapping, by_ident, used


ALLOC_BASE = 90001  # 与既有 10001–10012 / 70001–70012 / 60038 完全错开


def alloc_inner(used, start=ALLOC_BASE):
    n = start
    while str(n) in used:
        n += 1
    used.add(str(n))
    return str(n), n + 1


# 人工指定取号表的默认位置：界面「多条档案待选」选中的就是写在这里。
# 以前只有函数没人调用（run.py 与 pipeline 都传 override_path=None），
# 于是一个「存在但永远不生效」的口子 —— 人以为选了，下一批照样判歧义。
DEFAULT_OVERRIDES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "inbound_overrides.json")


def load_overrides(path):
    """人工指定取号表：身份 -> 物品编号。歧义行必须走这里，程序不得自行猜测。

    path 传 None 时读 DEFAULT_OVERRIDES（config/inbound_overrides.json），
    不是返回空字典：这是运行期唯一需要人工拍板的映射，跳过它等于让人白选。
    单测要干净结果时请显式传一个不存在的路径。
    """
    path = DEFAULT_OVERRIDES if path is None else path
    if not path or not os.path.exists(path):
        return {}
    raw = json.load(open(path, encoding="utf-8"))
    return {identity_key(k.split("|")[0], k.split("|")[1] if len(k.split("|")) > 1 else "",
                         k.split("|")[2] if len(k.split("|")) > 2 else ""): str(v)
            for k, v in raw.items()}


def add_override(ident, item_no, path=None):
    """记下「这个身份就用这个物品编号」，返回写入后的条数。

    ident 是三段 "注册证|规格|品牌"（identity_key 的原样三元组用 | 拼起来）——
    用字符串当键而不是哈希，是为了人能直接打开 config/inbound_overrides.json
    看懂自己选了哪一条（这份文件是给人工核对的，不是缓存）。
    """
    path = DEFAULT_OVERRIDES if path is None else path
    j = {}
    if os.path.exists(path):
        try:
            j = json.load(open(path, encoding="utf-8")) or {}
        except (ValueError, OSError):
            j = {}
    j[str(ident)] = str(item_no)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return len(j)


def _unpack_lib(lib):
    """把 lib 拆成四个索引，同时兼容 3 元组与 4 元组。

    前三个（内部编号->物品编号 / 身份->条目 / 已占用内部编号）是原始契约，
    老调用方（单测、run.py 的 xlsx 路径）只给这三个也照样能跑；第 4 项是
    本地库才有的补充索引：

        cats     身份 -> 一级分类（库里已建档时的分类，用来救关键词规则没命中的物品）
        pending  身份 -> 内部编号（库里等着建档的物品，重新出表必须沿用同一个号）
    """
    parts = tuple(lib or ())
    mapping = parts[0] if len(parts) > 0 else {}
    by_ident = parts[1] if len(parts) > 1 else {}
    used = parts[2] if len(parts) > 2 else set()
    idx = parts[3] if len(parts) > 3 else {}
    return mapping, by_ident, used, (idx or {})


def build(extracted_path, library_path=None, inner_base=ALLOC_BASE, override_path=None,
          extra_used=None, lib=None, fixes=None, alloc=None):
    """生成入库表行 + 待建档产品。

    alloc 参数：整批共用的发号状态 {"used": set, "cursor": int}。一批多单必须
    传同一个：build() 自己发号时游标从 inner_base 起，每份单据各发一遍就会
    在同一个批次里发出重复的内部编号，美亚导入时报「内部编号已存在」。

    lib 参数：直接给一个 (mapping, by_ident, used[, idx]) 元组，跳过读 xlsx。
    为什么要这个口子：本地库改成 SQLite 以后（用户 2026-11-23 决定不再读美亚），
    产品库不再是一张导出的 xlsx，而是 data/local.db。emit.py 从库里现算这三个
    索引传进来，就不必为了调一次 build 先导出一张临时表再删掉。
    """
    data = json.load(open(extracted_path, encoding="utf-8"))
    doc = data["document"]
    # 人工处置（弹窗里选的一级分类、补的拆包数量、「这批先不入」）。
    # 在**这里**套用而不是事后改表：一级分类决定整行能不能交出去，
    # 事后改表会出现「表已经出好了，人又改了分类」两份不一致的东西。
    # fixes 传 False 表示显式不要（单测要可复现的干净结果）。
    doc_no_key = str(doc.get("doc_no") or "").strip()
    if fixes is None:
        fixes = manual_fixes.index()
    elif fixes is False:
        # 显式「不要任何人工处置」：单测要的是可复现的结果，不能被
        # config/manual_fixes.json 里的历史记录改变。原先这里只处理 None，
        # 传 False 会一路传成布尔值，行循环里 .get() 直接抛 AttributeError。
        fixes = {}
    mapping, by_ident, used, idx = _unpack_lib(
        lib if lib is not None else load_library(library_path))
    # 批次内多份单据必须共用一段内部编号空间：分开跑最容易出的事故就是两份单据
    # 各发一个 90001，导入后产品库出现两条撞号档案。
    # 所以发号状态放在 alloc 里由调用方在整批之间传递；不传就退化成「本单自己一段」，
    # 与老行为一致（单测只跑一份单据时够用）。
    if alloc is None:
        alloc = {"used": set(used) | set(extra_used or ()), "cursor": inner_base}
    else:
        alloc["used"] |= set(extra_used or ())
        if alloc.get("cursor") is None:
            alloc["cursor"] = inner_base
    used = alloc["used"]
    # 本批已经把某个内部编号认给了哪件物品。库里的 pending 号本来就在 used 里
    # （它就是那件物品自己的号），所以「在 used 里」不能当成「被别人的占了」；
    # 真正要防的是库里有重复号（历史脏数据）时两件不同物品争同一个号。
    claimed = alloc.setdefault("claimed", {})
    overrides = load_overrides(override_path)
    rows, warnings, products = [], [], {}
    # 游标从整批共用的发号状态里接着走：一批多单各自从 inner_base 起，
    # 第二份单据就会把第一份刚发出去的号再发一遍。
    cursor = int(alloc.get("cursor") or inner_base)

    for ln in data["lines"]:
        name = clean(ln.get("product_name"))
        spec = clean(ln.get("spec"))
        unit = clean(ln.get("unit"))
        qty = float(ln.get("quantity") or 0)
        price = money2(ln.get("unit_price"))
        amount = money2(ln.get("amount"))
        lot = clean(ln.get("lot_no"))
        mfg_raw = ln.get("mfg_date") or ln.get("mfg_date_raw")
        exp_raw = ln.get("expiry_date") or ln.get("expiry_date_raw")
        mfg, mfg_prec = norm_date(mfg_raw)
        exp, _ = norm_date(exp_raw)
        lic_main, lic_hist = split_license(ln.get("license_no") or ln.get("license_no_raw"))
        lt_raw = clean(ln.get("manufacturer") or ln.get("manufacturer_raw"))
        mfr = resolve_mfr(lt_raw, lic_main)
        pkg = split_package(spec)
        brand = norm_brand(ln.get("brand")) or norm_brand(mfr["manufacturer"])
        brand_from_mfr = not clean(ln.get("brand"))
        l1, l2, l3 = infer_category(name, spec)

        # 人工处置优先于关键词规则，也优先于「这批先不入」之前的一切判定。
        # 顺序是有意的：人明确说过的事，程序不该再猜一遍然后给出相反结论。
        fx = manual_fixes.find(doc_no_key, name, spec, idx=fixes) or {}
        if fx.get("l1"):
            if (fx.get("l1") or "") not in ALLOWED_L1:
                # 记录里存着一个不是分类树字面值的值（手改过 json？），宁可不用
                # 也不能把它写进表里——美亚对非法分类整行拒绝，而且报错只说「分类不对」。
                warnings.append(f"[人工分类无效] {name} 记录的「{fx.get('l1')}」不是分类树字面值，已忽略")
            elif fx.get("l1") != l1:
                warnings.append(f"[人工分类] {name} 一级分类按人工选择填「{fx.get('l1')}」"
                                f"（关键词规则{'推断为「%s」' % l1 if l1 else '没推断出'}）")
                l1, l2, l3 = fx.get("l1"), "", ""

        # 非耗材/非药品的赠品（食品、水果等）直接跳过，不进新建档表也不进入库表。
        # 为什么是「跳过」而不是「阻断整单」：同一张单里常混着正常耗材，
        # 为了一个赠品把整单卡住，人会去改数据或手工入，反而更容易出错。
        if is_non_medical(name, spec):
            warnings.append(f"[非医疗赠品] {name} 不是耗材/药品（食品、饮料、水果等），"
                            f"已跳过不入库；若确实要入，请先人工建档再手工入")
            continue

        # 「这批先不入」是人工判定，和赠品跳过同一类结果：不进入库表、不进建档表，
        # 但要在警告里留一行，否则结果表上看不出这行去哪了，人以为漏读了。
        # 与 reject（禁止入库，永久）不同：这里只对这张单的这批货生效。
        if fx.get("skip"):
            warnings.append(f"[人工跳过] {name} {spec} 按人工选择这批不入库（记录在 config/manual_fixes.json）")
            continue

        if not unit_ok(unit):
            warnings.append(f"[单位] {name} 单位「{unit}」不在白名单，需人工确认")
        elif unit in PIECE_UNITS:
            # 整包口径：吸唾管、手套这类物品按整包（袋/盒/双/箱）出入库，
            # 一只/一支是拆开单位。单据上若写的是散装数，直接当包数填会把
            # 100 支记成 100 袋。这里只提醒不改数——改数需要知道一包多少只，
            # 而那个数在各家规格里写法不一样，猜错比不改更糟。
            warnings.append(f"[整包口径] {name} 采购单位是「{unit}」（拆开单位），"
                            f"产品库同类物品按整包入库（袋/盒/双/箱）。"
                            f"若单据上的数量是散装只数，请改成整包单位与包数后再入库")
        if mfg_prec == "month":
            warnings.append(f"[日期精度] {name} 生产日期仅到月（{mfg_raw}），已补 -01 为 {mfg}，需人工确认")
        if amount is not None and price is not None and qty:
            diff = abs(round(price * qty, 2) - amount)
            if diff > 0.01:
                ratio = diff / amount if amount else 1.0
                warnings.append(
                    f"[金额不平] {name} 单价x数量={round(price*qty,2)} 与单据金额 {amount:.2f} 差 {diff:.2f}"
                    f"（{ratio*100:.2f}%）。按 §15 第 4 条禁止自动反算，"
                    f"已原样保留单据单价 {price} 与金额 {amount}，需人工确认是折扣、抹零还是录错")
        if not qty or qty <= 0:
            # 数量 0 的行美亚会整行拒绝（实测 MS260127163114015050：
            # 平台原始 xls 里采购数量就是 0，是赠品/待定行；导入后表格只剩「暂无数据」占位，
            # 上层还把 rows 数成 1、记成 committed —— 必须在这里就拦住。
            warnings.append(f"[数量为0] {name} 采购数量是 {qty:g}，美亚会整行拒绝，禁止入库")
        # 批号缺失不再阻断（用户 2026-11-23 定下的规则：有有效期、无批号可入库）。
        # 仍然要提示：批号是追溯链的一环，能补就该补。
        if not lot:
            warnings.append(f"[缺批号] {name} 无批号（不阻断入库，但建议补上以便追溯）")
        # 有效期才是硬门槛：缺失或不足一年，run.py 会拦下来等人工审批。
        _ex = expiry_mod.check(exp, doc.get("doc_date"))
        if not _ex["ok"]:
            warnings.append(f"[效期不足] {name} {_ex['msg']}")
        if not pkg:
            warnings.append(f"[拆包未解析] {name} 规格「{spec}」未含包装信息，拆包数量按 1、【拆包单位】= 采购单位填（默认值），需人工确认")
        if brand_from_mfr:
            warnings.append(f"[品牌兜底] {name} 无独立品牌字段，使用生产厂家「{brand}」并标记 brand_from_mfr")
        if mfr["method"] == "prefix_fill":
            warnings.append(f"[厂家截断补全] {name} 原文「{mfr['manufacturer_raw']}」疑似列宽截断，"
                            f"已按别名表补全为「{mfr['manufacturer']}」（method=prefix_fill, 置信度 0.85），须人工确认")
        elif mfr["method"] == "raw_only":
            warnings.append(f"[厂家未识别] {name} 原文「{mfr['manufacturer_raw']}」未在别名表命中，"
                            f"已原样保留并置 needs_review；请补录 config/manufacturer_aliases.json")
        elif mfr["method"] == "license_exact" and lt_raw:
            warnings.append(f"[厂家按注册证补全] {name} 原文「{lt_raw}」→「{mfr['manufacturer']}」（method=license_exact, 置信度 0.99），与注册证 {lic_main} 一致")
        _mfr_len = len(mfr["manufacturer"] or "")
        if _mfr_len > MAX_LEN["生产厂家"]:
            warnings.append(f"[厂家超长] {name} 生产厂家「{mfr['manufacturer']}」{_mfr_len} 字，"
                            f"美亚「生产厂家」最多支持 {MAX_LEN['生产厂家']} 个字，超了整行会被拒；"
                            f"出建档表时已截断，本地库仍保留全称（想根本解决：把该厂的短法定名补进 "
                            f"config/manufacturer_aliases.json）")
        if l1 is None:
            warnings.append(f"[分类未命中] {name} 未匹配到分类词规则，一级分类必须人工填写（红字必填）")
        elif not fx.get("l1"):
            # 只有「规则推断出来的」才写这句。人工选过的那条上面已经打过 [人工分类]，
            # 再补一句「由关键词规则推断」就是自相矛盾 —— 人正要确认自己选的有没生效。
            warnings.append(f"[推断] {name} 一级分类「{l1}」由关键词规则推断，非单据字段；二级/三级分类留空（实测产品库 297 条全空）")

        warnings.append(f"[默认值] {name} 税率按 13 填；最小库存/最大库存/有效期预警一律留空"
                        f"（实测产品库 297 条：最小库存与有效期预警 100% 为空，最大库存不填也不阻断导入）")

        # 拆包数量/拆包单位是建档表红字必填；解析不到时按 1 + 采购单位兜底
        pack_qty = pkg[0] if pkg else 1
        pack_unit = pkg[1] if pkg else unit
        # 人工补的拆包优先于规格里解析出来的：规格解析只认「5包/袋」这类写法，
        # 写「5×20支」或压根没写时要靠人。缺它建档表整批过不了（红字必填）。
        if fx.get("pack_qty"):
            pack_qty = fx.get("pack_qty")
            warnings.append(f"[人工拆包] {name} 拆包数量按人工填写 {pack_qty}")
        if fx.get("pack_unit"):
            pack_unit = fx.get("pack_unit")

        key = product_key(ln)
        if key not in products:
            # 库里等着建档的物品必须沿用上次发出去的内部编号：重新发一个新号，
            # 用户上一批的建档表里是 90005、这一批变成 90012，两份表都导进美亚
            # 就是同一物品两条档案（之后按身份查就成了「歧义」，整条链要人工介入）。
            ident_new = identity_key(lic_main, spec, brand)
            pend_inner = str((idx.get("pending") or {}).get(ident_new) or "")
            if pend_inner and claimed.get(pend_inner, ident_new) == ident_new:
                code = pend_inner
                used.add(code)
                if code.isdigit() and cursor <= int(code):
                    cursor = int(code) + 1
            else:
                code, cursor = alloc_inner(used, cursor)
            claimed[code] = ident_new
            products[key] = {
                "内部编号": code, "物品名称": name, "一级分类": l1 or "", "二级分类": "", "三级分类": "",
                "规格": spec, "采购单位": unit, "拆包数量": pack_qty, "拆包单位": pack_unit,
                "型号": spec, "剂型": clean(ln.get("dosage_form")) or "", "品牌": brand or "",
                # 这里**存全称**（本地库要留证据），超长只在实际写 xlsx 时截断。
                "条形码": "", "生产厂家": mfr["manufacturer"] or "",
                "一级供应商": doc.get("supplier_raw", ""),
                "二级供应商": "", "最小库存": None, "最大库存": None, "有效期预警": None,
                "采购价": price, "销售价": "", "批准文号": lic_main or "", "药品通用名": "",
                "药品本位码": "", "是否高值耗材": "是" if l1 in HIGH_VALUE_L1 else "否",
                "国家医保编码": clean(ln.get("insurance_code")) or "", "货架信息": "",
                "备注": (f"厂家原文「{mfr['manufacturer_raw']}」；补全 {mfr['method']}"
                         if mfr["manufacturer_raw"] and mfr["method"] != "alias_exact" else ""),
            }
        code = products[key]["内部编号"]
        if l1 is None:
            # 关键词规则没命中时先回落到本地库里已有的分类，别急着让人填。
            # 实测 396 条物品里 76 条规则推不出，其中 75 条库里就有正确分类
            # （石膏类、轻体、楔子、调拌纸、机扩针……）—— 一律要求人工填，
            # 等于把 75 个已经有答案的问题重新问一遍，人会在弹窗上乱点。
            lib_l1 = (idx.get("cats") or {}).get(identity_key(lic_main, spec, brand)) or ""
            if lib_l1 in ALLOWED_L1:
                l1, l2, l3 = lib_l1, "", ""
                products[key]["一级分类"] = lib_l1
        # 取号优先级：内部编号命中 → 身份（注册证主号+规格+品牌）命中 → 空（阻断入库）
        item_no = mapping.get(code, "")
        hit_by = "内部编号" if item_no else ""
        if not item_no:
            # 人工指定优先：歧义行必须由人或审核台显式指定，不能由程序猜
            ident = identity_key(lic_main, spec, brand)
            if ident in overrides:
                item_no = str(overrides[ident])
                hit_by = "人工指定"
                # 沿用该物品编号在库内的内部编号，保证入库单与产品库同号
                lib_inner = ""
                for n, i in (by_ident.get(ident) or []):
                    if n == item_no and i:
                        lib_inner = i
                        break
                if lib_inner:
                    products[key]["内部编号"] = lib_inner
                    code = lib_inner
                warnings.append(f"[取号] {name} {spec} → 物品编号 {item_no}"
                                f"（人工指定{('，内部编号沿用 ' + lib_inner) if lib_inner else ''}，原为歧义）")
                products[key]["_skip"] = True   # 已建档：不得再进新建档表，否则整批被拒
                rows.append([name, item_no, spec, brand or "", unit, qty, price, 13, code,
                             l1 or "", l2 or "", l3 or "", lot, mfg, exp])
                continue
        is_amb = False
        if not item_no:
            ident = identity_key(lic_main, spec, brand)
            hits = by_ident.get(ident) or []
            if len(hits) > 1:
                # 先按物品名消歧：库里有好几条同身份档案时，多数时候物品名不同
                # （啄木鸟 P1/P3/P33/P95/B7R 工作尖、大/中/小号托盘、大小棉棒），
                # 单据上写着名字，这就是唯一可靠的区分信息。
                # 只在「恰好一条名字对上」时采用；对上两条以上仍然判歧义——
                # 同名不同物品编号（阿莫西林胶囊 10333/10348）程序无从判断该入哪条。
                names = (idx.get("names") or {}).get(ident) or {}
                same = [h for h in hits
                        if _norm_name(names.get(h[0]) or "") == _norm_name(name)]
                if len(same) == 1:
                    warnings.append(
                        f"[消歧] {name} 在产品库中有 {len(hits)} 条同身份档案，"
                        f"按物品名唯一命中 {same[0][0]}")
                    hits = same
            if len(hits) == 1 and not (ident[0] or ident[1]):
                # 身份只剩品牌（注册证与规格都是空）：库里一个供应商有几十条货，
                # 仅凭品牌命中等于断言「就是它」，会静默把货入到别的物品上，
                # 而且入库表看起来完全正常。唯一能确认的是物品名也对上。
                # 实测快照里这类键 43 个（其中 15 个还挂着多条档案）。
                names = (idx.get("names") or {}).get(ident) or {}
                if _norm_name(names.get(hits[0][0]) or "") != _norm_name(name):
                    hits = []
            if len(hits) == 1:
                item_no, lib_inner = hits[0]
                hit_by = "身份(注册证+规格+品牌)"
                if lib_inner and lib_inner != code:
                    # 已建档物品：改用库里的内部编号，保持与产品库一致
                    products[key]["内部编号"] = lib_inner
                    code = lib_inner
                # 身份命中 = 产品库已有这条档案（只是内部编号不是本次发的号）。
                # 绝不能进新建档表：美亚会整批拒绝（实测牙e SC260105135151015050，
                # 麦迪康手套已存在 10367，整批 successCount=1 failCount=1，
                # 同批的新吸唾管也没建成）。
                # 注意是 bool(item_no)：本地库里 status=pending 的物品（出过建档表但
                # 还没导进美亚）身份能命中，可美亚没给它物品编号。这种必须继续留在
                # 建档表里，否则下一批就再也不会提醒人去建它 —— 而它其实还没建。
                products[key]["_skip"] = bool(item_no)
            elif len(hits) > 1:
                # 库里同一身份有多条档案：无法判断该入哪一条，必须人工确认，禁止猜。
                # 内部编号也不能乱发，否则会与库内已用的号冲突或对不上。
                is_amb = True
                products[key]["内部编号"] = ""
                code = ""
                cand_names = (idx.get("names") or {}).get(ident) or {}
                cands = [{"item_no": n, "inner": i, "name": cand_names.get(n) or name}
                         for n, i in hits]
                products[key]["_cands"] = cands
                # **歧义行绝对不能再进新建档表**。它已经是库里的物品（只是有两条档案），
                # 建档表是给「美亚里还没有的物品」用的：推进去必然报「该物品已存在」，
                # 整行被退回，人看到红字却不知道哪里做错了（实测 2026-11-24 甲硝唑棒
                # 10321/10325 两条同名档案，人按弹窗导了建档表，美亚回「1.该物品已存在」）。
                products[key]["_skip"] = True
                cand = "、".join(f"{n}(内部{i or '-'})" for n, i in hits)
                warnings.append(
                    f"[歧义] {name} {spec} 在产品库中有 {len(hits)} 条同身份档案：{cand}；"
                    f"这一行已从建档表里剔除（美亚里已有它，再建只会被拒），"
                    f"请在界面「多条档案待选」里指定唯一物品编号，或先在产品库删除重复档案")
                doc.setdefault("_ambiguous", []).append({
                    "物品名称": name, "规格": spec,
                    # ident 用 | 拼成字符串带出去：界面选中哪一条要按身份写回
                    # config/inbound_overrides.json，键就是这个三元组。
                    "ident": "|".join(identity_key(lic_main, spec, brand)),
                    "cands": cands})
        if not item_no and not is_amb:
            warnings.append(f"[待建档] {name}（内部编号 {code}）尚未在产品库建档，物品编号暂空；"
                            f"批量导入建档后需重新【批量导出】本表，再回填物品编号")
        else:
            warnings.append(f"[取号] {name} → 物品编号 {item_no}（命中方式：{hit_by}）")

        rows.append([name, item_no, spec, brand or "", unit, qty, price, 13, code,
                     l1 or "", l2 or "", l3 or "", lot, mfg, exp])
    # 已建档的行必须从 products 里剔掉——它是「新建档表」，不是「更新表」。
    # 血案（牙e SC260105135151015050，2026-11-22）：麦迪康手套上一次已建过档（10367），
    # 这次又把同一条塞进新建档表，美亚整批返回 successCount=1 failCount=1 fail=true，
    # 于是同批本该新建的吸唾管也没建成，单据被判 upload_failed。
    # 只要还有真正待建档的行就只上传它们；一条都不剩时返回空列表，
    # 由 run.py 跳过上传并重新取号（物品编号本来就能从快照按身份查到）。
    new_products = [p for p in products.values() if not p.get("_skip")]
    # 把游标还回整批共用的发号状态：下一份单据必须从这里接着发，不能从 inner_base 重来。
    if isinstance(alloc, dict):
        alloc["cursor"] = max(int(alloc.get("cursor") or 0), int(cursor))
    return doc, rows, warnings, new_products


def write_inbound(rows, doc, path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "期初物品空模板"
    ws["A1"] = ("模板填写须知：\n1、标红字段为必填项，请务必填写完整；\n"
                "2、请按实际采购单位填写采购单位、数量及单价（含税）\n"
                "3、税率为非必填的0-100整数（%），采购单价（不含税）由系统自动计算\n"
                "4、日期格式请统一使用以下任一形式：2025-01-01 或 2025/01/01；")
    ws.merge_cells("A1:O1")
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[1].height = 60
    for i, h in enumerate(HEADERS, 1):
        c = ws.cell(row=2, column=i, value=h)
        c.font = Font(bold=True, color="FFFF0000" if h in
                      {"物品名称", "物品编号", "规格", "品牌", "采购单位", "采购数量", "采购单价（含税）",
                       "内部编号", "一级分类", "二级分类", "三级分类", "批次号", "生产日期", "有效期至"}
                      else "FF000000")
    for r, row in enumerate(rows, 3):
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            if c in (2, 13, 14, 15):  # 物品编号 / 批次号 / 生产日期 / 有效期至
                cell.number_format = "@"
                cell.value = "" if v is None else str(v)
    widths = [26, 12, 34, 16, 9, 10, 15, 7, 11, 12, 12, 12, 16, 13, 13]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    wb.save(path)


def write_products(products, path):
    wb = openpyxl.load_workbook(PRODUCT_TEMPLATE)
    ws = wb.worksheets[0]
    hdr = [ws.cell(row=2, column=c).value for c in range(1, ws.max_column + 1)]
    # 模板自带示例数据行必须整行删除（须知：示例内容请删除），须知行 A1 保留
    if ws.max_row >= 3:
        ws.delete_rows(3, ws.max_row - 2)
    for i, p in enumerate(products, 3):
        for c, h in enumerate(hdr, 1):
            if h in p:
                cell = ws.cell(row=i, column=c, value=fit_cell(h, p[h]))
                if h in ("内部编号", "规格", "条形码", "国家医保编码"):
                    cell.number_format = "@"
                    if p[h] is not None:
                        cell.value = str(p[h])
    wb.save(path)


if __name__ == "__main__":
    src, base = sys.argv[1], sys.argv[2]
    library = sys.argv[3] if len(sys.argv) > 3 else None
    inner_base = int(sys.argv[4]) if len(sys.argv) > 4 else ALLOC_BASE
    override_path = sys.argv[5] if len(sys.argv) > 5 else None
    base = base[:-5] if base.endswith(".json") else base
    doc, rows, warnings, products = build(src, library, inner_base, override_path)
    write_inbound(rows, doc, base + "_inbound.xlsx")
    write_products(products, base + "_products.xlsx")
    report = {
        "doc": doc, "rows": len(rows), "warnings": warnings, "products": products,
        "file_inbound": base + "_inbound.xlsx",
        "file_products": base + "_products.xlsx",
    }
    json.dump(report, open(base + "_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("OK", len(rows), "rows,", len(warnings), "warnings")
