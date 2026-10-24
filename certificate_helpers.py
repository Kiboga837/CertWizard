"""Database (PostgreSQL), admin login and font picker for the Certificate & ID Card Maker."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
from datetime import datetime
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine, inspect, text


# ------------------------------------------------------------------
# Secrets (Streamlit Secrets online, environment variables locally)
# ------------------------------------------------------------------
def get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets[name])
    except Exception:
        return os.getenv(name, default)


# ------------------------------------------------------------------
# 1. DATABASE  (PostgreSQL = permanent; SQLite = temporary fallback)
# ------------------------------------------------------------------
def using_permanent_db() -> bool:
    return bool(get_secret("DATABASE_URL"))


@st.cache_resource(show_spinner=False)
def get_engine():
    url = get_secret("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return create_engine(
            url, pool_pre_ping=True, pool_recycle=300,
            connect_args={"connect_timeout": 20},
        )
    return create_engine(
        "sqlite:///certificates.db", connect_args={"check_same_thread": False}
    )


def init_db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS organizations (
                org_id          VARCHAR(64) PRIMARY KEY,
                name            TEXT NOT NULL,
                tagline         TEXT,
                logo_b64        TEXT,
                primary_color   VARCHAR(9),
                secondary_color VARCHAR(9),
                website         TEXT,
                contact         TEXT,
                footer_note     TEXT,
                portal_hash     TEXT,
                created_at      VARCHAR(40)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id      VARCHAR(64) PRIMARY KEY,
                doc_type    VARCHAR(20) NOT NULL,
                name        TEXT NOT NULL,
                course      TEXT,
                role        TEXT,
                department  TEXT,
                id_number   TEXT,
                issued_on   VARCHAR(40),
                created_at  VARCHAR(40),
                org_id      VARCHAR(64)
            )
        """))
    # older databases were created without some columns: add them once
    insp = inspect(engine)
    doc_cols = [c["name"] for c in insp.get_columns("documents")]
    if "org_id" not in doc_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE documents ADD COLUMN org_id VARCHAR(64)"))
    org_cols = [c["name"] for c in insp.get_columns("organizations")]
    if "sheet_id" not in org_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE organizations ADD COLUMN sheet_id VARCHAR(128)"))


@st.cache_resource(show_spinner=False)
def ensure_schema() -> bool:
    init_db()
    return True


