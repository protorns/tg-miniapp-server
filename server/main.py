# server/main.py
import os, hmac, hashlib, urllib.parse, json, asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

import psycopg2
from psycopg2.extras import RealDictCursor
import httpx

# ========= ENV =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
TZ_OFFSET = +3  # GMT+3, как мы делали в боте

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

SECRET_KEY = hashlib.sha256(BOT_TOKEN.encode()).digest() if BOT_TOKEN else b""

ORIGINS = ["*"] if ALLOWED_ORIGINS in ("", "*") else [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

# ========= DB =========
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
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
        # Заявка пользователя (одна «своя» смена + список желаемых)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id SERIAL PRIMARY KEY,
            user_tg BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            department TEXT NOT NULL,
            have_date DATE NOT NULL,
            have_hour TEXT NOT NULL, -- 'HH:00' по GMT+3 (как в боте)
            status TEXT NOT NULL DEFAULT 'active', -- active|matched|cancelled|expired
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        # Мультивыбор желаемых смен
        cur.execute("""
        CREATE TABLE IF NOT EXISTS offer_wants (
            id SERIAL PRIMARY KEY,
            offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            want_date DATE NOT NULL,
            want_hour TEXT NOT NULL
        );
        """)
        # Индексы для скорости
        cur.execute("CREATE INDEX IF NOT EXISTS idx_offers_active ON offers(status, have_date);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wants_offer ON offer_wants(offer_id);")
        conn.commit()

# ========= DOMAIN =========

# Допустимые смены по отделам (как вы просили)
DEPT_SLOTS = {
    "VIP CALLS": [
        ("08:00","17:00"),
        ("10:00","19:00"),
        ("12:00","21:00"),
    ],
    "VIP LITE CHAT": [
        ("13:00","01:00"),
        ("12:00","21:00"),
        ("07:00","16:00"),
        ("09:00","18:00"),
    ],
    "VIP NIGHTS CHAT": [
        ("20:00","08:00"),
    ],
}

def as_local_now():
    # «локальное» время GMT+3
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=TZ_OFFSET)

def validate_future(date_iso: str, hour_hh: str):
    """Проверяем, что (дата, час) не в прошлом относительно GMT+3.
       hour='07:00'..'23:00'"""
    try:
        y, m, d = map(int, date_iso.split("-"))
        hh = int(hour_hh.split(":")[0])
    except Exception:
        raise HTTPException(400, "Bad date/hour format")
    candidate = datetime(y, m, d, hh, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET)  # переводим из +3 в UTC
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    if candidate <= now_utc:
        raise HTTPException(400, "Date/time already passed")

def check_department(dept: str):
    if dept not in DEPT_SLOTS:
        raise HTTPException(400, "Unknown department")

# ========= AUTH (Telegram WebApp) =========
def check_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(500, "Server misconfigured: BOT_TOKEN is not set")
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

# ========= SCHEMAS =========
class ProfileIn(BaseModel):
    full_name: str
    department: str

    @field_validator("department")
    @classmethod
    def _dept_ok(cls, v):
        if v not in DEPT_SLOTS:
            raise ValueError("Unknown department")
        return v

class ProfileOut(BaseModel):
    tg_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    department: Optional[str] = None

class WantItem(BaseModel):
    date: str   # YYYY-MM-DD
    hour: str   # HH:00

class OfferIn(BaseModel):
    department: str
    have_date: str       # YYYY-MM-DD
    have_hour: str       # HH:00
    wants: List[WantItem]

    @field_validator("department")
    @classmethod
    def _dept_ok(cls, v):
        if v not in DEPT_SLOTS:
            raise ValueError("Unknown department")
        return v

class OfferOut(BaseModel):
    id: int
    department: str
    have_date: str
    have_hour: str
    wants: List[WantItem]
    status: str
    created_at: str

class OfferBrief(BaseModel):
    id: int
    user_tg: int
    username: Optional[str]
    full_name: Optional[str]
    department: str
    have_date: str
    have_hour: str

# ========= APP =========
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup():
    init_db()

# ====== PROFILE ======
@app.post("/api/auth/telegram", response_model=ProfileOut)
def auth(initData: str):
    u = check_init_data(initData)
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
                "INSERT INTO users (tg_id, tg_username, full_name) VALUES (%s,%s,%s) RETURNING tg_id, tg_username, full_name, department",
                (tg_id, username, full_from_tg)
            )
            row = cur.fetchone()
            conn.commit()
    return ProfileOut(**row)

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
    check_department(body.department)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET full_name=%s, department=%s WHERE tg_id=%s",
                    (body.full_name, body.department, tg_id))
        conn.commit()
        cur.execute("SELECT tg_id, tg_username, full_name, department FROM users WHERE tg_id=%s", (tg_id,))
        row = cur.fetchone()
    return ProfileOut(**row)

