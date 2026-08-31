#!/usr/bin/env python3
"""Приветственный баннер бота: assets/welcome.png.

Стиль — арт-деко в зелёных тонах с латунными линиями и «чуточку поломанности»:
хроматический сдвиг по заголовку, пара сдвинутых горизонтальных полос,
надломленный угол рамки и лёгкий наклон подзаголовка.

Картинка рисуется кодом (Pillow), а не генерируется нейросетью: её можно
править и пересобирать в любой момент, а в репозитории лежит обычный PNG.

Запуск:  python scripts/make_banner.py
Правки:  палитра и тексты — в блоке НАСТРОЙКИ ниже.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_FILE = BASE_DIR / "assets" / "welcome.png"

# ─────────────────────────────── НАСТРОЙКИ ────────────────────────────────
W, H = 1280, 720

# Зелёная палитра: от почти чёрного хвойного к глубокому изумруду
BG_TOP = (4, 18, 13)
BG_MID = (9, 36, 26)
BG_BOTTOM = (3, 12, 9)

EMERALD = (16, 185, 129)      # изумруд
JADE = (52, 211, 153)         # нефрит
MINT = (198, 246, 226)        # мята — основной текст
BRASS = (198, 166, 104)       # латунь
BRASS_LIGHT = (232, 208, 156)
MUTED = (128, 172, 154)
GLITCH_A = (16, 185, 129)     # цвета хроматического сдвига
GLITCH_B = (206, 138, 92)

EYEBROW = "АВТОМАТИЗАЦИЯ  TELEGRAM"
TITLE_LINES = ["Помощник", "автоматизаций"]
SUBTITLE = "в Telegram"
LEAD = "Пересылка постов, копирование каналов\nи задачи, которые выполняются сами."

FONTS = {
    "eyebrow": ("/System/Library/Fonts/Supplemental/Copperplate.ttc", 0, 22),
    "title": ("/System/Library/Fonts/Supplemental/Futura.ttc", 2, 84),
    "subtitle": ("/System/Library/Fonts/Supplemental/Didot.ttc", 2, 44),
    "lead": ("/System/Library/Fonts/Supplemental/Arial.ttf", 0, 26),
}
FALLBACK = "/Library/Fonts/Arial Unicode.ttf"

FRAME_INSET = 42          # рамка-бордюр арт-деко
RIGHT_CENTER = (1010, 250)  # центр декоративной композиции справа
# ──────────────────────────────────────────────────────────────────────────


def load_font(kind: str, size: int | None = None) -> ImageFont.FreeTypeFont:
    path, index, default = FONTS[kind]
    size = size or default
    if Path(path).exists():
        return ImageFont.truetype(path, size, index=index)
    if Path(FALLBACK).exists():
        return ImageFont.truetype(FALLBACK, size)
    print("Не нашёл ни одного шрифта.")
    return ImageFont.load_default()


def fit_font(kind: str, text: str, max_width: int, start: int, min_size: int = 40) -> ImageFont.FreeTypeFont:
    size = start
    while size > min_size:
        font = load_font(kind, size)
        if font.getlength(text) <= max_width:
            return font
        size -= 2
    return load_font(kind, min_size)


def vertical_gradient(size, top, mid, bottom) -> Image.Image:
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    half = size[1] // 2
    for y in range(size[1]):
        if y < half:
            t = y / max(half, 1)
            c = [int(top[i] + (mid[i] - top[i]) * t) for i in range(3)]
        else:
            t = (y - half) / max(size[1] - half, 1)
            c = [int(mid[i] + (bottom[i] - mid[i]) * t) for i in range(3)]
        draw.line([(0, y), (size[0], y)], fill=tuple(c))
    return img


def overlay(img, func) -> Image.Image:
    """Рисует на отдельном RGBA-слое и аккуратно накладывает на основу."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    func(ImageDraw.Draw(layer))
    return Image.alpha_composite(img, layer)


def draw_sunburst(img: Image.Image, origin, radius: int, rays: int = 46) -> Image.Image:
    """Классический декo-веер: тонкие лучи из одной точки."""
    ox, oy = origin
    def painter(draw):
        for i in range(rays):
            angle = math.pi + (i / (rays - 1)) * math.pi * 0.62
            ex, ey = ox + math.cos(angle) * radius, oy + math.sin(angle) * radius
            draw.line([(ox, oy), (ex, ey)], fill=(*BRASS, 16), width=3)
    return overlay(img, painter)


