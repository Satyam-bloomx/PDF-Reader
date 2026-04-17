"""
PDF vector-level recoloring using pikepdf content stream manipulation.

Strategy: parse every PDF content stream operator and remap color values
so the page gets a dark navy theme while preserving all vector/text quality.

Color mapping:
  - White / near-white background fills → deep navy BG
  - Black / near-black strokes & fills  → warm parchment TEXT
  - Red / orange / gold tones           → antique gold GOLD
  - Blue / purple tones                 → soft violet VIOLET
  - Green tones                         → teal TEAL
"""

import pikepdf
from pikepdf import Array
from typing import Tuple

# ── Default palette (0-1 float RGB) — overridden per job via recolor_pdf() ────
BG     = (0.502, 0.027, 0.063)   # #800110 deep maroon
TEXT   = (1.000, 0.980, 0.820)   # #fff9d1 warm gold-white
GOLD   = (1.000, 0.843, 0.353)   # #ffd85a bright gold
VIOLET = (1.000, 0.700, 0.400)   # #ffb266 warm orange
TEAL   = (1.000, 0.900, 0.600)   # #ffe699 light gold-yellow
CORAL  = (1.000, 0.780, 0.580)   # #ffc794 warm peach


def _set_palette(palette: dict) -> None:
    """Override module-level palette globals from a dict of (r,g,b) tuples."""
    global BG, TEXT, GOLD, VIOLET, TEAL, CORAL
    if "bg"     in palette: BG     = palette["bg"]
    if "text"   in palette: TEXT   = palette["text"]
    if "gold"   in palette: GOLD   = palette["gold"]
    if "violet" in palette: VIOLET = palette["violet"]
    if "teal"   in palette: TEAL   = palette["teal"]
    if "coral"  in palette: CORAL  = palette["coral"]


def _map_rgb(r: float, g: float, b: float, for_text: bool = False,
             for_stroke: bool = False) -> Tuple[float, float, float]:
    """
    Map a single RGB color (0-1 each) to the dark theme palette.
    for_text=True : inside a BT/ET text block → TEXT color.
    for_stroke=True: stroke operators (G/RG/K) outside text → TEXT color so
                     lines/borders remain visible on the dark background.
    for_text=False, for_stroke=False: fill operators → achromatic/lightly-
                     colored fills collapse to BG (higher sat threshold 0.55
                     catches lightly-tinted chart cell backgrounds).
    Returns new (r, g, b).
    """
    # Compute rough luminance and saturation
    mx = max(r, g, b)
    mn = min(r, g, b)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = (mx - mn) / (mx + 1e-6)

    # ── Stroke (lines/borders): always TEXT color ─────────────────────────
    if for_stroke:
        return TEXT

    # ── Text block: vivid colored text (e.g. red dates/headers) → accent ──
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
        # ── Duotone Interpolation for Neutral Colors ──────────────────
        # Maps Black -> TEXT, White -> BG, and Grays to the space between.
        # This preserves watermarks/patterns instead of deleting them.
        res_r = TEXT[0] + lum * (BG[0] - TEXT[0])
        res_g = TEXT[1] + lum * (BG[1] - TEXT[1])
        res_b = TEXT[2] + lum * (BG[2] - TEXT[2])
        return (res_r, res_g, res_b)

    # ── Vivid colored fill — pick accent by hue ───────────────────────────
    delta = mx - mn + 1e-6
    if mx == r:
        hue = ((g - b) / delta) % 6
    elif mx == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    hue /= 6.0  # 0-1

    if hue < 0.18 or hue > 0.88:
        accent = GOLD
    elif 0.18 <= hue < 0.55:
        accent = TEAL
    elif 0.55 <= hue <= 0.85:
        accent = VIOLET
    else:
        accent = CORAL

    # Full-saturation → accent; partial saturation fades toward the duotone base
    mix = min((sat - 0.4) / 0.6, 1.0)   # 0 at sat=0.4, 1 at sat=1.0
    grey_r = TEXT[0] + lum * (BG[0] - TEXT[0])
    grey_g = TEXT[1] + lum * (BG[1] - TEXT[1])
    grey_b = TEXT[2] + lum * (BG[2] - TEXT[2])

    return (
        (1 - mix) * grey_r + mix * accent[0],
        (1 - mix) * grey_g + mix * accent[1],
        (1 - mix) * grey_b + mix * accent[2],
    )


