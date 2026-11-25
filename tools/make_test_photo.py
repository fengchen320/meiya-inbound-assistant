# -*- coding: utf-8 -*-
"""生成一张「仿手机拍的随货同行对账单」用于验证 OCR 通道。

为什么自己造样本：真实对账单照片在用户手机上，落不到工作区；而 OCR 通道的
每一处设计（纠偏、放大、列聚类）都必须用「带表格线、带小字、略微倾斜」的图
才能验证。参数抄自真实随货单：表头 9 列，明细 2 行，末尾合计行。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = r"C:/Windows/Fonts/simsun.ttc"
W, H = 1654, 2339  # A4 @200dpi


def build(out, angle=1.2, noise=False):
    im = Image.new("RGB", (W, H), (252, 252, 250))
    d = ImageDraw.Draw(im)
    f_title = ImageFont.truetype(FONT, 46)
    f_h = ImageFont.truetype(FONT, 26)
    f_b = ImageFont.truetype(FONT, 22)
    f_s = ImageFont.truetype(FONT, 18)

    d.text((330, 80), "示例医疗器械有限公司 随货同行对账单", font=f_title, fill=(20, 20, 20))
    d.text((120, 170), "销售单号：DEMO00012345", font=f_h, fill=(30, 30, 30))
    d.text((900, 170), "制单日期：2026-11-16", font=f_h, fill=(30, 30, 30))
    d.text((120, 215), "购货单位：示例口腔诊所", font=f_b, fill=(30, 30, 30))
    d.text((900, 215), "收货地址：示例市示例路 1 号", font=f_b, fill=(30, 30, 30))
    d.text((120, 255), "销售员：张三　　收货人：李四", font=f_b, fill=(30, 30, 30))
    d.text((900, 255), "咨询电话：00000000", font=f_b, fill=(30, 30, 30))

    # 表格
    x0, y0 = 100, 320
    colw = [70, 150, 250, 210, 130, 90, 90, 110, 150]
    rows = [
        ["编号", "商品代码", "品名", "规格型号", "生产企业", "单位", "数量", "单价", "金额"],
        ["300348", "ezt14608", "种植体系统", "BCSD-1042 L10*4.2mm", "Dentall Implants Industries Ltd.", "个", "2.00", "466.00", "930.00"],
        ["300347", "ezt14608", "种植体系统", "BCSD-0842 L06*4.2mm", "Dentall Implants Industries Ltd.", "个", "2.00", "466.00", "930.00"],
    ]
    rowh = 96
    y = y0
    for ri, row in enumerate(rows):
        x = x0
        for ci, cell in enumerate(row):
            d.rectangle([x, y, x + colw[ci], y + rowh], outline=(90, 90, 120), width=2)
            font = f_h if ri == 0 else f_b
            txt = cell
            # 长文本按列宽折行，模拟真实表格的窄列挤压
            if ri > 0 and ci in (2, 3, 4):
                words = txt.split(" ")
                lines, cur = [], ""
                for wd in words:
                    probe = (cur + " " + wd).strip()
                    if d.textlength(probe, font=font) > colw[ci] - 8 and cur:
                        lines.append(cur)
                        cur = wd
                    else:
                        cur = probe
                if cur:
                    lines.append(cur)
                for li, ln in enumerate(lines[:3]):
                    d.text((x + 4, y + 6 + li * 26), ln, font=font, fill=(25, 25, 25))
            else:
                d.text((x + 4, y + 30), txt, font=font, fill=(25, 25, 25))
            x += colw[ci]
        y += rowh

    # 批号等小字行（真实单据里在品名下方）
    d.text((100, y + 20), "批号 W0261474 / W0261385　　生产日期 2026-06-23 / 2026-06-16　　有效期至 2031-06-22 / 2031-06-15", font=f_s, fill=(40, 40, 40))
    d.text((100, y + 55), "注册证号 国械注进20153172425　　医保编码 C07Q201　　商品ID 975", font=f_s, fill=(40, 40, 40))
    d.text((100, y + 120), "合计数量：4　　本页小计：¥1860.00", font=f_h, fill=(30, 30, 30))
    d.text((100, y + 165), "合计金额（大写）：壹仟捌佰陆拾元整", font=f_b, fill=(30, 30, 30))
    d.text((100, y + 215), "备注：请收货后核对品种、数量、金额，当日有效", font=f_s, fill=(60, 60, 60))

    if angle:
        im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=(250, 250, 248), expand=False)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    im.save(out, quality=88)
    print("saved", out, im.size)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tmp", "photos", "synthetic.jpg"))
