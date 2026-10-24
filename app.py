"""
🎓 Certificate & ID Card Maker  (Streamlit) - v2
-------------------------------------------------
* Choose: certificates + ID cards, certificates only, or ID cards only
* 7 certificate designs + 4 ID card designs, with a live gallery
* Company colours (palettes or your own), logo, custom fonts, custom background
* Edit the certificate body / ID fields once - every document follows it
* Every generated document is saved in a DATABASE; the QR code opens a
  verification page that checks the database automatically
  (SQLite by default, PostgreSQL for permanent storage via DATABASE_URL)

Run locally:  streamlit run app.py
"""

import hmac
import io
import math
import os
import re
import secrets
import tempfile
import zipfile
from datetime import date, datetime
from functools import lru_cache

import pandas as pd
import qrcode
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, and_,
                        create_engine, delete, or_, select, update)

st.set_page_config(page_title="Certificate & ID Card Maker", page_icon="🎓", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 1.2rem; max-width: 1300px;}
    .app-title {text-align:center; font-size:2.2rem; font-weight:800; color:#16296b; margin-bottom:0;}
    .app-sub {text-align:center; color:#555; margin-bottom:1rem;}
    </style>""",
    unsafe_allow_html=True,
)


def get_conf(name, default=""):
    """Read a setting from Streamlit secrets, then environment variables."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name, default)


# =============================================================================
# DATABASE (SQLAlchemy: works with SQLite and PostgreSQL)
# =============================================================================
metadata = MetaData()
documents = Table(
    "issued_documents", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(40), unique=True, nullable=False, index=True),
    Column("doc_type", String(20), nullable=False),
    Column("holder_name", String(200), nullable=False),
    Column("course", String(200)),
    Column("roll_no", String(100)),
    Column("issue_date", String(60)),
    Column("organization", String(200)),
    Column("status", String(20), nullable=False, default="valid"),
    Column("created_at", DateTime),
)


@st.cache_resource(show_spinner=False)
def get_engine():
    url = get_conf("DATABASE_URL") or "sqlite:///certificates.db"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, **kwargs)
    metadata.create_all(eng)
    return eng


def new_code(doc_type):
    return ("CERT-" if doc_type == "certificate" else "ID-") + secrets.token_hex(5).upper()


def db_register(items, org):
    """items = [(doc_type, record)]. Saves each in the database and returns the codes.
    The same person/course/type always keeps the same code (old QR codes stay valid)."""
    codes = []
    with get_engine().begin() as c:
        for dt, r in items:
            cond = and_(documents.c.doc_type == dt, documents.c.holder_name == r["name"],
                        documents.c.roll_no == r["roll"], documents.c.course == r["course"],
                        documents.c.organization == org)
            row = c.execute(select(documents.c.code).where(cond)).first()
            if row:
                c.execute(update(documents).where(documents.c.code == row[0]).values(issue_date=r["date"]))
                codes.append(row[0])
            else:
                code = new_code(dt)
                c.execute(documents.insert().values(
                    code=code, doc_type=dt, holder_name=r["name"], course=r["course"],
                    roll_no=r["roll"], issue_date=r["date"], organization=org,
                    status="valid", created_at=datetime.utcnow()))
                codes.append(code)
    return codes


def db_get(code):
    with get_engine().connect() as c:
        row = c.execute(select(documents).where(documents.c.code == code)).first()
    return dict(row._mapping) if row else None


def db_search(q="", doc_type="All", status="All", limit=2000):
    stmt = select(documents).order_by(documents.c.id.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(documents.c.holder_name.ilike(like), documents.c.roll_no.ilike(like),
                              documents.c.code.ilike(like), documents.c.course.ilike(like)))
    if doc_type != "All":
        stmt = stmt.where(documents.c.doc_type == doc_type)
    if status != "All":
        stmt = stmt.where(documents.c.status == status)
    with get_engine().connect() as c:
        rows = c.execute(stmt).fetchall()
    return pd.DataFrame([dict(r._mapping) for r in rows])


def db_set_status(codes, status):
    with get_engine().begin() as c:
        c.execute(update(documents).where(documents.c.code.in_(codes)).values(status=status))


def db_delete(codes):
    with get_engine().begin() as c:
        c.execute(delete(documents).where(documents.c.code.in_(codes)))


def verification_page(code):
    """Opened when somebody scans the QR code."""
    st.markdown('<div class="app-title">🔍 Document Verification</div>', unsafe_allow_html=True)
    code = code.strip().upper()
    try:
        rec = db_get(code)
    except Exception as e:
        st.error(f"Verification service unavailable: {e}")
        return
    if rec is None:
        st.error("❌ NOT FOUND - this code is not in our records. The document may be fake.")
    elif rec["status"] != "valid":
        st.warning("⚠️ REVOKED - this document was issued but is no longer valid.")
    else:
        kind = "Certificate" if rec["doc_type"] == "certificate" else "Student ID Card"
        st.success(f"✅ AUTHENTIC - this {kind} was issued by **{rec['organization']}**.")
    if rec:
        st.table(pd.DataFrame({
            "Field": ["Name", "Course", "Roll / ID No", "Date", "Issued by", "Verification code"],
            "Value": [rec["holder_name"], rec["course"], rec["roll_no"], rec["issue_date"],
                      rec["organization"], rec["code"]]}).set_index("Field"))
    st.divider()
    manual = st.text_input("Verify another code", placeholder="e.g. CERT-1A2B3C4D5E")
    if manual:
        st.query_params["verify"] = manual.strip().upper()
        st.rerun()


# =============================================================================
# COLOURS, FONTS, DRAWING HELPERS
# =============================================================================
PALETTES = {
    "Navy & Gold": ("#102A60", "#D4AF37", "#333333", "#FFFDF7"),
    "Royal Purple": ("#4B1D82", "#E0A526", "#2D2A32", "#FBF9FF"),
    "Emerald Green": ("#0B6B4D", "#C9A227", "#23332D", "#F8FFFB"),
    "Crimson Red": ("#9B1B30", "#C9A227", "#2B2B2B", "#FFFAF9"),
    "Ocean Teal": ("#0E7490", "#F59E0B", "#1F2937", "#F5FCFE"),
    "Charcoal & Orange": ("#1F2937", "#F97316", "#374151", "#FFFFFF"),
    "Custom": None,
}
WHITE = (255, 255, 255)


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def scheme_colors(P, scheme):
    """Colour roles used by the designs."""
    if scheme == "dark":
        bg = mix(P["primary"], (0, 0, 0), 0.55)
        c = dict(bg=bg, title=P["accent"], sub=WHITE, intro=mix(WHITE, bg, .25), name=WHITE,
                 body=mix(WHITE, bg, .1), hi=P["accent"], org=P["accent"], muted=mix(WHITE, bg, .4),
                 frame=P["accent"])
    else:
        c = dict(bg=P["bg"], title=P["primary"], sub=P["accent"], intro=mix(P["text"], P["bg"], .25),
                 name=P["primary"], body=P["text"], hi=P["primary"], org=P["primary"],
                 muted=mix(P["text"], P["bg"], .35), frame=P["primary"])
    c.update(light=WHITE, accent=P["accent"], primary=P["primary"])
    return c