def save_documents(rows: list):
    """rows: list of dicts (must include org_id). One transaction for the whole batch."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    payload = [{**r, "created_at": now} for r in rows]
    with get_engine().begin() as conn:
        conn.execute(
            text("""INSERT INTO documents
                    (doc_id, doc_type, name, course, role, department, id_number,
                     issued_on, created_at, org_id)
                    VALUES (:doc_id, :doc_type, :name, :course, :role, :department,
                            :id_number, :issued_on, :created_at, :org_id)"""),
            payload,
        )


def get_document(doc_id: str):
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM documents WHERE doc_id = :i"), {"i": doc_id}
        ).mappings().first()
    return dict(row) if row else None


def list_documents(limit: int = 1000, org_id: str | None = None):
    sql = "SELECT * FROM documents"
    params: dict = {"n": limit}
    if org_id:
        sql += " WHERE org_id = :o"
        params["o"] = org_id
    sql += " ORDER BY created_at DESC LIMIT :n"
    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


# ---- organizations (the admin's clients) ----
ORG_COLUMNS = ["name", "tagline", "logo_b64", "primary_color", "secondary_color",
               "website", "contact", "footer_note", "portal_hash", "sheet_id"]


def save_org(org: dict):
    """Insert or update an organization. org must contain org_id and every ORG_COLUMNS key."""
    params = {k: org.get(k) for k in ["org_id"] + ORG_COLUMNS}
    with get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM organizations WHERE org_id = :org_id"), params
        ).first()
        if exists:
            sets = ", ".join(f"{c} = :{c}" for c in ORG_COLUMNS)
            conn.execute(text(f"UPDATE organizations SET {sets} WHERE org_id = :org_id"), params)
        else:
            params["created_at"] = datetime.utcnow().isoformat(timespec="seconds")
            cols = ", ".join(["org_id"] + ORG_COLUMNS + ["created_at"])
            vals = ", ".join(":" + c for c in ["org_id"] + ORG_COLUMNS + ["created_at"])
            conn.execute(text(f"INSERT INTO organizations ({cols}) VALUES ({vals})"), params)


def get_org(org_id: str | None):
    if not org_id:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM organizations WHERE org_id = :i"), {"i": org_id}
        ).mappings().first()
    return dict(row) if row else None


def list_orgs():
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT * FROM organizations ORDER BY name")).mappings().all()
    return [dict(r) for r in rows]


def assign_unassigned(org_id: str) -> int:
    """Give every document that has no organization to org_id. Returns how many were updated."""
    with get_engine().begin() as conn:
        res = conn.execute(text("UPDATE documents SET org_id = :o WHERE org_id IS NULL"), {"o": org_id})
    return res.rowcount or 0


def hash_password(pw: str) -> str:
    salt = pysecrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(test, h)


# ------------------------------------------------------------------
# GOOGLE SHEETS SYNC  (each client's own sheet receives a row per issued document)
# ------------------------------------------------------------------
SHEET_HEADERS = ["Document ID", "Type", "Name", "Course / Award", "Role", "Department",
                 "ID Number", "Issued On", "Verification link"]


def _service_account_info():
    raw = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        return None


def sheets_configured() -> bool:
    return _service_account_info() is not None


def service_account_email() -> str:
    return (_service_account_info() or {}).get("client_email", "")


def parse_sheet_id(value: str) -> str:
    """Accepts a full Google Sheets link or a bare Sheet ID. Returns '' if it looks invalid."""
    value = (value or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]+)", value)
    sid = m.group(1) if m else value
    return sid if re.fullmatch(r"[A-Za-z0-9_-]{20,}", sid) else ""


@st.cache_resource(show_spinner=False)
def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        _service_account_info(), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)


def _open_sheet(sheet_id: str):
    return _client().open_by_key(sheet_id).sheet1


def sync_org_sheet(org: dict, app_url: str = "") -> int:
    """Append every document of this organization that is not yet in its Google Sheet.
    Safe to run repeatedly: documents already in column A are skipped. Returns rows added."""
    sheet_id = org.get("sheet_id")
    if not sheet_id:
        raise ValueError("This organization has no Google Sheet set.")
    ws = _open_sheet(sheet_id)
    existing = ws.col_values(1)
    if not existing or existing[0] != "Document ID":
        if any(c.strip() for c in existing):
            raise ValueError("The sheet's first column already has other data. Use a new, empty sheet.")
        ws.append_row(SHEET_HEADERS, value_input_option="RAW")
        existing = ["Document ID"]
    done = set(existing)
    docs = [d for d in reversed(list_documents(100000, org["org_id"])) if d["doc_id"] not in done]
    base = app_url.rstrip("/")
    rows = [[
        d["doc_id"], "Certificate" if d["doc_type"] == "certificate" else "ID card", d["name"],
        d.get("course") or "", d.get("role") or "", d.get("department") or "",
        d.get("id_number") or "", d.get("issued_on") or "",
        f"{base}/?verify={d['doc_id']}" if base else "",
    ] for d in docs]
    for i in range(0, len(rows), 500):
        ws.append_rows(rows[i:i + 500], value_input_option="RAW")
    return len(rows)


def sheet_error_text(e: Exception) -> str:
    name = type(e).__name__
    msg = str(e)
    email = service_account_email() or "the service account email"
    if isinstance(e, ValueError):
        return msg
    if name == "SpreadsheetNotFound" or "404" in msg or "not found" in msg.lower():
        return f"Sheet not found. Check the Sheet ID, and share the sheet with {email} as Editor."
    if "403" in msg or "PERMISSION" in msg.upper():
        return f"Permission denied. Share the sheet with {email} as Editor."
    if "429" in msg or "quota" in msg.lower():
        return "Google is limiting requests right now. Wait a minute and use 'Send missing records'."
    return f"{name}: {msg[:200]}"


def safe_sync(org: dict, app_url: str = ""):
    """Never raises. Returns (ok, message) for display."""
    if not org or not org.get("sheet_id"):
        return None, ""
    if not sheets_configured():
        return False, "Google Sheets is not set up yet: add the service account to Secrets."
    try:
        n = sync_org_sheet(org, app_url)
        return True, f"{n} new row(s) added to the client's Google Sheet."
    except Exception as e:  # noqa: BLE001
        return False, "Google Sheet sync failed (your records are safe in the database). " + sheet_error_text(e)


def test_sheet(org: dict):
    """Returns (ok, message): can the app open this organization's sheet?"""
    if not sheets_configured():
        return False, "Google Sheets is not set up yet: add the service account to Secrets."
    try:
        ws = _open_sheet(org["sheet_id"])
        return True, f"Connected to sheet '{ws.spreadsheet.title}'."
    except Exception as e:  # noqa: BLE001
        return False, sheet_error_text(e)


