"""
🎓 Certificate & ID Card Maker  (Streamlit)
-------------------------------------------------
Bulk-generate certificates and student ID cards from an Excel/CSV file.

Features
  1. Import Excel / CSV data (name, course, roll no, date, photo)
  2. Bulk certificates + ID cards
  3. Use your own templates (PNG/JPG) or the built-in designs
  4. Photos from an uploaded ZIP / multiple images / URLs
  5. Digital gold seal + signed QR code (scan to verify authenticity)
  6. Export PNG / JPG / PDF inside one ZIP file
  7. Built-in verification page (the QR code opens it)

Run locally:   streamlit run app.py
"""

import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import tempfile
import zipfile
from datetime import date, datetime
from functools import lru_cache
from urllib.parse import urlencode

import pandas as pd
import qrcode
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ----------------------------------------------------------------------------
# Page setup
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Certificate & ID Card Maker", page_icon="🎓", layout="wide")

NAVY = (16, 42, 96)
GOLD = (212, 175, 55)
DARK_GOLD = (150, 112, 20)
GREY = (70, 70, 70)

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; max-width: 1250px;}
    .app-title {text-align:center; font-size: 2.4rem; font-weight: 800; color:#16296b; margin-bottom:0;}
    .app-sub {text-align:center; color:#555; margin-bottom: 1.2rem;}
    div.stButton > button[kind="primary"] {background:#e11d2e; border:none; font-weight:700; padding:.7rem 1rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------
BOLD_FONTS = [
    "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
REG_FONTS = [
    "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]


@lru_cache(maxsize=512)
def get_font(size, bold=False, custom=None):
    size = max(8, int(size))
    if custom:
        try:
            return ImageFont.truetype(custom, size)
        except Exception:
            pass
    for name in (BOLD_FONTS if bold else REG_FONTS):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def fit_font(draw, text, max_w, size, bold=True, custom=None, min_size=14):
    """Shrink the font until the text fits inside max_w."""
    size = int(size)
    while size > min_size:
        f = get_font(size, bold, custom)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return get_font(min_size, bold, custom)


# ----------------------------------------------------------------------------
# Security: signed verification links (HMAC) -> QR code
# ----------------------------------------------------------------------------
def get_secret():
    try:
        if "SECRET_KEY" in st.secrets:
            return str(st.secrets["SECRET_KEY"])
    except Exception:
        pass
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    return st.session_state.get("secret_input") or "change-this-secret"


def sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:24]


def build_verify_url(base_url, kind, rec, org, secret):
    payload = {"k": kind, "n": rec["name"], "c": rec["course"], "r": rec["roll"],
               "d": rec["date"], "o": org}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{base_url.rstrip('/')}/?{urlencode({'v': b64, 's': sign(b64, secret)})}"


def make_qr(url, size_px):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    return img.resize((int(size_px), int(size_px)), Image.NEAREST)


def verification_page():
    """Shown when someone scans the QR code."""
    st.markdown('<div class="app-title">🔍 Document Verification</div>', unsafe_allow_html=True)
    v = st.query_params.get("v", "")
    s = st.query_params.get("s", "")
    try:
        payload = json.loads(base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode())
        valid = hmac.compare_digest(sign(v, get_secret()), s)
    except Exception:
        payload, valid = None, False

    if valid and payload:
        kind = "Certificate" if payload.get("k") == "certificate" else "Student ID Card"
        st.success(f"✅ AUTHENTIC — this {kind} was issued by **{payload.get('o', 'the institute')}**.")
        st.table(pd.DataFrame({
            "Field": ["Name", "Course", "Roll / ID No", "Date", "Issued by"],
            "Value": [payload.get("n"), payload.get("c"), payload.get("r"),
                      payload.get("d"), payload.get("o")],
        }).set_index("Field"))
    else:
        st.error("❌ INVALID — this link was not issued by this system or has been tampered with.")


# ----------------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------------
def draw_seal(img, cx, cy, r, custom=None):
    """Gold scalloped 'AUTHENTIC' seal, composited onto img."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = 40
    pts = []
    for i in range(n * 2):
        ang = math.pi * i / n
        rad = r if i % 2 == 0 else r * 0.92
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=GOLD + (255,))
    w = max(2, int(r * 0.04))
    for k in (0.80, 0.64):
        d.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], outline=DARK_GOLD + (255,), width=w)
    d.text((cx, cy - r * 0.34), "AUTHENTIC", font=get_font(r * 0.15, True, custom),
           fill=DARK_GOLD + (255,), anchor="mm")
    d.line([(cx - r * 0.22, cy + r * 0.02), (cx - r * 0.06, cy + r * 0.20), (cx + r * 0.26, cy - r * 0.16)],
           fill=DARK_GOLD + (255,), width=max(3, int(r * 0.08)), joint="curve")
    d.text((cx, cy + r * 0.42), "VERIFIED", font=get_font(r * 0.15, True, custom),
           fill=DARK_GOLD + (255,), anchor="mm")
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def paste_photo(img, photo, box):
    x, y, w, h = box
    if photo is None:
        ph = Image.new("RGB", (w, h), (225, 228, 235))
        d = ImageDraw.Draw(ph)
        d.ellipse([w * .3, h * .18, w * .7, h * .48], fill=(165, 172, 188))
        d.ellipse([w * .12, h * .55, w * .88, h * 1.15], fill=(165, 172, 188))
        ph_img = ph
    else:
        ph_img = ImageOps.fit(ImageOps.exif_transpose(photo).convert("RGB"), (w, h), Image.LANCZOS)
    img.paste(ph_img, (x, y))
    ImageDraw.Draw(img).rectangle([x - 3, y - 3, x + w + 3, y + h + 3], outline=NAVY, width=4)


def load_photo(ref, photo_map):
    if ref is None or (isinstance(ref, float) and pd.isna(ref)):
        return None
    ref = str(ref).strip()
    if not ref:
        return None
    try:
        if ref.lower().startswith(("http://", "https://")):
            r = requests.get(ref, timeout=10)
            return Image.open(io.BytesIO(r.content))
        key = os.path.basename(ref.replace("\\", "/")).lower()
        data = photo_map.get(key) or photo_map.get(os.path.splitext(key)[0])
        if data:
            return Image.open(io.BytesIO(data))
        if os.path.exists(ref):
            return Image.open(ref)
    except Exception:
        return None
    return None


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


# ----------------------------------------------------------------------------
# Default templates (used when you don't upload your own)
# ----------------------------------------------------------------------------
def default_certificate(org, custom=None):
    W, H = 1600, 1130
    img = Image.new("RGB", (W, H), (255, 253, 247))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, W - 21, H - 21], outline=NAVY, width=14)
    d.rectangle([48, 48, W - 49, H - 49], outline=GOLD, width=5)
    d.rectangle([62, 62, W - 63, 150], fill=NAVY)
    d.text((W // 2, 106), org.upper(), font=fit_font(d, org.upper(), W * 0.8, 46, True, custom),
           fill="white", anchor="mm")
    cx = int(0.60 * W)
    d.text((cx, int(.23 * H)), "CERTIFICATE", font=get_font(104, True, custom), fill=NAVY, anchor="mm")
    d.text((cx, int(.30 * H)), "OF COMPLETION", font=get_font(40, True, custom), fill=GOLD, anchor="mm")
    d.text((cx, int(.385 * H)), "This is to certify that", font=get_font(36, False, custom), fill=GREY, anchor="mm")
    d.text((cx, int(.585 * H)), "has successfully completed the", font=get_font(36, False, custom), fill=GREY, anchor="mm")
    d.line([(int(.27 * W), int(.90 * H)), (int(.45 * W), int(.90 * H))], fill=NAVY, width=3)
    d.text((int(.36 * W), int(.93 * H)), "Authorized Signature", font=get_font(26, False, custom),
           fill=GREY, anchor="mm")
    return img


def default_idcard(org, size, custom=None):
    W, H = size
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, int(.22 * H)], fill=NAVY)
    d.text((W // 2, int(.10 * H)), org.upper(),
           font=fit_font(d, org.upper(), W * 0.9, H * 0.075, True, custom), fill="white", anchor="mm")
    d.text((W // 2, int(.17 * H)), "STUDENT ID CARD", font=get_font(H * 0.04, True, custom),
           fill=GOLD, anchor="mm")
    d.rectangle([0, int(.915 * H), W, int(.925 * H)], fill=GOLD)
    d.rectangle([0, int(.925 * H), W, H], fill=NAVY)
    d.text((W // 2, int(.96 * H)), "Scan the QR code to verify authenticity",
           font=get_font(H * 0.035, False, custom), fill="white", anchor="mm")
    return img


# ----------------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------------
CERT_DEFAULTS = dict(text_cx=0.60, name_y=0.47, course_y=0.665, date_y=0.76,
                     name_size=84, course_size=60, date_size=40,
                     photo_x=0.07, photo_y=0.32, photo_w=0.17, photo_h=0.36,
                     qr_x=0.80, qr_y=0.74, qr_s=0.13,
                     seal_x=0.62, seal_y=0.85, seal_r=0.055)

ID_DEFAULTS = dict(text_x=0.33, row1_y=0.38, row2_y=0.52, row3_y=0.66, font_size=40,
                   photo_x=0.05, photo_y=0.28, photo_w=0.25, photo_h=0.52,
                   qr_x=0.76, qr_y=0.55, qr_s=0.19)


def render_certificate(rec, tpl, L, o, photo):
    img = tpl.convert("RGB").copy()
    W, H = img.size
    s = W / 1600
    d = ImageDraw.Draw(img)
    cx = int(L["text_cx"] * W)
    maxw = int(W * 0.58)
    d.text((cx, int(L["name_y"] * H)), rec["name"],
           font=fit_font(d, rec["name"], maxw, L["name_size"] * s, True, o["font"]), fill=NAVY, anchor="mm")
    d.text((cx, int(L["course_y"] * H)), rec["course"],
           font=fit_font(d, rec["course"], maxw, L["course_size"] * s, True, o["font"]), fill=(30, 60, 140), anchor="mm")
    if rec["date"]:
        txt = f"{o['date_prefix']}{rec['date']}"
        d.text((cx, int(L["date_y"] * H)), txt, font=get_font(L["date_size"] * s, False, o["font"]),
               fill=GREY, anchor="mm")
    if o["photo"]:
        box = (int(L["photo_x"] * W), int(L["photo_y"] * H), int(L["photo_w"] * W), int(L["photo_h"] * H))
        paste_photo(img, photo, box)
    url = build_verify_url(o["base_url"], "certificate", rec, o["org"], o["secret"])
    if o["qr"]:
        q = int(L["qr_s"] * W)
        qx, qy = int(L["qr_x"] * W), int(L["qr_y"] * H)
        img.paste(make_qr(url, q), (qx, qy))
        d.text((qx + q // 2, qy + q + int(22 * s)), f"ID: {rec['roll']}",
               font=get_font(22 * s, False, o["font"]), fill=GREY, anchor="mm")
        d.text((qx + q // 2, qy - int(14 * s)), "Scan to verify",
               font=get_font(20 * s, False, o["font"]), fill=GREY, anchor="mm")
    if o["seal"]:
        img = draw_seal(img, int(L["seal_x"] * W), int(L["seal_y"] * H), int(L["seal_r"] * W), o["font"])
    return img, url


def render_idcard(rec, tpl, L, o, photo):
    img = tpl.convert("RGB").copy()
    W, H = img.size
    s = H / 540
    d = ImageDraw.Draw(img)
    tx = int(L["text_x"] * W)
    vx = tx + int(0.20 * W)
    maxw = int(0.95 * W) - vx
    f_label = get_font(L["font_size"] * 0.7 * s, False, o["font"])
    rows = [("Name", rec["name"], L["row1_y"]), ("Course", rec["course"], L["row2_y"]),
            ("Roll No", rec["roll"], L["row3_y"])]
    for label, value, y in rows:
        yy = int(y * H)
        d.text((tx, yy), label, font=f_label, fill=GREY, anchor="lm")
        d.text((vx - int(14 * s), yy), ":", font=f_label, fill=GREY, anchor="lm")
        d.text((vx, yy), value, font=fit_font(d, value, maxw, L["font_size"] * s, True, o["font"]),
               fill=NAVY, anchor="lm")
    if o["photo"]:
        box = (int(L["photo_x"] * W), int(L["photo_y"] * H), int(L["photo_w"] * W), int(L["photo_h"] * H))
        paste_photo(img, photo, box)
    url = build_verify_url(o["base_url"], "idcard", rec, o["org"], o["secret"])
    if o["qr"]:
        img.paste(make_qr(url, int(L["qr_s"] * W)), (int(L["qr_x"] * W), int(L["qr_y"] * H)))
    return img, url


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


# ----------------------------------------------------------------------------
# Data helpers
# ----------------------------------------------------------------------------
def guess(cols, keys, allow_none=False):
    opts = (["(none)"] if allow_none else []) + list(cols)
    for i, c in enumerate(opts):
        if any(k in str(c).lower() for k in keys):
            return i
    return 0


def fmt_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(v).strftime("%d %B %Y")
    return str(v)


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s)).strip("_")[:40] or "x"


def sample_excel():
    df = pd.DataFrame({
        "Name": ["Akshat Singh", "Rahul Sharma", "Neha Verma", "Pooja Jain"],
        "Course": ["Data Analytics", "Python Developer", "Web Developer", "Data Science"],
        "Roll No": ["DA001", "PY002", "WD003", "DS004"],
        "Date": ["15 October 2026"] * 4,
        "Photo Path": ["akshat.jpg", "rahul.jpg", "neha.jpg", "pooja.jpg"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def layout_controls(title, defaults, spec, key):
    with st.expander(title):
        cols = st.columns(3)
        out = {}
        for i, (k, label, lo, hi, step) in enumerate(spec):
            with cols[i % 3]:
                out[k] = st.slider(label, lo, hi, defaults[k], step, key=f"{key}_{k}")
        return out


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------
def main():
    # QR code landing page
    if "v" in st.query_params and "s" in st.query_params:
        verification_page()
        return

    st.markdown('<div class="app-title">🎓 Certificate & ID Card Maker</div>', unsafe_allow_html=True)
    st.markdown('<div class="app-sub">Create multiple certificates and ID cards from an Excel file</div>',
                unsafe_allow_html=True)

    left, right = st.columns(2, gap="large")

    # ---------------- 1. Upload data ----------------
    with left.container(border=True):
        st.subheader("📊 1. Upload Excel File")
        data_file = st.file_uploader("Excel (.xlsx) or CSV", type=["xlsx", "xls", "csv"])
        st.download_button("⬇️ Download sample Excel", sample_excel(), "students_data.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        zip_file = st.file_uploader("Photos as a ZIP (optional)", type=["zip"])
        img_files = st.file_uploader("…or upload photos directly", type=["jpg", "jpeg", "png", "webp"],
                                     accept_multiple_files=True)
        st.caption("The Photo column should contain the file name (e.g. `akshat.jpg`) or a public image URL.")

    # ---------------- 2. Templates ----------------
    with right.container(border=True):
        st.subheader("🖼️ 2. Select Template")
        org = st.text_input("Institute / organisation name", "Global World Computer")
        cert_tpl_file = st.file_uploader("Certificate template (optional)", type=["png", "jpg", "jpeg"])
        id_tpl_file = st.file_uploader("ID card template (optional)", type=["png", "jpg", "jpeg"])
        font_file = st.file_uploader("Custom font .ttf (optional)", type=["ttf", "otf"])
        st.caption("No template? The built-in designs are used. Custom templates: tune positions "
                   "in *Advanced layout* below.")

    custom_font = None
    if font_file is not None:
        custom_font = os.path.join(tempfile.gettempdir(), f"cim_{slug(font_file.name)}")
        with open(custom_font, "wb") as fh:
            fh.write(font_file.getvalue())

    if data_file is None:
        st.info("👆 Upload your Excel/CSV file to get started (use the sample file to see the format).")
        return

    try:
        df = pd.read_csv(data_file) if data_file.name.lower().endswith(".csv") else pd.read_excel(data_file)
    except Exception as e:
        st.error(f"Could not read the file: {e}")
        return
    df = df.dropna(how="all")
    if df.empty:
        st.error("The file has no rows.")
        return
    cols = list(df.columns)

    # ---------------- 3. Columns ----------------
    with left.container(border=True):
        st.subheader("🧩 3. Select Columns from Excel")
        c_name = st.selectbox("Name column", cols, index=guess(cols, ["name"]))
        c_course = st.selectbox("Course column", cols, index=guess(cols, ["course", "program", "class"]))
        c_roll = st.selectbox("Roll No column", ["(none)"] + cols, index=guess(cols, ["roll", "id", "reg"], True))
        c_date = st.selectbox("Date column", ["(none)"] + cols, index=guess(cols, ["date"], True))
        c_photo = st.selectbox("Photo column", ["(none)"] + cols, index=guess(cols, ["photo", "image", "pic"], True))
        default_date = st.date_input("Default date (used if no date column)", date.today())

    # ---------------- 4. Output settings ----------------
    with right.container(border=True):
        st.subheader("⚙️ 4. Output Settings")
        what = st.multiselect("Generate", ["Certificates", "ID Cards"], default=["Certificates", "ID Cards"])
        o1, o2 = st.columns(2)
        fmt = o1.selectbox("File format", ["PNG", "JPG", "PDF"])
        size_label = o2.selectbox("ID card size", ["Standard (850 x 540)", "CR80 300dpi (1012 x 638)",
                                                   "Large (1275 x 810)"])
        id_size = {"Standard (850 x 540)": (850, 540), "CR80 300dpi (1012 x 638)": (1012, 638),
                   "Large (1275 x 810)": (1275, 810)}[size_label]
        f1, f2, f3 = st.columns(3)
        use_photo = f1.checkbox("Photo", True)
        use_qr = f2.checkbox("QR code", True)
        use_seal = f3.checkbox("Digital seal", True)
        date_prefix = st.text_input("Text before date on certificate", "on ")
        base_url = st.text_input("Verification URL (your deployed app link)", "http://localhost:8501",
                                 help="The QR code opens this URL. After deploying, paste your "
                                      "https://yourapp.streamlit.app link here.")
        st.text_input("Secret key for QR signatures", type="password", key="secret_input",
                      help="Keep it private. On Streamlit Cloud set SECRET_KEY in Secrets instead "
                           "so the verification page can validate codes.")
        if get_secret() == "change-this-secret":
            st.warning("Using the default secret key. Set your own so nobody can forge QR codes.")

    # ---------------- Advanced layout ----------------
    cert_spec = [
        ("text_cx", "Text centre X", 0.1, 0.9, 0.01), ("name_y", "Name Y", 0.1, 0.9, 0.005),
        ("course_y", "Course Y", 0.1, 0.9, 0.005), ("date_y", "Date Y", 0.1, 0.95, 0.005),
        ("name_size", "Name size", 30, 160, 2), ("course_size", "Course size", 24, 120, 2),
        ("date_size", "Date size", 20, 80, 2),
        ("photo_x", "Photo X", 0.0, 0.9, 0.005), ("photo_y", "Photo Y", 0.0, 0.9, 0.005),
        ("photo_w", "Photo width", 0.05, 0.4, 0.005), ("photo_h", "Photo height", 0.05, 0.6, 0.005),
        ("qr_x", "QR X", 0.0, 0.95, 0.005), ("qr_y", "QR Y", 0.0, 0.95, 0.005),
        ("qr_s", "QR size", 0.05, 0.3, 0.005),
        ("seal_x", "Seal X", 0.0, 1.0, 0.005), ("seal_y", "Seal Y", 0.0, 1.0, 0.005),
        ("seal_r", "Seal radius", 0.02, 0.15, 0.0025),
    ]
    id_spec = [
        ("text_x", "Text X", 0.0, 0.8, 0.005), ("row1_y", "Name row Y", 0.1, 0.95, 0.005),
        ("row2_y", "Course row Y", 0.1, 0.95, 0.005), ("row3_y", "Roll row Y", 0.1, 0.95, 0.005),
        ("font_size", "Font size", 16, 80, 1),
        ("photo_x", "Photo X", 0.0, 0.9, 0.005), ("photo_y", "Photo Y", 0.0, 0.9, 0.005),
        ("photo_w", "Photo width", 0.05, 0.5, 0.005), ("photo_h", "Photo height", 0.05, 0.9, 0.005),
        ("qr_x", "QR X", 0.0, 0.95, 0.005), ("qr_y", "QR Y", 0.0, 0.95, 0.005),
        ("qr_s", "QR size", 0.05, 0.4, 0.005),
    ]
    LC = layout_controls("🎛️ Advanced layout – certificate", CERT_DEFAULTS, cert_spec, "cert")
    LI = layout_controls("🎛️ Advanced layout – ID card", ID_DEFAULTS, id_spec, "id")

    # ---------------- Build records & templates ----------------
    photo_map = build_photo_map(zip_file, img_files)
    records = []
    for i, (_, row) in enumerate(df.iterrows()):
        records.append({
            "name": clean(row[c_name]) or "Unnamed",
            "course": clean(row[c_course]),
            "roll": clean(row[c_roll]) if c_roll != "(none)" else f"{i + 1:04d}",
            "date": fmt_date(row[c_date]) if c_date != "(none)" else default_date.strftime("%d %B %Y"),
            "photo": row[c_photo] if c_photo != "(none)" else None,
        })

    cert_tpl = Image.open(cert_tpl_file) if cert_tpl_file else default_certificate(org, custom_font)
    id_tpl = (ImageOps.fit(Image.open(id_tpl_file).convert("RGB"), id_size, Image.LANCZOS)
              if id_tpl_file else default_idcard(org, id_size, custom_font))
    opts = dict(font=custom_font, photo=use_photo, qr=use_qr, seal=use_seal, org=org,
                date_prefix=date_prefix, base_url=base_url, secret=get_secret())

    # ---------------- 5. Preview data ----------------
    with left.container(border=True):
        st.subheader("📋 5. Preview Data")
        prev = df.head(10).copy()
        if c_photo != "(none)":
            prev["Photo found"] = ["✅" if load_photo(r, photo_map) is not None else "❌"
                                   for r in df[c_photo].head(10)]
        st.dataframe(prev, use_container_width=True, hide_index=True)
        st.caption(f"{len(records)} record(s) loaded.")

    # ---------------- Live preview ----------------
    st.divider()
    st.subheader("👀 Live Preview")
    idx = st.number_input("Record #", 1, len(records), 1) - 1
    rec = records[idx]
    ph = load_photo(rec["photo"], photo_map)
    p1, p2 = st.columns([1.35, 1])
    if "Certificates" in what:
        with p1:
            st.caption("Sample Certificate (output)")
            st.image(render_certificate(rec, cert_tpl, LC, opts, ph)[0], use_container_width=True)
    if "ID Cards" in what:
        with p2:
            st.caption("Sample ID Card (output)")
            st.image(render_idcard(rec, id_tpl, LI, opts, ph)[0], use_container_width=True)

    # ---------------- Generate ----------------
    st.divider()
    if st.button("⚙️ Generate Certificates & ID Cards", type="primary", use_container_width=True):
        if not what:
            st.warning("Choose at least one thing to generate.")
            return
        zbuf, log = io.BytesIO(), []
        bar = st.progress(0.0, text="Starting…")
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, r in enumerate(records):
                photo = load_photo(r["photo"], photo_map)
                base = f"{i + 1:03d}_{slug(r['roll'])}_{slug(r['name'])}"
                if "Certificates" in what:
                    img, url = render_certificate(r, cert_tpl, LC, opts, photo)
                    data, ext = to_bytes(img, fmt)
                    z.writestr(f"certificates/{base}_certificate.{ext}", data)
                    log.append({"type": "certificate", "name": r["name"], "roll": r["roll"], "verify_url": url})
                if "ID Cards" in what:
                    img, url = render_idcard(r, id_tpl, LI, opts, photo)
                    data, ext = to_bytes(img, fmt)
                    z.writestr(f"id_cards/{base}_idcard.{ext}", data)
                    log.append({"type": "id_card", "name": r["name"], "roll": r["roll"], "verify_url": url})
                bar.progress((i + 1) / len(records), text=f"Generated {i + 1} of {len(records)}")
            z.writestr("verification_log.csv", pd.DataFrame(log).to_csv(index=False))
        st.session_state["result_zip"] = zbuf.getvalue()
        st.session_state["result_count"] = len(records)
        bar.empty()

    if st.session_state.get("result_zip"):
        st.success(f"✅ Done! {st.session_state['result_count']} record(s) processed.")
        st.download_button("📦 Download all (ZIP)", st.session_state["result_zip"],
                           "certificates_and_id_cards.zip", "application/zip", use_container_width=True)


main()
