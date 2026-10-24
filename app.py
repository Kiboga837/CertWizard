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
from renderer import DESIGNS, draw_certificate, draw_id_card

st.set_page_config(page_title="Certificate & ID Card Maker", page_icon="🎓", layout="wide")

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
with tab_gen:
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

    st.subheader("3. Design and colours")
    c1, c2 = st.columns(2)
    with c1:
        design_name = st.selectbox("Design", list(DESIGNS))
        design = DESIGNS[design_name]
        colour_mode = st.radio("Colours", ["Design colours", "Company colours"], horizontal=True)
        primary, secondary = design["primary"], design["secondary"]
        if colour_mode == "Company colours":
            primary = st.color_picker("Main company colour", design["primary"], key=f"pc_{design_name}")
            secondary = st.color_picker("Second company colour", design["secondary"], key=f"sc_{design_name}")
    with c2:
        company = st.text_input("Company / organisation name")
        logo_file = st.file_uploader("Logo (optional)", type=["png", "jpg", "jpeg"])
        logo = Image.open(logo_file).convert("RGBA") if logo_file else None

    st.subheader("4. Wording and fonts")
    c3, c4 = st.columns(2)
    with c3:
        title = st.text_input("Certificate title", "Certificate of Completion")
        intro = st.text_input("Line above the name", "This is to certify that")
        body = st.text_area(
            "Body text (you can use {name} {course} {date} {role} {department} {id})",
            "has successfully completed the course in {course} and is hereby awarded this certificate "
            "on {date}.", height=120)
        sig_name = st.text_input("Signatory name")
        sig_title = st.text_input("Signatory title")
    with c4:
        heading_font = font_picker("Heading font (title and name)", "heading", prefer="Playfair")
        body_font = font_picker("Body font", "body", prefer="DejaVu Sans")

    cfg = {
        "style": design["style"], "bg": design["bg"], "text": design["text"],
        "primary": primary, "secondary": secondary,
        "company": company, "logo": logo,
        "title": title, "intro": intro, "body": body,
        "signatory": sig_name, "signatory_title": sig_title,
        "heading_font": heading_font, "body_font": body_font,
    }

    st.subheader("5. Preview and generate")
    sample = records[0] if records else {
        "name": "Jane Wanjiru", "course": "Insurance Fundamentals", "date": fmt_date(""),
        "role": "Underwriter", "department": "General Insurance", "id_number": "EMP001"}
    preview_url = f"{app_url or 'https://example.com'}/?verify=PREVIEW"
    pc1, pc2 = st.columns(2)
    if want_cert:
        pc1.image(draw_certificate(sample, cfg, "CERT-PREVIEW", preview_url), caption="Certificate preview")
    if want_id:
        ph = photos.get(sample.get("id_number", "").lower()) or photos.get(sample["name"].lower())
        pc2.image(draw_id_card(sample, cfg, "ID-PREVIEW", preview_url, ph), caption="ID card preview")

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
