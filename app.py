"""Certificate & ID Card Maker  (Streamlit)

- Bulk certificates / ID cards from Excel, each with a QR code that verifies against a database
- Permanent storage: PostgreSQL via DATABASE_URL in Secrets
- Only the admin (ADMIN_PASSWORD in Secrets) can generate; verification is public
- Each client organization has its own branding (logo, colours, name) on the verification page
- Clients can open a read-only portal to see and download only their own records
- No photos on certificates
"""
from __future__ import annotations

import base64
import html
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
    admin_gate, assign_unassigned, check_password, ensure_schema, font_picker, get_document, get_org, get_secret,
    hash_password, list_documents, list_orgs, save_documents, save_org, using_permanent_db,
)
from renderer import (
    DESIGNS, PALETTES, PAPERS, auto_text, draw_certificate, draw_id_card,
)


# ====================================================================
# BRANDING HELPERS
# ====================================================================
def safe_color(c, default: str = "#1D4ED8") -> str:
    c = (c or "").strip()
    return c if re.fullmatch(r"#[0-9a-fA-F]{6}", c) else default


def decode_logo(org):
    b64 = (org or {}).get("logo_b64")
    if not b64:
        return None
    try:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def encode_logo(img: Image.Image) -> str:
    img = img.convert("RGBA")
    img.thumbnail((500, 500))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def org_header_html(org) -> str:
    P = safe_color((org or {}).get("primary_color"), "#1D4ED8")
    e = html.escape
    if not org:
        return '<div style="text-align:center;font-size:24px;font-weight:700;margin:6px 0 22px">Document Verification</div>'
    logo = (f'<img src="data:image/png;base64,{org["logo_b64"]}" '
            f'style="max-height:90px;max-width:260px;margin-bottom:8px"><br>'
            if org.get("logo_b64") else "")
    tag = (f'<div style="opacity:.75;margin-top:2px">{e(org["tagline"])}</div>'
           if org.get("tagline") else "")
    return (f'<div style="text-align:center;margin:6px 0 22px">{logo}'
            f'<div style="font-size:28px;font-weight:700;color:{P}">{e(org["name"])}</div>{tag}</div>')


def verification_html(rec, org) -> str:
    """The public verification card, branded with the client's logo, name and colours."""
    P = safe_color((org or {}).get("primary_color"), "#1D4ED8")
    e = html.escape
    header = org_header_html(org)

    if rec:
        kind = "Certificate" if rec["doc_type"] == "certificate" else "ID card"
        fields = [("Document ID", rec["doc_id"]), ("Type", kind), ("Course / Award", rec.get("course")),
                  ("Role", rec.get("role")), ("Department", rec.get("department")),
                  ("ID number", rec.get("id_number")), ("Issued on", rec.get("issued_on"))]
        rows = "".join(f'<div style="margin:4px 0"><b>{e(k)}:</b> {e(str(v))}</div>' for k, v in fields if v)
        issuer = f" issued by {e(org['name'])}" if org else ""
        body = (
            '<div style="border-left:8px solid #15803D;background:rgba(21,128,61,.10);'
            'padding:18px 22px;border-radius:10px">'
            f'<div style="font-size:19px;font-weight:700;color:#15803D">&#10004; Authentic {kind}{issuer}</div>'
            f'<div style="font-size:30px;font-weight:700;margin:10px 0 12px;color:{P}">{e(rec["name"])}</div>'
            f'{rows}</div>'
        )
    else:
        body = ('<div style="border-left:8px solid #B91C1C;background:rgba(185,28,28,.10);'
                'padding:18px 22px;border-radius:10px"><div style="font-size:19px;font-weight:700;color:#B91C1C">'
                '&#10008; No record found</div><div style="margin-top:6px">This document could not be verified. '
                'Check the ID and try again, or contact the issuing organization.</div></div>')

    footer = ""
    if org:
        bits = []
        if org.get("website", "").startswith(("http://", "https://")):
            bits.append(f'<a href="{e(org["website"])}" target="_blank" rel="noopener">{e(org["website"])}</a>')
        if org.get("contact"):
            bits.append(e(org["contact"]))
        note = f'<div style="margin-bottom:4px">{e(org["footer_note"])}</div>' if org.get("footer_note") else ""
        footer = (f'<div style="text-align:center;opacity:.8;margin-top:22px;font-size:14px">{note}'
                  f'{" &nbsp;|&nbsp; ".join(bits)}</div>')
    return f'<div style="max-width:640px;margin:0 auto">{header}{body}{footer}</div>'


def show_verification(raw_id: str):
    doc_id = (raw_id or "").strip()[:64]
    if not doc_id:
        return
    rec = get_document(doc_id)
    org = get_org(rec.get("org_id")) if rec else None
    st.markdown(verification_html(rec, org), unsafe_allow_html=True)


