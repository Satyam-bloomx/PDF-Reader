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

    # ── Text block or stroke: render as TEXT color ────────────────────────
    if for_text or for_stroke:
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
    max_dim = 1200
    if img.width > max_dim or img.height > max_dim:
        scale = max_dim / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    arr = np.array(img, dtype=np.float32) / 255.0
    out = _recolor_array(arr)

    out_img = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    new_data = buf.read()

    xobj.write(new_data, filter=pikepdf.Name("/DCTDecode"))
    xobj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    xobj["/BitsPerComponent"] = 8


def _recolor_xobjects(resources: pikepdf.Object, _pdf: pikepdf.Pdf, visited: set) -> None:
    """Recursively recolor all XObjects (Form + Image) in a resource dictionary."""
    if resources is None:
        return
    xobj_dict = resources.get("/XObject")
    if xobj_dict is None:
        return
    for key in xobj_dict.keys():
        xobj = xobj_dict[key]
        objid = xobj.objgen  # use PDF object number — shared objects recolored once
        if objid in visited:
            continue
        visited.add(objid)
        subtype = xobj.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            _recolor_image_xobj(xobj)


def recolor_page_stream(page: pikepdf.Page, pdf: pikepdf.Pdf, visited_xobjs: set = None) -> None:
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
    in_text = False  # track BT/ET text blocks so text gets TEXT color, fills get BG
    for operands, operator in pikepdf.parse_content_stream(page):
        op = bytes(operator)
        if op == b"BT":
            in_text = True
            # Inject default TEXT color so text is visible even if no explicitly set
            r, g, b = TEXT
            instructions.append(([_to_pdf_num(v) for v in (r, g, b)], pikepdf.Operator("rg")))
            instructions.append((operands, operator))
            continue
        elif op == b"ET":
            in_text = False
        # No longer skip — transparent recoloring makes the interior see-through
        if op in _COLOR_OPS:
            n, is_cmyk, is_stroke = _COLOR_OPS[op]
            new_operands = _remap_color_args(list(operands), n,
                                             for_text=in_text,
                                             for_stroke=(is_stroke and not in_text))
            # Promote grey ops to RGB when they now carry a 3-component TEXT color
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

    # Recolor all XObjects
    _recolor_xobjects(resources, pdf, visited_xobjs)

    recolored_bytes = pikepdf.unparse_content_stream(instructions)

    # Two separate stream objects in an Array — PDF spec §7.8.3
    # This is the correct way to prepend content; all compliant viewers support it
    bg_obj = pdf.make_stream(bg_bytes)
    content_obj = pdf.make_stream(recolored_bytes)
    page.obj["/Contents"] = Array([bg_obj, content_obj])


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


def recolor_pdf(input_path: str, output_path: str, palette: dict = None) -> None:
    """
    Open a PDF, recolor every page at vector level, write output.
    Preserves all text as vectors — no rasterization.
    palette: optional dict with keys bg, text, gold, violet, teal, coral → (r,g,b) tuples
    """
    if palette:
        _set_palette(palette)

    with pikepdf.open(input_path) as pdf:
        visited_xobjs: set = set()
        for page in pdf.pages:
            try:
                recolor_page_stream(page, pdf, visited_xobjs)
            except Exception as e:
                print(f"[recolor] page failed: {e}")

        pdf.save(output_path, compress_streams=True)