def draw_arcs(img: Image.Image, center, radius_from: int, radius_to: int,
              step: int = 20, color=EMERALD, alpha: int = 48) -> Image.Image:
    """Концентрические дуги — частый мотив арт-деко."""
    cx, cy = center
    def painter(draw):
        for r in range(radius_from, radius_to, step):
            draw.arc([cx - r, cy - r, cx + r, cy + r], start=196, end=344,
                     fill=(*color, alpha), width=2)
    return overlay(img, painter)


def draw_ziggurat(img: Image.Image, x: int, base_y: int) -> Image.Image:
    """Ступенчатый силуэт внизу — «зиккурат», ещё один декo-штамп."""
    steps = [(300, 26), (240, 24), (186, 22), (138, 20), (96, 18)]
    def painter(draw):
        y = base_y
        for width, height in steps:
            left = x + (300 - width) // 2
            draw.rectangle([left, y - height, left + width, y],
                           fill=(*EMERALD, 26), outline=(*BRASS, 60), width=1)
            y -= height
    return overlay(img, painter)


def draw_porter(img: Image.Image, cx: int, base_y: int, scale: float = 1.0) -> Image.Image:
    """Папа, ночной портье — маскот сервиса.

    Тот же персонаж, что и в мини-аппе (`<symbol id="papa">`), только
    нарисованный примитивами: силуэт в арке, фуражка с монограммой, фонарь
    и два письма. Силуэт намеренно простой — на масштабе баннера читается
    именно контур, а не детали лица.
    """
    def s(value: float) -> int:
        return int(value * scale)

    SKIN = (58, 118, 92)          # лицо: приглушённый зелёный, не «живая кожа»
    FACE_SHADOW = (26, 66, 50)
    COAT = (6, 30, 22)            # мундир темнее фона
    CAP = (10, 44, 31)
    OUT = (*BRASS, 165)
    OUT_SOFT = (*BRASS, 90)

    def painter(draw):
        # ── арка-зиккурат: ступенчатый портал за спиной ──
        arch_w, arch_top = s(150), base_y - s(400)
        for step, inset in enumerate((0, 26, 52)):
            draw.rectangle([cx - arch_w + inset, arch_top + step * s(16),
                            cx + arch_w - inset, base_y],
                           fill=(*EMERALD, 14), outline=(*BRASS, 34), width=1)
        draw.rectangle([cx - s(74), arch_top + s(48), cx + s(74), base_y],
                       fill=(*EMERALD, 20), outline=(*BRASS, 58), width=2)

        # ── свечение фонаря: концентрические эллипсы ──
        lx, ly = cx + s(146), base_y - s(150)
        for r in range(s(120), 0, -s(8)):
            a = int(46 * (1 - r / s(120)) ** 1.6)
            draw.ellipse([lx - r, ly - r * 1.15, lx + r, ly + r * 1.15],
                         fill=(*JADE, a))

        # ── торс со скошенными плечами ──
        draw.polygon([(cx - s(120), base_y), (cx - s(96), base_y - s(150)),
                      (cx - s(46), base_y - s(192)), (cx + s(46), base_y - s(192)),
                      (cx + s(96), base_y - s(150)), (cx + s(120), base_y)],
                     fill=COAT + (255,), outline=OUT)

        # лацканы и пуговицы
        draw.line([(cx - s(40), base_y - s(188)), (cx, base_y - s(96)),
                   (cx + s(40), base_y - s(188))], fill=OUT_SOFT, width=2)
        for by in (base_y - s(70), base_y - s(122)):
            draw.ellipse([cx - s(6), by - s(6), cx + s(6), by + s(6)],
                         fill=(*BRASS_LIGHT, 200))

        # ── шея и голова ──
        draw.rectangle([cx - s(24), base_y - s(250), cx + s(24), base_y - s(184)],
                       fill=FACE_SHADOW + (255,))
        hx0, hy0 = cx - s(52), base_y - s(354)
        hx1, hy1 = cx + s(52), base_y - s(228)
        draw.ellipse([hx0, hy0, hx1, hy1], fill=SKIN + (255,), outline=OUT)

        # ── усы: два мягких штриха ──
        mx, my = cx - s(2), base_y - s(272)
        for sign in (-1, 1):
            draw.arc([mx + sign * s(4) - s(20), my - s(12),
                      mx + sign * s(4) + s(20), my + s(16)],
                     start=200 if sign < 0 else 340,
                     end=20 if sign < 0 else 160,
                     fill=(*BRASS_LIGHT, 200), width=max(2, s(5)))

        # ── глаза: закрытые дуги — «спокойно дежурит» ──
        for sign in (-1, 1):
            ex = cx + sign * s(20)
            ey = base_y - s(300)
            draw.arc([ex - s(13), ey - s(9), ex + s(13), ey + s(9)],
                     start=200, end=340, fill=(*MINT, 225), width=max(2, s(4)))

        # ── фуражка: купол, околыш, козырёк, монограмма ──
        draw.ellipse([cx - s(62), base_y - s(392), cx + s(62), base_y - s(300)],
                     fill=CAP + (255,), outline=OUT)
        draw.rectangle([cx - s(64), base_y - s(322), cx + s(64), base_y - s(300)],
                       fill=(*BRASS, 210))
        draw.pieslice([cx - s(96), base_y - s(322), cx + s(34), base_y - s(272)],
                      start=180, end=360, fill=CAP + (255,), outline=OUT)
        draw.text((cx - s(16), base_y - s(378)), "P",
                  font=load_font("subtitle", max(20, s(30))), fill=(*BRASS_LIGHT, 235))

        # ── рука с фонарём ──
        draw.line([(cx + s(74), base_y - s(168)), (cx + s(120), base_y - s(146)),
                   (lx, ly + s(18))], fill=COAT + (255,), width=s(26), joint="curve")
        draw.line([(cx + s(74), base_y - s(168)), (cx + s(120), base_y - s(146)),
                   (lx, ly + s(18))], fill=OUT_SOFT, width=2, joint="curve")
        draw.rounded_rectangle([lx - s(24), ly - s(30), lx + s(24), ly + s(30)],
                               radius=s(8), fill=(*MINT, 235), outline=(*BRASS, 230),
                               width=2)
        draw.line([(lx, ly - s(30)), (lx, ly - s(46))], fill=(*BRASS, 200), width=2)

        # ── письма: их он и разносит ──
        for i, (ox, oy, tilt) in enumerate(((-s(178), -s(64), -13), (-s(196), -s(148), 9))):
            card = Image.new("RGBA", (s(96), s(64)), (0, 0, 0, 0))
            cd = ImageDraw.Draw(card)
            cd.rounded_rectangle([1, 1, s(94), s(62)], radius=s(6),
                                 fill=(*MINT, 225), outline=(*BRASS, 215), width=2)
            cd.line([(1, 1), (s(47), s(34)), (s(94), 1)], fill=(*BRASS, 190), width=2)
            cd.line([(s(10), s(48)), (s(70), s(48))], fill=(*EMERALD, 150), width=2)
            card = card.rotate(tilt, resample=Image.BICUBIC, expand=True)
            img.paste(card, (cx + ox - card.width // 2, base_y + oy - card.height // 2), card)

    return overlay(img, painter)


def draw_chevron(img: Image.Image, y: int, height: int = 16) -> Image.Image:
    """Зигзаг-полоса (шеврон) во всю ширину."""
    step = 34
    def painter(draw):
        points = []
        for i in range(0, W + step, step):
            points.append((i, y + (height if (i // step) % 2 else 0)))
        draw.line(points, fill=(*BRASS, 70), width=3, joint="curve")
    return overlay(img, painter)


def draw_frame(img: Image.Image) -> Image.Image:
    """Двойная рамка с угловыми скобками; правый нижний угол надломлен."""
    pad = FRAME_INSET

    def painter(draw):
        draw.rectangle([pad, pad, W - pad - 1, H - pad - 1],
                       outline=(*BRASS, 90), width=2)
        draw.rectangle([pad + 9, pad + 9, W - pad - 10, H - pad - 10],
                       outline=(*EMERALD, 44), width=1)

        # угловые скобки-ступеньки
        arm, gap = 46, 9
        corners = [(pad, pad, 1, 1), (W - pad, pad, -1, 1), (pad, H - pad, 1, -1)]
        for cx, cy, sx, sy in corners:
            draw.line([(cx + sx * arm, cy), (cx, cy), (cx, cy + sy * arm)],
                      fill=(*BRASS_LIGHT, 170), width=3)
            draw.line([(cx + sx * (arm - 18), cy + sy * gap),
                       (cx + sx * gap, cy + sy * gap),
                       (cx + sx * gap, cy + sy * (arm - 18))],
                      fill=(*BRASS, 80), width=2)

        # сломанный угол: кусок рамки пропущен и смещён
        bx, by = W - pad, H - pad
        draw.line([(bx - arm, by), (bx - 14, by)], fill=(*BRASS_LIGHT, 170), width=3)
        draw.line([(bx, by - arm), (bx, by - 14)], fill=(*BRASS_LIGHT, 170), width=3)
        draw.line([(bx - 12, by - 6), (bx - 4, by + 4)], fill=(*MINT, 120), width=2)
        draw.line([(bx - 6, by - 12), (bx + 4, by - 4)], fill=(*MINT, 120), width=2)

    return overlay(img, painter)


def draw_crack(img: Image.Image, start, length: int = 150) -> Image.Image:
    """Тонкая трещина — та самая «чуточку поломанности»."""
    x, y = start
    random.seed(7)

    def painter(draw):
        points = [(x, y)]
        cx, cy = x, y
        for _ in range(length // 12):
            cx += random.uniform(6, 16)
            cy += random.uniform(-6, 9)
            points.append((cx, cy))
        draw.line(points, fill=(*MINT, 70), width=2, joint="curve")
    return overlay(img, painter)


def draw_text_rgba(img, xy, text, font, fill, anchor=None) -> Image.Image:
    return overlay(img, lambda d: d.text(xy, text, font=font, fill=fill, anchor=anchor))


def draw_chromatic(img, xy, text, font, fill, dx: int = 3) -> Image.Image:
    """Хроматический сдвиг: два цветных отпечатка по бокам, основной — сверху."""
    x, y = xy
    img = draw_text_rgba(img, (x - dx, y), text, font, (*GLITCH_A, 150))
    img = draw_text_rgba(img, (x + dx, y), text, font, (*GLITCH_B, 150))
    return draw_text_rgba(img, (x, y), text, font, (*fill, 242))


def glitch_slices(img: Image.Image, bands) -> Image.Image:
    """Сдвинутые по горизонтали полосы — эффект помехи."""
    snapshot = img.copy()
    for y0, y1, dx in bands:
        strip = snapshot.crop((0, y0, W, y1))
        img.paste(strip, (dx, y0))
    return img


def draw_letterspaced(img, xy, text, font, fill, spacing: int) -> Image.Image:
    x, y = xy

    def painter(draw):
        cursor = x
        for ch in text:
            draw.text((cursor, y), ch, font=font, fill=fill)
            cursor += font.getlength(ch) + spacing
    return overlay(img, painter)


def main() -> int:
    img = vertical_gradient((W, H), BG_TOP, BG_MID, BG_BOTTOM).convert("RGBA")

    # декоративная композиция справа
    img = draw_sunburst(img, (W + 60, -140), 760)
    img = draw_arcs(img, RIGHT_CENTER, 90, 300, step=24)
    img = draw_arcs(img, RIGHT_CENTER, 96, 300, step=24, color=BRASS, alpha=26)
    img = draw_porter(img, 1010, H - 92)
    img = draw_chevron(img, H - 118)

    f_eyebrow = load_font("eyebrow")
    f_title = fit_font("title", max(TITLE_LINES, key=len), 660, FONTS["title"][2])
    f_subtitle = load_font("subtitle")
    f_lead = load_font("lead")

    left = 108
    img = draw_letterspaced(img, (left, 176), EYEBROW, f_eyebrow, (*BRASS, 220), 3)

    # тонкая линия-разделитель под надзаголовком
    img = overlay(img, lambda d: d.line([(left, 214), (left + 120, 214)],
                                        fill=(*EMERALD, 200), width=3))

    y = 250
    for line in TITLE_LINES:
        img = draw_chromatic(img, (left, y), line, f_title, MINT, dx=3)
        y += int(FONTS["title"][2] * 1.08)

    # подзаголовок с лёгким наклоном
    sub = Image.new("RGBA", (520, 90), (0, 0, 0, 0))
    ImageDraw.Draw(sub).text((0, 4), SUBTITLE, font=f_subtitle, fill=(*BRASS_LIGHT, 235))
    sub = sub.rotate(-1.6, resample=Image.BICUBIC, expand=True)
    img.paste(sub, (left + 4, y - 8), sub)

    img = draw_text_rgba(img, (left, y + 96), LEAD, f_lead, (*MUTED, 235))

    img = draw_frame(img)
    img = draw_crack(img, (W - 300, FRAME_INSET + 26), length=170)

    # помеха: три узкие полосы, сдвинутые по горизонтали
    img = glitch_slices(img, [(268, 276, 7), (430, 436, -9), (486, 490, 5)])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT_FILE, "PNG", optimize=True)
    print(f"Готово: {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} КБ, {W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