def _to_pdf_num(v: float) -> float:
    """Round float for use as a PDF operand."""
    return round(float(v), 4)


def _remap_color_args(operands: list, n: int, for_text: bool = False,
                      for_stroke: bool = False) -> list:
    """
    Given the last `n` operands (floats) as an RGB or grey color,
    return remapped operands as pikepdf Objects.
    """
    nums = [float(op) for op in operands[-n:]]
    prefix = list(operands[:-n])

    if n == 1:
        # Grey → treat as (g, g, g)
        g = nums[0]
        r2, g2, b2 = _map_rgb(g, g, g, for_text=for_text, for_stroke=for_stroke)
        if for_text or for_stroke:
            # Result may not be grey — switch to RGB operator downstream
            new_nums = [r2, g2, b2]
            return prefix + [_to_pdf_num(v) for v in new_nums]
        lum2 = 0.299 * r2 + 0.587 * g2 + 0.114 * b2
        new_nums = [lum2]
    elif n == 3:
        r2, g2, b2 = _map_rgb(nums[0], nums[1], nums[2], for_text=for_text,
                               for_stroke=for_stroke)
        new_nums = [r2, g2, b2]
    elif n == 4:
        # CMYK → convert to RGB first, remap, output as RGB
        c, m, y, k = nums
        r = (1 - c) * (1 - k)
        g = (1 - m) * (1 - k)
        b = (1 - y) * (1 - k)
        r2, g2, b2 = _map_rgb(r, g, b, for_text=for_text, for_stroke=for_stroke)
        new_nums = [r2, g2, b2]
    else:
        return operands

    return prefix + [_to_pdf_num(v) for v in new_nums]


# Map PDF color operators → (components, is_cmyk, is_stroke)
_COLOR_OPS = {
    b"g":   (1, False, False),   # fill grey
    b"G":   (1, False, True),    # stroke grey
    b"rg":  (3, False, False),   # fill RGB
    b"RG":  (3, False, True),    # stroke RGB
    b"k":   (4, True,  False),   # fill CMYK
    b"K":   (4, True,  True),    # stroke CMYK
}


def _recolor_array(arr: "np.ndarray") -> "np.ndarray":
    """Vectorized recolor of a float32 RGB array (values 0-1). Returns same shape."""
    import numpy as np
    r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    lum = 0.299*r + 0.587*g + 0.114*b
    sat = (mx - mn) / (mx + 1e-6)

    BG_a  = np.array(BG,    dtype=np.float32)
    TEXT_a= np.array(TEXT,  dtype=np.float32)
    GOLD_a= np.array(GOLD,  dtype=np.float32)
    VIO_a = np.array(VIOLET,dtype=np.float32)
    TEAL_a= np.array(TEAL,  dtype=np.float32)
    COR_a = np.array(CORAL, dtype=np.float32)

    # Low-saturation (achromatic + lightly-tinted) -> Duotone interpolation
    # Maps original luminance 0..1 to a gradient between TEXT_a and BG_a.
    # This ensures watermarks and light patterns are preserved.
    lum_3d = lum[:,:,None]
    out = TEXT_a + lum_3d * (BG_a - TEXT_a)

    # Vivid colored pixels (sat > 0.4) → accent color
    color_mask = sat >= 0.4
    if color_mask.any():
        delta = mx - mn + 1e-6
        hue = np.zeros(arr.shape[:2], dtype=np.float32)
        mr, mg_m, mb_m = (mx==r), (mx==g), (mx==b)
        hue[mr]  = ((g[mr]  - b[mr])  / delta[mr])  % 6
        hue[mg_m]= (b[mg_m] - r[mg_m])/ delta[mg_m] + 2
        hue[mb_m]= (r[mb_m] - g[mb_m])/ delta[mb_m] + 4
        hue /= 6.0

        warm  = (hue < 0.18) | (hue > 0.88)
        green = (hue >= 0.18) & (hue < 0.55)
        cool  = (hue >= 0.55) & (hue <= 0.85)

        accent = np.where(warm[:,:,None],  GOLD_a,
                 np.where(green[:,:,None], TEAL_a,
                 np.where(cool[:,:,None],  VIO_a, COR_a)))

        # Fade in accent from sat=0.4 to sat=1.0
        mix = np.clip((sat - 0.4) / 0.6, 0, 1)[:,:,None]
        grey = TEXT_a + lum[:,:,None] * (BG_a - TEXT_a)
        out[color_mask] = ((1-mix)*grey + mix*accent)[color_mask]

    return np.clip(out, 0, 1)