# ====================================================================
# START-UP  (database first, so the verification page can use the client's name and logo)
# ====================================================================
_db_error = None
try:
    ensure_schema()
except Exception as _e:  # noqa: BLE001
    _db_error = _e

_verify_id = st.query_params.get("verify")
_org_for_page = None
if _verify_id and not _db_error:
    try:
        _d = get_document(str(_verify_id).strip()[:64])
        _org_for_page = get_org(_d.get("org_id")) if _d else None
    except Exception:  # noqa: BLE001
        pass

if _verify_id:
    _logo = decode_logo(_org_for_page)
    st.set_page_config(
        page_title=(_org_for_page["name"] if _org_for_page else "Document verification"),
        page_icon=(_logo if _logo is not None else "✅"),
        layout="centered", initial_sidebar_state="collapsed",
    )
else:
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

if _db_error:
    st.error("Could not connect to the database. Check DATABASE_URL in Secrets.")
    st.caption(f"Details: {type(_db_error).__name__}")
    st.stop()


# ====================================================================
# PUBLIC VERIFICATION PAGE  (QR codes open  <app url>/?verify=<ID>)
# ====================================================================
if _verify_id:
    st.markdown(
        "<style>[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl'], "
        "[data-testid='stExpandSidebarButton'] {display:none !important;}</style>",
        unsafe_allow_html=True,
    )
    show_verification(str(_verify_id))
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
            "or by scanning its QR code. Clients can open the Client portal tab. "
            "Admins: log in from the sidebar.")
    tab_ver, tab_portal = st.tabs(["Verify a document", "Client portal"])
    tab_gen = tab_org = tab_rec = None
else:
    tab_gen, tab_org, tab_rec, tab_ver, tab_portal = st.tabs(
        ["Generate", "Organizations", "Issued records", "Verify a document", "Client portal"])

# -------------------------------------------------------------- Verify (public)
with tab_ver:
    vid = st.text_input("Enter a document ID (e.g. CERT-2026-1A2B3C4D)")
    if vid:
        show_verification(vid)

# -------------------------------------------------------------- Client portal (read-only)
with tab_portal:
    st.write("Organizations can view and download the documents issued in their name. "
             "Use the Organization ID and access password you were given.")
    if not st.session_state.get("portal_org"):
        tries = st.session_state.get("portal_tries", 0)
        if tries >= 5:
            st.error("Too many wrong attempts. Refresh the page to try again.")
        else:
            with st.form("portal_login"):
                p_id = st.text_input("Organization ID")
                p_pw = st.text_input("Access password", type="password")
                p_go = st.form_submit_button("Open portal")
            if p_go:
                o = get_org(p_id.strip().lower())
                if o and o.get("portal_hash") and check_password(p_pw, o["portal_hash"]):
                    st.session_state.portal_org = o["org_id"]
                    st.session_state.portal_tries = 0
                    st.rerun()
                else:
                    st.session_state.portal_tries = tries + 1
                    st.error("Wrong Organization ID or password.")
    else:
        p_org = get_org(st.session_state.portal_org)
        if p_org is None:
            st.session_state.portal_org = None
            st.rerun()
        st.markdown(org_header_html(p_org), unsafe_allow_html=True)
        docs = list_documents(5000, p_org["org_id"])
        st.write(f"**{len(docs)}** documents issued for {p_org['name']}.")
        if docs:
            dfp = pd.DataFrame(docs).drop(columns=["org_id"], errors="ignore")
            st.dataframe(dfp)
            st.download_button("Download my records (CSV)", dfp.to_csv(index=False).encode(),
                               f"{p_org['org_id']}_records.csv", "text/csv")
        if st.button("Log out of portal"):
            st.session_state.portal_org = None
            st.rerun()

if not is_admin:
    st.stop()

