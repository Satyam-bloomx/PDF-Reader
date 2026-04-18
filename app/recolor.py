"""
PDF vector-level recoloring using pikepdf content stream manipulation.
"""

from typing import Tuple

_DEFAULT_PALETTE = {
    "bg":     (0.502, 0.027, 0.063),
    "text":   (1.000, 0.980, 0.820),
    "gold":   (1.000, 0.843, 0.353),
    "violet": (1.000, 0.700, 0.400),
    "teal":   (1.000, 0.900, 0.600),
    "coral":  (1.000, 0.780, 0.580),
}


def _resolve(palette: dict) -> tuple:
    """Return (BG, TEXT, GOLD, VIOLET, TEAL, CORAL) tuples from palette dict."""
    p = _DEFAULT_PALETTE.copy()
    if palette:
        p.update(palette)
    return p["bg"], p["text"], p["gold"], p["violet"], p["teal"], p["coral"]


def _map_rgb(r: float, g: float, b: float, palette_resolved: tuple,
             for_text: bool = False, for_stroke: bool = False) -> Tuple[float, float, float]:
    BG, TEXT, GOLD, VIOLET, TEAL, CORAL = palette_resolved

    mx = max(r, g, b)
    mn = min(r, g, b)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = (mx - mn) / (mx + 1e-6)

    if for_stroke:
        return TEXT

    if for_text:
        if sat >= 0.35:
            delta = mx - mn + 1e-6
            if mx == r:
                hue = ((g - b) / delta) % 6
            elif mx == g:
                hue = (b - r) / delta + 2
            else:
                hue = (r - g) / delta + 4
            hue /= 6.0
            if hue < 0.18 or hue > 0.88:
                return GOLD
            elif 0.18 <= hue < 0.55:
                return TEAL
            elif 0.55 <= hue <= 0.85:
                return VIOLET
            else:
                return CORAL
        return TEXT

    if sat < 0.4:
        return (
            TEXT[0] + lum * (BG[0] - TEXT[0]),
            TEXT[1] + lum * (BG[1] - TEXT[1]),
            TEXT[2] + lum * (BG[2] - TEXT[2]),
        )

    delta = mx - mn + 1e-6
    if mx == r:
        hue = ((g - b) / delta) % 6
    elif mx == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    hue /= 6.0

    if hue < 0.18 or hue > 0.88:
        accent = GOLD
    elif 0.18 <= hue < 0.55:
        accent = TEAL
    elif 0.55 <= hue <= 0.85:
        accent = VIOLET
    else:
        accent = CORAL

    mix = min((sat - 0.4) / 0.6, 1.0)
    grey_r = TEXT[0] + lum * (BG[0] - TEXT[0])
    grey_g = TEXT[1] + lum * (BG[1] - TEXT[1])
    grey_b = TEXT[2] + lum * (BG[2] - TEXT[2])
    return (
        (1 - mix) * grey_r + mix * accent[0],
        (1 - mix) * grey_g + mix * accent[1],
        (1 - mix) * grey_b + mix * accent[2],
    )


def _to_pdf_num(v: float) -> float:
    return round(float(v), 4)


def _remap_color_args(operands: list, n: int, palette_resolved: tuple,
                      for_text: bool = False, for_stroke: bool = False) -> list:
    nums = [float(op) for op in operands[-n:]]
    prefix = list(operands[:-n])

    if n == 1:
        g = nums[0]
        r2, g2, b2 = _map_rgb(g, g, g, palette_resolved, for_text=for_text, for_stroke=for_stroke)
        if for_text or for_stroke:
            return prefix + [_to_pdf_num(v) for v in (r2, g2, b2)]
        lum2 = 0.299 * r2 + 0.587 * g2 + 0.114 * b2
        new_nums = [lum2]
    elif n == 3:
        r2, g2, b2 = _map_rgb(nums[0], nums[1], nums[2], palette_resolved,
                               for_text=for_text, for_stroke=for_stroke)
        new_nums = [r2, g2, b2]
    elif n == 4:
        c, m, y, k = nums
        r = (1 - c) * (1 - k)
        g = (1 - m) * (1 - k)
        b = (1 - y) * (1 - k)
        r2, g2, b2 = _map_rgb(r, g, b, palette_resolved, for_text=for_text, for_stroke=for_stroke)
        new_nums = [r2, g2, b2]
    else:
        return operands

    return prefix + [_to_pdf_num(v) for v in new_nums]