def _decode_image_xobj(xobj: pikepdf.Object):
    """Decode an Image XObject to a PIL Image. Returns None on failure."""
    from pikepdf import PdfImage
    try:
        return PdfImage(xobj).as_pil_image().convert("RGB")
    except Exception as e:
        print(f"[recolor img] could not decode: {e}")
        return None


def _recolor_image_xobj(xobj: pikepdf.Object) -> None:
    """Recolor an embedded Image XObject in-place using PIL pixel mapping."""
    import io
    import numpy as np
    from PIL import Image

    img = _decode_image_xobj(xobj)
    if img is None:
        return

    # Downsample large decorative images before recoloring — saves time, no visible quality loss
    max_dim = 800
    if img.width > max_dim or img.height > max_dim:
        scale = max_dim / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)

    arr = np.array(img, dtype=np.float32) / 255.0
    out = _recolor_array(arr)

    out_img = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    new_data = buf.read()

    xobj.write(new_data, filter=pikepdf.Name("/DCTDecode"))
    for key in ["/DecodeParms", "/SMask", "/Mask", "/ColorSpace"]:
        if key in xobj:
            del xobj[key]
    xobj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    xobj["/BitsPerComponent"] = 8
    xobj["/Width"] = out_img.width
    xobj["/Height"] = out_img.height


def _recolor_xobjects(resources: pikepdf.Object, _pdf: pikepdf.Pdf, visited: set, used_keys: set = None) -> None:
    """Recursively recolor all XObjects (Form + Image) in a resource dictionary."""
    if resources is None:
        return
    xobj_dict = resources.get("/XObject")
    if xobj_dict is None:
        return
    for key in xobj_dict.keys():
        if used_keys is not None and str(key) not in used_keys:
            continue
        xobj = xobj_dict[key]
        objid = xobj.objgen  # use PDF object number — shared objects recolored once
        if objid in visited:
            continue
        visited.add(objid)
        subtype = xobj.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            _recolor_image_xobj(xobj)


