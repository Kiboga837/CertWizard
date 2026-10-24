"""Database (PostgreSQL), admin login and font picker for the Certificate & ID Card Maker."""
from __future__ import annotations

import hmac
import os
from datetime import datetime
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine, text


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


@st.cache_resource
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
    with get_engine().begin() as conn:
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
                created_at  VARCHAR(40)
            )
        """))


def save_documents(rows: list):
    """rows: list of dicts with the keys used below. One transaction for the whole batch."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    payload = [{**r, "created_at": now} for r in rows]
    with get_engine().begin() as conn:
        conn.execute(
            text("""INSERT INTO documents
                    (doc_id, doc_type, name, course, role, department, id_number, issued_on, created_at)
                    VALUES (:doc_id, :doc_type, :name, :course, :role, :department,
                            :id_number, :issued_on, :created_at)"""),
            payload,
        )


def get_document(doc_id: str):
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM documents WHERE doc_id = :i"), {"i": doc_id}
        ).mappings().first()
    return dict(row) if row else None


def list_documents(limit: int = 500):
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM documents ORDER BY created_at DESC LIMIT :n"), {"n": limit}
        ).mappings().all()
    return [dict(r) for r in rows]


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