# ====== OFFERS ======

def _row_to_offer_out(conn, offer_row) -> OfferOut:
    with conn.cursor() as cur:
        cur.execute("SELECT want_date, want_hour FROM offer_wants WHERE offer_id=%s ORDER BY want_date, want_hour", (offer_row["id"],))
        wants = [WantItem(date=r["want_date"].isoformat(), hour=r["want_hour"]) for r in cur.fetchall()]
    return OfferOut(
        id=offer_row["id"],
        department=offer_row["department"],
        have_date=offer_row["have_date"].isoformat(),
        have_hour=offer_row["have_hour"],
        wants=wants,
        status=offer_row["status"],
        created_at=offer_row["created_at"].isoformat()
    )

@app.post("/api/offers", response_model=OfferOut)
def create_offer(initData: str, body: OfferIn):
    # аутентификация
    u = check_init_data(initData)
    tg_id = u["id"]

    # профиль должен быть заполнен
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT department, full_name, tg_username FROM users WHERE tg_id=%s", (tg_id,))
        row = cur.fetchone()
        if not row or not row["department"] or not row["full_name"]:
            raise HTTPException(400, "Fill profile (full name & department) first")

    # валидации
    check_department(body.department)
    validate_future(body.have_date, body.have_hour)
    if not body.wants:
        raise HTTPException(400, "wants is empty")
    for w in body.wants:
        validate_future(w.date, w.hour)

    # сохранение
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO offers (user_tg, department, have_date, have_hour)
            VALUES (%s, %s, %s, %s)
            RETURNING id, user_tg, department, have_date, have_hour, status, created_at
        """, (tg_id, body.department, body.have_date, body.have_hour))
        offer = cur.fetchone()
        for w in body.wants:
            cur.execute("INSERT INTO offer_wants (offer_id, want_date, want_hour) VALUES (%s,%s,%s)",
                        (offer["id"], w.date, w.hour))
        conn.commit()
        # после коммита попробуем найти матч
        try_match_and_notify(offer["id"])
        # отдадим полную заявку
        cur.execute("SELECT * FROM offers WHERE id=%s", (offer["id"],))
        full = cur.fetchone()
        return _row_to_offer_out(conn, full)

@app.get("/api/offers/my", response_model=List[OfferOut])
def my_offers(initData: str):
    u = check_init_data(initData)
    tg_id = u["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM offers WHERE user_tg=%s AND status IN ('active','matched') ORDER BY have_date, have_hour", (tg_id,))
        rows = cur.fetchall()
        result = [_row_to_offer_out(conn, r) for r in rows]
    return result

@app.delete("/api/offers/{offer_id}")
def delete_offer(initData: str, offer_id: int):
    u = check_init_data(initData)
    tg_id = u["id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM offers WHERE id=%s AND user_tg=%s", (offer_id, tg_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "offer not found")
        conn.commit()
    return {"ok": True}

# Даты, на которые уже есть активные заявки (для "Актуальные смены")
@app.get("/api/actual-dates", response_model=List[str])
def actual_dates():
    # только будущие даты с учётом GMT+3 текущего часа
    local_now = as_local_now().date()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT have_date FROM offers
            WHERE status='active' AND have_date >= %s
            ORDER BY have_date
        """, (local_now,))
        return [r["have_date"].isoformat() for r in cur.fetchall()]