def recolor_page_stream(page: pikepdf.Page, pdf: pikepdf.Pdf, visited_xobjs: set = None, inject_bg_rect: bool = True) -> None:
    """
    Recolor a single pikepdf Page in-place by rewriting its content stream.
    Injects navy background rect at the start, then remaps all color operators.
    """
    mb = page.mediabox
    x0, y0 = float(mb[0]), float(mb[1])
    x1, y1 = float(mb[2]), float(mb[3])
    w, h = x1 - x0, y1 - y0

    # Background rect as raw bytes — simplest, most cross-viewer compatible
    r, g, b = BG
    bg_bytes = b""
    if inject_bg_rect:
        bg_bytes = (
            b"q\n"
            + f"{r:.4f} {g:.4f} {b:.4f} rg\n".encode()
            + f"{x0:.4f} {y0:.4f} {w:.4f} {h:.4f} re\n".encode()
            + b"f\n"
            + b"Q\n"
        )

    if visited_xobjs is None:
        visited_xobjs = set()
    resources = page.obj.get("/Resources")

    instructions = []
    in_text = False  # track BT/ET text blocks
    used_xobjs = set()  # track which XObjects are actually drawn

    for operands, operator in pikepdf.parse_content_stream(page):
        op = bytes(operator)

        if op == b"Do" and len(operands) > 0 and isinstance(operands[0], pikepdf.Name):
            used_xobjs.add(str(operands[0]))

        if op == b"BT":
            in_text = True
            # Inject default TEXT fill color at start of every text block
            # so glyphs without an explicit color are visible
            r, g, b = TEXT
            instructions.append(([_to_pdf_num(v) for v in (r, g, b)], pikepdf.Operator("rg")))
            instructions.append((operands, operator))
            continue
        elif op == b"ET":
            in_text = False

        if op in _COLOR_OPS or op in (b"sc", b"scn", b"SC", b"SCN"):
            if op in (b"sc", b"scn", b"SC", b"SCN"):
                n = len(operands)
                if n > 0 and isinstance(operands[-1], pikepdf.Name):
                    instructions.append((operands, operator))
                    continue
                if n in (1, 3, 4):
                    is_stroke = op in (b"SC", b"SCN")
                    new_operands = _remap_color_args(list(operands), n, for_text=in_text, for_stroke=(is_stroke and not in_text))
                    if len(new_operands) == 3 and n in (1, 4):
                        new_op = pikepdf.Operator("rg") if not is_stroke else pikepdf.Operator("RG")
                        instructions.append((new_operands, new_op))
                    else:
                        instructions.append((new_operands, operator))
                else:
                    instructions.append((operands, operator))
            else:
                n, is_cmyk, is_stroke = _COLOR_OPS[op]
                new_operands = _remap_color_args(list(operands), n,
                                                 for_text=in_text,
                                                 for_stroke=(is_stroke and not in_text))
                needs_rgb = (in_text or (is_stroke and not in_text)) and op in (b"g", b"G")
                if needs_rgb and len(new_operands) == 3:
                    new_op = pikepdf.Operator("rg") if op == b"g" else pikepdf.Operator("RG")
                    instructions.append((new_operands, new_op))
                elif is_cmyk:
                    new_op = pikepdf.Operator("rg") if op == b"k" else pikepdf.Operator("RG")
                    instructions.append((new_operands, new_op))
                else:
                    instructions.append((new_operands, operator))
        else:
            instructions.append((operands, operator))

    # Recolor all XObjects that are actually drawn on the page
    _recolor_xobjects(resources, pdf, visited_xobjs, used_keys=used_xobjs)

    recolored_bytes = pikepdf.unparse_content_stream(instructions)

    # Two separate stream objects in an Array — PDF spec §7.8.3
    # This is the correct way to prepend content; all compliant viewers support it
    combined_bytes = bg_bytes + recolored_bytes
    page.obj["/Contents"] = pdf.make_stream(combined_bytes)


def add_background_rect(page: pikepdf.Page, pdf: pikepdf.Pdf) -> None:
    """
    Prepend a navy background rect to the page so areas with no fill show navy
    rather than transparent/white.
    """
    mb = page.mediabox
    x0 = float(mb[0])
    y0 = float(mb[1])
    x1 = float(mb[2])
    y1 = float(mb[3])

    r, g, b = BG
    # Deep cosmic background
    bg_stream = (
        f"q "
        f"{r:.4f} {g:.4f} {b:.4f} rg "
        f"{x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f} re f "
        f"Q "
    ).encode()

    existing = page.obj.get("/Contents")
    bg_obj = pdf.make_stream(bg_stream)

    if existing is None:
        page.obj["/Contents"] = bg_obj
    elif isinstance(existing, Array):
        existing.insert(0, bg_obj)
        page.obj["/Contents"] = existing
    else:
        page.obj["/Contents"] = Array([bg_obj, existing])


