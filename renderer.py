"""Draws certificates and ID cards. No photos are ever placed on certificates."""
from __future__ import annotations

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

CERT_SIZE = (1754, 1240)   # A4 landscape @150dpi
ID_SIZE = (1012, 638)      # CR80 card @300dpi

DESIGNS = {
    "Classic Gold":  {"style": "classic",  "bg": "#FFFBF0", "primary": "#B8860B", "secondary": "#1F2937", "text": "#1F2937"},
    "Modern Blue":   {"style": "modern",   "bg": "#FFFFFF", "primary": "#1D4ED8", "secondary": "#0F172A", "text": "#0F172A"},
    "Elegant Green": {"style": "elegant",  "bg": "#F7FAF7", "primary": "#166534", "secondary": "#B45309", "text": "#14231A"},
    "Bold Red":      {"style": "bold",     "bg": "#FFFFFF", "primary": "#B91C1C", "secondary": "#111827", "text": "#111827"},
    "Midnight":      {"style": "midnight", "bg": "#0B1220", "primary": "#D4AF37", "secondary": "#F5D98B", "text": "#E5E7EB"},
    "Ribbon":        {"style": "ribbon",   "bg": "#FFFFFF", "primary": "#0F766E", "secondary": "#E11D48", "text": "#1F2937"},
    "Minimal":       {"style": "minimal",  "bg": "#FFFFFF", "primary": "#111827", "secondary": "#374151", "text": "#1F2937"},
    "Framed":        {"style": "framed",   "bg": "#FFFFFF", "primary": "#1E3A8A", "secondary": "#B8860B", "text": "#1F2937"},
    "Circles":       {"style": "circles",  "bg": "#FFFFFF", "primary": "#6D28D9", "secondary": "#C2410C", "text": "#1F2937"},
}

PALETTES = {  # name: (main colour, second colour)
    "Navy & Gold": ("#1E3A8A", "#B8860B"),
    "Royal Blue": ("#1D4ED8", "#0F172A"),
    "Emerald": ("#047857", "#B45309"),
    "Burgundy & Gold": ("#7F1D1D", "#B8860B"),
    "Purple & Gold": ("#6D28D9", "#B8860B"),
    "Teal & Coral": ("#0F766E", "#E11D48"),
    "Charcoal & Orange": ("#1F2937", "#EA580C"),
    "Black & Gold": ("#111827", "#B8860B"),
    "Sky & Navy": ("#0284C7", "#0C4A6E"),
    "Rose & Plum": ("#BE185D", "#4C1D95"),
    "Forest & Bronze": ("#14532D", "#A16207"),
}

PAPERS = {  # name: colour
    "White": "#FFFFFF", "Cream": "#FFF8E7", "Light grey": "#F3F4F6",
    "Light blue": "#EFF6FF", "Light green": "#F0FDF4", "Dark navy": "#0B1220",
}


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(hex_color: str) -> str:
    """White text on dark colours, dark text on light colours."""
    return "#FFFFFF" if luminance(hex_color) < 0.6 else "#111827"


def auto_text(bg: str) -> str:
    return "#F1F5F9" if luminance(bg) < 0.4 else "#1F2937"


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ---------------------------------------------------------------- helpers
def load_font(path, size):
    try:
        if path:
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fsize(font) -> int:
    return int(getattr(font, "size", 20))


def fit_font(draw, s, path, size, max_w, min_size=22):
    f = load_font(path, size)
    while draw.textlength(s, font=f) > max_w and size > min_size:
        size -= 4
        f = load_font(path, size)
    return f


def wrap(draw, s, font, max_w):
    lines = []
    for para in s.split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=font) <= max_w:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def center(draw, cx, y, s, font, fill):
    w = draw.textlength(s, font=font)
    draw.text((cx - w / 2, y), s, font=font, fill=fill)
    return y + int(_fsize(font) * 1.3)


def paste_logo(img, logo, cx, y, max_h, max_w=420):
    if logo is None:
        return y
    lg = logo.copy().convert("RGBA")
    lg.thumbnail((max_w, max_h))
    img.paste(lg, (int(cx - lg.width / 2), y), lg)
    return y + lg.height + 10