# ------------------------------------------------------------------
# 2. ADMIN LOGIN  (only the admin can generate documents)
# ------------------------------------------------------------------
MAX_ATTEMPTS = 5


def admin_gate() -> bool:
    """Sidebar login. Returns True only when the admin is logged in."""
    if st.session_state.get("is_admin"):
        st.sidebar.success("Logged in as admin")
        if st.sidebar.button("Log out"):
            st.session_state.is_admin = False
            st.rerun()
        return True

    real = get_secret("ADMIN_PASSWORD")
    if not real:
        st.sidebar.error("ADMIN_PASSWORD is not set in Secrets, so generation is locked.")
        return False

    attempts = st.session_state.get("login_attempts", 0)
    if attempts >= MAX_ATTEMPTS:
        st.sidebar.error("Too many wrong attempts. Refresh the page to try again.")
        return False

    with st.sidebar.form("admin_login"):
        pwd = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        if hmac.compare_digest(pwd.encode(), real.encode()):
            st.session_state.is_admin = True
            st.session_state.login_attempts = 0
            st.rerun()
        else:
            st.session_state.login_attempts = attempts + 1
            st.sidebar.error("Wrong password")
    return False


# ------------------------------------------------------------------
# 3. FONTS  (dropdown with a sample-word preview beside every font)
# ------------------------------------------------------------------
HERE = Path(__file__).parent
FONT_DIRS = [
    HERE / "fonts",                 # your own .ttf/.otf files (commit them to GitHub)
    Path("/usr/share/fonts"),       # Linux / Streamlit Cloud
    Path("/Library/Fonts"),         # macOS
    Path("C:/Windows/Fonts"),       # Windows (local use)
]
SAMPLE_WORD = "Certificate"
PAGE_SIZE = 25


@st.cache_data
def list_fonts() -> dict:
    fonts = {}
    for folder in FONT_DIRS:
        if folder.exists():
            for p in sorted(folder.rglob("*")):
                if p.suffix.lower() in (".ttf", ".otf"):
                    name = p.stem.replace("-", " ").replace("_", " ")
                    fonts.setdefault(name, str(p))
    return dict(sorted(fonts.items(), key=lambda kv: kv[0].lower()))


@st.cache_data
def font_preview(path: str, word: str = SAMPLE_WORD, size: int = 34):
    font = ImageFont.truetype(path, size)
    l, t, r, b = font.getbbox(word)
    img = Image.new("RGB", (max(r - l, 1) + 24, max(b - t, 1) + 18), "white")
    ImageDraw.Draw(img).text((12 - l, 9 - t), word, font=font, fill="black")
    return img


def font_picker(label: str, key: str, prefer: str = "") -> str | None:
    """Dropdown-style picker. Returns the chosen font file path (or None if no fonts exist)."""
    fonts = list_fonts()
    if not fonts:
        st.warning("No fonts found. Add .ttf files to a 'fonts' folder in your repo.")
        return None

    state_key = f"{key}_font"
    if st.session_state.get(state_key) not in fonts:
        default = next((n for n in fonts if prefer and prefer.lower() in n.lower()), None)
        st.session_state[state_key] = default or next(iter(fonts))

    st.caption(label)
    with st.popover(f"{st.session_state[state_key]}  ▾"):
        query = st.text_input("Search fonts", key=f"{key}_search")
        names = [n for n in fonts if query.lower() in n.lower()]
        pages = max(1, -(-len(names) // PAGE_SIZE))
        page = 1
        if pages > 1:
            page = st.selectbox(f"Page (of {pages})", list(range(1, pages + 1)), key=f"{key}_page_{pages}")
        st.caption(f"{len(names)} fonts")
        for name in names[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
            c1, c2, c3 = st.columns([2, 3, 1])
            c1.write(name)
            try:
                c2.image(font_preview(fonts[name]))
            except Exception:
                c2.caption("(no preview)")
            if c3.button("Use", key=f"{key}_use_{name}"):
                st.session_state[state_key] = name
                st.rerun()

    return fonts[st.session_state[state_key]]
