# -*- coding: utf-8 -*-
"""2026-10-25 工具帖 v4（极简版）两张卡：封面 + 说明卡。
跑法：python scripts/gen_tool_v4.py（需 PIL；用系统 python 3.11）
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1620          # CSS 尺寸，@2x 出 2160×3240
SCALE = 2
INK = (27, 30, 36)
PAPER = (246, 243, 236)
TEXT = (42, 46, 53)
MUTED = (106, 110, 118)
ACC_INK = (224, 123, 60)
ACC = (196, 96, 42)

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
if not os.path.exists(FONT):
    FONT = "C:/Windows/Fonts/simhei.ttf"
if not os.path.exists(FONT_B):
    FONT_B = FONT


def f(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def text(d, xy, s, size, color, bold=False):
    d.text(xy, s, font=f(size, bold), fill=color)


def save(img, name):
    out = img.resize((W * SCALE, H * SCALE), Image.LANCZOS)
    p = os.path.join(OUT, name)
    out.save(p)
    assert out.size == (2160, 3240), out.size
    print("ok", name, out.size)


OUT = os.path.join("运营", "归档", "2026-10-25-开源工具帖", "配图")
os.makedirs(OUT, exist_ok=True)

# ---------------- 封面 ----------------
img = Image.new("RGB", (W, H), INK)
d = ImageDraw.Draw(img)
text(d, (110, 96), "业余项目 · 2026.10", 30, ACC_INK, True)
text(d, (110, 700), "我开源了", 156, (242, 238, 230), True)
text(d, (110, 880), "一个小工具", 156, (242, 238, 230), True)
d.rectangle([110, 660, 230, 668], fill=ACC_INK)
text(d, (110, 1120), "不卖课 · 不带单 · 不收费", 46, ACC_INK, True)
text(d, (110, 1380), "历史业绩不代表未来，不构成投资建议", 28, (152, 160, 168))
save(img, "01-封面-v4.png")

# ---------------- 说明卡 ----------------
img = Image.new("RGB", (W, H), PAPER)
d = ImageDraw.Draw(img)
text(d, (110, 150), "它是什么，不是什么", 76, TEXT, True)
rows = [
    ("是", "我自己看债基时用的那几个条件，", "写成了代码"),
    ("不是", "不荐基、不预测、不收费", ""),
    ("代码", "公开的，谁都能看", ""),
]
y = 420
for tag, l1, l2 in rows:
    text(d, (110, y), tag, 44, ACC, True)
    text(d, (280, y + 4), l1, 40, TEXT)
    if l2:
        text(d, (280, y + 66), l2, 40, TEXT)
    d.line([110, y + 150, 970, y + 150], fill=(226, 219, 204), width=2)
    y += 230
text(d, (110, 1250), "图 3 是它跑出来的页面", 38, MUTED)
text(d, (110, 1400), "历史业绩不代表未来，不构成投资建议", 28, MUTED)
save(img, "02-说明-v4.png")