def make_qr(url: str, color: str = "#111111") -> Image.Image:
    qr = qrcode.QRCode(box_size=8, border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    im = qr.make_image(fill_color=color, back_color="white")
    im = im.get_image() if hasattr(im, "get_image") else im
    return im.convert("RGB")


def _body_text(template: str, rec: dict, doc_id: str) -> str:
    data = _Safe(
        name=rec.get("name", ""), course=rec.get("course", ""), date=rec.get("date", ""),
        role=rec.get("role", ""), department=rec.get("department", ""), id=doc_id,
    )
    try:
        return template.format_map(data)
    except Exception:
        return template


# ---------------------------------------------------------------- certificate
def _decor(d, W, H, style, P, S, bg):
    """Draws the design's decoration. Returns the left margin used by the content."""
    corners = [(0, 0), (1, 0), (0, 1), (1, 1)]
    left = 0
    if style == "classic":
        d.rectangle([40, 40, W - 40, H - 40], outline=P, width=10)
        d.rectangle([70, 70, W - 70, H - 70], outline=P, width=3)
    elif style == "modern":
        d.rectangle([0, 0, W, 60], fill=P)
        d.rectangle([0, 60, W, 70], fill=S)
        d.rectangle([0, H - 60, W, H], fill=P)
        d.rectangle([0, H - 70, W, H - 60], fill=S)
    elif style == "elegant":
        d.rectangle([50, 50, W - 50, H - 50], outline=P, width=4)
        d.rectangle([80, 80, W - 80, H - 80], outline=S, width=2)
        for cx_, cy_ in corners:
            x, y = 50 + cx_ * (W - 100), 50 + cy_ * (H - 100)
            d.rectangle([x - 18, y - 18, x + 18, y + 18], fill=S)
    elif style == "bold":
        d.rectangle([0, 0, 90, H], fill=P)
        d.rectangle([90, 0, 110, H], fill=S)
        d.rectangle([W - 30, 0, W, H], fill=P)
        left = 110
    elif style == "midnight":
        d.rectangle([40, 40, W - 40, H - 40], outline=P, width=6)
        d.rectangle([62, 62, W - 62, H - 62], outline=P, width=2)
        for cx_, cy_ in corners:
            x, y = 40 + cx_ * (W - 80), 40 + cy_ * (H - 80)
            d.rectangle([x - 14, y - 14, x + 14, y + 14], fill=P)
    elif style == "ribbon":
        d.polygon([(0, 0), (430, 0), (0, 430)], fill=P)
        d.polygon([(0, 0), (270, 0), (0, 270)], fill=S)
        d.polygon([(W, H), (W - 430, H), (W, H - 430)], fill=P)
        d.polygon([(W, H), (W - 270, H), (W, H - 270)], fill=S)
    elif style == "minimal":
        d.rectangle([60, 60, W - 60, H - 60], outline=S, width=2)
        d.rectangle([W / 2 - 90, 92, W / 2 + 90, 100], fill=P)
    elif style == "framed":
        d.rectangle([0, 0, W, H], fill=P)
        d.rectangle([45, 45, W - 45, H - 45], fill=bg)
        d.rectangle([75, 75, W - 75, H - 75], outline=S, width=3)
    elif style == "circles":
        d.ellipse([-240, -240, 240, 240], fill=P)
        d.ellipse([-150, -150, 150, 150], fill=S)
        d.ellipse([W - 240, H - 240, W + 240, H + 240], fill=P)
        d.ellipse([W - 150, H - 150, W + 150, H + 150], fill=S)
    return left


def _signature(img, d, cx, line_y, sig, labelp, S, T):
    d.line([cx - 150, line_y, cx + 150, line_y], fill=S, width=2)
    if sig.get("image") is not None:
        im = sig["image"].copy().convert("RGBA")
        im.thumbnail((280, 90))
        img.paste(im, (int(cx - im.width / 2), line_y - im.height - 4), im)
    if sig.get("name"):
        center(d, cx, line_y + 8, sig["name"], fit_font(d, sig["name"], labelp, 30, 300, 18), S)
    if sig.get("title"):
        center(d, cx, line_y + 44, sig["title"], fit_font(d, sig["title"], labelp, 24, 300, 16), T)


def draw_certificate(rec: dict, cfg: dict, doc_id: str, verify_url: str) -> Image.Image:
    W, H = CERT_SIZE
    P, S, T = cfg["primary"], cfg["secondary"], cfg["text"]
    style = cfg["style"]
    f = cfg["fonts"]
    img = Image.new("RGB", (W, H), cfg["bg"])
    d = ImageDraw.Draw(img)

    left = _decor(d, W, H, style, P, S, cfg["bg"])
    cx = (W + left) / 2
    content_w = W - left - 420

    sigs = [s for s in cfg.get("signatories", []) if s.get("name") or s.get("title") or s.get("image") is not None]
    two = len(sigs) == 2

    y = 120
    y = paste_logo(img, cfg.get("logo"), cx, y, 110)
    if cfg.get("company"):
        y = center(d, cx, y, cfg["company"].upper(), fit_font(d, cfg["company"].upper(), f["company"], 34, content_w, 18), S)
    y += 10

    y = center(d, cx, y, cfg["title"], fit_font(d, cfg["title"], f["title"], 96, content_w), P)
    y = center(d, cx, y + 5, cfg["intro"], load_font(f["body"], 36), T)

    name = rec.get("name", "")
    nf = fit_font(d, name, f["name"], 112, content_w)
    ny = y + 10
    center(d, cx, ny, name, nf, S)
    uy = ny + int(_fsize(nf) * 1.28)
    uw = min(max(d.textlength(name, font=nf) + 120, 500), W - left - 300)
    d.line([cx - uw / 2, uy, cx + uw / 2, uy], fill=P, width=4)
    y = uy + 28

    body = _body_text(cfg["body"], rec, doc_id)
    bottom_limit = H - (420 if two else 360)
    size = 40
    while True:
        bf = load_font(f["body"], size)
        lines = wrap(d, body, bf, content_w - 100)
        lh = int(size * 1.4)
        if (len(lines) * lh <= bottom_limit - y) or size <= 22:
            break
        size -= 2
    for ln in lines:
        center(d, cx, y, ln, bf, T)
        y += lh

    qy = H - 360 if style == "modern" else H - 350
    img.paste(make_qr(verify_url).resize((170, 170)), (int(cx - 85), qy))
    center(d, cx, qy + 176, doc_id, load_font(f["label"], 24), T)
    center(d, cx, qy + 204, "Scan to verify", load_font(f["label"], 22), T)

    line_y = H - 200
    lcx, rcx = left + 380, W - 380
    date = rec.get("date", "")
    if two:
        center(d, cx, qy - 50, f"Issued on {date}", load_font(f["label"], 30), T)
        _signature(img, d, lcx, line_y, sigs[0], f["label"], S, T)
        _signature(img, d, rcx, line_y, sigs[1], f["label"], S, T)
    else:
        d.line([lcx - 150, line_y, lcx + 150, line_y], fill=S, width=2)
        center(d, lcx, line_y - 55, date, load_font(f["label"], 32), T)
        center(d, lcx, line_y + 8, "Date", load_font(f["label"], 26), S)
        if sigs:
            _signature(img, d, rcx, line_y, sigs[0], f["label"], S, T)
    return img


# ---------------------------------------------------------------- ID card
def draw_id_card(rec: dict, cfg: dict, doc_id: str, verify_url: str, photo=None) -> Image.Image:
    W, H = ID_SIZE
    P, S, T = cfg["primary"], cfg["secondary"], cfg["text"]
    f = cfg["fonts"]
    img = Image.new("RGB", (W, H), cfg["bg"])
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 130], fill=P)
    d.rectangle([0, H - 70, W, H], fill=S)
    x_text = 50
    if cfg.get("logo") is not None:
        lg = cfg["logo"].copy().convert("RGBA")
        lg.thumbnail((200, 90))
        img.paste(lg, (W - lg.width - 40, int((130 - lg.height) / 2)), lg)
    company = (cfg.get("company") or "").upper() or "IDENTITY CARD"
    d.text((50, 38), company, font=fit_font(d, company, f["company"], 50, W - 330), fill=contrast(P))

    if photo is not None:
        ph = ImageOps.fit(photo, (180, 220))
        img.paste(ph, (50, 170))
        d.rectangle([50, 170, 230, 390], outline=P, width=4)
        x_text = 260

    max_w = W - 260 - x_text - 20
    name = rec.get("name", "")
    nf = fit_font(d, name, f["name"], 56, max_w, 26)
    d.text((x_text, 175), name, font=nf, fill=S)
    y = 175 + int(_fsize(nf) * 1.35)
    if rec.get("role"):
        rf = fit_font(d, rec["role"], f["body"], 34, max_w, 20)
        d.text((x_text, y), rec["role"], font=rf, fill=P)
        y += 52
    sf = load_font(f["body"], 28)
    for label, val in (("Dept", rec.get("department")), ("ID No", rec.get("id_number"))):
        if val:
            d.text((x_text, y), f"{label}: {val}", font=sf, fill=T)
            y += 42
    d.text((x_text, y), f"Issued: {rec.get('date', '')}", font=sf, fill=T)

    img.paste(make_qr(verify_url).resize((200, 200)), (W - 240, 185))
    d.text((W - 240, 392), "Scan to verify", font=load_font(f["label"], 22), fill=T)
    d.text((40, H - 50), doc_id, font=load_font(f["label"], 26), fill=contrast(S))
    return img