FONTS = {
    ("sans", "regular"): ["arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    ("sans", "bold"): ["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    ("sans", "italic"): ["ariali.ttf", "LiberationSans-Italic.ttf", "DejaVuSans-Oblique.ttf"],
    ("serif", "regular"): ["times.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"],
    ("serif", "bold"): ["timesbd.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"],
    ("serif", "italic"): ["timesi.ttf", "LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf"],
}


@lru_cache(maxsize=1024)
def font(size, fam="sans", style="regular", custom=None):
    size = max(8, int(size))
    if custom:
        try:
            return ImageFont.truetype(custom, size)
        except Exception:
            pass
    for name in FONTS[(fam, style)] + FONTS[("sans", style)] + ["DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit(d, text, maxw, size, fam="sans", style="bold", cu=None, minsize=10):
    size = int(size)
    while size > minsize:
        f = font(size, fam, style, cu)
        if d.textlength(text, font=f) <= maxw:
            return f
        size -= 2
    return font(minsize, fam, style, cu)


def wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fmt_tpl(template, data):
    """Replace {name} {course} {roll} {date} {org} {code}; unknown braces are left alone."""
    return re.sub(r"\{(\w+)\}", lambda m: str(data.get(m.group(1), m.group(0))), template or "")


def rect(d, box, **kw):
    d.rectangle([round(v) for v in box], **kw)


def poly(d, pts, **kw):
    d.polygon(pts, **kw)


def draw_body(d, text, cx, y0, y1, maxw, size, fam_b, fam_h, c, cu):
    """Multi-line body. Blank line = gap. A line wrapped in **double asterisks** is highlighted."""
    lines = text.split("\n")

    def build(sz):
        items = []
        for ln in lines:
            t = ln.strip()
            if not t:
                items.append(("", None, None, sz * .5))
                continue
            if t.startswith("**") and t.endswith("**") and len(t) > 4:
                f, col, t = font(sz * 1.3, fam_h, "bold", cu), c["hi"], t[2:-2].strip()
            else:
                f, col = font(sz, fam_b, "regular", cu), c["body"]
            for piece in wrap(d, t, f, maxw):
                items.append((piece, f, col, f.size * 1.3))
        return items

    size = int(size)
    while True:
        items = build(size)
        total = sum(i[3] for i in items)
        if total <= (y1 - y0) or size <= 14:
            break
        size -= 2
    y = y0 + ((y1 - y0) - total) / 2
    for t, f, col, h in items:
        if t:
            d.text((cx, y + h / 2), t, font=f, fill=col, anchor="mm")
        y += h


def paste_fit(img, im, cx, cy, mw, mh):
    im = ImageOps.exif_transpose(im).convert("RGBA")
    r = min(mw / im.width, mh / im.height)
    im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    img.paste(im, (int(cx - im.width / 2), int(cy - im.height / 2)), im)


def paste_photo(img, photo, box, border, shape="rect", bw=4):
    x, y, w, h = [int(v) for v in box]
    if shape == "circle":
        w = h = min(w, h)
    if photo is None:
        ph = Image.new("RGB", (w, h), (225, 228, 235))
        pd_ = ImageDraw.Draw(ph)
        pd_.ellipse([w * .3, h * .18, w * .7, h * .48], fill=(165, 172, 188))
        pd_.ellipse([w * .12, h * .55, w * .88, h * 1.15], fill=(165, 172, 188))
    else:
        ph = ImageOps.fit(ImageOps.exif_transpose(photo).convert("RGB"), (w, h), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    if shape == "circle":
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).ellipse([0, 0, w - 1, h - 1], fill=255)
        img.paste(ph, (x, y), m)
        d.ellipse([x - bw, y - bw, x + w + bw, y + h + bw], outline=border, width=bw)
    else:
        img.paste(ph, (x, y))
        d.rectangle([x - bw, y - bw, x + w + bw - 1, y + h + bw - 1], outline=border, width=bw)


def draw_seal(img, cx, cy, r, accent, t1, t2, cu=None):
    dark = mix(accent, (0, 0, 0), .45)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = 40
    pts = [(cx + (r if i % 2 == 0 else r * .92) * math.cos(math.pi * i / n),
            cy + (r if i % 2 == 0 else r * .92) * math.sin(math.pi * i / n)) for i in range(n * 2)]
    d.polygon(pts, fill=accent + (255,))
    for k in (.80, .64):
        d.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], outline=dark + (255,),
                  width=max(2, int(r * .04)))
    d.text((cx, cy - r * .34), t1, font=fit(d, t1, r * .95, r * .13, "sans", "bold", cu), fill=dark + (255,), anchor="mm")
    d.line([(cx - r * .22, cy + r * .02), (cx - r * .06, cy + r * .20), (cx + r * .26, cy - r * .16)],
           fill=dark + (255,), width=max(3, int(r * .08)), joint="curve")
    d.text((cx, cy + r * .42), t2, font=fit(d, t2, r * .95, r * .13, "sans", "bold", cu), fill=dark + (255,), anchor="mm")
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def make_qr(url, size):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    n = qr.modules_count + 6
    qr.box_size = max(1, size // n)
    im = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    if im.width > size:
        im = im.resize((size, size), Image.NEAREST)
    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return canvas


def verify_url(base_url, code):
    return f"{base_url.rstrip('/')}/?verify={code}"


# =============================================================================
# CERTIFICATE DESIGNS
# =============================================================================
def cb_classic(img, d, P, c, s, W, H):
    rect(d, [20 * s, 20 * s, W - 21 * s, H - 21 * s], outline=P["primary"], width=int(14 * s))
    rect(d, [48 * s, 48 * s, W - 49 * s, H - 49 * s], outline=P["accent"], width=max(2, int(5 * s)))
    rect(d, [62 * s, 62 * s, W - 63 * s, 150 * s], fill=P["primary"])


def cb_band(img, d, P, c, s, W, H):
    rect(d, [0, 0, .26 * W, H], fill=P["primary"])
    rect(d, [.26 * W, 0, .268 * W, H], fill=P["accent"])


def cb_corners(img, d, P, c, s, W, H):
    m, L = 40 * s, 170 * s
    rect(d, [m + 28 * s, m + 28 * s, W - m - 28 * s, H - m - 28 * s], outline=P["accent"], width=max(1, int(3 * s)))
    for x, y, sx, sy in [(m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
        d.line([(x, y), (x + sx * L, y)], fill=P["primary"], width=max(2, int(10 * s)))
        d.line([(x, y), (x, y + sy * L)], fill=P["primary"], width=max(2, int(10 * s)))
        d.ellipse([x + sx * 18 * s - 8 * s, y + sy * 18 * s - 8 * s, x + sx * 18 * s + 8 * s, y + sy * 18 * s + 8 * s],
                  fill=P["accent"])


def cb_minimal(img, d, P, c, s, W, H):
    rect(d, [0, 0, W, 26 * s], fill=P["primary"])
    rect(d, [0, H - 26 * s, W, H], fill=P["accent"])
    rect(d, [.07 * W, .165 * H, .93 * W, .165 * H + 3 * s], fill=P["accent"])


def cb_corporate(img, d, P, c, s, W, H):
    rect(d, [0, 0, W, .27 * H], fill=P["primary"])
    poly(d, [(.91 * W, 0), (W, 0), (W, .27 * H), (.86 * W, .27 * H)], fill=P["accent"])
    rect(d, [0, H - .035 * H - 6 * s, W, H - .035 * H], fill=P["accent"])
    rect(d, [0, H - .035 * H, W, H], fill=P["primary"])


def cb_dark(img, d, P, c, s, W, H):
    rect(d, [24 * s, 24 * s, W - 25 * s, H - 25 * s], outline=P["accent"], width=max(2, int(6 * s)))
    rect(d, [48 * s, 48 * s, W - 49 * s, H - 49 * s], outline=mix(P["accent"], c["bg"], .5), width=max(1, int(2 * s)))


def CL(**k):
    base = dict(cx=.60, org=(.5, .094, .62, 46), logo=(.11, .094, .10, .07), title_y=.23, sub_y=.30,
                intro_y=.385, name_y=.47, body=(.54, .76), body_w=.56, photo=(.07, .32, .17, .36),
                qr=(.075, .72, .12), seal=(.60, .865, .055), sig=((.40, .90), (.80, .90)))
    base.update(k)
    return base


CERT_DESIGNS = {
    "Classic Frame": dict(bg=cb_classic, scheme="light", org_col="light", L=CL()),
    "Modern Band": dict(bg=cb_band, scheme="light", photo_border="accent", qr_txt="light", L=CL(
        cx=.635, org=(.635, .095, .6, 34), logo=(.13, .09, .20, .10), title_y=.21, sub_y=.28, intro_y=.36,
        name_y=.45, body=(.52, .77), body_w=.62, photo=(.04, .22, .18, .36), qr=(.055, .70, .15),
        seal=(.64, .865, .055), sig=((.46, .90), (.82, .90)))),
    "Elegant Corners": dict(bg=cb_corners, scheme="light", L=CL(
        cx=.5, org=(.5, .10, .5, 38), logo=(.12, .11, .10, .09), title_y=.22, sub_y=.29, intro_y=.37,
        name_y=.46, body=(.53, .76), body_w=.6, photo=(.80, .07, .12, .20), qr=(.14, .75, .11),
        seal=(.5, .865, .055), sig=((.34, .90), (.66, .90)))),
    "Minimal Line": dict(bg=cb_minimal, scheme="light", L=CL(
        org=(.5, .095, .7, 40), logo=(.10, .095, .10, .07), title_y=.25, sub_y=.32, intro_y=.40, name_y=.48,
        body=(.55, .76), photo=(.07, .30, .16, .34), qr=(.075, .70, .115))),
    "Corporate Header": dict(bg=cb_corporate, scheme="light", org_col="light", title_light=True, L=CL(
        cx=.62, org=(.62, .075, .55, 34), logo=(.10, .135, .12, .15), title_y=.16, sub_y=.235, intro_y=.345,
        name_y=.43, body=(.50, .75), photo=(.07, .33, .17, .36), qr=(.075, .74, .12),
        seal=(.62, .865, .055), sig=((.42, .90), (.82, .90)))),
    "Dark Premium": dict(bg=cb_dark, scheme="dark", L=CL(
        org=(.5, .115, .6, 42), logo=(.12, .12, .10, .10), title_y=.25, sub_y=.32, intro_y=.40, name_y=.485,
        body=(.555, .76))),
    "Custom background": dict(bg=lambda *a: None, scheme="light", L=CL()),
}


def render_certificate(cfg, rec, A, photo, code, base_url, W=1600):
    D = CERT_DESIGNS[cfg["design"]]
    L = D["L"]
    H, s = round(W * 1130 / 1600), W / 1600
    P = {k: hex2rgb(cfg[k]) for k in ("primary", "accent", "text", "bg")}
    c = scheme_colors(P, D["scheme"])
    if D.get("title_light"):
        c["title"] = WHITE
    img = Image.new("RGB", (W, H), c["bg"])
    if cfg["design"] == "Custom background" and A.get("bg") is not None:
        img = ImageOps.fit(A["bg"].convert("RGB"), (W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    D["bg"](img, d, P, c, s, W, H)
    data = dict(name=rec["name"], course=rec["course"], roll=rec["roll"], date=rec["date"],
                org=cfg["org"], code=code)
    fh, fb, cu = cfg["head_font"].lower(), cfg["body_font"].lower(), cfg.get("custom_font")
    cx = (L["cx"] + cfg["dx"]) * W
    Y = lambda f: (f + cfg["dy"]) * H
    maxw = L["body_w"] * W

    if cfg["show_logo"] and A.get("logo") is not None:
        lx, ly, lw, lh = L["logo"]
        paste_fit(img, A["logo"], lx * W, ly * H, lw * W, lh * H)
    if cfg["show_org"] and cfg["org"]:
        ox, oy, omw, osz = L["org"]
        t = cfg["org"].upper()
        d.text((ox * W, oy * H), t, font=fit(d, t, omw * W, osz * s, fh, "bold", cu),
               fill=c["light"] if D.get("org_col") == "light" else c["org"], anchor="mm")
    for key, yk, szk, role, fam, style in (("title", "title_y", "title_size", "title", fh, "bold"),
                                           ("sub", "sub_y", "sub_size", "sub", fh, "bold"),
                                           ("intro", "intro_y", "intro_size", "intro", fb, "italic")):
        t = fmt_tpl(cfg[key], data)
        if t.strip():
            d.text((cx, Y(L[yk])), t, font=fit(d, t, maxw, cfg[szk] * s, fam, style, cu),
                   fill=c[role], anchor="mm")
    t = fmt_tpl(cfg["name"], data)
    d.text((cx, Y(L["name_y"])), t, font=fit(d, t, maxw, cfg["name_size"] * s, fh, "bold", cu),
           fill=c["name"], anchor="mm")
    draw_body(d, fmt_tpl(cfg["body"], data), cx, Y(L["body"][0]), Y(L["body"][1]), maxw,
              cfg["body_size"] * s, fb, fh, c, cu)

    if cfg["show_photo"]:
        px, py, pw, ph = L["photo"]
        paste_photo(img, photo, (px * W, py * H, pw * W, ph * H), c[D.get("photo_border", "frame")], "rect", max(2, int(4 * s)))
    if cfg["show_qr"]:
        qx, qy, qs = L["qr"]
        q, x0, y0 = int(qs * W), int(qx * W), int(qy * H)
        img.paste(make_qr(verify_url(base_url, code), q), (x0, y0))
        tc = c[D.get("qr_txt", "muted")]
        d.text((x0 + q // 2, y0 - int(16 * s)), "Scan to verify", font=font(20 * s, fb, "regular", cu), fill=tc, anchor="mm")
        d.text((x0 + q // 2, y0 + q + int(20 * s)), code, font=fit(d, code, q + 10 * s, 18 * s, fb, "regular", cu), fill=tc, anchor="mm")
    for i, (sx, sy) in enumerate(L["sig"]):
        n, t = cfg[f"sig{i + 1}_name"].strip(), cfg[f"sig{i + 1}_title"].strip()
        if not (n or t):
            continue
        hw, x, yy = .08 * W, (sx + cfg["dx"]) * W, sy * H
        d.line([(x - hw, yy), (x + hw, yy)], fill=c["muted"], width=max(2, int(3 * s)))
        if i == 0 and A.get("sig") is not None:
            paste_fit(img, A["sig"], x, yy - 48 * s, hw * 1.8, 90 * s)
        ty = yy + 24 * s
        if n:
            d.text((x, ty), n, font=fit(d, n, hw * 2.2, 28 * s, fb, "bold", cu), fill=c["name"], anchor="mm")
            ty += 32 * s
        if t:
            d.text((x, ty), t, font=fit(d, t, hw * 2.2, 24 * s, fb, "regular", cu), fill=c["muted"], anchor="mm")
    if cfg["show_seal"]:
        sx, sy, sr = L["seal"]
        img = draw_seal(img, sx * W, sy * H, sr * W, P["accent"], cfg["seal1"], cfg["seal2"], cu)
    return img


# =============================================================================
# ID CARD DESIGNS
# =============================================================================
def ib_header(img, d, P, c, W, H):
    rect(d, [0, 0, W, .22 * H], fill=P["primary"])
    rect(d, [0, .915 * H, W, .925 * H], fill=P["accent"])
    rect(d, [0, .925 * H, W, H], fill=P["primary"])


def ib_band(img, d, P, c, W, H):
    rect(d, [0, 0, .31 * W, H], fill=P["primary"])
    rect(d, [.31 * W, 0, .32 * W, H], fill=P["accent"])


def ib_diag(img, d, P, c, W, H):
    poly(d, [(0, 0), (W, 0), (W, .12 * H), (0, .30 * H)], fill=P["primary"])
    poly(d, [(0, .30 * H), (W, .12 * H), (W, .145 * H), (0, .325 * H)], fill=P["accent"])
    rect(d, [0, .96 * H, W, H], fill=P["primary"])


def ib_dark(img, d, P, c, W, H):
    rect(d, [0, 0, W, .025 * H], fill=P["accent"])
    rect(d, [0, .925 * H, W, .93 * H], fill=P["accent"])


ID_DESIGNS = {
    "Header Classic": dict(bg=ib_header, scheme="light", L=dict(
        org=(.5, .10, .78, .075, "light"), title=(.5, .17, .04, "accent"), logo=(.075, .11, .10, .16),
        photo=(.05, .28, .25, .52, "rect"), text_x=.34, label_w=.20, rows=(.36, .66), qr=(.76, .60, .19),
        footer=(.5, .96, .035, "light"))),
    "Side Band": dict(bg=ib_band, scheme="light", photo_border="accent", L=dict(
        org=(.655, .11, .62, .065, "primary"), title=(.655, .19, .038, "accent"), logo=(.155, .075, .24, .10),
        photo=(.045, .17, .22, .46, "rect"), text_x=.36, label_w=.19, rows=(.36, .66), qr=(.76, .60, .19),
        footer=(.655, .955, .033, "muted"))),
    "Modern Diagonal": dict(bg=ib_diag, scheme="light", photo_border="accent", L=dict(
        org=(.55, .075, .70, .06, "light"), title=(.55, .14, .034, "accent"), logo=(.94, .065, .08, .09),
        photo=(.05, .22, .27, .50, "circle"), text_x=.38, label_w=.19, rows=(.38, .64), qr=(.76, .58, .19),
        footer=(.5, .98, .03, "light"))),
    "Dark Card": dict(bg=ib_dark, scheme="dark", L=dict(
        org=(.5, .11, .78, .07, "org"), title=(.5, .18, .037, "light"), logo=(.07, .11, .10, .16),
        photo=(.05, .27, .25, .52, "rect"), text_x=.34, label_w=.20, rows=(.36, .66), qr=(.76, .60, .19),
        footer=(.5, .96, .033, "accent"))),
}


def parse_fields(text):
    rows = []
    for line in text.splitlines():
        if ":" in line:
            a, b = line.split(":", 1)
            rows.append((a.strip(), b.strip()))
        elif line.strip():
            rows.append(("", line.strip()))
    return rows[:5]


def render_idcard(cfg, rec, A, photo, code, base_url, size=(850, 540)):
    D = ID_DESIGNS[cfg["design"]]
    L = D["L"]
    W, H = size
    s = H / 540
    P = {k: hex2rgb(cfg[k]) for k in ("primary", "accent", "text", "bg")}
    c = scheme_colors(P, D["scheme"])
    img = Image.new("RGB", size, c["bg"])
    d = ImageDraw.Draw(img)
    D["bg"](img, d, P, c, W, H)
    data = dict(name=rec["name"], course=rec["course"], roll=rec["roll"], date=rec["date"],
                org=cfg["org"], code=code)
    fam, cu = cfg["font"].lower(), cfg.get("custom_font")

    if cfg["show_logo"] and A.get("logo") is not None:
        lx, ly, lw, lh = L["logo"]
        paste_fit(img, A["logo"], lx * W, ly * H, lw * W, lh * H)
    if cfg["show_org"] and cfg["org"]:
        ox, oy, omw, osz, role = L["org"]
        t = cfg["org"].upper()
        d.text((ox * W, oy * H), t, font=fit(d, t, omw * W, osz * H, fam, "bold", cu), fill=c[role], anchor="mm")
    if cfg["title"].strip():
        tx_, ty_, tsz, role = L["title"]
        d.text((tx_ * W, ty_ * H), fmt_tpl(cfg["title"], data), font=font(tsz * H, fam, "bold", cu), fill=c[role], anchor="mm")
    if cfg["show_photo"]:
        px, py, pw, ph, shape = L["photo"]
        paste_photo(img, photo, (px * W, py * H, pw * W, ph * H), c[D.get("photo_border", "frame")], shape, max(2, int(4 * s)))

    qx, qy, qs = L["qr"]
    qbox = (qx * W, qy * H, qx * W + qs * W, qy * H + qs * W)
    rows = [(fmt_tpl(a, data), fmt_tpl(b, data)) for a, b in parse_fields(cfg["fields"])]
    n = len(rows)
    if n:
        y0, y1 = L["rows"]
        ys = [(y0 + y1) / 2] if n == 1 else [y0 + (y1 - y0) * i / (n - 1) for i in range(n)]
        base = cfg["font_size"] * s * (1 if n <= 3 else .85 if n == 4 else .72)
        tx = L["text_x"] * W
        for (lab, val), yf in zip(rows, ys):
            yy = yf * H
            vx = tx + L["label_w"] * W if lab else tx
            mw = .96 * W - vx
            if cfg["show_qr"] and yy + .05 * H > qbox[1] and yy - .05 * H < qbox[3]:
                mw = qbox[0] - vx - 10 * s
            if lab:
                fl = font(base * .7, fam, "regular", cu)
                d.text((tx, yy), lab, font=fl, fill=c["muted"], anchor="lm")
                d.text((vx - 14 * s, yy), ":", font=fl, fill=c["muted"], anchor="lm")
            d.text((vx, yy), val, font=fit(d, val, mw, base, fam, "bold", cu), fill=c["name"], anchor="lm")
    if cfg["show_qr"]:
        img.paste(make_qr(verify_url(base_url, code), int(qs * W)), (int(qbox[0]), int(qbox[1])))
    if cfg["footer"].strip():
        fx, fy, fsz, role = L["footer"]
        t = fmt_tpl(cfg["footer"], data)
        d.text((fx * W, fy * H), t, font=fit(d, t, .9 * W, fsz * H, fam, "regular", cu), fill=c[role], anchor="mm")
    return img


# =============================================================================
# DATA HELPERS
# =============================================================================
DATE_FORMATS = {"15 October 2026": "%d %B %Y", "15/10/2026": "%d/%m/%Y",
                "October 15, 2026": "%B %d, %Y", "2026-10-15": "%Y-%m-%d"}


def clean(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def fmt_date(v, fmt):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(v).strftime(fmt)
    return str(v)


def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s)).strip("_")[:40] or "x"


def guess(cols, keys, allow_none=False):
    opts = (["(none)"] if allow_none else []) + list(cols)
    for i, col in enumerate(opts):
        if any(k in str(col).lower() for k in keys):
            return i
    return 0


def sample_excel():
    df = pd.DataFrame({
        "Name": ["Akshat Singh", "Rahul Sharma", "Neha Verma", "Pooja Jain"],
        "Course": ["Data Analytics", "Python Developer", "Web Developer", "Data Science"],
        "Roll No": ["DA001", "PY002", "WD003", "DS004"],
        "Date": ["15 October 2026"] * 4,
        "Photo Path": ["akshat.jpg", "rahul.jpg", "neha.jpg", "pooja.jpg"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def build_photo_map(zip_file, image_files):
    pm = {}

    def add(name, data):
        key = os.path.basename(name.replace("\\", "/")).lower()
        if key and key.rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp", "bmp"):
            pm[key] = data
            pm[os.path.splitext(key)[0]] = data

    if zip_file is not None:
        with zipfile.ZipFile(io.BytesIO(zip_file.getvalue())) as z:
            for n in z.namelist():
                if not n.endswith("/"):
                    add(n, z.read(n))
    for f in image_files or []:
        add(f.name, f.getvalue())
    return pm


def load_photo(ref, photo_map):
    ref = clean(ref)
    if not ref:
        return None
    try:
        if ref.lower().startswith(("http://", "https://")):
            return Image.open(io.BytesIO(requests.get(ref, timeout=10).content))
        key = os.path.basename(ref.replace("\\", "/")).lower()
        data = photo_map.get(key) or photo_map.get(os.path.splitext(key)[0])
        if data:
            return Image.open(io.BytesIO(data))
        if os.path.exists(ref):
            return Image.open(ref)
    except Exception:
        return None
    return None


def load_img(f):
    try:
        return Image.open(io.BytesIO(f.getvalue())) if f is not None else None
    except Exception:
        return None


def to_bytes(img, fmt):
    buf = io.BytesIO()
    if fmt == "PNG":
        img.save(buf, "PNG")
        return buf.getvalue(), "png"
    if fmt == "JPG":
        img.convert("RGB").save(buf, "JPEG", quality=95)
        return buf.getvalue(), "jpg"
    img.convert("RGB").save(buf, "PDF", resolution=200.0)
    return buf.getvalue(), "pdf"


def generate_zip(records, photo_map, do_cert, do_id, ccfg, icfg, A, base_url, fmt, cert_w, id_size, progress=None):
    """Registers every document in the database, renders it with its own QR code, returns a ZIP."""
    items = []
    for r in records:
        if do_cert:
            items.append(("certificate", r))
        if do_id:
            items.append(("idcard", r))
    codes = db_register(items, ccfg["org"])
    buf, log, k = io.BytesIO(), [], 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, r in enumerate(records):
            photo = load_photo(r["photo"], photo_map)
            base = f"{i + 1:03d}_{slug(r['roll'])}_{slug(r['name'])}"
            if do_cert:
                code, k = codes[k], k + 1
                data, ext = to_bytes(render_certificate(ccfg, r, A, photo, code, base_url, cert_w), fmt)
                z.writestr(f"certificates/{base}_certificate.{ext}", data)
                log.append(dict(type="certificate", name=r["name"], roll_no=r["roll"], code=code,
                                verify_url=verify_url(base_url, code)))
            if do_id:
                code, k = codes[k], k + 1
                data, ext = to_bytes(render_idcard(icfg, r, A, photo, code, base_url, id_size), fmt)
                z.writestr(f"id_cards/{base}_idcard.{ext}", data)
                log.append(dict(type="id_card", name=r["name"], roll_no=r["roll"], code=code,
                                verify_url=verify_url(base_url, code)))
            if progress:
                progress((i + 1) / len(records), f"Generated {i + 1} of {len(records)}")
        z.writestr("registry_log.csv", pd.DataFrame(log).to_csv(index=False))
    return buf.getvalue(), len(log)


# =============================================================================
# UI
# =============================================================================
def apply_palette():
    p = PALETTES.get(st.session_state["palette"])
    if p:
        for k, v in zip(("c_primary", "c_accent", "c_text", "c_bg"), p):
            st.session_state[k] = v


def mark_custom():
    st.session_state["palette"] = "Custom"


def set_state(key, value):
    st.session_state[key] = value


def gallery(designs, state_key, thumb):
    cols = st.columns(3)
    for i, name in enumerate(designs):
        with cols[i % 3]:
            st.image(thumb(name))
            sel = st.session_state[state_key] == name
            st.button(("✅ " if sel else "") + name, key=f"{state_key}_{name}", on_click=set_state,
                      args=(state_key, name), type="primary" if sel else "secondary")


def admin_gate():
    pw = get_conf("ADMIN_PASSWORD")
    if not pw:
        st.sidebar.warning("⚠️ No ADMIN_PASSWORD set - anyone with this link can generate documents and "
                           "edit the registry. Add it in Secrets.")
        return
    if st.session_state.get("admin_ok"):
        if st.sidebar.button("Log out"):
            st.session_state["admin_ok"] = False
            st.rerun()
        return
    st.markdown('<div class="app-title">🔐 Admin login</div>', unsafe_allow_html=True)
    with st.form("login"):
        p = st.text_input("Admin password", type="password")
        if st.form_submit_button("Log in"):
            if hmac.compare_digest(p.encode(), pw.encode()):
                st.session_state["admin_ok"] = True
                st.rerun()
            st.error("Wrong password")
    st.stop()


def app():
    for k, v in dict(palette="Navy & Gold", c_primary="#102A60", c_accent="#D4AF37", c_text="#333333",
                     c_bg="#FFFDF7", cert_design="Classic Frame", id_design="Header Classic").items():
        st.session_state.setdefault(k, v)

    st.markdown('<div class="app-title">🎓 Certificate & ID Card Maker</div>', unsafe_allow_html=True)
    st.markdown('<div class="app-sub">Design once, generate hundreds - every document is verifiable by QR code</div>',
                unsafe_allow_html=True)

    # ---- sidebar: verification URL + database status
    st.sidebar.markdown("### 🔗 Verification")
    base_url = st.sidebar.text_input("Your app's public URL", get_conf("BASE_URL", "http://localhost:8501"),
                                     help="QR codes open this address. After deploying, use your "
                                          "https://yourapp.streamlit.app link.")
    st.sidebar.markdown("### 🗄️ Database")
    try:
        eng = get_engine()
        st.sidebar.success(f"Connected: {eng.dialect.name}")
        if eng.dialect.name == "sqlite":
            st.sidebar.warning("SQLite file is erased when Streamlit Cloud restarts. Set DATABASE_URL "
                               "(PostgreSQL) in Secrets for permanent storage.")
    except Exception as e:
        eng = None
        st.sidebar.error(f"Database error: {e}")

    mode = st.radio("What do you want to generate?",
                    ["Certificates + ID cards", "Certificates only", "ID cards only"], horizontal=True, key="mode")
    do_cert, do_id = mode != "ID cards only", mode != "Certificates only"

    t_data, t_design, t_gen, t_reg = st.tabs(["1️⃣ Data", "2️⃣ Design", "3️⃣ Generate", "4️⃣ Registry"])

    # ------------------------------------------------------------------ DATA
    records, photo_map = [], {}
    with t_data:
        d1, d2 = st.columns(2, gap="large")
        with d1.container(border=True):
            st.subheader("📊 Upload data")
            data_file = st.file_uploader("Excel (.xlsx) or CSV", type=["xlsx", "xls", "csv"])
            st.download_button("⬇️ Download sample Excel", sample_excel(), "students_data.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            zip_f = st.file_uploader("Photos as a ZIP (optional)", type=["zip"])
            img_fs = st.file_uploader("…or upload photos directly", type=["jpg", "jpeg", "png", "webp"],
                                      accept_multiple_files=True)
            st.caption("Photo column = file name (e.g. `akshat.jpg`) or a public image URL.")
        photo_map = build_photo_map(zip_f, img_fs)
        df = None
        if data_file is not None:
            try:
                df = pd.read_csv(data_file) if data_file.name.lower().endswith(".csv") else pd.read_excel(data_file)
                df = df.dropna(how="all")
            except Exception as e:
                st.error(f"Could not read the file: {e}")
        if df is not None and not df.empty:
            cols = list(df.columns)
            with d2.container(border=True):
                st.subheader("🧩 Match columns")
                col_name = st.selectbox("Name column", cols, index=guess(cols, ["name"]))
                col_course = st.selectbox("Course column", cols, index=guess(cols, ["course", "program", "class"]))
                col_roll = st.selectbox("Roll No column", ["(none)"] + cols, index=guess(cols, ["roll", "id", "reg"], True))
                col_date = st.selectbox("Date column", ["(none)"] + cols, index=guess(cols, ["date"], True))
                col_photo = st.selectbox("Photo column", ["(none)"] + cols, index=guess(cols, ["photo", "image", "pic"], True))
                date_label = st.selectbox("Date format", list(DATE_FORMATS))
                default_date = st.date_input("Default date (if no date column)", date.today())
            dfmt = DATE_FORMATS[date_label]
            for i, (_, row) in enumerate(df.iterrows()):
                records.append(dict(
                    name=clean(row[col_name]) or "Unnamed", course=clean(row[col_course]),
                    roll=clean(row[col_roll]) if col_roll != "(none)" else f"{i + 1:04d}",
                    date=fmt_date(row[col_date], dfmt) if col_date != "(none)" else default_date.strftime(dfmt),
                    photo=row[col_photo] if col_photo != "(none)" else None))
            prev = df.head(10).copy()
            if col_photo != "(none)":
                prev["Photo found"] = ["✅" if load_photo(r, photo_map) is not None else "❌" for r in df[col_photo].head(10)]
            st.dataframe(prev, hide_index=True)
            st.caption(f"{len(records)} record(s) loaded.")
        else:
            st.info("Upload your Excel/CSV to begin. You can already explore the **Design** tab with sample data.")

    sample = dict(name="Akshat Singh Rajput", course="Data Analytics Course", roll="DA001",
                  date=date.today().strftime("%d %B %Y"), photo=None)
    sample_photo = None

    # ---------------------------------------------------------------- DESIGN
    with t_design:
        if records:
            pidx = st.number_input("Preview using record #", 1, len(records), 1, key="pidx") - 1
            sample = records[pidx]
            sample_photo = load_photo(sample["photo"], photo_map)

        st.subheader("🎨 Brand & colours")
        b1, b2 = st.columns([1.2, 1], gap="large")
        with b1:
            org = st.text_input("Organisation / company name", "Your Organization", key="org")
            logo_f = st.file_uploader("Company logo (transparent PNG works best)", type=["png", "jpg", "jpeg"], key="logo")
            font_f = st.file_uploader("Custom font .ttf (optional)", type=["ttf", "otf"], key="fontfile")
        with b2:
            st.selectbox("Colour palette", list(PALETTES), key="palette", on_change=apply_palette)
            k1, k2 = st.columns(2)
            k1.color_picker("Primary", key="c_primary", on_change=mark_custom)
            k2.color_picker("Accent", key="c_accent", on_change=mark_custom)
            k1.color_picker("Text", key="c_text", on_change=mark_custom)
            k2.color_picker("Background", key="c_bg", on_change=mark_custom)
        custom_font = None
        if font_f is not None:
            custom_font = os.path.join(tempfile.gettempdir(), f"cim_{slug(font_f.name)}")
            with open(custom_font, "wb") as fh:
                fh.write(font_f.getvalue())
        colors = dict(primary=st.session_state["c_primary"], accent=st.session_state["c_accent"],
                      text=st.session_state["c_text"], bg=st.session_state["c_bg"])

        cert_t, id_t = st.tabs(["🎓 Certificate" + ("" if do_cert else " (off)"),
                                "🪪 ID card" + ("" if do_id else " (off)")])

        # ---------------- certificate designer
        with cert_t:
            if not do_cert:
                st.info("Certificates are switched off above - this design will be skipped.")
            gal_box = st.container()
            cE, cP = st.columns([1, 1.35], gap="large")
            with cE:
                st.markdown("**✏️ Edit the certificate text** (applies to every certificate)")
                ct_title = st.text_input("Title", "CERTIFICATE", key="ct_title")
                ct_sub = st.text_input("Subtitle", "OF COMPLETION", key="ct_sub")
                ct_intro = st.text_input("Line above the name", "This is to certify that", key="ct_intro")
                ct_name = st.text_input("Name line", "{name}", key="ct_name")
                ct_body = st.text_area("Body text", "has successfully completed the\n**{course}**\non {date}",
                                       height=140, key="ct_body")
                st.caption("Placeholders: `{name} {course} {roll} {date} {org}` · wrap a line in `**stars**` to "
                           "highlight it · empty line = gap")
                s1, s2 = st.columns(2)
                sig1_name = s1.text_input("Signatory 1 name", "", key="sig1n")
                sig1_title = s1.text_input("Signatory 1 title", "Director", key="sig1t")
                sig2_name = s2.text_input("Signatory 2 name", "", key="sig2n")
                sig2_title = s2.text_input("Signatory 2 title", "Course Instructor", key="sig2t")
                with st.expander("🔤 Fonts & sizes"):
                    head_font = st.selectbox("Heading font", ["Serif", "Sans"], key="hf")
                    body_font = st.selectbox("Body font", ["Sans", "Serif"], key="bf")
                    title_size = st.slider("Title size", 40, 160, 100, 2, key="ts")
                    sub_size = st.slider("Subtitle size", 20, 90, 40, 2, key="ss")
                    intro_size = st.slider("Intro line size", 20, 80, 36, 2, key="is")
                    name_size = st.slider("Name size", 30, 160, 84, 2, key="ns")
                    body_size = st.slider("Body size", 20, 90, 40, 2, key="bs")
                with st.expander("📐 Elements & position"):
                    show_photo = st.checkbox("Photo", True, key="sp")
                    show_qr = st.checkbox("QR code", True, key="sq")
                    show_seal = st.checkbox("Digital seal", True, key="sse")
                    show_logo = st.checkbox("Logo", True, key="sl")
                    show_org = st.checkbox("Organisation name", True, key="so")
                    seal1 = st.text_input("Seal top text", "AUTHENTIC", key="seal1")
                    seal2 = st.text_input("Seal bottom text", "VERIFIED", key="seal2")
                    dx = st.slider("Move text left/right", -0.15, 0.15, 0.0, 0.005, key="dx")
                    dy = st.slider("Move text up/down", -0.08, 0.08, 0.0, 0.005, key="dy")
                with st.expander("🖼️ Own background & signature image"):
                    bg_f = st.file_uploader("Custom background (choose the 'Custom background' design)",
                                            type=["png", "jpg", "jpeg"], key="bgfile")
                    sig_f = st.file_uploader("Signature image (transparent PNG)", type=["png"], key="sigfile")
            A = dict(logo=load_img(logo_f), bg=load_img(bg_f), sig=load_img(sig_f))
            ccfg = dict(design=st.session_state["cert_design"], org=org, **colors, title=ct_title, sub=ct_sub,
                        intro=ct_intro, name=ct_name, body=ct_body, sig1_name=sig1_name, sig1_title=sig1_title,
                        sig2_name=sig2_name, sig2_title=sig2_title, head_font=head_font, body_font=body_font,
                        title_size=title_size, sub_size=sub_size, intro_size=intro_size, name_size=name_size,
                        body_size=body_size, show_photo=show_photo, show_qr=show_qr, show_seal=show_seal,
                        show_logo=show_logo, show_org=show_org, seal1=seal1, seal2=seal2, dx=dx, dy=dy,
                        custom_font=custom_font)
            with cP:
                st.markdown(f"**👀 Live preview - {ccfg['design']}**")
                st.image(render_certificate(ccfg, sample, A, sample_photo, "PREVIEW", base_url, 1100))
                st.caption("Preview QR codes are not registered. Real codes are created when you generate.")
            with gal_box:
                st.markdown("**Choose a design** (uses your colours and text)")
                gallery(CERT_DESIGNS, "cert_design",
                        lambda n: render_certificate({**ccfg, "design": n}, sample, A, None, "PREVIEW", base_url, 520))

        # ---------------- ID card designer
        with id_t:
            if not do_id:
                st.info("ID cards are switched off above - this design will be skipped.")
            gal_box2 = st.container()
            iE, iP = st.columns([1, 1.35], gap="large")
            with iE:
                st.markdown("**✏️ Edit the ID card**")
                it_title = st.text_input("Card heading", "STUDENT ID CARD", key="it_title")
                it_fields = st.text_area("Fields (one per line - `Label: {placeholder}`, max 5)",
                                         "Name: {name}\nCourse: {course}\nRoll No: {roll}", height=120, key="it_fields")
                st.caption("Placeholders: `{name} {course} {roll} {date} {org}` - e.g. `Valid Till: 31 Dec 2027`")
                it_footer = st.text_input("Footer text", "Scan the QR code to verify authenticity", key="it_footer")
                it_font = st.selectbox("Font", ["Sans", "Serif"], key="it_font")
                it_size = st.slider("Text size", 24, 60, 40, 1, key="it_size")
                id_label = st.selectbox("Card size", ["Standard (850 x 540)", "CR80 300dpi (1012 x 638)",
                                                      "Large (1275 x 810)"], key="idsize")
                i1, i2, i3, i4 = st.columns(4)
                ishow_photo = i1.checkbox("Photo", True, key="isp")
                ishow_qr = i2.checkbox("QR", True, key="isq")
                ishow_logo = i3.checkbox("Logo", True, key="isl")
                ishow_org = i4.checkbox("Org", True, key="iso")
            id_size = {"Standard (850 x 540)": (850, 540), "CR80 300dpi (1012 x 638)": (1012, 638),
                       "Large (1275 x 810)": (1275, 810)}[id_label]
            icfg = dict(design=st.session_state["id_design"], org=org, **colors, title=it_title, fields=it_fields,
                        footer=it_footer, font=it_font, font_size=it_size, show_photo=ishow_photo, show_qr=ishow_qr,
                        show_logo=ishow_logo, show_org=ishow_org, custom_font=custom_font)
            with iP:
                st.markdown(f"**👀 Live preview - {icfg['design']}**")
                st.image(render_idcard(icfg, sample, A, sample_photo, "PREVIEW", base_url, id_size))
            with gal_box2:
                st.markdown("**Choose a design** (uses your colours and text)")
                gallery(ID_DESIGNS, "id_design",
                        lambda n: render_idcard({**icfg, "design": n}, sample, A, None, "PREVIEW", base_url, (510, 324)))

    # ------------------------------------------------------------------ GENERATE
    with t_gen:
        st.subheader("🚀 Generate final documents")
        if not records:
            st.info("Upload your data in the **Data** tab first.")
        else:
            what = {"Certificates + ID cards": "certificates and ID cards", "Certificates only": "certificates",
                    "ID cards only": "ID cards"}[mode]
            st.write(f"Ready to create **{what}** for **{len(records)}** record(s) using the designs "
                     f"**{ccfg['design'] if do_cert else ''}{' / ' if do_cert and do_id else ''}"
                     f"{icfg['design'] if do_id else ''}**.")
            g1, g2 = st.columns(2)
            fmt = g1.selectbox("File format", ["PNG", "JPG", "PDF"])
            cw_label = g2.selectbox("Certificate resolution", ["Standard (1600 px)", "High (2400 px)"])
            cert_w = 1600 if cw_label.startswith("Standard") else 2400
            if do_cert and ccfg["design"] == "Custom background" and A["bg"] is None:
                st.warning("You chose 'Custom background' but have not uploaded a background image.")
            if "localhost" in base_url:
                st.warning("The QR codes will point to localhost. Set your deployed app URL in the sidebar first.")
            if eng is None:
                st.error("Database not available - cannot register documents.")
            elif st.button("⚙️ Generate & register in database", type="primary"):
                bar = st.progress(0.0, text="Starting…")
                try:
                    zdata, n = generate_zip(records, photo_map, do_cert, do_id, ccfg, icfg, A, base_url, fmt,
                                            cert_w, id_size, lambda p, t: bar.progress(p, text=t))
                    st.session_state["result_zip"], st.session_state["result_n"] = zdata, n
                except Exception as e:
                    st.error(f"Generation failed: {e}")
                bar.empty()
            if st.session_state.get("result_zip"):
                st.success(f"✅ {st.session_state['result_n']} document(s) created and registered. "
                           "Scanning any QR code now verifies it against the database.")
                st.download_button("📦 Download all (ZIP)", st.session_state["result_zip"],
                                   "certificates_and_id_cards.zip", "application/zip")

    # ------------------------------------------------------------------ REGISTRY
    with t_reg:
        st.subheader("🗂️ Verification registry")
        if eng is None:
            st.error("Database not available.")
        else:
            r1, r2, r3 = st.columns([2, 1, 1])
            q = r1.text_input("Search name / roll no / code / course")
            ftype = r2.selectbox("Type", ["All", "certificate", "idcard"])
            fstat = r3.selectbox("Status", ["All", "valid", "revoked"])
            reg = db_search(q, ftype, fstat)
            st.caption(f"{len(reg)} record(s)")
            if not reg.empty:
                st.dataframe(reg.drop(columns=["id"]), hide_index=True)
                sel = st.multiselect("Select documents (by code)", reg["code"].tolist())
                a1, a2, a3 = st.columns(3)
                if a1.button("🚫 Revoke selected") and sel:
                    db_set_status(sel, "revoked")
                    st.rerun()
                if a2.button("♻️ Re-activate selected") and sel:
                    db_set_status(sel, "valid")
                    st.rerun()
                confirm = a3.checkbox("Confirm delete")
                if a3.button("🗑️ Delete selected") and sel and confirm:
                    db_delete(sel)
                    st.rerun()
                st.download_button("⬇️ Export registry (CSV)", reg.to_csv(index=False), "registry.csv", "text/csv")


def main():
    code = st.query_params.get("verify")
    if code:
        verification_page(code)
        return
    admin_gate()
    app()


main()