# Список заявок по дате (для экрана "Актуальные смены" -> выбрать дату)
@app.get("/api/offers/by-date", response_model=List[OfferBrief])
def offers_by_date(date: str = Query(..., description="YYYY-MM-DD")):
    local_now = as_local_now()
    y, m, d = map(int, date.split("-"))
    # выбираем активные и не прошедшие по времени
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.user_tg, u.tg_username, u.full_name, o.department, o.have_date, o.have_hour
            FROM offers o
            JOIN users u ON u.tg_id=o.user_tg
            WHERE o.status='active' AND o.have_date=%s
            ORDER BY o.have_hour
        """, (date,))
        rows = []
        for r in cur.fetchall():
            # фильтруем прошедшие часы в текущий день (GMT+3)
            if r["have_date"] == local_now.date() and int(r["have_hour"].split(":")[0]) <= local_now.hour:
                continue
            rows.append(OfferBrief(
                id=r["id"],
                user_tg=r["user_tg"],
                username=r["tg_username"],
                full_name=r["full_name"],
                department=r["department"],
                have_date=r["have_date"].isoformat(),
                have_hour=r["have_hour"],
            ))
        return rows

# ====== MATCHING & NOTIFY ======

def try_match_and_notify(offer_id: int):
    """Ищем взаимный обмен:
       A: have=X, wants содержит Y
       B: have=Y, wants содержит X
       → обоим статус matched + отправляем сообщения в Telegram."""
    with get_conn() as conn, conn.cursor() as cur:
        # читаем заявку A
        cur.execute("SELECT * FROM offers WHERE id=%s AND status='active'", (offer_id,))
        A = cur.fetchone()
        if not A:
            return
        cur.execute("SELECT want_date, want_hour FROM offer_wants WHERE offer_id=%s", (offer_id,))
        A_wants = [(r["want_date"].isoformat(), r["want_hour"]) for r in cur.fetchall()]
        X = (A["have_date"].isoformat(), A["have_hour"])

        # ищем кандидатов B, у которых have ∈ A_wants и статус active, и тот же отдел
        cur.execute("""
            SELECT o.*
            FROM offers o
            WHERE o.status='active' AND o.department=%s
              AND (o.have_date, o.have_hour) IN %s
              AND o.user_tg <> %s
        """, (A["department"], tuple(A_wants), A["user_tg"]))
        Bs = cur.fetchall()
        if not Bs:
            return

        # для каждого B проверим, что X ∈ B.wants
        for B in Bs:
            cur.execute("SELECT want_date, want_hour FROM offer_wants WHERE offer_id=%s", (B["id"],))
            B_wants = {(r["want_date"].isoformat(), r["want_hour"]) for r in cur.fetchall()}
            if X in B_wants:
                # нашли взаимный обмен → помечаем matched и шлём уведомления
                cur.execute("UPDATE offers SET status='matched' WHERE id IN (%s,%s)", (A["id"], B["id"]))
                conn.commit()
                notify_match(A["user_tg"], B["user_tg"], A, B)
                return  # одного достаточно

def notify_match(tgA: int, tgB: int, A: dict, B: dict):
    if not BOT_TOKEN:
        return
    base = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Сообщения с контактами
    def fmt(dt, hh):
        d = dt.strftime("%d.%m.%Y") if isinstance(dt, datetime) else dt
        return f"{d} {hh}"
    textA = (
        "🎉 Найден взаимный обмен!\n\n"
        f"Вы отдаёте: {A['have_date']} {A['have_hour']}\n"
        f"Получаете: {B['have_date']} {B['have_hour']}\n\n"
        f"Связаться: tg://user?id={tgB}"
    )
    textB = (
        "🎉 Найден взаимный обмен!\n\n"
        f"Вы отдаёте: {B['have_date']} {B['have_hour']}\n"
        f"Получаете: {A['have_date']} {A['have_hour']}\n\n"
        f"Связаться: tg://user?id={tgA}"
    )
    try:
        with httpx.Client(timeout=10) as cli:
            cli.post(base, data={"chat_id": tgA, "text": textA})
            cli.post(base, data={"chat_id": tgB, "text": textB})
    except Exception as e:
        print("notify error:", e)

# ====== CLEANUP (удаление устаревших) ======

def cleanup_expired():
    """Помечаем просроченные заявки как expired (с учётом GMT+3)."""
    now_local = as_local_now()
    with get_conn() as conn, conn.cursor() as cur:
        # все активные заявки, которые в прошлом
        cur.execute("SELECT id, have_date, have_hour FROM offers WHERE status='active'")
        to_expire = []
        for r in cur.fetchall():
            y, m, d = r["have_date"].year, r["have_date"].month, r["have_date"].day
            hh = int(r["have_hour"].split(":")[0])
            dt_local = datetime(y, m, d, hh, 0)  # локально в +3
            if dt_local <= now_local.replace(tzinfo=None):
                to_expire.append(r["id"])
        if to_expire:
            cur.execute("UPDATE offers SET status='expired' WHERE id = ANY(%s)", (to_expire,))
            conn.commit()

@app.on_event("startup")
async def _schedule_cleanup():
    # простая периодическая задача без внешних сервисов
    async def loop():
        while True:
            try:
                cleanup_expired()
            except Exception as e:
                print("cleanup error:", e)
            await asyncio.sleep(15 * 60)  # каждые 15 минут
    asyncio.create_task(loop())

# ====== Вспомогательное ======

@app.get("/api/dept/slots", response_model=List[List[str]])
def dept_slots(department: str):
    check_department(department)
    return [list(x) for x in DEPT_SLOTS[department]]
