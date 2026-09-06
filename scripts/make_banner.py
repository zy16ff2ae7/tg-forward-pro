#!/usr/bin/env python3
"""Запасной баннер бота: assets/welcome-code.png.

Стиль — тот же, что в кабинете (webapp/styles.css): неон на почти чёрном,
розово-пурпурный акцент, знак-корона. Из прежнего арт-деко остались только
геометрия и «чуточку поломанности» (хроматический сдвиг, сдвинутые полосы) —
для неона это к месту, а палитра и маскот теперь бренда ДОЧА.

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
OUT_FILE = BASE_DIR / "assets" / "welcome-code.png"

# ─────────────────────────────── НАСТРОЙКИ ────────────────────────────────
W, H = 1280, 720

# Палитра один в один с кабинетом: --bg, --accent, --violet, --cyan и т.д.
BG_TOP = (42, 12, 51)         # #2A0C33 — верх герой-градиента
BG_MID = (20, 6, 29)          # #14061D
BG_BOTTOM = (10, 5, 16)       # #0A0510 — --bg

MAGENTA = (255, 61, 154)      # --accent
MAGENTA_DEEP = (195, 30, 155)  # --accent-2
VIOLET = (168, 85, 247)       # --violet
CYAN = (34, 211, 238)         # --cyan
TEXT = (248, 237, 247)        # --text
PINK_TEXT = (255, 194, 226)   # подпись в герой-блоке
PINK_SOFT = (255, 166, 214)   # низ градиента короны
MUTED = (185, 160, 201)       # --muted
GLITCH_A = MAGENTA            # цвета хроматического сдвига: неоновая аберрация
GLITCH_B = CYAN

EYEBROW = "АВТОМАТИЗАЦИЯ  TELEGRAM"
TITLE_LINES = ["ДОЧА"]
SUBTITLE = "папина дочка на связи"
LEAD = "Пересылка постов, копирование каналов\nи задачи, которые работают сами."

FONTS = {
    "eyebrow": ("/System/Library/Fonts/Supplemental/Copperplate.ttc", 0, 22),
    "title": ("/System/Library/Fonts/Supplemental/Futura.ttc", 2, 150),
    "subtitle": ("/System/Library/Fonts/Supplemental/Didot.ttc", 2, 44),
    "lead": ("/System/Library/Fonts/Supplemental/Arial.ttf", 0, 26),
}
FALLBACK = "/Library/Fonts/Arial Unicode.ttf"

FRAME_INSET = 42            # рамка-бордюр
RIGHT_CENTER = (1010, 300)  # центр декоративной композиции справа
CROWN_SIZE = 330            # знак-корона: сторона квадрата, как viewBox 120×120
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
    """Веер тонких лучей из одной точки — задник композиции.

    Точку держим за кадром: луч, у которого не видно начала, читается как свет,
    падающий в кадр, а не как случайно оборванная линия.
    """
    ox, oy = origin
    def painter(draw):
        for i in range(rays):
            angle = math.pi + (i / (rays - 1)) * math.pi * 0.62
            ex, ey = ox + math.cos(angle) * radius, oy + math.sin(angle) * radius
            draw.line([(ox, oy), (ex, ey)], fill=(*MAGENTA, 10), width=2)
    return overlay(img, painter)


def draw_arcs(img: Image.Image, center, radius_from: int, radius_to: int,
              step: int = 20, color=VIOLET, alpha: int = 48) -> Image.Image:
    """Концентрические дуги — ореол вокруг знака.

    Радиусы подобраны так, чтобы самая широкая дуга оставалась внутри рамки:
    обрезанная краем холста дуга выглядит недоделанной, а не декоративной.
    """
    cx, cy = center
    def painter(draw):
        for r in range(radius_from, radius_to, step):
            draw.arc([cx - r, cy - r, cx + r, cy + r], start=196, end=344,
                     fill=(*color, alpha), width=2)
    return overlay(img, painter)


def draw_glow(img: Image.Image, center, radius: int, color, alpha: int = 74) -> Image.Image:
    """Мягкое свечение: концентрические эллипсы с падающей прозрачностью.

    Гауссово размытие тут не нужно — фигура радиальная, а так меньше зависимостей
    и результат предсказуем на любой версии Pillow.
    """
    cx, cy = center

    def painter(draw):
        for r in range(radius, 0, -max(2, radius // 40)):
            a = int(alpha * (1 - r / radius) ** 1.7)
            if a <= 0:
                continue
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, a))
    return overlay(img, painter)


def draw_crown(img: Image.Image, cx: int, cy: int, size: int = 320) -> Image.Image:
    """Знак бренда — корона. Те же пропорции, что у `<symbol id="crown">`
    в кабинете (viewBox 120×120), только нарисованные Pillow.

    Тело короны залито вертикальным градиентом белое → розовое: сплошной цвет
    на большом размере смотрится плоско, а маска даёт ровно тот же переход,
    что и `linearGradient` в SVG.
    """
    k = size / 120
    ox, oy = cx - size // 2, cy - size // 2

    def p(x: float, y: float) -> tuple[int, int]:
        return int(ox + x * k), int(oy + y * k)

    body = [p(28, 86), p(28, 48), p(44, 62), p(60, 34), p(76, 62), p(92, 48), p(92, 86)]
    base = [*p(26, 84), *p(94, 99)]

    # 1. Ореол под знаком — тот же crownHalo, что в SVG.
    img = draw_glow(img, p(60, 62), int(52 * k), MAGENTA, alpha=86)

    # 2. Маска тела: зубцы + основание одной фигурой, чтобы градиент был общий.
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    md.polygon(body, fill=255)
    md.rounded_rectangle(base, radius=int(5.5 * k), fill=255)
    # Градиент считаем по габаритам самого знака (в SVG-координатах это y 31…99),
    # а не по холсту и не по всему квадрату: только так белые зубцы переходят
    # в розовое основание, как в макете.
    top_y, bottom_y = int(31 * k), int(99 * k)
    mid = tuple((TEXT[i] + PINK_SOFT[i]) // 2 for i in range(3))
    tile = vertical_gradient((size, bottom_y - top_y), TEXT, mid, PINK_SOFT).convert("RGBA")
    fill = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fill.paste(tile, (ox, oy + top_y))
    fill.putalpha(mask)
    img = Image.alpha_composite(img, fill)

    def painter(draw):
        # 3. Контур — тёмно-вишнёвый, как stroke в SVG: знак не «плывёт» по фону.
        stroke = (120, 10, 80, 110)
        draw.line([*body, body[0]], fill=stroke, width=max(2, int(1.6 * k)), joint="curve")
        draw.rounded_rectangle(base, radius=int(5.5 * k), outline=stroke,
                               width=max(2, int(1.6 * k)))

        # 4. Камни в зубцах и точки на основании.
        for (gx, gy), r, color in (((28, 45), 5.4, VIOLET), ((60, 31), 6.2, CYAN),
                                   ((92, 45), 5.4, VIOLET)):
            x, y = p(gx, gy)
            rr = int(r * k)
            draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(*color, 255))
        for gx, r in ((44, 3.2), (60, 4.0), (76, 3.2)):
            x, y = p(gx, 91.5)
            rr = int(r * k)
            draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(*MAGENTA_DEEP, 215))

        # 5. Искры-звёздочки: те же две, что в SVG.
        for sx, sy, arm in ((104, 24, 6.0), (17, 30, 4.4)):
            x, y = p(sx, sy)
            a, b = int(arm * k), int(arm * k * 0.34)
            draw.polygon([(x, y - a), (x + b, y - b), (x + a, y), (x + b, y + b),
                          (x, y + a), (x - b, y + b), (x - a, y), (x - b, y - b)],
                         fill=(255, 227, 242, 235))
    return overlay(img, painter)


def draw_chevron(img: Image.Image, y: int, height: int = 16) -> Image.Image:
    """Зигзаг-полоса во всю ширину — ритм у нижнего края."""
    step = 34
    def painter(draw):
        points = []
        for i in range(0, W + step, step):
            points.append((i, y + (height if (i // step) % 2 else 0)))
        draw.line(points, fill=(*VIOLET, 70), width=3, joint="curve")
    return overlay(img, painter)


def draw_frame(img: Image.Image) -> Image.Image:
    """Двойная рамка с угловыми скобками; правый нижний угол надломлен."""
    pad = FRAME_INSET

    def painter(draw):
        draw.rectangle([pad, pad, W - pad - 1, H - pad - 1],
                       outline=(*MAGENTA, 82), width=2)
        draw.rectangle([pad + 9, pad + 9, W - pad - 10, H - pad - 10],
                       outline=(*VIOLET, 44), width=1)

        # угловые скобки-ступеньки
        arm, gap = 46, 9
        corners = [(pad, pad, 1, 1), (W - pad, pad, -1, 1), (pad, H - pad, 1, -1)]
        for cx, cy, sx, sy in corners:
            draw.line([(cx + sx * arm, cy), (cx, cy), (cx, cy + sy * arm)],
                      fill=(*PINK_TEXT, 170), width=3)
            draw.line([(cx + sx * (arm - 18), cy + sy * gap),
                       (cx + sx * gap, cy + sy * gap),
                       (cx + sx * gap, cy + sy * (arm - 18))],
                      fill=(*MAGENTA, 80), width=2)

        # сломанный угол: кусок рамки пропущен и смещён
        bx, by = W - pad, H - pad
        draw.line([(bx - arm, by), (bx - 14, by)], fill=(*PINK_TEXT, 170), width=3)
        draw.line([(bx, by - arm), (bx, by - 14)], fill=(*PINK_TEXT, 170), width=3)
        draw.line([(bx - 12, by - 6), (bx - 4, by + 4)], fill=(*CYAN, 120), width=2)
        draw.line([(bx - 6, by - 12), (bx + 4, by - 4)], fill=(*CYAN, 120), width=2)

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
        draw.line(points, fill=(*CYAN, 70), width=2, joint="curve")
    return overlay(img, painter)


def draw_text_rgba(img, xy, text, font, fill, anchor=None) -> Image.Image:
    return overlay(img, lambda d: d.text(xy, text, font=font, fill=fill, anchor=anchor))


def draw_chromatic(img, xy, text, font, fill, dx: int = 3, spacing: int = 0) -> Image.Image:
    """Хроматический сдвиг: два цветных отпечатка по бокам, основной — сверху.

    spacing > 0 — тот же разряд между буквами, что у логотипа в кабинете
    (letter-spacing: 0.1em): без него слово «ДОЧА» читается как обычный текст.
    """
    x, y = xy

    def put(dx_shift: int, color, alpha: int):
        if spacing:
            return draw_letterspaced(img, (x + dx_shift, y), text, font,
                                     (*color, alpha), spacing)
        return draw_text_rgba(img, (x + dx_shift, y), text, font, (*color, alpha))

    img = put(-dx, GLITCH_A, 150)
    img = put(dx, GLITCH_B, 150)
    return put(0, fill, 242)


def glitch_slices(img: Image.Image, bands) -> Image.Image:
    """Сдвинутые по горизонтали полосы — эффект помехи."""
    snapshot = img.copy()
    for y0, y1, dx in bands:
        strip = snapshot.crop((0, y0, W, y1))
        img.paste(strip, (dx, y0))
    return img


def draw_rotated_text(img, xy, text, kind: str, size: int, fill, angle: float,
                      box=(620, 100), ss: int = 4) -> Image.Image:
    """Наклонная надпись через суперсэмплинг: рисуем в ss раз крупнее и уменьшаем.

    Прямой поворот текста на финальном размере убивал волосяные линии Didot:
    перекладина «н» и диагональ «и» — толщиной меньше пикселя, бикубический
    поворот их размывал в ничто, и подпись читалась как «папипа дочка па связи».
    """
    font = load_font(kind, size * ss)
    layer = Image.new("RGBA", (box[0] * ss, box[1] * ss), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((0, 4 * ss), text, font=font, fill=fill)
    layer = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    layer = layer.resize((layer.width // ss, layer.height // ss), Image.LANCZOS)
    img.paste(layer, xy, layer)
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

    # декоративная композиция справа: лучи, дуги-ореол и знак-корона
    img = draw_sunburst(img, (W + 60, H + 80), 780)
    img = draw_arcs(img, RIGHT_CENTER, 150, 230, step=22)
    img = draw_arcs(img, RIGHT_CENTER, 156, 230, step=22, color=MAGENTA, alpha=26)
    img = draw_crown(img, *RIGHT_CENTER, size=CROWN_SIZE)
    img = draw_chevron(img, H - 118)

    f_eyebrow = load_font("eyebrow")
    f_title = fit_font("title", max(TITLE_LINES, key=len), 620, FONTS["title"][2])
    f_lead = load_font("lead")

    left = 108
    img = draw_letterspaced(img, (left, 176), EYEBROW, f_eyebrow, (*MAGENTA, 220), 3)

    # тонкая линия-разделитель под надзаголовком
    img = overlay(img, lambda d: d.line([(left, 214), (left + 120, 214)],
                                        fill=(*VIOLET, 200), width=3))

    y = 258
    for line in TITLE_LINES:
        # Логотип: разряд между буквами и неоновая аберрация по краям.
        img = draw_chromatic(img, (left, y), line, f_title, TEXT, dx=3, spacing=10)
        y += int(f_title.size * 1.08)

    # Подзаголовок с лёгким наклоном. Отступ считаем от нижней кромки
    # логотипа: у «Д» в Futura ножки уходят ниже базовой линии и подпись
    # прилипала к ним.
    img = draw_rotated_text(img, (left + 4, y + 16), SUBTITLE, "subtitle",
                            FONTS["subtitle"][2], (*PINK_TEXT, 235), -1.6)

    img = draw_text_rgba(img, (left, y + 128), LEAD, f_lead, (*MUTED, 235))

    img = draw_frame(img)
    img = draw_crack(img, (W - 300, FRAME_INSET + 26), length=170)

    # Помеха: три узкие полосы, сдвинутые по горизонтали. Полосы намеренно
    # проходят по декору, а не по тексту и не по знаку — иначе подпись выглядит
    # не «с помехой», а криво набранной, а у короны откусывает камень.
    img = glitch_slices(img, [(150, 156, 7), (612, 618, -9), (664, 668, 5)])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT_FILE, "PNG", optimize=True)
    print(f"Готово: {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} КБ, {W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