def recolor_pdf(input_path: str, output_path: str, palette: dict = None, max_pages: int = None, inject_bg_rect: bool = True) -> None:
    """
    Open a PDF, recolor every page at vector level, write output.
    palette: optional dict with keys bg, text, gold, violet, teal, coral → (r,g,b) tuples
    max_pages: if set, only recolor the first N pages (useful for preview).
    """
    if palette:
        _set_palette(palette)

def fast_regex_recolor(content: bytes, palette: dict, inject_bg_rect: bool = True, w: float = 0, h: float = 0) -> bytes:
    import re
    _set_palette(palette)

    # We must match standard numbers and scientific notation just in case, though standard numbers are 99%
    # Float regex: \-?[0-9]*\.?[0-9]+
    num_re = rb'\-?[0-9]*\.?[0-9]+'
    
    def replacer_rgb(m):
        try:
            r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
            is_stroke = (m.group(4) == b'RG')
            # in regex we don't safely track in_text across blocks reliably if BT/ET are complex, 
            # so we use for_text=False which resolves properly for duotone text as well.
            r2, g2, b2 = _map_rgb(r, g, b, for_text=False, for_stroke=is_stroke)
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {m.group(4).decode()}".encode()
        except: return m.group(0)

    # rgb matches: 1 1 1 rg
    rgb_pat = rb'\b(' + num_re + rb')\s+(' + num_re + rb')\s+(' + num_re + rb')\s+(rg|RG)\b'
    content = re.sub(rgb_pat, replacer_rgb, content)
    
    def replacer_g(m):
        try:
            g_val = float(m.group(1))
            is_stroke = (m.group(2) == b'G')
            r2, g2, b2 = _map_rgb(g_val, g_val, g_val, for_text=False, for_stroke=is_stroke)
            op = b"RG" if is_stroke else b"rg"
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {op.decode()}".encode()
        except: return m.group(0)

    # grey matches: 1 g
    g_pat = rb'\b(' + num_re + rb')\s+(g|G)\b'
    content = re.sub(g_pat, replacer_g, content)
    
    def replacer_cmyk(m):
        try:
            c, m_val, y, k = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
            is_stroke = (m.group(5) == b'K')
            r = (1 - c) * (1 - k)
            g_val = (1 - m_val) * (1 - k)
            b = (1 - y) * (1 - k)
            r2, g2, b2 = _map_rgb(r, g_val, b, for_text=False, for_stroke=is_stroke)
            op = b"RG" if is_stroke else b"rg"
            return f"{r2:.4f} {g2:.4f} {b2:.4f} {op.decode()}".encode()
        except: return m.group(0)
        
    cmyk_pat = rb'\b(' + num_re + rb')\s+(' + num_re + rb')\s+(' + num_re + rb')\s+(' + num_re + rb')\s+(k|K)\b'
    content = re.sub(cmyk_pat, replacer_cmyk, content)

    # Inject BG rect
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

def recolor_pdf(input_path: str, output_path: str, palette: dict = None, max_pages: int = None, inject_bg_rect: bool = True) -> None:
    import fitz
    src = fitz.open(input_path)
    pages = range(len(src)) if not max_pages else range(min(max_pages, len(src)))
    for i in pages:
        page = src[i]
        content = page.read_contents()
        w, h = page.rect.width, page.rect.height
        new_content = fast_regex_recolor(content, palette, inject_bg_rect=inject_bg_rect, w=w, h=h)
        xref = page.get_contents()[0] if page.get_contents() else 0
        if xref:
            src.update_stream(xref, new_content)
            
    src.save(output_path, deflate=True)
    src.close()