_COLOR_OPS = {
    b"g":  (1, False, False),
    b"G":  (1, False, True),
    b"rg": (3, False, False),
    b"RG": (3, False, True),
    b"k":  (4, True,  False),
    b"K":  (4, True,  True),
}


def fast_regex_recolor(content: bytes, palette: dict, inject_bg_rect: bool = True,
                       w: float = 0, h: float = 0) -> bytes:
    import re
    pr = _resolve(palette)
    BG = pr[0]

    num_re = rb'\-?[0-9]*\.?[0-9]+'

    def replacer_rgb(m):
        try:
            r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
            is_stroke = (m.group(4) == b'RG')
            r2, g2, b2 = _map_rgb(r, g, b, pr, for_text=False, for_stroke=is_stroke)
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {m.group(4).decode()}".encode()
        except:
            return m.group(0)

    rgb_pat = rb'\b(' + num_re + rb')\s+(' + num_re + rb')\s+(' + num_re + rb')\s+(rg|RG)\b'
    content = re.sub(rgb_pat, replacer_rgb, content)

    def replacer_g(m):
        try:
            g_val = float(m.group(1))
            is_stroke = (m.group(2) == b'G')
            r2, g2, b2 = _map_rgb(g_val, g_val, g_val, pr, for_text=False, for_stroke=is_stroke)
            op = b"RG" if is_stroke else b"rg"
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {op.decode()}".encode()
        except:
            return m.group(0)

    g_pat = rb'\b(' + num_re + rb')\s+(g|G)\b'
    content = re.sub(g_pat, replacer_g, content)

    def replacer_cmyk(m):
        try:
            c, m_val, y, k = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
            is_stroke = (m.group(5) == b'K')
            r = (1 - c) * (1 - k)
            g_val = (1 - m_val) * (1 - k)
            b = (1 - y) * (1 - k)
            r2, g2, b2 = _map_rgb(r, g_val, b, pr, for_text=False, for_stroke=is_stroke)
            op = b"RG" if is_stroke else b"rg"
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {op.decode()}".encode()
        except:
            return m.group(0)

    cmyk_pat = (rb'\b(' + num_re + rb')\s+(' + num_re + rb')\s+(' + num_re + rb')\s+('
                + num_re + rb')\s+(k|K)\b')
    content = re.sub(cmyk_pat, replacer_cmyk, content)

    if inject_bg_rect and w > 0 and h > 0:
        r, g_val, b = BG
        bg_bytes = (
            b"q\n"
            + f"{r:.4f} {g_val:.4f} {b:.4f} rg\n".encode()
            + f"0.00 0.00 {w:.2f} {h:.2f} re\n".encode()
            + b"f\nQ\n"
        )
        content = bg_bytes + content

    return content


def recolor_pdf(input_path: str, output_path: str, palette: dict = None,
                max_pages: int = None, inject_bg_rect: bool = True) -> None:
    import fitz
    src = fitz.open(input_path)
    pages = range(len(src)) if not max_pages else range(min(max_pages, len(src)))
    for i in pages:
        page = src[i]
        content = page.read_contents()
        w, h = page.rect.width, page.rect.height
        new_content = fast_regex_recolor(content, palette or {}, inject_bg_rect=inject_bg_rect, w=w, h=h)
        xref = page.get_contents()[0] if page.get_contents() else 0
        if xref:
            src.update_stream(xref, new_content)

    src.save(output_path, deflate=True)
    src.close()