# -------------------------------------------------------------- Organizations (admin)
with tab_org:
    st.write("Each client (school, company, institution) is an organization. Its logo, colours and name "
             "appear on the public verification page for every document you issue for it.")
    if st.session_state.get("org_flash"):
        st.success(st.session_state.pop("org_flash"))
    orgs_now = list_orgs()
    labels = ["➕ New organization"] + [f"{o['name']}  ({o['org_id']})" for o in orgs_now]
    pick = st.selectbox("Organization", labels)
    cur = orgs_now[labels.index(pick) - 1] if labels.index(pick) > 0 else {}
    ck = cur.get("org_id", "new")

    with st.form(f"org_form_{ck}"):
        f_id = st.text_input("Organization ID (short, lowercase, no spaces, e.g. kisumu-high)",
                             value=cur.get("org_id", ""), disabled=bool(cur))
        f_name = st.text_input("Organization name", value=cur.get("name", ""))
        f_tag = st.text_input("Motto / tagline (optional)", value=cur.get("tagline") or "")
        f_logo = st.file_uploader("Logo (leave empty to keep the current one)", type=["png", "jpg", "jpeg"])
        k1, k2 = st.columns(2)
        f_p = k1.color_picker("Main brand colour", safe_color(cur.get("primary_color"), "#1D4ED8"))
        f_s = k2.color_picker("Second brand colour", safe_color(cur.get("secondary_color"), "#B8860B"))
        f_web = st.text_input("Website (optional, start with https://)", value=cur.get("website") or "")
        f_contact = st.text_input("Contact email / phone (optional)", value=cur.get("contact") or "")
        f_note = st.text_input("Footer note on the verification page (optional)",
                               value=cur.get("footer_note") or "")
        f_pw = st.text_input("Client portal password (leave empty to keep the current one)", type="password")
        saved = st.form_submit_button("Save organization", type="primary")

    if saved:
        oid = (cur.get("org_id") or f_id).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", oid):
            st.error("Organization ID: 2-41 characters, lowercase letters, numbers and dashes only.")
        elif not f_name.strip():
            st.error("Organization name is required.")
        elif not cur and get_org(oid):
            st.error("That Organization ID already exists. Pick it from the list to edit it.")
        else:
            save_org({
                "org_id": oid, "name": f_name.strip(), "tagline": f_tag.strip(),
                "logo_b64": encode_logo(Image.open(f_logo)) if f_logo else cur.get("logo_b64"),
                "primary_color": f_p, "secondary_color": f_s,
                "website": f_web.strip(), "contact": f_contact.strip(), "footer_note": f_note.strip(),
                "portal_hash": hash_password(f_pw) if f_pw else cur.get("portal_hash"),
            })
            st.session_state.org_flash = f"Saved {f_name.strip()}."
            st.rerun()

    if cur:
        st.markdown("**How the verification page looks for this organization:**")
        st.markdown(verification_html(
            {"doc_id": "CERT-2026-SAMPLE", "doc_type": "certificate", "name": "Jane Wanjiru",
             "course": "Sample Course", "issued_on": fmt_date("")}, cur), unsafe_allow_html=True)
        if not cur.get("portal_hash"):
            st.info("No portal password set: this client cannot open the Client portal yet.")

# -------------------------------------------------------------- Issued records (admin)
with tab_rec:
    org_names = {o["org_id"]: o["name"] for o in list_orgs()}
    flt = st.selectbox("Show records for", ["All organizations"] + list(org_names.values()))
    flt_id = next((k for k, v in org_names.items() if v == flt), None)
    recs_db = list_documents(2000, flt_id)
    if recs_db:
        df_rec = pd.DataFrame(recs_db)
        df_rec["organization"] = df_rec["org_id"].map(org_names).fillna("(none)")
        st.dataframe(df_rec)
        st.download_button("Download records (CSV)", df_rec.to_csv(index=False).encode(),
                           "issued_records.csv", "text/csv")
    else:
        st.write("Nothing issued yet.")

    n_free = sum(1 for d in list_documents(5000) if not d.get("org_id"))
    if n_free and org_names:
        st.divider()
        st.write(f"**{n_free}** documents were issued before organizations existed and show no branding.")
        tgt = st.selectbox("Assign them to", list(org_names.values()), key="assign_target")
        if st.button("Assign these documents"):
            done = assign_unassigned(next(k for k, v in org_names.items() if v == tgt))
            st.success(f"{done} documents assigned to {tgt}.")

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

    orgs = list_orgs()
    if not orgs:
        st.warning("Create your first client organization in the **Organizations** tab, then come back here.")
        st.stop()
    org_map = {f"{o['name']}  ({o['org_id']})": o for o in orgs}
    org = org_map[st.selectbox("Issue documents for which organization?", list(org_map))]
    org_id = org["org_id"]
    org_has_colours = bool(safe_color(org.get("primary_color"), "") and safe_color(org.get("secondary_color"), ""))

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
        palette_options = (["Organization's colours"] if org_has_colours else []) + \
            ["Design's own colours"] + list(PALETTES) + ["Custom colours"]
        palette = st.selectbox("Colour palette", palette_options, key=f"palette_{org_id}")
        if palette == "Organization's colours":
            primary = safe_color(org["primary_color"], "#1D4ED8")
            secondary = safe_color(org["secondary_color"], "#B8860B")
        elif palette == "Design's own colours":
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
        company = st.text_input("Organisation name shown on the document", value=org["name"],
                                key=f"company_{org_id}")
        logo_file = st.file_uploader(
            "Logo (the organization's saved logo is used unless you upload a different one here)",
            type=["png", "jpg", "jpeg"])
        logo = Image.open(logo_file).convert("RGBA") if logo_file else decode_logo(org)
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
                        "issued_on": rec["date"], "org_id": org_id,
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
