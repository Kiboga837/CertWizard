"""Draws certificates and ID cards. No photos are ever placed on certificates."""
from __future__ import annotations

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

CERT_SIZE = (1754, 1240)   # A4 landscape @150dpi
ID_SIZE = (1012, 638)      # CR80 card @300dpi

DESIGNS = {
    "Classic Gold":  {"style": "classic", "bg": "#FFFBF0", "primary": "#B8860B", "secondary": "#1F2937", "text": "#1F2937"},
    "Modern Blue":   {"style": "modern",  "bg": "#FFFFFF", "primary": "#1D4ED8", "secondary": "#0F172A", "text": "#0F172A"},
    "Elegant Green": {"style": "elegant", "bg": "#F7FAF7", "primary": "#166534", "secondary": "#B45309", "text": "#14231A"},
    "Bold Red":      {"style": "bold",    "bg": "#FFFFFF", "primary": "#B91C1C", "secondary": "#111827", "text": "#111827"},
}


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


def make_qr(url: str, color: str = "#000000") -> Image.Image:
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
def draw_certificate(rec: dict, cfg: dict, doc_id: str, verify_url: str) -> Image.Image:
    W, H = CERT_SIZE
    P, S, T = cfg["primary"], cfg["secondary"], cfg["text"]
    style = cfg["style"]
    img = Image.new("RGB", (W, H), cfg["bg"])
    d = ImageDraw.Draw(img)

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
        for x, y in [(50, 50), (W - 50, 50), (50, H - 50), (W - 50, H - 50)]:
            d.rectangle([x - 18, y - 18, x + 18, y + 18], fill=S)
    else:  # bold
        d.rectangle([0, 0, 90, H], fill=P)
        d.rectangle([90, 0, 110, H], fill=S)
        d.rectangle([W - 30, 0, W, H], fill=P)
        left = 110
    cx = (W + left) / 2
    content_w = W - left - 420

    hp, bp = cfg["heading_font"], cfg["body_font"]
    y = 120
    y = paste_logo(img, cfg.get("logo"), cx, y, 110)
    if cfg.get("company"):
        y = center(d, cx, y, cfg["company"].upper(), load_font(bp, 34), S)
    y += 10

    y = center(d, cx, y, cfg["title"], fit_font(d, cfg["title"], hp, 96, content_w), P)
    y = center(d, cx, y + 5, cfg["intro"], load_font(bp, 36), T)

    name = rec.get("name", "")
    nf = fit_font(d, name, hp, 112, content_w)
    ny = y + 10
    center(d, cx, ny, name, nf, S)
    uy = ny + int(_fsize(nf) * 1.28)
    uw = min(max(d.textlength(name, font=nf) + 120, 500), W - left - 300)
    d.line([cx - uw / 2, uy, cx + uw / 2, uy], fill=P, width=4)
    y = uy + 28

    body = _body_text(cfg["body"], rec, doc_id)
    bottom_limit = H - 360
    size = 40
    while True:
        bf = load_font(bp, size)
        lines = wrap(d, body, bf, content_w - 100)
        lh = int(size * 1.4)
        if (len(lines) * lh <= bottom_limit - y) or size <= 22:
            break
        size -= 2
    for ln in lines:
        center(d, cx, y, ln, bf, T)
        y += lh

    # QR + id (centre bottom)
    qr = make_qr(verify_url, S).resize((170, 170))
    qy = H - 350 if style != "modern" else H - 360
    img.paste(qr, (int(cx - 85), qy))
    center(d, cx, qy + 176, doc_id, load_font(bp, 24), T)
    center(d, cx, qy + 204, "Scan to verify", load_font(bp, 22), T)

    # date (left) and signature (right)
    line_y = H - 200
    lx = left + 230
    d.line([lx, line_y, lx + 300, line_y], fill=S, width=2)
    center(d, lx + 150, line_y - 55, rec.get("date", ""), load_font(bp, 32), T)
    center(d, lx + 150, line_y + 8, "Date", load_font(bp, 26), S)

    rx = W - 230 - 300
    d.line([rx, line_y, rx + 300, line_y], fill=S, width=2)
    if cfg.get("signatory"):
        center(d, rx + 150, line_y + 8, cfg["signatory"], load_font(bp, 30), S)
    if cfg.get("signatory_title"):
        center(d, rx + 150, line_y + 44, cfg["signatory_title"], load_font(bp, 24), T)
    return img


# ---------------------------------------------------------------- ID card
def draw_id_card(rec: dict, cfg: dict, doc_id: str, verify_url: str, photo=None) -> Image.Image:
    W, H = ID_SIZE
    P, S, T = cfg["primary"], cfg["secondary"], cfg["text"]
    img = Image.new("RGB", (W, H), cfg["bg"])
    d = ImageDraw.Draw(img)
    hp, bp = cfg["heading_font"], cfg["body_font"]

    d.rectangle([0, 0, W, 130], fill=P)
    d.rectangle([0, H - 70, W, H], fill=S)
    x_text = 50
    if cfg.get("logo") is not None:
        lg = cfg["logo"].copy().convert("RGBA")
        lg.thumbnail((200, 90))
        img.paste(lg, (W - lg.width - 40, int((130 - lg.height) / 2)), lg)
    company = (cfg.get("company") or "").upper() or "IDENTITY CARD"
    d.text((50, 38), company, font=fit_font(d, company, hp, 50, W - 330), fill="white")

    if photo is not None:
        ph = ImageOps.fit(photo, (180, 220))
        img.paste(ph, (50, 170))
        d.rectangle([50, 170, 230, 390], outline=P, width=4)
        x_text = 260

    max_w = W - 260 - x_text - 20
    name = rec.get("name", "")
    nf = fit_font(d, name, hp, 56, max_w, 26)
    d.text((x_text, 175), name, font=nf, fill=S)
    y = 175 + int(_fsize(nf) * 1.35)
    if rec.get("role"):
        rf = fit_font(d, rec["role"], bp, 34, max_w, 20)
        d.text((x_text, y), rec["role"], font=rf, fill=P)
        y += 52
    sf = load_font(bp, 28)
    for label, val in (("Dept", rec.get("department")), ("ID No", rec.get("id_number"))):
        if val:
            d.text((x_text, y), f"{label}: {val}", font=sf, fill=T)
            y += 42
    d.text((x_text, y), f"Issued: {rec.get('date', '')}", font=sf, fill=T)

    qr = make_qr(verify_url, S).resize((200, 200))
    img.paste(qr, (W - 240, 185))
    d.text((W - 240, 392), "Scan to verify", font=load_font(bp, 22), fill=T)
    d.text((40, H - 50), doc_id, font=load_font(bp, 26), fill="white")
    return img
