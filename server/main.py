import os, hmac, hashlib, urllib.parse, json
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import psycopg2
from psycopg2.extras import RealDictCursor

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN not set")

DATABASE_URL = os.environ.get("DATABASE_URL")  # на Railway появится после подключения Postgres
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = ["*"] if ALLOWED_ORIGINS == "*" else [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

SECRET_KEY = hashlib.sha256(BOT_TOKEN.encode()).digest() if BOT_TOKEN else b""

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    if not DATABASE_URL:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT UNIQUE NOT NULL,
            tg_username TEXT,
            full_name TEXT,
            department TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        conn.commit()

def check_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(500, "Server misconfigured: no BOT_TOKEN")
    data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    if "hash" not in data:
        raise HTTPException(401, "missing hash")
    their_hash = data.pop("hash")
    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    h = hmac.new(SECRET_KEY, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()
    if h != their_hash:
        raise HTTPException(401, "bad hash")
    user_json = data.get("user")
    if not user_json:
        raise HTTPException(401, "missing user")
    try:
        return json.loads(user_json)
    except Exception:
        raise HTTPException(401, "bad user json")

class AuthPayload(BaseModel):
    initData: str

class ProfileIn(BaseModel):
    full_name: str
    department: str

class ProfileOut(BaseModel):
    tg_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    department: Optional[str] = None

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup():
    try:
        init_db()
    except Exception as e:
        print("DB init skipped or failed:", e)

@app.post("/api/auth/telegram", response_model=ProfileOut)
def auth(payload: AuthPayload):
    u = check_init_data(payload.initData)
    tg_id = u["id"]
    username = u.get("username")
    full_from_tg = ((u.get("first_name","") + " " + u.get("last_name","")).strip()) or None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT tg_id, tg_username, full_name, department FROM users WHERE tg_id=%s", (tg_id,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE users SET tg_username=%s WHERE tg_id=%s", (username, tg_id))
            conn.commit()
            row["tg_username"] = username
        else:
            cur.execute(
                "INSERT INTO users (tg_id, tg_username, full_name) VALUES (%s, %s, %s) RETURNING tg_id, tg_username, full_name, department",
                (tg_id, username, full_from_tg)
            )
            row = cur.fetchone()
            conn.commit()

    return ProfileOut(
        tg_id=row["tg_id"],
        username=row["tg_username"],
        full_name=row["full_name"],
        department=row["department"],
    )

@app.get("/api/profile", response_model=ProfileOut)
def get_profile(initData: str):
    u = check_init_data(initData)
    tg_id = u["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT tg_id, tg_username, full_name, department FROM users WHERE tg_id=%s", (tg_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "user not found")
    return ProfileOut(**row)

@app.post("/api/profile", response_model=ProfileOut)
def save_profile(initData: str, body: ProfileIn):
    u = check_init_data(initData)
    tg_id = u["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET full_name=%s, department=%s WHERE tg_id=%s",
                    (body.full_name, body.department, tg_id))
        conn.commit()
        cur.execute("SELECT tg_id, tg_username, full_name, department FROM users WHERE tg_id=%s", (tg_id,))
        row = cur.fetchone()
    return ProfileOut(**row)
