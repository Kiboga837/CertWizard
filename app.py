"""Certificate & ID Card Maker  (Streamlit)

- Bulk certificates / ID cards from Excel, each with a QR code that verifies against a database
- Permanent storage: PostgreSQL via DATABASE_URL in Secrets
- Only the admin (ADMIN_PASSWORD in Secrets) can generate; verification is public
- No photos on certificates
"""
from __future__ import annotations

import io
import re
import secrets as pysecrets
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

from certificate_helpers import (
    admin_gate, font_picker, get_document, get_secret, init_db,
    list_documents, save_documents, using_permanent_db,
)
from renderer import (
    DESIGNS, PALETTES, PAPERS, auto_text, draw_certificate, draw_id_card,
)

st.set_page_config(page_title="Certificate & ID Card Maker", page_icon="🎓", layout="wide")

# Hide the top-right toolbar (edit pencil, GitHub link, menu), the footer and the viewer badge
st.markdown(
    """
    <style>
    [data-testid="stToolbar"], [data-testid="stToolbarActions"], [data-testid="stAppDeployButton"],
    [data-testid="stMainMenu"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    #MainMenu, footer, .stDeployButton, .viewerBadge_container__r5tak,
    [class*="viewerBadge"], [class*="StyledViewerBadge"], a[href*="github.com"][class*="viewer"] {
        display: none !important; visibility: hidden !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

try:
    init_db()
except Exception as e:  # noqa: BLE001
    st.error("Could not connect to the database. Check DATABASE_URL in Secrets.")
    st.caption(f"Details: {type(e).__name__}")
    st.stop()


# ====================================================================
# PUBLIC VERIFICATION  (QR codes open  <app url>/?verify=<ID>)
# ====================================================================
def show_verification(raw_id: str):
    doc_id = (raw_id or "").strip()[:64]
    rec = get_document(doc_id) if doc_id else None
    if rec:
        kind = "Certificate" if rec["doc_type"] == "certificate" else "ID card"
        st.success(f"✅ Authentic {kind}")
        st.subheader(rec["name"])
        rows = {
            "Document ID": rec["doc_id"],
            "Type": kind,
            "Course / Award": rec.get("course"),
            "Role": rec.get("role"),
            "Department": rec.get("department"),
            "ID number": rec.get("id_number"),
            "Issued on": rec.get("issued_on"),
        }
        for k, v in rows.items():
            if v:
                st.write(f"**{k}:** {v}")
    elif doc_id:
        st.error("❌ No record found. This document could not be verified.")


if st.query_params.get("verify"):
    st.title("🎓 Document Verification")
    show_verification(st.query_params.get("verify"))
    st.stop()


# ====================================================================
# HELPERS
# ====================================================================
ALIASES = {
    "name": "name", "full name": "name",
    "course": "course", "programme": "course", "program": "course", "title": "course",
    "date": "date", "issue date": "date",
    "role": "role", "position": "role",
    "department": "department", "dept": "department",
    "id number": "id_number", "id no": "id_number", "id": "id_number", "id_number": "id_number",
}


def fmt_date(v) -> str:
    v = str(v).strip()
    if not v:
        return date.today().strftime("%d %B %Y")
    try:
        return pd.to_datetime(v).strftime("%d %B %Y")
    except Exception:  # noqa: BLE001
        return v


def load_records(file) -> list[dict]:
    df = pd.read_excel(file, dtype=str).fillna("")
    df.columns = [ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns]
    if "name" not in df.columns:
        raise ValueError("Your Excel file needs a 'Name' column.")
    recs = []
    for _, row in df.iterrows():
        r = {k: str(row.get(k, "")).strip() for k in
             ("name", "course", "date", "role", "department", "id_number")}
        if r["name"]:
            r["date"] = fmt_date(r["date"])
            recs.append(r)
    return recs


def template_bytes() -> bytes:
    df = pd.DataFrame({
        "Name": ["Jane Wanjiru", "John Otieno"],
        "Course": ["Insurance Fundamentals", "Data Analytics"],
        "Date": ["2026-10-24", "2026-10-24"],
        "Role": ["Underwriter", "Analyst"],
        "Department": ["General Insurance", "Actuarial"],
        "ID Number": ["EMP001", "EMP002"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def load_photos(zip_file) -> dict:
    photos = {}
    with zipfile.ZipFile(zip_file) as z:
        for n in z.namelist():
            if n.endswith("/") or not n.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            try:
                photos[Path(n).stem.lower().strip()] = Image.open(io.BytesIO(z.read(n))).convert("RGB")
            except Exception:  # noqa: BLE001
                pass
    return photos


def new_id(doc_type: str) -> str:
    prefix = "CERT" if doc_type == "certificate" else "ID"
    return f"{prefix}-{date.today().year}-{pysecrets.token_hex(4).upper()}"


def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "file"


def pdf_bytes(images: list) -> bytes:
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=150, quality=95)
    return buf.getvalue()


# ====================================================================
# MAIN APP
# ====================================================================
st.title("🎓 Certificate & ID Card Maker")

if not using_permanent_db():
    st.sidebar.warning("No DATABASE_URL set: using temporary storage. Records are lost when the app restarts.")

is_admin = admin_gate()

if not is_admin:
    st.info("Only the admin can generate documents. Anyone can verify a document below "
            "or by scanning its QR code. Admins: log in from the sidebar.")
    tabs = st.tabs(["Verify a document"])
    tab_gen = tab_rec = None
    tab_ver = tabs[0]
else:
    tab_gen, tab_rec, tab_ver = st.tabs(["Generate", "Issued records", "Verify a document"])

with tab_ver:
    vid = st.text_input("Enter a document ID (e.g. CERT-2026-1A2B3C4D)")
    if vid:
        show_verification(vid)

if not is_admin:
    st.stop()

# -------------------------------------------------------------- Issued records
with tab_rec:
    recs_db = list_documents()
    if recs_db:
        df_rec = pd.DataFrame(recs_db)
        st.dataframe(df_rec)
        st.download_button("Download records (CSV)", df_rec.to_csv(index=False).encode(),
                           "issued_records.csv", "text/csv")
    else:
        st.write("Nothing issued yet.")

# -------------------------------------------------------------- Generate
SAMPLE = {
    "name": "Jane Wanjiru", "course": "Insurance Fundamentals", "date": fmt_date(""),
    "role": "Underwriter", "department": "General Insurance", "id_number": "EMP001",
}


def swatch(*colors):
    blocks = "".join(
        f'<span style="display:inline-block;width:34px;height:34px;border-radius:6px;'
        f'margin-right:6px;border:1px solid #9994;background:{c}"></span>' for c in colors)
    st.markdown(blocks, unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def design_thumb(design_name, primary, secondary, bg, text, f_company, f_title, f_name, f_body, f_label):
    dg = DESIGNS[design_name]
    cfg = {
        "style": dg["style"], "bg": bg, "text": text, "primary": primary, "secondary": secondary,
        "company": "Your Company", "logo": None, "title": "Certificate of Completion",
        "intro": "This is to certify that", "body": "has successfully completed the course.",
        "signatories": [{"name": "A. Signatory", "title": "Director"}],
        "fonts": {"company": f_company, "title": f_title, "name": f_name, "body": f_body, "label": f_label},
    }
    return draw_certificate(SAMPLE, cfg, "CERT-SAMPLE", "https://example.com/?verify=SAMPLE").resize((520, 368))


with tab_gen:
    st.session_state.setdefault("design", "Classic Gold")

    app_url = st.text_input(
        "Your app's web address (used inside the QR codes)",
        value=get_secret("APP_URL"),
        placeholder="https://your-app-name.streamlit.app",
        help="Set APP_URL in Secrets so you don't have to type it. QR codes open this address.",
    ).strip().rstrip("/")

    st.subheader("1. What do you want to generate?")
    mode = st.radio("Output", ["Certificates", "ID cards", "Both"], horizontal=True,
                    label_visibility="collapsed")
    want_cert = mode in ("Certificates", "Both")
    want_id = mode in ("ID cards", "Both")

    st.subheader("2. Upload your Excel file")
    st.download_button("Download Excel template", template_bytes(), "template.xlsx")
    up = st.file_uploader("Excel file (.xlsx) with a 'Name' column", type=["xlsx"])
    records, photos = [], {}
    if up:
        try:
            records = load_records(up)
            st.success(f"{len(records)} people found.")
            st.dataframe(pd.DataFrame(records).head(20))
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the file: {e}")
    if want_id:
        pz = st.file_uploader(
            "Optional: ZIP of photos for ID cards only (file names = ID Number or Name, e.g. EMP001.jpg). "
            "Certificates never show photos.", type=["zip"])
        if pz:
            photos = load_photos(pz)
            st.caption(f"{len(photos)} photos loaded.")

    # ---------------- colours
    st.subheader("3. Colours")
    design = DESIGNS[st.session_state.design]
    cc1, cc2 = st.columns(2)
    with cc1:
        palette = st.selectbox("Colour palette", ["Design's own colours"] + list(PALETTES) + ["Custom colours"])
        if palette == "Design's own colours":
            primary, secondary = design["primary"], design["secondary"]
        elif palette == "Custom colours":
            k1, k2 = st.columns(2)
            primary = k1.color_picker("Main colour", "#1D4ED8", key="custom_primary")
            secondary = k2.color_picker("Second colour (names, labels)", "#B8860B", key="custom_secondary")
        else:
            primary, secondary = PALETTES[palette]
        swatch(primary, secondary)
        with st.expander("See all palettes"):
            for pname, (p1, p2) in PALETTES.items():
                st.caption(pname)
                swatch(p1, p2)
    with cc2:
        paper = st.selectbox("Paper colour", ["Design's own"] + list(PAPERS) + ["Custom"])
        if paper == "Design's own":
            bg, text_col = design["bg"], design["text"]
        else:
            bg = st.color_picker("Paper colour", "#FFFFFF", key="custom_paper") if paper == "Custom" else PAPERS[paper]
            text_col = auto_text(bg)
        swatch(bg)

    # ---------------- fonts
    st.subheader("4. Fonts (every text has its own dropdown)")
    fc = st.columns(5)
    with fc[0]:
        f_company = font_picker("Company name", "f_company", prefer="DejaVuSans")
    with fc[1]:
        f_title = font_picker("Title", "f_title", prefer="DejaVuSerif Bold")
    with fc[2]:
        f_name = font_picker("Recipient name", "f_name", prefer="DejaVuSerif Bold")
    with fc[3]:
        f_body = font_picker("Body text", "f_body", prefer="DejaVuSans")
    with fc[4]:
        f_label = font_picker("Dates, signatures, captions", "f_label", prefer="DejaVuSans")
    fonts = {"company": f_company, "title": f_title, "name": f_name, "body": f_body, "label": f_label}

    # ---------------- design gallery
    st.subheader("5. Choose a design")
    st.caption("Previews use your colours and fonts. Click Select under the one you like.")
    names = list(DESIGNS)
    for row in range(0, len(names), 3):
        cols = st.columns(3)
        for col, dn in zip(cols, names[row:row + 3]):
            dg = DESIGNS[dn]
            if palette == "Design's own colours":
                tp, ts = dg["primary"], dg["secondary"]
            else:
                tp, ts = primary, secondary
            tbg, ttx = (dg["bg"], dg["text"]) if paper == "Design's own" else (bg, text_col)
            with col:
                col.image(design_thumb(dn, tp, ts, tbg, ttx, f_company, f_title, f_name, f_body, f_label))
                chosen = st.session_state.design == dn
                if col.button(f"✓ {dn}" if chosen else f"Select {dn}", key=f"pick_{dn}",
                              type="primary" if chosen else "secondary"):
                    st.session_state.design = dn
                    st.rerun()

    # ---------------- wording + organisation
    st.subheader("6. Wording, logo and signatories")
    w1, w2 = st.columns(2)
    with w1:
        company = st.text_input("Company / organisation name")
        logo_file = st.file_uploader("Logo (optional)", type=["png", "jpg", "jpeg"])
        logo = Image.open(logo_file).convert("RGBA") if logo_file else None
        title = st.text_input("Certificate title", "Certificate of Completion")
        intro = st.text_input("Line above the name", "This is to certify that")
        body = st.text_area(
            "Body text (you can use {name} {course} {date} {role} {department} {id})",
            "has successfully completed the course in {course} and is hereby awarded this certificate "
            "on {date}.", height=120)
    with w2:
        sig_mode = st.radio("Who signs the certificates?",
                            ["No signature", "One signatory", "Two signatories"], index=1)
        n_sig = {"No signature": 0, "One signatory": 1, "Two signatories": 2}[sig_mode]
        signatories = []
        for i in range(n_sig):
            side = "" if n_sig == 1 else (" (left)" if i == 0 else " (right)")
            st.markdown(f"**Signatory {i + 1}{side}**")
            sn = st.text_input("Name", key=f"sig_name_{i}")
            stt = st.text_input("Title / position", key=f"sig_title_{i}")
            sf = st.file_uploader("Signature image (optional, PNG with transparent background is best)",
                                  type=["png", "jpg", "jpeg"], key=f"sig_img_{i}")
            signatories.append({
                "name": sn, "title": stt,
                "image": Image.open(sf).convert("RGBA") if sf else None,
            })

    cfg = {
        "style": design["style"], "bg": bg, "text": text_col,
        "primary": primary, "secondary": secondary,
        "company": company, "logo": logo,
        "title": title, "intro": intro, "body": body,
        "signatories": signatories, "fonts": fonts,
    }

    # ---------------- preview + generate
    st.subheader("7. Preview and generate")
    sample = records[0] if records else SAMPLE
    preview_url = f"{app_url or 'https://example.com'}/?verify=PREVIEW"
    if want_cert:
        st.image(draw_certificate(sample, cfg, "CERT-PREVIEW", preview_url), caption="Certificate preview")
    if want_id:
        ph = photos.get(sample.get("id_number", "").lower()) or photos.get(sample["name"].lower())
        st.image(draw_id_card(sample, cfg, "ID-PREVIEW", preview_url, ph), caption="ID card preview")

    if st.button("Generate all", type="primary", disabled=not records):
        if not app_url:
            st.error("Enter your app's web address above so the QR codes work.")
        else:
            types = (["certificate"] if want_cert else []) + (["id_card"] if want_id else [])
            images = {t: [] for t in types}
            files, db_rows = [], []
            bar = st.progress(0.0, text="Generating...")
            total = len(records) * len(types)
            done = 0
            for rec in records:
                for t in types:
                    doc_id = new_id(t)
                    url = f"{app_url}/?verify={doc_id}"
                    if t == "certificate":
                        im = draw_certificate(rec, cfg, doc_id, url)
                    else:
                        ph = photos.get(rec.get("id_number", "").lower()) or photos.get(rec["name"].lower())
                        im = draw_id_card(rec, cfg, doc_id, url, ph)
                    images[t].append(im)
                    b = io.BytesIO()
                    im.save(b, "PNG")
                    files.append((f"{t}_{safe(rec['name'])}_{doc_id}.png", b.getvalue()))
                    db_rows.append({
                        "doc_id": doc_id, "doc_type": t, "name": rec["name"],
                        "course": rec.get("course", ""), "role": rec.get("role", ""),
                        "department": rec.get("department", ""), "id_number": rec.get("id_number", ""),
                        "issued_on": rec["date"],
                    })
                    done += 1
                    bar.progress(done / total, text=f"Generating {done} of {total}...")
            save_documents(db_rows)  # saved only after every document rendered successfully
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                for fn, data in files:
                    z.writestr(fn, data)
            st.session_state.results = {
                "zip": zbuf.getvalue(),
                "pdfs": {t: pdf_bytes(ims) for t, ims in images.items()},
                "count": len(db_rows),
            }
            bar.empty()

    res = st.session_state.get("results")
    if res:
        st.success(f"{res['count']} documents generated and saved to the database.")
        d1, d2, d3 = st.columns(3)
        d1.download_button("Download all (ZIP of PNGs)", res["zip"], "documents.zip")
        if "certificate" in res["pdfs"]:
            d2.download_button("Certificates (PDF)", res["pdfs"]["certificate"], "certificates.pdf")
        if "id_card" in res["pdfs"]:
            d3.download_button("ID cards (PDF)", res["pdfs"]["id_card"], "id_cards.pdf")
