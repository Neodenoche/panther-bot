#!/usr/bin/env python3
"""
PANTHER WALLET — MANADA PANTHER GAME BOT
Módulo completo: Bot + API HTTP para Mini App
"""

import os, json, logging, random, asyncio, threading, sqlite3
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# Webhook configuration
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # e.g. https://panther-bot-production.up.railway.app

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN   = os.environ.get("BOT_TOKEN", "")
DB_FILE  = "/data/panther_db.json"   # JSON legacy (para migración)
SQLITE_FILE = "/data/panther.db"

# ── Moderadores ───────────────────────────────────────────────────────────────
MOD_IDS = [int(x) for x in os.environ.get("MOD_IDS", "8234467845,8249484524,1769405650,5605380987,1781826630").split(",") if x.strip()]
MOD_GROUP_ID = int(os.environ.get("MOD_GROUP_ID", "-3777494908"))
MAIN_GROUP_ID = int(os.environ.get("MAIN_GROUP_ID", "-1001234567890"))  # chat general

# Evento Operación 1,000 Cazadores
COFRE_PNT        = 1125
PREMIOS_TOP_PNT  = {1: 500, 2: 250, 3: 125}
META_CAZADORES   = 1000
EVENTO_DIAS_BASE = 20

# Links oficiales
LINKS = {
    "ig":       "https://www.instagram.com/panther.wallet/",
    "yt":       "https://www.youtube.com/@Panther.Wallet",
    "tiktok":   "https://www.tiktok.com/@panther_wallet",
    "web":      "https://mypanther.io/es/",
    "canal":    "https://t.me/pantherwalletoficial",
    "chat":     "https://t.me/manadapanther",
}

# Links de campaña — prefijos reconocidos como fuentes externas
CAMPAIGN_SOURCES = {
    "camp_ig":   "Instagram",
    "camp_mail": "Email",
    "camp_tk":   "TikTok",
    "camp_web":  "Sitio Web",
}
PENDING_MISSIONS: dict = {}  # uid -> tipo de misión pendiente de subir

def save_pending_missions():
    """Persiste PENDING_MISSIONS en la tabla globals."""
    with DB_LOCK:
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO globals(key,value) VALUES(?,?)",
                ("pending_missions", json.dumps(PENDING_MISSIONS))
            )
            conn.commit()

def load_pending_missions():
    """Carga PENDING_MISSIONS desde la tabla globals al arrancar."""
    global PENDING_MISSIONS
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM globals WHERE key='pending_missions'"
            ).fetchone()
            if row:
                PENDING_MISSIONS = json.loads(row["value"])
                logger.info(f"✅ PENDING_MISSIONS cargado: {len(PENDING_MISSIONS)} misiones pendientes")
    except Exception as e:
        logger.warning(f"No se pudo cargar PENDING_MISSIONS: {e}")
STAR_COOLDOWN: dict = {}    # uid -> list of timestamps (máx 5 por hora)
CHAT_STARS: dict = {}       # uid -> {stars, pts, username, first_name} — persistido en SQLite
CHAT_AWARDS: dict = {}      # uid -> list of awards — persistido en SQLite

def load_chat_stars():
    """Carga CHAT_STARS y CHAT_AWARDS desde SQLite"""
    global CHAT_STARS, CHAT_AWARDS
    try:
        import json as _json
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS chat_stars (uid TEXT PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS chat_awards (uid TEXT PRIMARY KEY, data TEXT)")
        conn.commit()
        for row in cur.execute("SELECT uid, data FROM chat_stars"):
            CHAT_STARS[row[0]] = _json.loads(row[1])
        for row in cur.execute("SELECT uid, data FROM chat_awards"):
            CHAT_AWARDS[row[0]] = _json.loads(row[1])
        conn.close()
    except Exception as e:
        logger.error(f"Error cargando chat_stars: {e}")

def save_chat_stars():
    """Persiste CHAT_STARS y CHAT_AWARDS en SQLite"""
    try:
        import json as _json
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS chat_stars (uid TEXT PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS chat_awards (uid TEXT PRIMARY KEY, data TEXT)")
        for uid, data in CHAT_STARS.items():
            cur.execute("INSERT OR REPLACE INTO chat_stars (uid, data) VALUES (?, ?)", (uid, _json.dumps(data)))
        for uid, data in CHAT_AWARDS.items():
            cur.execute("INSERT OR REPLACE INTO chat_awards (uid, data) VALUES (?, ?)", (uid, _json.dumps(data)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error guardando chat_stars: {e}")

# ── Puntos por acción ─────────────────────────────────────────────────────────
PTS = {
    "checkin_1_3":       5,
    "checkin_4_6":      10,
    "streak_7":         50,
    "streak_14":       150,
    "streak_30":       500,
    "referral_join":    25,
    "referral_wallet": 150,
    "share_reel":       30,
    "follow_ig":        15,
    "follow_x":         15,
    "follow_tiktok":    15,
    "follow_facebook":  15,
    "follow_youtube":   15,
    "follow_all_bonus": 20,
    "share_story":      20,
    "own_content":     100,
    "wallet_activate": 175,
    "review_store":    175,
    "review_trust":    175,
}

# ── Niveles actualizados ──────────────────────────────────────────────────────
LEVELS = [
    (0,       149,     "🐾 Cachorro"),
    (150,     499,     "🔍 Rastreador"),
    (500,     999,     "🛡️ Guardián"),
    (1000,    2999,    "🧭 Explorador"),
    (3000,    6999,    "⚡ Embajador"),
    (7000,    14999,   "🦁 Leyenda"),
    (15000,   29999,   "🔥 Elite"),
    (30000,   59999,   "💎 Diamante"),
    (60000,   124999,  "👑 Rey de la Manada"),
    (125000,  249999,  "🌕 Lunar"),
    (250000,  499999,  "⚡🐆 Panther Alpha"),
    (500000,  999999,  "🏆 Inmortal"),
    (1000000, 99999999,"🌟 Dios de la Manada"),
]

# ── Ruleta ────────────────────────────────────────────────────────────────────
RULETA = [
    ("+50 puntos",   50,   None,   35),
    ("+100 puntos", 100,   None,   20),
    ("+200 puntos", 200,   None,   12),
    ("×2 puntos",     0,   "x2",   10),
    ("USDT",          0,   "usdt",  3),
    ("PNT",           0,   "pnt",   8),
    ("+15 puntos",   15,   None,   12),
]

# ── Pool de premios mensual ───────────────────────────────────────────────────
USDT_POOL = [
    {"amount": "$50",  "qty": 1},
    {"amount": "$10",  "qty": 5},
    {"amount": "$5",   "qty": 20},
]
PNT_POOL = [
    {"amount": 500, "qty": 3},
    {"amount": 250, "qty": 5},
    {"amount": 100, "qty": 10},
    {"amount": 50,  "qty": 30},
]

def spin_ruleta():
    pool = []
    for item in RULETA:
        pool.extend([item] * item[3])
    return random.choice(pool)

def get_pnt_prize():
    """Retorna un premio PNT aleatorio ponderado"""
    weights = [p["qty"] for p in PNT_POOL]
    total = sum(weights)
    r = random.random() * total
    for i, p in enumerate(PNT_POOL):
        r -= weights[i]
        if r <= 0:
            return p["amount"]
    return PNT_POOL[-1]["amount"]

def get_usdt_prize():
    """Retorna el premio USDT disponible más pequeño"""
    for p in reversed(USDT_POOL):
        if p["qty"] > 0:
            return p["amount"]
    return None

# ── DB — SQLite ──────────────────────────────────────────────────────────────
DB_LOCK = threading.Lock()

def get_conn():
    """Retorna una conexión SQLite thread-safe."""
    conn = sqlite3.connect(SQLITE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def download_fonts():
    """Descarga fuentes si no están disponibles"""
    import subprocess
    font_dir = "/app/fonts"
    os.makedirs(font_dir, exist_ok=True)
    
    fonts = {
        "bold.ttf": "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2",
    }
    # Usar fuentes del sistema o instalar via apt
    try:
        import subprocess
        subprocess.run(["apt-get", "install", "-y", "fonts-dejavu-core"], 
                      capture_output=True, timeout=30)
        logger.info("✅ Fuentes DejaVu instaladas via apt")
    except Exception as e:
        logger.error(f"Error instalando fuentes: {e}")
    fonts = {}
    
    for fname, url in fonts.items():
        fpath = os.path.join(font_dir, fname)
        if not os.path.exists(fpath):
            try:
                import urllib.request
                urllib.request.urlretrieve(url, fpath)
                logger.info(f"✅ Fuente descargada: {fname}")
            except Exception as e:
                logger.error(f"Error descargando fuente {fname}: {e}")

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def init_db():
    """Crea la tabla si no existe y migra datos del JSON legacy."""
    db_dir = os.path.dirname(SQLITE_FILE)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              TEXT PRIMARY KEY,
                username        TEXT DEFAULT '',
                first_name      TEXT DEFAULT '',
                points          INTEGER DEFAULT 0,
                streak          INTEGER DEFAULT 0,
                last_checkin    TEXT,
                last_ruleta     TEXT,
                double_pts_until TEXT,
                referral_code   TEXT DEFAULT '',
                referred_by     TEXT,
                referrals       TEXT DEFAULT '[]',
                referrals_active INTEGER DEFAULT 0,
                joined_at       TEXT,
                usdt_won_month  TEXT,
                pnt_won_month   TEXT,
                reel_verified   INTEGER DEFAULT 0,
                story_verified  INTEGER DEFAULT 0,
                follow_ig       INTEGER DEFAULT 0,
                follow_x        INTEGER DEFAULT 0,
                follow_tiktok   INTEGER DEFAULT 0,
                follow_facebook INTEGER DEFAULT 0,
                follow_youtube  INTEGER DEFAULT 0,
                follow_all_bonus INTEGER DEFAULT 0,
                has_virtual_card INTEGER DEFAULT 0,
                has_physical_card INTEGER DEFAULT 0,
                big_transaction INTEGER DEFAULT 0,
                wallet_activated INTEGER DEFAULT 0,
                pending_wallet_proof INTEGER DEFAULT 0,
                spins_used_this_event INTEGER DEFAULT 0,
                history         TEXT DEFAULT '[]',
                extra           TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS globals (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

    # ── Migración de columnas nuevas (ALTER TABLE) ──
    new_columns = [
        ("reel_count_today",    "INTEGER DEFAULT 0"),
        ("story_count_today",   "INTEGER DEFAULT 0"),
        ("content_count_today", "INTEGER DEFAULT 0"),
        ("last_mission_date",   "TEXT"),
        ("review_store_done",   "INTEGER DEFAULT 0"),
        ("review_trust_done",   "INTEGER DEFAULT 0"),
        ("founder_number",      "INTEGER"),
    ]
    with get_conn() as conn:
        for col_name, col_def in new_columns:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                logger.info(f"✅ Columna {col_name} agregada a users")
            except Exception:
                pass  # Ya existe, ignorar
        conn.commit()

    # ── Migración desde JSON legacy ──
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            migrated = 0
            with get_conn() as conn:
                for uid, data in old.items():
                    if uid == "_global":
                        for k, v in data.items():
                            conn.execute(
                                "INSERT OR IGNORE INTO globals(key,value) VALUES(?,?)",
                                (k, json.dumps(v))
                            )
                        continue
                    if not isinstance(data, dict) or "points" not in data:
                        continue
                    # Verificar si ya existe
                    row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
                    if row:
                        continue
                    refs = data.get("referrals", [])
                    if not isinstance(refs, list):
                        refs = []
                    history = data.get("history", [])
                    conn.execute("""
                        INSERT OR IGNORE INTO users
                        (id, username, first_name, points, streak, last_checkin, last_ruleta,
                         double_pts_until, referral_code, referred_by, referrals, referrals_active,
                         joined_at, usdt_won_month, pnt_won_month, reel_verified, story_verified,
                         follow_ig, follow_x, follow_tiktok, follow_facebook, follow_youtube,
                         follow_all_bonus, wallet_activated, history)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        uid,
                        data.get("username", ""),
                        data.get("first_name", ""),
                        data.get("points", 0),
                        data.get("streak", 0),
                        data.get("last_checkin"),
                        data.get("last_ruleta"),
                        data.get("double_pts_until"),
                        data.get("referral_code", uid[-6:]),
                        data.get("referred_by"),
                        json.dumps(refs),
                        data.get("referrals_active", 0),
                        data.get("joined_at", datetime.now().isoformat()),
                        data.get("usdt_won_month"),
                        data.get("pnt_won_month"),
                        int(data.get("reel_verified", False)),
                        int(data.get("story_verified", False)),
                        int(data.get("follow_ig", False)),
                        int(data.get("follow_x", False)),
                        int(data.get("follow_tiktok", False)),
                        int(data.get("follow_facebook", False)),
                        int(data.get("follow_youtube", False)),
                        int(data.get("follow_all_bonus", False)),
                        int(data.get("wallet_activated", False)),
                        json.dumps(history),
                    ))
                    migrated += 1
                conn.commit()
            if migrated > 0:
                logger.info(f"✅ Migrados {migrated} usuarios desde JSON a SQLite")
                # Renombrar JSON para no migrar dos veces
                os.rename(DB_FILE, DB_FILE + ".migrated")
        except Exception as e:
            logger.error(f"Error en migración JSON→SQLite: {e}")

def _row_to_dict(row):
    """Convierte una fila SQLite al dict que usa el resto del código."""
    if row is None:
        return None
    d = dict(row)
    # Deserializar campos JSON
    for field in ("referrals", "history"):
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except Exception:
            d[field] = []
    # Booleans
    for field in ("reel_verified", "story_verified", "follow_ig", "follow_x",
                  "follow_tiktok", "follow_facebook", "follow_youtube",
                  "follow_all_bonus", "has_virtual_card", "has_physical_card",
                  "big_transaction", "wallet_activated", "pending_wallet_proof"):
        d[field] = bool(d.get(field, 0))
    return d

def load_db():
    """Carga TODOS los usuarios como dict {uid: data} — compatibilidad total."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
        db = {row["id"]: _row_to_dict(row) for row in rows}
        # Cargar globals
        g_rows = conn.execute("SELECT key, value FROM globals").fetchall()
        if g_rows:
            db["_global"] = {r["key"]: json.loads(r["value"]) for r in g_rows}
    return db

def save_db(db):
    """Guarda el dict completo de vuelta a SQLite."""
    with DB_LOCK:
        with get_conn() as conn:
            for uid, data in db.items():
                if uid == "_global":
                    for k, v in data.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO globals(key,value) VALUES(?,?)",
                            (k, json.dumps(v))
                        )
                    continue
                if not isinstance(data, dict) or "id" not in data:
                    continue
                refs = data.get("referrals", [])
                if not isinstance(refs, list):
                    refs = []
                history = data.get("history", [])
                conn.execute("""
                    INSERT OR REPLACE INTO users
                    (id, username, first_name, points, streak, last_checkin, last_ruleta,
                     double_pts_until, referral_code, referred_by, referrals, referrals_active,
                     joined_at, usdt_won_month, pnt_won_month, reel_verified, story_verified,
                     follow_ig, follow_x, follow_tiktok, follow_facebook, follow_youtube,
                     follow_all_bonus, has_virtual_card, has_physical_card, big_transaction,
                     wallet_activated, pending_wallet_proof, spins_used_this_event,
                    reel_count_today, story_count_today, content_count_today, last_mission_date, history)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    data["id"],
                    sanitize_name(data.get("username", "")),
                    sanitize_name(data.get("first_name", "")),
                    data.get("points", 0),
                    data.get("streak", 0),
                    data.get("last_checkin"),
                    data.get("last_ruleta"),
                    data.get("double_pts_until"),
                    data.get("referral_code", ""),
                    data.get("referred_by"),
                    json.dumps(refs),
                    data.get("referrals_active", 0),
                    data.get("joined_at", datetime.now().isoformat()),
                    data.get("usdt_won_month"),
                    data.get("pnt_won_month"),
                    int(data.get("reel_verified", False)),
                    int(data.get("story_verified", False)),
                    int(data.get("follow_ig", False)),
                    int(data.get("follow_x", False)),
                    int(data.get("follow_tiktok", False)),
                    int(data.get("follow_facebook", False)),
                    int(data.get("follow_youtube", False)),
                    int(data.get("follow_all_bonus", False)),
                    int(data.get("has_virtual_card", False)),
                    int(data.get("has_physical_card", False)),
                    int(data.get("big_transaction", False)),
                    int(data.get("wallet_activated", False)),
                    int(data.get("pending_wallet_proof", False)),
                    data.get("spins_used_this_event", 0),
                    data.get("reel_count_today", 0),
                    data.get("story_count_today", 0),
                    data.get("content_count_today", 0),
                    data.get("last_mission_date"),
                    json.dumps(history),
                ))
            conn.commit()

def get_user(db, uid: str, user=None):
    if uid not in db:
        code = uid[-6:] if len(uid) >= 6 else uid
        db[uid] = {
            "id": uid,
            "username": sanitize_name(user.username if user else ""),
            "first_name": sanitize_name(user.first_name if user else ""),
            "points": 0,
            "streak": 0,
            "last_checkin": None,
            "last_ruleta": None,
            "double_pts_until": None,
            "referral_code": code,
            "referred_by": None,
            "referrals": [],
            "referrals_active": 0,
            "joined_at": datetime.now().isoformat(),
            "usdt_won_month": None,
            "pnt_won_month": None,
            "reel_verified": False,
            "story_verified": False,
            "follow_ig": False,
            "follow_x": False,
            "follow_tiktok": False,
            "follow_facebook": False,
            "follow_youtube": False,
            "follow_all_bonus": False,
            "wallet_activated": False,
            "pending_wallet_proof": False,
            "spins_used_this_event": 0,
            "history": [],
        }
    elif user:
        db[uid]["username"] = sanitize_name(user.username or db[uid].get("username", ""))
        db[uid]["first_name"] = sanitize_name(user.first_name or db[uid].get("first_name", ""))
    # Asegurar campos nuevos en usuarios existentes
    for field, default in [
        ("usdt_won_month", None), ("pnt_won_month", None),
        ("referrals_active", 0), ("reel_verified", False),
        ("story_verified", False), ("follow_ig", False),
        ("follow_x", False), ("follow_tiktok", False),
        ("follow_facebook", False), ("follow_youtube", False),
        ("follow_all_bonus", False), ("wallet_activated", False),
        ("pending_wallet_proof", False), ("spins_used_this_event", 0), ("founder_number", None),
        ("history", []),
    ]:
        if field not in db[uid]:
            db[uid][field] = default
    if not isinstance(db[uid].get("referrals"), list):
        db[uid]["referrals"] = []
    return db[uid]

def sanitize_name(name: str) -> str:
    """Limpia nombres con caracteres especiales para SQLite"""
    if not name:
        return ""
    try:
        return name.encode('utf-8', errors='ignore').decode('utf-8')
    except Exception:
        return "Usuario"

def escape_md(text: str) -> str:
    """Escapa caracteres especiales de Markdown para Telegram"""
    if not text:
        return ""
    # Escapar caracteres que rompen Markdown
    for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, f'\\{ch}')
    return text

async def notify_mods(app, msg: str):
    """Envía un mensaje al grupo de mods"""
    try:
        await app.bot.send_message(
            chat_id=MOD_GROUP_ID,
            text=msg,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error notificando mods: {e}")


    """Limpia nombres con caracteres especiales para SQLite"""
    if not name:
        return ""
    try:
        # Encodear y decodear para eliminar caracteres problemáticos
        cleaned = name.encode('utf-8', errors='ignore').decode('utf-8')
        return cleaned
    except Exception:
        return "Usuario"

def get_level(pts: int):
    for mn, mx, name in LEVELS:
        if mn <= pts <= mx:
            return name
    return "👑 Leyenda"

def get_next_level(pts: int):
    for i, (mn, mx, name) in enumerate(LEVELS):
        if mn <= pts <= mx:
            if i + 1 < len(LEVELS):
                return LEVELS[i+1][2], LEVELS[i+1][0] - pts
    return None, 0

def add_points(data, amount: int):
    multiplier = 1
    if data.get("double_pts_until"):
        try:
            until = datetime.fromisoformat(data["double_pts_until"])
            if datetime.now() < until:
                multiplier = 2
            else:
                data["double_pts_until"] = None
        except Exception:
            data["double_pts_until"] = None
    data["points"] += amount * multiplier
    return amount * multiplier

def has_won_this_month(data, prize_type):
    """Verifica si el usuario ya ganó USDT o PNT este mes"""
    field = f"{prize_type}_won_month"
    won_month = data.get(field)
    if not won_month:
        return False
    current_month = date.today().strftime("%Y-%m")
    return won_month == current_month

def mark_won_month(data, prize_type):
    """Marca que el usuario ganó este mes"""
    data[f"{prize_type}_won_month"] = date.today().strftime("%Y-%m")

def is_ruleta_active():
    # Check manual override in DB
    db = load_db()
    override = db.get("_global", {}).get("ruleta_override")
    if override == "on":
        return True
    if override == "off":
        return False
    # Default: auto based on day 15 or 30
    return date.today().day in [15, 30]

def can_access_ruleta(data):
    # Sin requisito de racha durante el evento
    return True

def get_available_spins(data):
    # Evento especial: 3 giros base para todos
    return 3

def get_monthly_pnt_pool():
    BUDGET_USD = 1050
    PNT_PRICE = 0.20
    return int(BUDGET_USD / PNT_PRICE)

# ── Teclado principal ─────────────────────────────────────────────────────────
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check-in diario", callback_data="checkin")],
        [
            InlineKeyboardButton("📊 Mis puntos", callback_data="puntos"),
            InlineKeyboardButton("🏆 Ranking",    callback_data="ranking"),
        ],
        [
            InlineKeyboardButton("🎰 Ruleta",     callback_data="ruleta"),
            InlineKeyboardButton("📋 Misiones",   callback_data="misiones"),
        ],
        [InlineKeyboardButton("🎫 Mi código referido", callback_data="referido")],
        [InlineKeyboardButton("🏅 Tabla de niveles",   callback_data="niveles")],
    ])


# ── Badge de Fundador ─────────────────────────────────────────────────────────
def generate_founder_badge(name: str, number: int) -> bytes:
    """Genera el badge de Fundador como bytes PNG"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import math, io, os

        W, H = 1080, 1080
        NEGRO = "#0A0A0A"
        NARANJA = "#FF5C1A"
        NARANJA_DIM = "#2a1000"
        NARANJA_MED = "#7a2d0d"
        ORO = "#FFD700"

        # Fuentes
        fB_path = FONT_BOLD if os.path.exists(FONT_BOLD) else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        fR_path = FONT_REGULAR if os.path.exists(FONT_REGULAR) else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

        img = Image.new("RGB", (W, H), NEGRO)
        d = ImageDraw.Draw(img)

        # Fondo hexagonal
        for row in range(-1, 18):
            for col in range(-1, 18):
                cx = col * 78 + (39 if row % 2 else 0)
                cy = row * 68
                pts = [(cx + 34*math.cos(math.radians(60*i-30)),
                        cy + 34*math.sin(math.radians(60*i-30))) for i in range(6)]
                d.polygon(pts, outline="#181818", fill=NEGRO)

        # Marco dorado
        d.rounded_rectangle([30, 30, W-30, H-30], radius=30, outline=ORO, width=4, fill=NEGRO)
        d.rounded_rectangle([40, 40, W-40, H-40], radius=24, outline="#7a6000", width=1)

        # Asset pantera
        pantera_path = "/app/Recurso_1_4x.png"
        if not os.path.exists(pantera_path):
            pantera_path = "Recurso_1_4x.png"
        if os.path.exists(pantera_path):
            pantera = Image.open(pantera_path).convert("RGBA")
            ratio = 380 / pantera.height
            new_w = int(pantera.width * ratio)
            pantera = pantera.resize((new_w, 380), Image.LANCZOS)
            pixels = list(pantera.getdata())
            pantera.putdata([(0,0,0,0) if r<30 and g<30 and b<30 else (r,g,b,a) for r,g,b,a in pixels])
            img.paste(pantera, (W//2 - new_w//2, 160), pantera)

        def ft(path, size):
            try:
                return ImageFont.truetype(path, size) if path else ImageFont.load_default()
            except:
                return ImageFont.load_default()

        f_badge = ft(fB_path, 48)
        f_name  = ft(fB_path, 80)
        f_sub   = ft(fR_path, 42)
        f_small = ft(fR_path, 36)

        # Título
        titulo = "✦ FUNDADOR DE LA MANADA ✦"
        bb = d.textbbox((0,0), titulo, font=f_badge)
        d.text(((W-(bb[2]-bb[0]))//2, 88), titulo, font=f_badge, fill=ORO)
        d.rectangle([80, 125, W-80, 127], fill=ORO)

        # Nombre
        display_name = name[:22] + "..." if len(name) > 22 else name
        bb = d.textbbox((0,0), display_name, font=f_name)
        d.text(((W-(bb[2]-bb[0]))//2, 575), display_name, font=f_name, fill="#FFFFFF")

        d.rectangle([200, 648, W-200, 650], fill=NARANJA)

        sub = "Entre los primeros 500 en la Manada Panther"
        bb = d.textbbox((0,0), sub, font=f_sub)
        d.text(((W-(bb[2]-bb[0]))//2, 668), sub, font=f_sub, fill="#aaaaaa")

        num_text = f"# {number:04d}"
        d.rounded_rectangle([W//2-120, 730, W//2+120, 800], radius=20, fill=NARANJA_DIM, outline=NARANJA_MED, width=1)
        bb = d.textbbox((0,0), num_text, font=f_badge)
        d.text(((W-(bb[2]-bb[0]))//2, 748), num_text, font=f_badge, fill=NARANJA)

        fecha = "29 de abril, 2026"
        bb = d.textbbox((0,0), fecha, font=f_small)
        d.text(((W-(bb[2]-bb[0]))//2, 830), fecha, font=f_small, fill="#555555")

        handle = "@pantherwalletoficial"
        bb = d.textbbox((0,0), handle, font=f_small)
        d.text(((W-(bb[2]-bb[0]))//2, 870), handle, font=f_small, fill="#444444")

        d.rectangle([30, H-50, W-30, H-30], fill=NARANJA)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.error(f"Error generando badge: {e}")
        return None

async def send_founder_badge(bot, uid: str, name: str, number: int):
    """Envía el badge de Fundador a un usuario"""
    badge_bytes = generate_founder_badge(name, number)
    if not badge_bytes:
        return False
    try:
        import io
        await bot.send_photo(
            chat_id=int(uid),
            photo=io.BytesIO(badge_bytes),
            caption=(
                f"🏆 *¡Sos Fundador de la Manada!*\n\n"
                f"Guardaste tu lugar entre los primeros 500 miembros "
                f"de la Manada Panther.\n\n"
                f"Guardá tu badge y compartilo en tus historias 🐆\n\n"
                f"_Panther Wallet — Tu dinero, tus reglas._"
            ),
            parse_mode="Markdown"
        )
        return True
    except Exception as e:
        logger.error(f"Error enviando badge a {uid}: {e}")
        return False


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    is_new = uid not in db
    data = get_user(db, uid, user)

    # Handle mission deep link
    if context.args and context.args[0] == 'mission':
        db   = load_db()
        data = get_user(db, uid, user)
        save_db(db)
        # Mostrar mensaje de instrucción y esperar la foto
        mission_type = PENDING_MISSIONS.get(uid)
        tipo_labels = {
            "wallet_activate": "🔐 Activación de Wallet",
            "review_store":    "⭐ Review en Tienda",
            "review_trust":    "🌟 Review en Trustpilot",
            "content":         "✏️ Contenido propio",
            "reel":            "🎬 Reel de Panther",
            "story":           "📸 Historia de Panther",
        }
        tipo_label = tipo_labels.get(mission_type, "📎 Tu misión")
        await update.message.reply_text(
            f"📸 *¡Listo {user.first_name}!*\n\n"
            f"Misión: *{tipo_label}*\n\n"
            f"Enviá tu captura de pantalla acá directamente 👇\n\n"
            f"_Un moderador la verificará y acreditará los puntos en las próximas 24h 🐾_",
            parse_mode="Markdown"
        )
        return

    # Handle compartir deep links
    if context.args and context.args[0] in ('compartir_reel', 'compartir_historia'):
        tipo = context.args[0]
        tipo_label = 'reel de Instagram' if tipo == 'compartir_reel' else 'historia de Instagram'
        pts = PTS['share_reel'] if tipo == 'compartir_reel' else PTS['share_story']
        await update.message.reply_text(
            f"📸 *Enviá tu captura de {tipo_label}*\n\n"
            f"1️⃣ Compartí el {tipo_label} de Panther\n"
            f"2️⃣ Tomá una captura de pantalla\n"
            f"3️⃣ Enviála *acá en este chat* como foto 👇\n\n"
            f"Si se aprueba recibís *+{pts} pts* 🎉",
            parse_mode="Markdown"
        )
        return

    if context.args and is_new:
        ref_code = context.args[0]

        # ── Campaña externa (IG, mail, TikTok) ──
        if ref_code in CAMPAIGN_SOURCES:
            data["source"] = ref_code
        else:
            # ── Link de referido de usuario ──
            data["source"] = "referral"
            for rid, rdata in db.items():
                r_code = rdata.get("referral_code", "")
                if (r_code == ref_code or r_code == f"PANTH-{ref_code}") and rid != uid:
                    data["referred_by"] = rid
                    if uid not in rdata["referrals"]:
                        rdata["referrals"].append(uid)
                        earned = add_points(rdata, PTS["referral_join"])
                        db[rid] = rdata
                        try:
                            await context.bot.send_message(
                                chat_id=int(rid),
                                text=f"🎉 *¡Nuevo miembro en la Manada!*\n\n"
                                     f"*{user.first_name}* se unió con tu código 🐆\n"
                                     f"*+{earned} puntos* acreditados 🐾",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                    break

    # Asignar número de fundador si es nuevo y hay cupos
    if is_new:
        db2 = load_db()
        user_count = len([u for u in db2.keys() if not u.startswith("_")])
        if user_count <= 500:
            data["founder_number"] = user_count
            db[uid] = data
            save_db(db)
            asyncio.create_task(send_founder_badge(context.bot, uid, user.first_name or user.username or "Miembro", user_count))
        else:
            save_db(db)
        # Lanzar secuencia de bienvenida en background
        asyncio.create_task(send_welcome_sequence(context.bot, uid, user.first_name or "Cazador"))
    else:
        save_db(db)

    level = get_level(data["points"])
    next_lv, pts_needed = get_next_level(data["points"])

    app_url = f"https://go.mypanther.io/app?id={uid}&v=3"

    if is_new:
        text = f"🐆 La Manada te espera, {user.first_name}. Revisá los mensajes que te envié para empezar 👇"
    else:
        text = (
            f"🐾 *¡Hola, {user.first_name}!*\n\n"
            f"🏅 Nivel: *{level}*\n"
            f"⭐ Puntos: *{data['points']}*\n"
            f"🔥 Racha: *{data['streak']} dias*\n"
            f"{'📈 Proximo: *' + next_lv + '* — ' + str(pts_needed) + ' pts' if next_lv else '👑 Nivel maximo'}\n\n"
            f"_Haz check-in cada dia, refiere amigos y sube en el ranking para ganar recompensas en PNT y USDT 💰_"
        )

    from telegram import WebAppInfo
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐆 Abrir Manada Panther", web_app=WebAppInfo(url=app_url))],
        [InlineKeyboardButton("✅ Check-in diario", callback_data="checkin")],
        [
            InlineKeyboardButton("📊 Mis puntos", callback_data="puntos"),
            InlineKeyboardButton("🏆 Ranking",    callback_data="ranking"),
        ],
        [
            InlineKeyboardButton("🎰 Ruleta",     callback_data="ruleta"),
            InlineKeyboardButton("📋 Misiones",   callback_data="misiones"),
        ],
        [InlineKeyboardButton("🎫 Mi código referido", callback_data="referido")],
        [InlineKeyboardButton("🏅 Tabla de niveles",   callback_data="niveles")],
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ── /checkin ──────────────────────────────────────────────────────────────────
async def do_checkin(uid: str, user, context):
    db   = load_db()
    data = get_user(db, uid, user)
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last = data.get("last_checkin")

    if last == today:
        return (
            f"⏰ Ya hiciste tu check-in hoy.\n\n"
            f"🔥 Racha: *{data['streak']} días*\n"
            f"Volvé mañana para no perderla.",
            False
        )

    if last == yesterday:
        data["streak"] += 1
    else:
        data["streak"] = 1

    streak = data["streak"]
    base_pts = PTS["checkin_1_3"] if streak <= 3 else PTS["checkin_4_6"]

    bonus     = 0
    bonus_msg = ""
    if streak == 7:
        bonus = PTS["streak_7"]
        bonus_msg = f"\n🎉 *¡RACHA DE 7 DÍAS!* +{bonus} pts bonus"
    elif streak == 14:
        bonus = PTS["streak_14"]
        bonus_msg = f"\n🎉 *¡RACHA DE 14 DÍAS!* +{bonus} pts bonus"
    elif streak == 30:
        bonus = PTS["streak_30"]
        bonus_msg = f"\n🎉 *¡RACHA DE 30 DÍAS!* +{bonus} pts bonus"

    old_pts = data["points"]
    earned  = add_points(data, base_pts + bonus)
    data["last_checkin"] = today

    old_lv = get_level(old_pts)
    new_lv = get_level(data["points"])
    lvl_msg = f"\n\n⬆️ *¡SUBISTE DE NIVEL!*\n{old_lv} → *{new_lv}*" if old_lv != new_lv else ""

    next_lv, pts_needed = get_next_level(data["points"])
    save_db(db)

    text = (
        f"✅ *¡Check-in completado!*\n\n"
        f"🔥 Racha: *{streak} día{'s' if streak > 1 else ''}*\n"
        f"➕ Ganaste: *+{earned} puntos*{bonus_msg}\n"
        f"⭐ Total: *{data['points']} puntos*\n"
        f"🏅 Nivel: *{new_lv}*"
        f"{lvl_msg}\n\n"
        f"{'📈 Próximo: *' + next_lv + '* — faltan *' + str(pts_needed) + ' pts*' if next_lv else ''}"
    )
    return text, True

async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text, _ = await do_checkin(str(user.id), user, context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

# ── /puntos ───────────────────────────────────────────────────────────────────
async def cmd_puntos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)
    save_db(db)

    level = get_level(data["points"])
    next_lv, pts_needed = get_next_level(data["points"])
    refs = len(data.get("referrals", []))

    await update.message.reply_text(
        f"📊 *Tu perfil — Manada Panther*\n\n"
        f"👤 {user.first_name}\n"
        f"🏅 Nivel: *{level}*\n"
        f"⭐ Puntos: *{data['points']}*\n"
        f"🔥 Racha: *{data['streak']} días*\n"
        f"👥 Referidos: *{refs}*\n"
        f"🎫 Código: `{data['referral_code']}`\n\n"
        f"{'📈 Próximo: *' + next_lv + '* — faltan *' + str(pts_needed) + ' pts*' if next_lv else '👑 ¡Sos Leyenda!'}",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ── /niveles ──────────────────────────────────────────────────────────────────
async def cmd_niveles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)
    save_db(db)

    current = get_level(data["points"])

    lines = ["🏅 *NIVELES — MANADA PANTHER*\n"]
    for mn, mx, name in LEVELS:
        marker = " ✅ ← estás aquí" if name == current else ""
        pts_range = f"{mn:,} – {mx:,} pts" if mx < 999999 else f"{mn:,}+ pts"
        lines.append(f"{name}{marker}\n_{pts_range}_\n")

    lines.append(
        f"⭐ *Tus puntos actuales: {data['points']}*\n\n"
        f"*¿Cómo subir de nivel?*\n"
        f"🔥 Check-in diario → /checkin\n"
        f"🎰 Ruleta diaria → /ruleta\n"
        f"👥 Referir amigos → /referido\n"
        f"📱 Compartir contenido → /compartir"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ── /ranking ──────────────────────────────────────────────────────────────────
async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db:
        await update.message.reply_text("Todavía no hay usuarios 🐾")
        return

    uid     = str(update.effective_user.id)
    sorted_ = sorted(db.values(), key=lambda x: x["points"], reverse=True)
    top20   = sorted_[:20]
    medals  = ["🥇","🥈","🥉"]

    lines = ["🏆 *LEADERBOARD — MANADA PANTHER*\n"]
    for i, u in enumerate(top20):
        prefix = medals[i] if i < 3 else f"{i+1}."
        name   = u.get("username") or u.get("first_name") or "Anónimo"
        lv     = get_level(u["points"])
        me     = " ← vos" if u["id"] == uid else ""
        lines.append(f"{prefix} @{name} — *{u['points']} pts* {lv}{me}")

    my_pos = next((i+1 for i,u in enumerate(sorted_) if u["id"] == uid), None)
    if my_pos and my_pos > 20:
        lines.append(f"\n📍 Tu posición: *#{my_pos}* — {db[uid]['points']} pts")

    lines.append(f"\n_Actualizado: {datetime.now().strftime('%d/%m %H:%M')}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())

# ── /referido ─────────────────────────────────────────────────────────────────
async def cmd_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)
    save_db(db)

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={data['referral_code']}"
    refs = len(data.get("referrals", []))

    await update.message.reply_text(
        f"🎫 *Tu código de referido*\n\n"
        f"Código: `{data['referral_code']}`\n"
        f"Link: {link}\n\n"
        f"👥 Referidos actuales: *{refs}*\n\n"
        f"*Por cada referido:*\n"
        f"├ Se une al canal: *+{PTS['referral_join']} pts*\n"
        f"└ Activa Panther Wallet: *+{PTS['referral_wallet']} pts*\n\n"
        f"_Compartí tu link y acumulá puntos 🚀_",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ── /verificar_follow (honor system) ─────────────────────────────────────────
async def cmd_verificar_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    args = context.args or []
    red  = args[0].lower() if args else ""

    valid_reds = {"ig": "follow_ig", "x": "follow_x", "tiktok": "follow_tiktok"}
    if red not in valid_reds:
        await update.message.reply_text(
            "Uso: /verificar_follow ig | x | tiktok"
        )
        return

    field = valid_reds[red]
    if data.get(field):
        await update.message.reply_text(f"✅ Ya verificaste esta red social anteriormente.")
        return

    earned = add_points(data, PTS[field])
    data[field] = True

    # Check if all 3 followed → bonus
    bonus_msg = ""
    if data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok") and not data.get("follow_all_bonus"):
        bonus = add_points(data, PTS["follow_all_bonus"])
        data["follow_all_bonus"] = True
        bonus_msg = f"\n\n🎉 *¡Bonus por seguir todas las redes!* +{bonus} pts extra"

    db[uid] = data
    save_db(db)

    red_names = {"ig": "Instagram", "x": "X (Twitter)", "tiktok": "TikTok"}
    await update.message.reply_text(
        f"✅ *¡Mision completada!*\n\n"
        f"Seguiste a Panther en {red_names[red]}\n"
        f"*+{earned} pts* acreditados 🐆{bonus_msg}",
        parse_mode="Markdown"
    )

# ── /ruleta_on / /ruleta_off (moderadores) ────────────────────────────────────
async def cmd_ruleta_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"
    save_db(db)
    await update.message.reply_text("✅ Ruleta ACTIVADA manualmente")

async def cmd_ruleta_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "off"
    save_db(db)
    await update.message.reply_text("🔴 Ruleta DESACTIVADA manualmente")

async def cmd_ruleta_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = None
    save_db(db)
    await update.message.reply_text("🔄 Ruleta en modo AUTOMÁTICO (días 15 y 30)")

# ── /broadcast (moderadores) ──────────────────────────────────────────────────
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Uso: /broadcast Tu mensaje aquí\n\n"
            "Ejemplo: /broadcast ¡Bienvenidos al canal oficial! t.me/pantherwalletoficial"
        )
        return
    
    msg = " ".join(context.args)
    db = load_db()
    users = [u for u in db.keys() if not u.startswith("_")]
    
    await update.message.reply_text(f"📤 Enviando a {len(users)} usuarios...")
    
    sent = 0
    failed = 0
    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=f"📢 *Mensaje de Panther Wallet*\n\n{msg}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1
    
    await update.message.reply_text(
        f"✅ Broadcast completado\n\n"
        f"📤 Enviados: {sent}\n"
        f"❌ Fallidos: {failed}"
    )

# ── /compartir ────────────────────────────────────────────────────────────────
async def cmd_compartir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    tipo = args[0] if args else 'reel'
    tipo_label = 'reel de Instagram' if tipo == 'reel' else 'historia de Instagram'
    pts = PTS['share_reel'] if tipo == 'reel' else PTS['share_story']
    await update.message.reply_text(
        f"📸 *Enviá tu captura de {tipo_label}*\n\n"
        f"1️⃣ Compartí el {tipo_label} de Panther\n"
        f"2️⃣ Tomá una captura de pantalla\n"
        f"3️⃣ Enviála *acá en este chat* como foto 👇\n\n"
        f"Si se aprueba recibís *+{pts} pts* 🎉",
        parse_mode="Markdown"
    )

# ── /ruleta ───────────────────────────────────────────────────────────────────
async def cmd_ruleta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    today = date.today().isoformat()
    if data.get("last_ruleta") == today:
        await update.message.reply_text(
            "🎰 Ya giraste la ruleta hoy.\n\nVolvé mañana para otro giro 🐾",
            parse_mode="Markdown"
        )
        return

    result_label, pts_gain, special, _ = spin_ruleta()
    data["last_ruleta"] = today

    msg = "🎰 *¡GIRASTE LA RULETA!*\n\n"

    if pts_gain > 0:
        earned = add_points(data, pts_gain)
        msg += (
            f"🎊 Resultado: *{result_label}*\n"
            f"➕ Ganaste: *+{earned} puntos*\n"
            f"⭐ Total: *{data['points']} puntos*"
        )

    elif special == "x2":
        until = datetime.now() + timedelta(hours=24)
        data["double_pts_until"] = until.isoformat()
        msg += (
            "⚡ *¡PUNTOS DOBLES POR 24 HORAS!*\n"
            "Todas tus acciones de hoy valen el doble 🔥\n"
            f"⭐ Puntos actuales: *{data['points']}*"
        )

    elif special == "usdt":
        if has_won_this_month(data, "usdt"):
            earned = add_points(data, 50)
            msg += (
                f"⭐ *+{earned} puntos*\n"
                f"⭐ Total: *{data['points']} puntos*"
            )
        else:
            prize_amount = get_usdt_prize()
            if not prize_amount:
                prize_amount = "$5"
            mark_won_month(data, "usdt")
            msg += (
                f"💵 *¡PREMIO EN EFECTIVO!*\n\n"
                f"Ganaste: *{prize_amount} USDT*\n\n"
                f"📸 Tomá captura de esta pantalla y enviala al chat general "
                f"o al bot en privado. Un moderador te contactará para coordinar el pago.\n\n"
                f"_⚠️ Solo podés ganar USDT una vez por mes._"
            )
            name = user.username or user.first_name
            for mod_id in MOD_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=mod_id,
                        text=(
                            f"💵 *Premio USDT ganado*\n\n"
                            f"Usuario: @{name} (ID: `{uid}`)\n"
                            f"Premio: *{prize_amount} USDT*\n"
                            f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"No se pudo notificar mod {mod_id}: {e}")

    elif special == "pnt":
        if has_won_this_month(data, "pnt"):
            earned = add_points(data, 30)
            msg += (
                f"⭐ *+{earned} puntos*\n"
                f"⭐ Total: *{data['points']} puntos*"
            )
        else:
            pnt_amount = get_pnt_prize()
            mark_won_month(data, "pnt")
            msg += (
                f"🐾 *¡PREMIO PNT!*\n\n"
                f"Ganaste: *{pnt_amount} PNT*\n\n"
                f"📸 Tomá captura de esta pantalla y enviala al chat general "
                f"o al bot en privado. Los tokens serán acreditados en tu Panther Wallet.\n\n"
                f"_⚠️ Solo podés ganar PNT una vez por mes._"
            )
            name = user.username or user.first_name
            for mod_id in MOD_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=mod_id,
                        text=(
                            f"🐾 *Premio PNT ganado*\n\n"
                            f"Usuario: @{name} (ID: `{uid}`)\n"
                            f"Premio: *{pnt_amount} PNT*\n"
                            f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"No se pudo notificar mod {mod_id}: {e}")

    save_db(db)

    next_lv, pts_needed = get_next_level(data["points"])
    if next_lv and pts_gain > 0:
        msg += f"\n📈 Próximo nivel: *{next_lv}* — faltan *{pts_needed} pts*"

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

# ── /misiones ─────────────────────────────────────────────────────────────────
async def cmd_misiones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = str(user.id)
    db   = load_db()
    data = get_user(db, uid, user)
    save_db(db)
    app_url = f"https://go.mypanther.io/app?id={uid}&v=3"
    from telegram import WebAppInfo
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐆 Abrir Misiones en la Mini App", web_app=WebAppInfo(url=app_url))],
    ])
    await update.message.reply_text(
        "Las misiones estan disponibles en la Mini App. Toca el boton para abrirla.",
        reply_markup=keyboard
    )
    return
    db   = load_db()
    uid  = str(update.effective_user.id)
    data = get_user(db, uid, update.effective_user)
    today = date.today().isoformat()

    checkin_hoy = "✅" if data.get("last_checkin") == today else "⬜"
    ruleta_hoy  = "✅" if data.get("last_ruleta")  == today else "⬜"

    await update.message.reply_text(
        f"📋 *MISIONES DE HOY*\n\n"
        f"{checkin_hoy} *Check-in diario* → /checkin\n"
        f"_+5 a +10 pts · bonus por racha_\n\n"
        f"{ruleta_hoy} *Ruleta diaria* → /ruleta\n"
        f"_Puntos, x2, USDT o PNT_\n\n"
        f"⬜ *Compartir reel de Panther*\n"
        f"_Mandá la captura al bot · +{PTS['share_reel']} pts_\n\n"
        f"⬜ *Compartir historia de Panther*\n"
        f"_Mandá la captura al bot · +{PTS['share_story']} pts_\n\n"
        f"⬜ *Referir un amigo* → /referido\n"
        f"_+{PTS['referral_join']} pts por unirse · +{PTS['referral_wallet']} si activa la wallet_\n\n"
        f"_🐾 Los puntos se acreditan automáticamente_",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ── /verificar_follow (honor system) ─────────────────────────────────────────
async def cmd_verificar_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    args = context.args or []
    red  = args[0].lower() if args else ""

    valid_reds = {"ig": "follow_ig", "x": "follow_x", "tiktok": "follow_tiktok"}
    if red not in valid_reds:
        await update.message.reply_text(
            "Uso: /verificar_follow ig | x | tiktok"
        )
        return

    field = valid_reds[red]
    if data.get(field):
        await update.message.reply_text(f"✅ Ya verificaste esta red social anteriormente.")
        return

    earned = add_points(data, PTS[field])
    data[field] = True

    # Check if all 3 followed → bonus
    bonus_msg = ""
    if data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok") and not data.get("follow_all_bonus"):
        bonus = add_points(data, PTS["follow_all_bonus"])
        data["follow_all_bonus"] = True
        bonus_msg = f"\n\n🎉 *¡Bonus por seguir todas las redes!* +{bonus} pts extra"

    db[uid] = data
    save_db(db)

    red_names = {"ig": "Instagram", "x": "X (Twitter)", "tiktok": "TikTok"}
    await update.message.reply_text(
        f"✅ *¡Mision completada!*\n\n"
        f"Seguiste a Panther en {red_names[red]}\n"
        f"*+{earned} pts* acreditados 🐆{bonus_msg}",
        parse_mode="Markdown"
    )

# ── /ruleta_on / /ruleta_off (moderadores) ────────────────────────────────────
async def cmd_ruleta_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"
    save_db(db)
    await update.message.reply_text("✅ Ruleta ACTIVADA manualmente")

async def cmd_ruleta_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "off"
    save_db(db)
    await update.message.reply_text("🔴 Ruleta DESACTIVADA manualmente")

async def cmd_ruleta_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = None
    save_db(db)
    await update.message.reply_text("🔄 Ruleta en modo AUTOMÁTICO (días 15 y 30)")

# ── /broadcast (moderadores) ──────────────────────────────────────────────────
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Uso: /broadcast Tu mensaje aquí\n\n"
            "Ejemplo: /broadcast ¡Bienvenidos al canal oficial! t.me/pantherwalletoficial"
        )
        return
    
    msg = " ".join(context.args)
    db = load_db()
    users = [u for u in db.keys() if not u.startswith("_")]
    
    await update.message.reply_text(f"📤 Enviando a {len(users)} usuarios...")
    
    sent = 0
    failed = 0
    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=f"📢 *Mensaje de Panther Wallet*\n\n{msg}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1
    
    await update.message.reply_text(
        f"✅ Broadcast completado\n\n"
        f"📤 Enviados: {sent}\n"
        f"❌ Fallidos: {failed}"
    )

# ── /compartir ────────────────────────────────────────────────────────────────
async def cmd_compartir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📸 *Verificación de contenido*\n\n"
        f"Para acreditar tus puntos:\n\n"
        f"1️⃣ Compartí el reel o historia de Panther\n"
        f"2️⃣ Tomá una captura de pantalla\n"
        f"3️⃣ Enviá la captura *directamente acá* en el chat\n\n"
        f"Un moderador la verificará y acreditará los puntos en menos de 24h 🐾",
        parse_mode="Markdown"
    )

# ── Web App Data (desde Mini App) ────────────────────────────────────────────
async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json as json_lib
    try:
        data = json_lib.loads(update.effective_message.web_app_data.data)
        action = data.get('action')
        tipo = data.get('type', 'reel')
        
        if action == 'share':
            tipo_label = 'reel de Instagram' if tipo == 'reel' else 'historia de Instagram'
            pts = PTS['share_reel'] if tipo == 'reel' else PTS['share_story']
            await update.message.reply_text(
                f"📸 *Enviá tu captura de {tipo_label}*\n\n"
                f"1️⃣ Compartí el {tipo_label} de Panther\n"
                f"2️⃣ Tomá una captura de pantalla\n"
                f"3️⃣ Enviála *acá en este chat* como foto 👇\n\n"
                f"Si se aprueba recibís *+{pts} pts* 🎉",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error handling web_app_data: {e}")

# ── Manejo de fotos (capturas de misiones) ────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only handle photos in private chats
    if update.effective_chat.type != "private":
        return

    # Detectar #NuevoCazador en privado
    caption = (update.message.caption or "").lower()
    if "#nuevocazador" in caption:
        await handle_nuevo_cazador_privado(update, context)
        return

    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    raw_name = f"@{user.username}" if user.username else user.first_name
    name = raw_name  # Para mensajes sin Markdown
    name_md = escape_md(raw_name)  # Para mensajes con Markdown

    # Check if this is a wallet activation proof
    if data.get("pending_wallet_proof"):
        data["pending_wallet_proof"] = False
        save_db(db)

        await update.message.reply_text(
            f"✅ *¡Captura recibida!* Gracias {name_md}.\n\n"
            f"Un moderador verificará tu activación de wallet en las próximas 24h.\n\n"
            f"_Cuando se apruebe, tu referidor recibirá sus puntos_ 🐆",
            parse_mode="Markdown"
        )

        # Notify mods — grupo primero, luego individuales como fallback
        referred_by = data.get("referred_by")
        keyboard_wallet = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ Aprobar wallet (+150 pts al referidor)",
                callback_data=f"wallet_{uid}_{referred_by}"
            )],
            [InlineKeyboardButton(
                "❌ Rechazar",
                callback_data=f"reject_{uid}"
            )]
        ])
        wallet_text = (
            f"🔐 *Prueba de wallet*\n\n"
            f"Usuario: {name} (ID: {uid})\n"
            f"Referido por: {referred_by or 'N/A'}\n\n"
            f"¿Aprobar activación de wallet?"
        )
        notified = False
        try:
            await context.bot.forward_message(
                chat_id=MOD_GROUP_ID,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
            await context.bot.send_message(
                chat_id=MOD_GROUP_ID,
                text=wallet_text,
                parse_mode="Markdown",
                reply_markup=keyboard_wallet
            )
            notified = True
        except Exception as e:
            logger.error(f"Error notifying mod group: {type(e).__name__}: {e}")
        if not notified:
            for mod_id in MOD_IDS:
                try:
                    await context.bot.forward_message(
                        chat_id=mod_id,
                        from_chat_id=update.effective_chat.id,
                        message_id=update.message.message_id
                    )
                    await context.bot.send_message(
                        chat_id=mod_id,
                        text=wallet_text,
                        parse_mode="Markdown",
                        reply_markup=keyboard_wallet
                    )
                except Exception as e2:
                    logger.error(f"Error notifying mod {mod_id}: {type(e2).__name__}: {e2}")
        return

    try:
        save_db(db)
    except Exception as e:
        logger.error(f"Error guardando DB en handle_photo: {e}")

    # ── Detectar tipo de misión y verificar límite diario ──
    today = date.today().isoformat()
    if data.get("last_mission_date") != today:
        data["reel_count_today"] = 0
        data["story_count_today"] = 0
        data["content_count_today"] = 0
        data["last_mission_date"] = today

    mission_type = PENDING_MISSIONS.pop(uid, None)
    save_pending_missions()
    MAX_DAILY = 3

    # ── Foto sin contexto — rechazar con explicación ──
    if mission_type is None:
        await update.message.reply_text(
            "⚠️ Esta imagen no fue enviada desde una misión del gamebot.\n\n"
            "Para que cuente, tenés que entrar a la Mini App → Misiones → "
            "seleccionar la misión correspondiente (Reel, Historia, Contenido propio, "
            "Comentario IG, Comentario TikTok, etc.) y subir la captura desde ahí.\n\n"
            "Las imágenes enviadas sin contexto no son válidas y serán ignoradas. "
            "Tenés un límite de 3 capturas por misión por día."
        )
        return

    tipo_labels = {
        "reel":            "🎬 Reel de Panther",
        "story":           "📸 Historia de Panther",
        "content":         "✏️ Contenido propio",
        "wallet_activate":  "🔐 Activación de Wallet",
        "review_store":     "⭐ Review en Tienda (Play/App Store)",
        "review_trust":     "🌟 Review en Trustpilot",
        "comment_ig":       "💬 Comentario en Instagram",
        "comment_ig_last":  "💬 Comentario en Ultimo Post IG (+30 pts)",
        "comment_tt":       "💬 Comentario en TikTok",
        "comment_tt_last":  "💬 Comentario en Ultimo Video TikTok (+30 pts)",
        None:               "📎 Sin clasificar",
    }
    tipo_label = tipo_labels.get(mission_type, "📎 Sin clasificar")

    # Misiones de wallet no tienen límite diario
    wallet_missions = ["wallet_activate", "review_store", "review_trust", "comment_ig", "comment_ig_last", "comment_tt", "comment_tt_last"]
    if mission_type in wallet_missions:
        count_key = None  # Sin límite diario
    elif mission_type in ["reel", "story", "content"]:
        count_key = f"{mission_type}_count_today"
    else:
        # Sin tipo registrado — usar content como fallback
        mission_type = "content"
        count_key = "content_count_today"
    
    current_count = data.get(count_key, 0) if count_key else 0

    if count_key and current_count >= MAX_DAILY:
        type_name = {"reel": "reels", "story": "historias", "content": "contenidos"}.get(mission_type, "misiones")
        await update.message.reply_text(
            f"⚠️ Ya alcanzaste el límite de {MAX_DAILY} {type_name} por hoy.\n"
            f"Volvé mañana para seguir ganando puntos 🐾"
        )
        return

    if count_key:
        data[count_key] = current_count + 1
        remaining = MAX_DAILY - data[count_key]
    else:
        remaining = None

    try:
        save_db(db)
    except Exception as e:
        logger.error(f"Error guardando contadores en handle_photo: {e}")

    if count_key and remaining is not None:
        type_name = {"reel": "reels", "story": "historias", "content": "contenidos"}.get(mission_type, "misiones")
        counter_msg = f"\n\n📊 {tipo_label}: *{data[count_key]}/{MAX_DAILY}* hoy · te quedan *{remaining}* restantes."
    else:
        counter_msg = ""

    await update.message.reply_text(
        f"📨 Captura recibida. Misión: *{tipo_label}*{counter_msg}\n\n"
        f"Un moderador la revisará en las próximas 24 horas. "
        f"Si es aprobada recibirás tus puntos automáticamente. "
        f"Si es rechazada te avisaremos con el motivo. 🐾",
        parse_mode="Markdown"
    )

    # Notificar a moderadores — grupo primero, fallback individual
    mission_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Reel (+{PTS['share_reel']} pts)", callback_data=f"approve_{uid}_reel"),
            InlineKeyboardButton(f"✅ Historia (+{PTS['share_story']} pts)", callback_data=f"approve_{uid}_story"),
        ],
        [
            InlineKeyboardButton(f"✅ Contenido (+{PTS['own_content']} pts)", callback_data=f"approve_{uid}_content"),
            InlineKeyboardButton("✅ Wallet (+175 pts)", callback_data=f"approve_{uid}_wallet_activate"),
        ],
        [
            InlineKeyboardButton("✅ Review Store (+175 pts)", callback_data=f"approve_{uid}_review_store"),
            InlineKeyboardButton("✅ Review Trust (+175 pts)", callback_data=f"approve_{uid}_review_trust"),
        ],
        [
            InlineKeyboardButton("💬 Comment IG (+5 pts)", callback_data=f"approve_{uid}_comment_ig"),
            InlineKeyboardButton("💬 Ultimo IG (+30 pts)", callback_data=f"approve_{uid}_comment_ig_last"),
        ],
        [
            InlineKeyboardButton("💬 Comment TT (+5 pts)", callback_data=f"approve_{uid}_comment_tt"),
            InlineKeyboardButton("💬 Ultimo TT (+30 pts)", callback_data=f"approve_{uid}_comment_tt_last"),
        ],
        [
            InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{uid}"),
        ]
    ])
    mission_text = (
        f"📸 *Captura de verificación*\n"
        f"Tipo: *{tipo_label}*\n"
        f"Usuario: {name_md} (ID: `{uid}`)\n"
        f"Puntos actuales: *{data['points']}*\n\n"
        f"Seleccioná la acción:"
    )
    logger.info(f"handle_photo: uid={uid} mission_type={mission_type} tipo_label={tipo_label}")
    # Enviar al grupo de mods primero
    mission_notified = False
    try:
        await context.bot.forward_message(
            chat_id=MOD_GROUP_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        await context.bot.send_message(
            chat_id=MOD_GROUP_ID,
            text=mission_text,
            parse_mode="Markdown",
            reply_markup=mission_keyboard
        )
        mission_notified = True
    except Exception as e:
        logger.error(f"Error notifying mod group: {type(e).__name__}: {e}")
    # Fallback: enviar a mods individuales si el grupo falló
    if not mission_notified:
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(
                    chat_id=mod_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
                await context.bot.send_message(
                    chat_id=mod_id,
                    text=mission_text,
                    parse_mode="Markdown",
                    reply_markup=mission_keyboard
                )
            except Exception as e2:
                logger.warning(f"No se pudo notificar al mod {mod_id}: {e2}")

# ── /aprobar — comando de texto para moderadores (fallback) ───────────────────
async def cmd_aprobar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /aprobar USER_ID reel|story|content")
        return

    target_uid = context.args[0]
    tipo       = context.args[1].lower()
    pts_map    = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"], "wallet_activate": 175, "review_store": 175, "review_trust": 175, "comment_ig": 5, "comment_ig_last": 30, "comment_tt": 5, "comment_tt_last": 30}

    if tipo not in pts_map:
        await update.message.reply_text("Tipo inválido. Usá: reel, story o content")
        return

    db = load_db()
    if target_uid not in db:
        await update.message.reply_text("Usuario no encontrado.")
        return

    earned = add_points(db[target_uid], pts_map[tipo])
    save_db(db)

    await update.message.reply_text(f"✅ +{earned} pts acreditados al usuario {target_uid}")

    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text=f"✅ *¡Misión verificada!*\n\n"
                 f"Tu captura fue aprobada.\n"
                 f"➕ *+{earned} puntos* acreditados 🐾\n"
                 f"⭐ Total: *{db[target_uid]['points']} puntos*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ── Callbacks (botones inline) ────────────────────────────────────────────────
async def cmd_ruleta_redirect(update, context):
    uid = str(update.effective_user.id)
    app_url = f"https://go.mypanther.io/app?id={uid}&v=3"
    from telegram import WebAppInfo
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎰 Abrir Ruleta en la Mini App", web_app=WebAppInfo(url=app_url))
    ]])
    await update.message.reply_text(
        "La ruleta solo esta disponible en la Mini App. Toca el boton para abrirla.",
        reply_markup=keyboard
    )

async def cmd_misiones_redirect(update, context):
    uid = str(update.effective_user.id)
    app_url = f"https://go.mypanther.io/app?id={uid}&v=3"
    from telegram import WebAppInfo
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Abrir Misiones en la Mini App", web_app=WebAppInfo(url=app_url))
    ]])
    await update.message.reply_text(
        "Las misiones solo estan disponibles en la Mini App. Toca el boton para abrirla.",
        reply_markup=keyboard
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data

    # ── Aprobar wallet (moderadores) ──
    if data_str.startswith("wallet_"):
        logger.info(f"Wallet callback: from_user.id={query.from_user.id} MOD_IDS={MOD_IDS}")
        parts = data_str.split("_")
        target_uid = parts[1]
        referrer_uid = parts[2] if len(parts) > 2 else None

        db = load_db()

        # Mark wallet activated for referred user
        if target_uid in db:
            db[target_uid]["wallet_activated"] = True
            db[target_uid]["cazador_verificado"] = True

        # Give +150 pts to referrer
        if referrer_uid and referrer_uid in db:
            earned = add_points(db[referrer_uid], PTS["referral_wallet"])
            db[referrer_uid]["referrals_active"]  = db[referrer_uid].get("referrals_active", 0) + 1
            db[referrer_uid]["cazadores_evento"]  = db[referrer_uid].get("cazadores_evento", 0) + 1
            save_db(db)
            try:
                await context.bot.send_message(
                    chat_id=int(referrer_uid),
                    text=f"🎉 *¡Tu referido activó su wallet!*\n\n"
                         f"*+{earned} puntos* acreditados en tu cuenta 🐆\n\n"
                         f"_Seguí invitando amigos para ganar más recompensas_",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=f"✅ *¡Tu wallet fue verificada!*\n\n"
                         f"Tu activación fue aprobada. Ya podés acceder a todas las misiones 🐆",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        else:
            save_db(db)

        await query.edit_message_text(f"✅ Wallet aprobada. +150 pts enviados al referidor.")
        return

    # ── Aprobar/rechazar captura (moderadores) ──
    if data_str.startswith("approve_") or data_str.startswith("reject_"):
        logger.info(f"Callback mod check: from_user.id={query.from_user.id} type={type(query.from_user.id)} MOD_IDS={MOD_IDS}")
        if query.from_user.id not in MOD_IDS:
            await query.answer("❌ No tenés permisos de moderador.", show_alert=True)
            logger.warning(f"ID {query.from_user.id} no está en MOD_IDS {MOD_IDS}")
            return

        parts = data_str.split("_")
        action = parts[0]
        target_uid = parts[1]
        tipo = "_".join(parts[2:]) if len(parts) > 2 else None

        db = load_db()
        if target_uid not in db:
            await query.edit_message_text("❌ Usuario no encontrado.")
            return

        mod_name = query.from_user.first_name or str(query.from_user.id)

        if action == "approve" and tipo:
            pts_map = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"], "wallet_activate": 175, "review_store": 175, "review_trust": 175, "comment_ig": 5, "comment_ig_last": 30, "comment_tt": 5, "comment_tt_last": 30}
            earned = add_points(db[target_uid], pts_map.get(tipo, 0))

            # Acciones especiales por tipo
            if tipo == "wallet_activate":
                db[target_uid]["wallet_activated"] = True
                db[target_uid]["cazador_verificado"] = True
                # Dar +150 pts al referidor y sumar cazadores_evento
                referrer_uid = db[target_uid].get("referred_by")
                if referrer_uid and referrer_uid in db:
                    referrer_earned = add_points(db[referrer_uid], PTS["referral_wallet"])
                    db[referrer_uid]["referrals_active"]  = db[referrer_uid].get("referrals_active", 0) + 1
                    db[referrer_uid]["cazadores_evento"]  = db[referrer_uid].get("cazadores_evento", 0) + 1
                    try:
                        await context.bot.send_message(
                            chat_id=int(referrer_uid),
                            text=(
                                f"🎉 *¡Tu referido activó su wallet!*\n\n"
                                f"*+{referrer_earned} puntos* acreditados en tu cuenta 🐆\n\n"
                                f"_Seguí invitando amigos para ganar más recompensas_"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
            elif tipo == "review_store":
                db[target_uid]["review_store_done"] = True
            elif tipo == "review_trust":
                db[target_uid]["review_trust_done"] = True

            save_db(db)

            tipo_label = {"reel": "Reel", "story": "Historia", "content": "Contenido", "wallet_activate": "Activacion de Wallet", "review_store": "Review Store", "review_trust": "Review Trustpilot", "comment_ig": "Comentario IG", "comment_ig_last": "Comentario Ultimo Post IG", "comment_tt": "Comentario TikTok", "comment_tt_last": "Comentario Ultimo Video TikTok"}
            approve_text = (
                f"✅ *{tipo_label.get(tipo, tipo)} aprobado*\n"
                f"Usuario: `{target_uid}`\n"
                f"Puntos acreditados: *+{earned}*"
            )
            # Confirmar el tap inmediatamente
            await query.answer(f"✅ {tipo_label.get(tipo, tipo)} aprobado — +{earned} pts")
            # Editar el mensaje en el grupo
            try:
                await query.edit_message_text(approve_text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"No se pudo editar mensaje de aprobación: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=approve_text,
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            # Notificar al usuario
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=(
                        f"✅ *¡Misión verificada!*\n\n"
                        f"Tu captura fue aprobada.\n"
                        f"➕ *+{earned} puntos* acreditados 🐾\n"
                        f"⭐ Total: *{db[target_uid]['points']} puntos*"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif action == "reject":
            save_db(db)
            reject_text = (
                f"❌ *Captura rechazada*\n"
                f"Usuario: `{target_uid}`\n"
                f"Rechazado por: {mod_name}"
            )
            await query.answer("❌ Captura rechazada")
            try:
                await query.edit_message_text(reject_text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"No se pudo editar mensaje de rechazo: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=reject_text,
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=(
                        "❌ Tu captura no pudo ser verificada.\n\n"
                        "Asegurate de que se vea claramente el contenido "
                        "de Panther y volvé a intentarlo 🐾"
                    ),
                )
            except Exception:
                pass
        return

    # ── Navegación del menú principal ──
    # Función genérica de redirect a la mini app
    async def redirect_to_app(upd, ctx):
        uid = str(upd.effective_user.id)
        db  = load_db()
        # Registrar usuario si no existe
        if uid not in db:
            db[uid] = get_user(db, uid, upd.effective_user)
            save_db(db)
        app_url = f"https://go.mypanther.io/app?id={uid}&v=3"
        from telegram import WebAppInfo
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🐆 Abrir Manada Panther", web_app=WebAppInfo(url=app_url))
        ]])
        await upd.message.reply_text(
            "Todas las misiones y funciones estan en la Mini App. Toca el boton para abrirla.",
            reply_markup=kb
        )

    handlers = {
        "checkin":  redirect_to_app,
        "puntos":   redirect_to_app,
        "ranking":  redirect_to_app,
        "ruleta":   redirect_to_app,
        "compartir": redirect_to_app,
        "broadcast":  cmd_broadcast,
        "ruleta_on":  cmd_ruleta_on,
        "verificar_follow": cmd_verificar_follow,
        "ruleta_off": cmd_ruleta_off,
        "ruleta_auto": cmd_ruleta_auto,
        "misiones": redirect_to_app,
        "referido": redirect_to_app,
        "niveles":  cmd_niveles,
    }

    if data_str in handlers:
        fake_update = type('Update', (), {
            'effective_user': query.from_user,
            'effective_chat': query.message.chat,
            'message':        query.message,
            'callback_query': query,
        })()
        await handlers[data_str](fake_update, context)

# ── /ayuda ────────────────────────────────────────────────────────────────────
async def cmd_mi_badge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía el badge de Fundador al usuario si lo tiene"""
    user = update.effective_user
    db = load_db()
    uid = str(user.id)
    data = db.get(uid, {})
    
    founder_number = data.get("founder_number")
    if not founder_number:
        await update.message.reply_text(
            "❌ No tenés badge de Fundador.\n\n"
            "El badge es exclusivo para los primeros 500 miembros de la Manada 🐾"
        )
        return
    
    await update.message.reply_text("🏆 Generando tu badge...")
    fname = user.first_name or user.username or "Miembro"
    success = await send_founder_badge(context.bot, uid, fname, founder_number)
    if not success:
        await update.message.reply_text("❌ Error generando el badge. Intentá de nuevo.")

async def cmd_enviar_badges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía badges a todos los usuarios existentes — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos.")
        return
    
    db = load_db()
    users = [(uid, data) for uid, data in db.items() 
             if not uid.startswith("_") and isinstance(data, dict) and "points" in data]
    
    await update.message.reply_text(f"📤 Enviando badges a {len(users)} usuarios...")
    
    sent = 0
    failed = 0
    for i, (uid, data) in enumerate(users):
        number = data.get("founder_number", i + 1)
        if not data.get("founder_number"):
            data["founder_number"] = i + 1
            db[uid] = data
        fname = data.get("first_name") or data.get("username") or "Miembro"
        success = await send_founder_badge(context.bot, uid, fname, number)
        if success:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.3)
    
    save_db(db)
    await update.message.reply_text(
        f"✅ Badges enviados\n\n"
        f"📤 Enviados: {sent}\n"
        f"❌ Fallidos: {failed}"
    )

async def cmd_verificar_cazador(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Marca manualmente a un usuario como cazador verificado — solo mods
    Uso: /verificar_cazador <user_id>
    """
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /verificar_cazador <user_id>")
        return

    target_uid = context.args[0].strip()
    db = load_db()

    if target_uid not in db:
        await update.message.reply_text(f"Usuario {target_uid} no encontrado en la DB.")
        return

    data = db[target_uid]
    nombre = data.get("username") or data.get("first_name") or target_uid

    # Ya verificado
    if data.get("cazador_verificado"):
        await update.message.reply_text(f"@{nombre} ya estaba verificado como cazador.")
        return

    # Marcar como cazador verificado
    data["cazador_verificado"] = True
    data["wallet_activated"]   = True

    # Activar referido si tiene referidor
    referred_by = data.get("referred_by")
    ref_msg = ""
    if referred_by:
        ref_data = db.get(str(referred_by), {})
        if ref_data:
            ref_data["referrals_active"] = ref_data.get("referrals_active", 0) + 1
            ref_data["cazadores_evento"] = ref_data.get("cazadores_evento", 0) + 1
            db[str(referred_by)] = ref_data
            ref_nombre = ref_data.get("username") or ref_data.get("first_name") or referred_by
            ref_msg = f"\nReferidor @{ref_nombre} actualizado (+1 activo)."
            try:
                await context.bot.send_message(
                    chat_id=int(referred_by),
                    text=f"Tu referido {nombre} fue verificado como cazador.\n\nYa cuenta como referido activo en tu registro."
                )
            except Exception:
                pass

    db[target_uid] = data
    save_db(db)

    # Notificar al usuario
    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text="Tu ritual fue verificado.\n\nEres oficialmente un Cazador de la Manada."
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"Cazador verificado: @{nombre} (ID: {target_uid}){ref_msg}"
    )


async def cmd_dar_puntos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /dar_puntos USER_ID cantidad motivo")
        return

    target_uid = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un numero.")
        return

    if amount <= 0 or amount > 500:
        await update.message.reply_text("❌ La cantidad debe ser entre 1 y 500.")
        return

    motivo = " ".join(context.args[2:]) if len(context.args) > 2 else "Bonus especial"

    db = load_db()
    if target_uid not in db:
        await update.message.reply_text("❌ Usuario no encontrado.")
        return

    earned = add_points(db[target_uid], amount)
    save_db(db)

    name = db[target_uid].get("username") or db[target_uid].get("first_name") or target_uid
    await update.message.reply_text(
        f"✅ +{earned} puntos acreditados a @{name}\n"
        f"Motivo: {motivo}\n"
        f"Total: {db[target_uid]['points']} puntos",
    )

    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text=(
                f"🎉 Bonus especial!\n\n"
                f"Recibiste +{earned} puntos por: {motivo}\n\n"
                f"Total: {db[target_uid]['points']} puntos 🐾"
            ),
        )
    except Exception:
        pass

async def cmd_reset_ruleta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resetea los giros de la ruleta para todos los usuarios — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return
    db = load_db()
    count = 0
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        data["spins_used_this_event"] = 0
        data["spins_available"] = 3
        count += 1
    save_db(db)
    await update.message.reply_text("Giros reseteados para " + str(count) + " usuarios. Cada uno tiene 3 giros. Listos para la ruleta!")

async def cmd_ganadores_ruleta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra ganadores de USDT y PNT en la ruleta — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    db = load_db()
    usdt_winners = []
    pnt_winners  = []
    total_spins  = 0

    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue

        nombre = (data.get("username") or data.get("first_name") or uid)
        # Limpiar caracteres que rompen Markdown
        nombre = str(nombre).replace("_", " ").replace("*", "").replace("`", "").replace("[", "").replace("]", "")

        history = data.get("history", [])
        for h in history:
            if h.get("type") != "ruleta":
                continue
            if h.get("date") == "2026-05-15":
                total_spins += 1
            prize = (h.get("prize") or "").upper()
            if prize == "USDT" and h.get("date") == "2026-05-15":
                usdt_winners.append(f"- {nombre} (ID: {uid}) a las {h.get('time', '??:??')}")
            elif prize == "PNT" and h.get("date") == "2026-05-15":
                pnt_winners.append(f"- {nombre} (ID: {uid}) a las {h.get('time', '??:??')}")

        # Fallback: flags directos
        if data.get("usdt_won_month") and not any(uid in w for w in usdt_winners):
            usdt_winners.append(f"- {nombre} (ID: {uid}) hora desconocida")
        if data.get("pnt_won_month") and not any(uid in w for w in pnt_winners):
            pnt_winners.append(f"- {nombre} (ID: {uid}) hora desconocida")

    lineas = [
        f"Ganadores Ruleta 15 mayo 2026",
        f"Total giros ese dia: {total_spins}",
        "",
        f"USDT — {len(usdt_winners)} ganador(es)",
    ]
    lineas.extend(usdt_winners if usdt_winners else ["- Ninguno registrado"])
    lineas.append("")
    lineas.append(f"PNT — {len(pnt_winners)} ganador(es)")
    lineas.extend(pnt_winners if pnt_winners else ["- Ninguno registrado"])

    await update.message.reply_text("\n".join(lineas))


async def cmd_stats_referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stats de referidos y wallets — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    db = load_db()
    users = {uid: d for uid, d in db.items() if not uid.startswith("_") and isinstance(d, dict) and "points" in d}

    total         = len(users)
    con_wallet    = sum(1 for d in users.values() if d.get("wallet_activated"))
    por_referido  = sum(1 for d in users.values() if d.get("referred_by"))
    directo       = total - por_referido

    # Top 5 referidores
    top = sorted(users.items(), key=lambda x: len(x[1].get("referrals", [])), reverse=True)[:5]

    lineas = [
        "STATS MANADA PANTHER\n",
        f"Total usuarios: {total}",
        f"Con wallet activa: {con_wallet}",
        f"Sin wallet: {total - con_wallet}",
        f"Entraron por referido: {por_referido}",
        f"Entraron directo: {directo}",
        "",
        "TOP REFERIDORES",
    ]
    for uid, d in top:
        nombre = str(d.get("username") or d.get("first_name") or uid)
        refs   = len(d.get("referrals", []))
        activos = d.get("referrals_active", 0)
        lineas.append(f"- {nombre}: {refs} referidos ({activos} con wallet)")

    await update.message.reply_text("\n".join(lineas))



async def cmd_links_campana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra los links de campaña — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return
    base = "https://t.me/ManadaPantherBot?start="
    lineas = [
        "Links de campana - Operacion 1000:",
        "",
        "Instagram:",
        base + "camp_ig",
        "",
        "Email:",
        base + "camp_mail",
        "",
        "TikTok:",
        base + "camp_tk",
        "",
        "Sitio Web:",
        base + "camp_web",
        "",
        "Los links de usuarios siguen siendo sus codigos PANTH-XXXXXX de siempre.",
    ]
    msg = "\n".join(lineas)
    await update.message.reply_text(msg)


async def handle_nuevo_cazador(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta #NuevoCazador con foto en el grupo y notifica a mods"""
    msg = update.message
    if not msg or not msg.photo:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    caption = (msg.caption or "").lower()
    if "#nuevocazador" not in caption:
        return

    user = update.effective_user
    uid  = str(user.id)
    db   = load_db()
    data = get_user(db, uid, user)
    nombre = f"@{user.username}" if user.username else user.first_name

    # Ya verificado
    if data.get("cazador_verificado"):
        try:
            await msg.reply_text(f"🐆 {nombre}, tu ritual ya fue verificado anteriormente.")
        except Exception:
            pass
        return

    referred_by = data.get("referred_by")
    source      = data.get("source", "directo")

    if referred_by:
        ref_data   = db.get(str(referred_by), {})
        ref_nombre = ref_data.get("username") or ref_data.get("first_name") or str(referred_by)
        ref_txt    = f"Referido por: @{ref_nombre} (ID: {referred_by})"
    else:
        src_label = CAMPAIGN_SOURCES.get(source, "Directo / desconocido")
        ref_txt   = f"Sin referidor — Origen: {src_label}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar cazador", callback_data=f"cazador_ok_{uid}")],
        [InlineKeyboardButton("❌ Rechazar",        callback_data=f"cazador_no_{uid}")]
    ])

    mod_text = (
        "🎯 Nuevo Cazador - Verificacion pendiente\n\n"
        f"Usuario: {nombre} (ID: {uid})\n"
        f"{ref_txt}\n\n"
"Verificar que la captura muestre Panther Wallet instalada."
    )

    # Confirmar al usuario
    try:
        await msg.reply_text(
            f"Captura recibida {nombre}.\n\n"
            "Un moderador va a verificar tu ritual. "
            "Te avisamos cuando este aprobado."
        )
    except Exception:
        pass

    # Notificar al grupo de mods
    try:
        await context.bot.forward_message(
            chat_id=MOD_GROUP_ID,
            from_chat_id=update.effective_chat.id,
            message_id=msg.message_id
        )
        await context.bot.send_message(
            chat_id=MOD_GROUP_ID,
            text=mod_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.warning(f"Error notificando mods cazador: {e}")
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(
                    chat_id=mod_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=msg.message_id
                )
                await context.bot.send_message(
                    chat_id=mod_id,
                    text=mod_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception:
                pass



async def handle_nuevo_cazador_privado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta #NuevoCazador con foto en privado y notifica a mods"""
    msg = update.message
    if not msg or not msg.photo:
        return
    if update.effective_chat.type != "private":
        return

    caption = (msg.caption or "").lower()
    if "#nuevocazador" not in caption:
        return

    user = update.effective_user
    uid  = str(user.id)
    db   = load_db()
    data = get_user(db, uid, user)
    nombre = f"@{user.username}" if user.username else user.first_name

    if data.get("cazador_verificado"):
        await msg.reply_text(f"🐆 {nombre}, tu ritual ya fue verificado anteriormente.")
        return

    referred_by = data.get("referred_by")
    source      = data.get("source", "directo")

    if referred_by:
        ref_data   = db.get(str(referred_by), {})
        ref_nombre = ref_data.get("username") or ref_data.get("first_name") or str(referred_by)
        ref_txt    = f"Referido por: @{ref_nombre} (ID: {referred_by})"
    else:
        src_label = CAMPAIGN_SOURCES.get(source, "Directo / desconocido")
        ref_txt   = f"Sin referidor — Origen: {src_label}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar cazador", callback_data=f"cazador_ok_{uid}")],
        [InlineKeyboardButton("❌ Rechazar",        callback_data=f"cazador_no_{uid}")]
    ])

    mod_text = (
        f"🎯 Nuevo Cazador - Verificacion pendiente\n\n"
        f"Usuario: {nombre} (ID: {uid})\n"
        f"{ref_txt}\n\n"
        f"Verificar que la captura muestre Panther Wallet con 2FA activo."
    )

    await msg.reply_text(
        f"Captura recibida {nombre}.\n\n"
        f"Un moderador va a verificar tu ritual. "
        f"Te avisamos cuando este aprobado. 🐾"
    )

    try:
        await context.bot.forward_message(
            chat_id=MOD_GROUP_ID,
            from_chat_id=update.effective_chat.id,
            message_id=msg.message_id
        )
        await context.bot.send_message(
            chat_id=MOD_GROUP_ID,
            text=mod_text,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.warning(f"Error notificando mods cazador privado: {e}")
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(
                    chat_id=mod_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=msg.message_id
                )
                await context.bot.send_message(
                    chat_id=mod_id,
                    text=mod_text,
                    reply_markup=keyboard
                )
            except Exception:
                pass

async def handle_cazador_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para aprobar o rechazar un cazador"""
    query = update.callback_query

    if update.effective_user.id not in MOD_IDS:
        await query.answer("No tenes permisos.", show_alert=True)
        return

    await query.answer()

    data_str = query.data
    db = load_db()

    if data_str.startswith("cazador_ok_"):
        target_uid = data_str.replace("cazador_ok_", "")
        target_data = db.get(target_uid)
        if not target_data:
            await query.edit_message_text("Error: usuario no encontrado.")
            return

        nombre = target_data.get("username") or target_data.get("first_name") or target_uid

        # Marcar como cazador verificado
        target_data["cazador_verificado"] = True
        target_data["wallet_activated"]   = True

        # Activar referido si tiene referidor
        referred_by = target_data.get("referred_by")
        ref_msg = ""
        if referred_by:
            ref_data = db.get(str(referred_by), {})
            if ref_data:
                if target_uid not in ref_data.get("referrals", []):
                    ref_data.setdefault("referrals", []).append(target_uid)
                ref_data["referrals_active"]  = ref_data.get("referrals_active", 0) + 1
                ref_data["cazadores_evento"]  = ref_data.get("cazadores_evento", 0) + 1
                db[str(referred_by)] = ref_data
                ref_nombre = ref_data.get("username") or ref_data.get("first_name") or referred_by
                ref_msg = f"\nReferidor @{ref_nombre} actualizado (+1 activo)."

                # Notificar al referidor
                try:
                    await context.bot.send_message(
                        chat_id=int(referred_by),
                        text=(
                            f"Tu referido {nombre} completo el ritual de cazador.\n\n"
                            "Ya cuenta como referido activo en tu registro"
                        )
                    )
                except Exception:
                    pass

        db[target_uid] = target_data
        save_db(db)

        # Notificar al usuario aprobado
        try:
            await context.bot.send_message(
                chat_id=int(target_uid),
                text=(
                    "Tu ritual fue verificado.\n\n"
                    "Sos oficialmente un Cazador de la Manada\n"
                    "Cuando empiece el evento vas a recibir todos los detalles."
                )
            )
        except Exception:
            pass

        nombre_safe = str(nombre).replace("_", " ").replace("*", "").replace("`", "")
        await query.edit_message_text(
            f"✅ Cazador aprobado: @{nombre_safe} (ID: {target_uid}){ref_msg}"
        )

    elif data_str.startswith("cazador_no_"):
        target_uid = data_str.replace("cazador_no_", "")
        target_data = db.get(target_uid, {})
        nombre = target_data.get("username") or target_data.get("first_name") or target_uid

        try:
            await context.bot.send_message(
                chat_id=int(target_uid),
                text=(
                    "Tu captura no pudo ser verificada.\n\n"
                    "Asegurate de que la imagen muestre Panther Wallet instalada "
                    "y volvé a mandarla con #NuevoCazador."
                )
            )
        except Exception:
            pass

        nombre_safe2 = str(nombre).replace("_", " ").replace("*", "").replace("`", "")
        await query.edit_message_text(f"❌ Cazador rechazado: @{nombre_safe2} (ID: {target_uid})")



# ═══════════════════════════════════════════════════════════════
# ONBOARDING — Mensajes de bienvenida secuenciales
# ═══════════════════════════════════════════════════════════════

async def send_welcome_sequence(bot, uid: str, first_name: str):
    """Envía 3 mensajes de bienvenida con delays al usuario nuevo."""

    msg1 = (
        f"🐆 *¡Bienvenido a la Manada Panther, {first_name}!*\n\n"
        f"Me alegra que estés acá. Este es el espacio donde la comunidad de "
        f"Panther Wallet se reúne, aprende y gana recompensas reales.\n\n"
        f"Para ser parte oficial de la Manada necesitas completar tu ritual de iniciación:\n\n"
        f"*Paso 1:* Descarga Panther Wallet\n"
        f"👉 https://mypanther.io/es/\n\n"
        f"*Paso 2:* Activa tu cuenta y configura el Google 2FA\n"
        f"_(Configuración → Seguridad → Google Authenticator)_\n\n"
        f"*Paso 3:* Toma una captura de pantalla de esa sección mostrando el 2FA activo\n\n"
        f"*Paso 4:* Envía esa captura acá al bot en privado con el hashtag *#NuevoCazador*\n\n"
        f"Un moderador la verificará y quedarás oficialmente como Cazador de la Manada. 🐾"
    )

    msg2 = (
        f"📋 *Reglas de la Manada*\n\n"
        f"Para que este espacio funcione bien para todos, seguimos estas reglas:\n\n"
        f"✅ Respeto y buena onda — acá nos ayudamos entre todos\n"
        f"✅ Las dudas sobre la wallet son bienvenidas — la comunidad responde "
        f"y si no puede, te derivamos al soporte oficial\n"
        f"✅ No spam ni promoción de proyectos externos\n"
        f"✅ Solo contenido relacionado con Panther Wallet y crypto\n"
        f"✅ No FUD, no toxicidad, no comentarios malintencionados\n\n"
        f"⭐ La buena onda se premia — los miembros activos y colaborativos "
        f"acumulan puntos y reconocimiento dentro de la Manada.\n\n"
        f"⚠️ El incumplimiento puede resultar en suspensión del grupo.\n\n"
        f"El equipo de moderación está siempre presente. Ante cualquier duda, escribinos. 🐆"
    )

    msg3 = (
        f"🔗 *Seguinos en todas las plataformas*\n\n"
        f"Toda la actividad oficial de Panther Wallet pasa por acá:\n\n"
        f"🐾 Instagram: {LINKS['ig']}\n"
        f"📺 YouTube: {LINKS['yt']}\n"
        f"🎵 TikTok: {LINKS['tiktok']}\n"
        f"🌐 Sitio web: {LINKS['web']}\n"
        f"📢 Canal oficial: {LINKS['canal']}\n"
        f"💬 Chat general: {LINKS['chat']}\n\n"
        f"Seguinos para no perderte ningún anuncio, sorteo ni novedad. 🐆"
    )

    try:
        await bot.send_message(chat_id=int(uid), text=msg1, parse_mode="Markdown",
                               disable_web_page_preview=True)
        await asyncio.sleep(300)  # 5 minutos
        await bot.send_message(chat_id=int(uid), text=msg2, parse_mode="Markdown")
        await asyncio.sleep(300)  # 10 minutos
        await bot.send_message(chat_id=int(uid), text=msg3, parse_mode="Markdown",
                               disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"Error en welcome sequence para {uid}: {e}")


# ═══════════════════════════════════════════════════════════════
# EVENTO — Operación 1,000 Cazadores
# ═══════════════════════════════════════════════════════════════

def get_evento_state():
    """Retorna el estado actual del evento desde globals."""
    db = load_db()
    g = db.get("_global", {})
    return {
        "activo":      g.get("evento_activo", False),
        "start_date":  g.get("evento_start_date"),
        "end_date":    g.get("evento_end_date"),
        "extension":   g.get("evento_extension", 0),
        "cerrado":     g.get("evento_cerrado", False),
        "cofre_abierto": g.get("cofre_abierto", False),
    }

def set_evento_state(**kwargs):
    """Guarda estado del evento en globals."""
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"].update(kwargs)
    save_db(db)

def get_cazadores_count():
    """Retorna total de cazadores verificados en el evento."""
    db = load_db()
    return sum(d.get("cazadores_evento", 0) for uid, d in db.items()
               if not uid.startswith("_") and isinstance(d, dict))

def get_top_cazadores(n=10):
    """Retorna top N referidores del evento — solo cazadores_evento (desde inicio del evento)."""
    db = load_db()
    users = [(uid, d) for uid, d in db.items()
             if not uid.startswith("_") and isinstance(d, dict)
             and d.get("cazadores_evento", 0) > 0]
    ranked = sorted(users, key=lambda x: x[1].get("cazadores_evento", 0), reverse=True)
    return ranked[:n]

def calcular_cofre(db):
    """Calcula distribución del cofre según fórmula de Valeria."""
    # Solo usuarios con mínimo 3 cazadores del evento
    elegibles = {
        uid: d for uid, d in db.items()
        if not uid.startswith("_") and isinstance(d, dict)
        and d.get("cazadores_evento", 0) >= 3
    }
    total_refs = sum(d.get("cazadores_evento", 0) for d in elegibles.values())
    if total_refs == 0:
        return {}
    distribucion = {}
    for uid, d in elegibles.items():
        refs = d.get("cazadores_evento", 0)
        pnt = round((refs / total_refs) * COFRE_PNT, 4)
        distribucion[uid] = {"pnt": pnt, "refs": refs, "nombre": d.get("username") or d.get("first_name") or uid}
    return distribucion


async def cmd_evento_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa el evento — solo mods."""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    ev = get_evento_state()
    if ev["activo"]:
        await update.message.reply_text("El evento ya está activo.")
        return

    start = datetime.now()
    end   = start + timedelta(days=EVENTO_DIAS_BASE)

    set_evento_state(
        evento_activo=True,
        evento_start_date=start.isoformat(),
        evento_end_date=end.isoformat(),
        evento_extension=0,
        evento_cerrado=False,
        cofre_abierto=False,
    )

    # Anuncio al grupo
    msg = (
        "⚔️ *OPERACIÓN 1,000 CAZADORES — ARRANCÓ*\n\n"
        f"El evento está activo. Tenemos {EVENTO_DIAS_BASE} días.\n\n"
        f"🏆 *Premios individuales (top 3):*\n"
        f"1er lugar: {PREMIOS_TOP_PNT[1]} PNT\n"
        f"2do lugar: {PREMIOS_TOP_PNT[2]} PNT\n"
        f"3er lugar: {PREMIOS_TOP_PNT[3]} PNT\n\n"
        f"💰 *Cofre comunitario:* {COFRE_PNT} PNT\n"
        f"_(Se reparte entre todos los que traigan 3+ cazadores si llegamos a 1,000)_\n\n"
        f"¿Cómo participar?\n"
        f"1. Compartí tu link de referido\n"
        f"2. Tu referido descarga Panther Wallet y activa el 2FA\n"
        f"3. Te manda la captura al bot con *#NuevoCazador*\n"
        f"4. Un mod lo verifica → suma a tu cuenta\n\n"
        f"Cierre estimado: {end.strftime('%d/%m/%Y')} 🐾"
    )
    try:
        await context.bot.send_message(chat_id=MAIN_GROUP_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Error anunciando evento al grupo: {e}")

    await update.message.reply_text(f"✅ Evento activado. Cierre: {end.strftime('%d/%m/%Y')}")


async def cmd_estado_cofre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del evento — solo mods."""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    ev = get_evento_state()
    if not ev["activo"] and not ev["cerrado"]:
        await update.message.reply_text("El evento no está activo.")
        return

    cazadores = get_cazadores_count()
    top = get_top_cazadores(5)
    db  = load_db()

    start = datetime.fromisoformat(ev["start_date"]) if ev["start_date"] else datetime.now()
    end   = datetime.fromisoformat(ev["end_date"])   if ev["end_date"]   else datetime.now()
    dias_restantes = (end - datetime.now()).days
    dias_transcurridos = (datetime.now() - start).days

    top_txt = ""
    for i, (uid, d) in enumerate(top, 1):
        nombre = d.get("username") or d.get("first_name") or uid
        refs   = d.get("referrals_active", 0)
        top_txt += f"  {i}. {nombre} — {refs} cazadores\n"

    lineas = [
        "⚔️ ESTADO DEL COFRE",
        "",
        f"Día {dias_transcurridos} de {EVENTO_DIAS_BASE + ev.get('extension', 0)}",
        f"Cierre: {end.strftime('%d/%m/%Y')}",
        f"Días restantes: {max(0, dias_restantes)}",
        "",
        f"Cazadores verificados: {cazadores} / {META_CAZADORES}",
        f"Faltan: {max(0, META_CAZADORES - cazadores)}",
        "",
        "TOP 5 REFERIDORES:",
        top_txt,
        f"Cofre: {'ABIERTO' if ev['cofre_abierto'] else 'CERRADO'}",
        f"Estado: {'CERRADO' if ev['cerrado'] else 'ACTIVO'}",
    ]
    await update.message.reply_text("\n".join(lineas))


async def cmd_cazadores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leaderboard público del evento."""
    ev = get_evento_state()
    if not ev["activo"] and not ev["cerrado"]:
        await update.message.reply_text("El evento no está activo todavía.")
        return

    cazadores_total = get_cazadores_count()
    top = get_top_cazadores(10)

    lineas = [
        "⚔️ TOP CAZADORES — Operacion 1000",
        f"Cazadores verificados: {cazadores_total} / {META_CAZADORES}",
        "",
    ]
    for i, (uid, d) in enumerate(top, 1):
        nombre = d.get("username") or d.get("first_name") or uid
        refs   = d.get("referrals_active", 0)
        medal  = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        lineas.append(f"{medal} {nombre} — {refs} cazadores")

    lineas.append("")
    lineas.append("Compartí tu link desde la Mini App y sumá cazadores 🐾")
    await update.message.reply_text("\n".join(lineas))


async def check_evento_dia(app_or_context):
    """Job diario: verifica si hay que enviar mensajes automáticos o cerrar el evento."""
    bot = getattr(app_or_context, 'bot', None) or getattr(app_or_context, 'bot', app_or_context)
    ev = get_evento_state()
    if not ev["activo"] or ev["cerrado"]:
        return

    start = datetime.fromisoformat(ev["start_date"])
    dia   = (datetime.now() - start).days + 1
    cazadores = get_cazadores_count()

    # Meta alcanzada — abrir cofre
    if cazadores >= META_CAZADORES and not ev["cofre_abierto"]:
        await abrir_cofre(bot)
        return

    # Día 7 — top 5
    if dia == 7:
        top = get_top_cazadores(5)
        lineas = ["⚔️ *SEMANA 1 — TOP 5 CAZADORES*", ""]
        for i, (uid, d) in enumerate(top, 1):
            nombre = d.get("username") or d.get("first_name") or uid
            refs   = d.get("referrals_active", 0)
            medal  = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
            lineas.append(f"{medal} {nombre} — {refs} cazadores")
        lineas.extend(["", f"Total: {cazadores} / {META_CAZADORES} cazadores", "Seguimos 🐾"])
        try:
            await bot.send_message(chat_id=MAIN_GROUP_ID,
                                           text="\n".join(lineas), parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Error mensaje día 7: {e}")

    # Día 15 — alerta cofre
    elif dia == 15:
        faltan = max(0, META_CAZADORES - cazadores)
        msg = (
            f"⚠️ *ALERTA DEL COFRE*\n\n"
            f"Estamos en el día 15. Faltan *{faltan} cazadores* para abrir el cofre.\n\n"
            f"💰 {COFRE_PNT} PNT están esperando.\n"
            f"No dejen que se queme. Compartan sus links ahora 🐾"
        )
        try:
            await bot.send_message(chat_id=MAIN_GROUP_ID, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Error mensaje día 15: {e}")

    # Día 20+ — evaluar extensión o cierre
    end = datetime.fromisoformat(ev["end_date"])
    if datetime.now() >= end:
        await evaluar_cierre_evento(bot, cazadores)


async def evaluar_cierre_evento(bot, cazadores: int):
    """Evalúa si extender o cerrar el evento."""
    ev = get_evento_state()
    faltan = META_CAZADORES - cazadores

    if faltan <= 0:
        await abrir_cofre(bot)
        return

    # Calcular extensión
    if cazadores >= 800:
        dias_extra = 5
        rango = "800-999"
    elif cazadores >= 600:
        dias_extra = 10
        rango = "600-799"
    else:
        dias_extra = 15
        rango = "menos de 600"

    nueva_end = datetime.now() + timedelta(days=dias_extra)
    extension_total = ev.get("extension", 0) + dias_extra

    set_evento_state(
        evento_end_date=nueva_end.isoformat(),
        evento_extension=extension_total,
    )

    msg = (
        f"⏳ *EL EVENTO SE EXTIENDE*\n\n"
        f"Llegamos al día 20 con {cazadores} cazadores ({rango}).\n\n"
        f"El cofre sigue abierto. Tienen *{dias_extra} días más* para llegar a 1,000.\n\n"
        f"Nuevo cierre: *{nueva_end.strftime('%d/%m/%Y')}*\n\n"
        f"Los {COFRE_PNT} PNT siguen en juego. No paren. 🐾"
    )
    try:
        await context.bot.send_message(chat_id=MAIN_GROUP_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Error anunciando extensión: {e}")


async def abrir_cofre(bot):
    """Abre el cofre, distribuye PNT y anuncia ganadores."""
    db = load_db()
    distribucion = calcular_cofre(db)

    # Guardar PNT ganados en cada usuario
    for uid, info in distribucion.items():
        if uid in db:
            db[uid]["evento_pnt_ganado"] = info["pnt"]
    save_db(db)

    # Top 3 individual
    top3 = get_top_cazadores(3)

    set_evento_state(cofre_abierto=True, evento_cerrado=True, evento_activo=False)

    # Anuncio de ganadores
    lineas = [
        "🎉 *EL COFRE SE ABRIÓ — OPERACIÓN 1,000 CAZADORES*",
        "",
        "🏆 *PREMIOS INDIVIDUALES (TOP 3):*",
    ]
    for i, (uid, d) in enumerate(top3, 1):
        nombre = d.get("username") or d.get("first_name") or uid
        pnt    = PREMIOS_TOP_PNT.get(i, 0)
        medal  = ["🥇","🥈","🥉"][i-1]
        lineas.append(f"{medal} {nombre} — {pnt} PNT")

    lineas.extend([
        "",
        f"💰 *COFRE COMUNITARIO: {COFRE_PNT} PNT*",
        f"Distribuido entre {len(distribucion)} cazadores elegibles:",
        "",
    ])
    for info in sorted(distribucion.values(), key=lambda x: x["pnt"], reverse=True)[:5]:
        lineas.append(f"  🐾 {info['nombre']} — {info['pnt']} PNT ({info['refs']} cazadores)")
    if len(distribucion) > 5:
        lineas.append(f"  ...y {len(distribucion)-5} más")

    lineas.extend([
        "",
        "Los premios se entregarán en los próximos 5 días hábiles.",
        "Gracias a todos los que participaron. La Manada es real. 🐆",
    ])

    try:
        await bot.send_message(chat_id=MAIN_GROUP_ID,
                                       text="\n".join(lineas), parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Error anunciando apertura del cofre: {e}")

    # Notificar a mods con lista completa
    mod_lineas = ["📋 DISTRIBUCIÓN COMPLETA DEL COFRE", ""]
    for uid, info in sorted(distribucion.items(), key=lambda x: x[1]["pnt"], reverse=True):
        mod_lineas.append(f"{info['nombre']} (ID:{uid}) — {info['pnt']} PNT — {info['refs']} cazadores")
    for mod_id in MOD_IDS:
        try:
            await bot.send_message(chat_id=mod_id,
                                           text="\n".join(mod_lineas))
        except Exception:
            pass


async def cmd_misiones_recientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra misiones aprobadas/rechazadas de los últimos 2 días — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return

    db = load_db()
    hoy = date.today()
    fechas_validas = {
        (hoy - timedelta(days=i)).isoformat()
        for i in range(3)  # hoy, ayer, anteayer
    }

    tipo_label = {
        "reel":              "🎬 Reel",
        "story":             "📸 Historia",
        "historia":          "📸 Historia",
        "content":           "📱 Contenido propio",
        "wallet_activate":   "👛 Wallet activada",
        "comment_ig":        "💬 Comentario IG",
        "comment_ig_last":   "💬 Comentario IG último post",
        "comment_tt":        "💬 Comentario TikTok",
        "comment_tt_last":   "💬 Comentario TikTok último video",
        "referral":          "🔗 Referido",
        "referral_wallet":   "🔗 Referido con wallet",
        "cazador":           "⚔️ Cazador verificado",
        "follow_ig":         "👁 Follow IG",
        "follow_x":          "👁 Follow X",
        "follow_tiktok":     "👁 Follow TikTok",
        "follow_facebook":   "👁 Follow Facebook",
        "follow_youtube":    "👁 Follow YouTube",
        "glosario":          "📖 Glosario",
        "ruleta":            "🎰 Ruleta",
    }

    aprobadas = []
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        nombre = str(data.get("username") or data.get("first_name") or uid).replace("_", " ")
        for h in data.get("history", []):
            if h.get("date") in fechas_validas:
                tipo = h.get("type", "otro")
                pts  = h.get("pts", 0)
                hora = h.get("time", "??:??")
                fecha = h.get("date", "")
                label = tipo_label.get(tipo, tipo)
                aprobadas.append(f"{fecha} {hora} — {nombre} — {label} +{pts}pts")

    if not aprobadas:
        await update.message.reply_text("No hay misiones aprobadas en los últimos 2 días.")
        return

    aprobadas.sort(reverse=True)
    lineas = [f"📋 Misiones aprobadas (últimos 2 días)\n"]
    lineas.extend(aprobadas[:50])
    if len(aprobadas) > 50:
        lineas.append(f"...y {len(aprobadas)-50} más")

    await update.message.reply_text("\n".join(lineas))


async def cmd_star(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dar una estrella a un usuario respondiendo su mensaje"""
    if not update.message.reply_to_message:
        await update.message.reply_text("⭐ Respondé el mensaje del usuario al que querés dar una estrella.")
        return

    giver = update.effective_user
    receiver = update.message.reply_to_message.from_user

    if not receiver or receiver.id == giver.id:
        await update.message.reply_text("No podés darte estrellas a vos mismo 😄")
        return

    if receiver.is_bot:
        await update.message.reply_text("Los bots no reciben estrellas 🤖")
        return

    # Verificar cooldown — máximo 5 estrellas por hora
    now = datetime.now().timestamp()
    uid = str(giver.id)
    if uid not in STAR_COOLDOWN:
        STAR_COOLDOWN[uid] = []
    STAR_COOLDOWN[uid] = [t for t in STAR_COOLDOWN[uid] if now - t < 3600]

    if len(STAR_COOLDOWN[uid]) >= 5:
        secs = int(3600 - (now - STAR_COOLDOWN[uid][0]))
        mins = secs // 60
        await update.message.reply_text(
            f"⏳ Ya diste 5 estrellas esta hora. Podés dar más en {mins} minutos."
        )
        return

    STAR_COOLDOWN[uid].append(now)

    # Determinar puntos
    is_reply_of_reply = update.message.reply_to_message.reply_to_message is not None
    pts = 5 if is_reply_of_reply else 3

    # Registrar estrella
    rid = str(receiver.id)
    if rid not in CHAT_STARS:
        CHAT_STARS[rid] = {
            "stars": 0, "pts": 0,
            "username": receiver.username or "",
            "first_name": receiver.first_name or "Usuario"
        }
    CHAT_STARS[rid]["stars"] += 1
    CHAT_STARS[rid]["pts"] += pts

    giver_name = ("@" + giver.username) if giver.username else giver.first_name
    receiver_name = ("@" + receiver.username) if receiver.username else receiver.first_name

    stars_total = CHAT_STARS[rid]["stars"]
    pts_total = CHAT_STARS[rid]["pts"]

    await update.message.reply_text(
        "⭐ " + giver_name + " le dio una estrella a " + receiver_name + "!\n"
        "+" + str(pts) + " pts en el ranking del chat 🐾\n\n"
        "Total: " + str(stars_total) + " ⭐ · " + str(pts_total) + " pts"
    )

    # Notificar al receptor en privado
    try:
        await context.bot.send_message(
            chat_id=int(rid),
            text=(
                "⭐ Recibiste una estrella!\n\n" +
                giver_name + " reconocio tu aporte en el chat de la Manada.\n\n" +
                "+" + str(pts) + " pts en el ranking del chat\n" +
                "Total: " + str(stars_total) + " estrellas"
            )
        )
    except Exception:
        pass


async def cmd_award(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mods dan puntos especiales a usuarios en el chat general"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenés permisos para usar /award.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: /award @usuario cantidad razon\n"
            "Ejemplo: /award @juan 50 Mejor respuesta del quiz"
        )
        return

    username = context.args[0].lstrip("@")
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("La cantidad debe ser un número.")
        return

    if amount <= 0 or amount > 500:
        await update.message.reply_text("La cantidad debe ser entre 1 y 500.")
        return

    reason = " ".join(context.args[2:]) if len(context.args) > 2 else "Premio especial"
    mod_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name

    # Buscar usuario por username en CHAT_STARS o crear entrada
    uid_found = None
    for uid, data in CHAT_STARS.items():
        if data.get("username", "").lower() == username.lower():
            uid_found = uid
            break

    if not uid_found:
        uid_found = f"@{username}"
        CHAT_STARS[uid_found] = {"stars": 0, "pts": 0, "username": username, "first_name": username}

    CHAT_STARS[uid_found]["pts"] += amount

    if uid_found not in CHAT_AWARDS:
        CHAT_AWARDS[uid_found] = []
    CHAT_AWARDS[uid_found].append({"pts": amount, "reason": reason, "mod": mod_name})

    save_chat_stars()

    await update.message.reply_text(
        "🏆 " + mod_name + " le otorgo +" + str(amount) + " pts a @" + username + "\n" +
        "Motivo: " + reason + "\n\n" +
        "Total en ranking del chat: " + str(CHAT_STARS[uid_found]['pts']) + " pts"
    )


async def cmd_recompensa_todos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Da puntos a todos los usuarios registrados — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /recompensa_todos cantidad motivo")
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("La cantidad debe ser un numero.")
        return
    if amount <= 0 or amount > 10000:
        await update.message.reply_text("La cantidad debe ser entre 1 y 10000.")
        return
    motivo = " ".join(context.args[1:]) if len(context.args) > 1 else "Recompensa especial"
    db = load_db()
    count = 0
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict) or "points" not in data:
            continue
        add_points(data, amount)
        count += 1
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text="Recompensa especial!\n\n+" + str(amount) + " puntos acreditados\nMotivo: " + motivo + "\n\nTotal: " + str(data["points"]) + " puntos"
            )
        except Exception:
            pass
    save_db(db)
    await update.message.reply_text("✅ +" + str(amount) + " pts acreditados a " + str(count) + " usuarios. Motivo: " + motivo)

async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca un usuario por username y devuelve su ID — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tenes permisos.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /buscar @username o /buscar nombre")
        return
    query = context.args[0].lstrip("@").lower()
    db = load_db()
    found = []
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        username   = (data.get("username") or "").lower()
        first_name = (data.get("first_name") or "").lower()
        if query in username or query in first_name:
            found.append(data)
    if not found:
        await update.message.reply_text("No se encontro ningun usuario con ese nombre.")
        return
    lines = ["Usuarios encontrados:\n"]
    for u in found[:10]:
        name = u.get("username") or u.get("first_name") or "?"
        pts  = u.get("points", 0)
        lines.append("@" + str(name) + " — ID: " + str(u.get("id", "?")) + " — " + str(pts) + " pts")
    lines.append("\nUsa /dar_puntos ID cantidad motivo")
    await update.message.reply_text("\n".join(lines))

async def cmd_mis_estrellas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in CHAT_STARS:
        d = CHAT_STARS[uid]
        stars = d.get("stars", 0)
        pts   = d.get("pts", 0)
        text  = "Tus estrellas en la Manada\n\nEstrellas: " + str(stars) + "\nPuntos del chat: " + str(pts) + "\n\nUsa /leaderboard para el ranking."
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("Todavia no tenes estrellas. Participa en el chat y otros pueden darte estrellas con /star.")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el ranking del chat general por estrellas"""
    if not CHAT_STARS:
        await update.message.reply_text("🌟 Aún no hay estrellas repartidas. Usá /star para reconocer a alguien!")
        return

    sorted_users = sorted(CHAT_STARS.items(), key=lambda x: x[1]["pts"], reverse=True)[:10]

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Ranking de la Manada 🏆\n"]

    for i, (uid, data) in enumerate(sorted_users):
        medal = medals[i] if i < 3 else str(i+1) + "."
        name = ("@" + data['username']) if data.get("username") else data.get("first_name", "Usuario")
        stars = data.get("stars", 0)
        pts = data.get("pts", 0)
        lines.append(medal + " " + name + " — " + str(stars) + " ⭐ · " + str(pts) + " pts")

    await update.message.reply_text("\n".join(lines))


async def cmd_pingmods(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje de prueba a todos los mods — solo moderadores"""
    if update.effective_user.id not in MOD_IDS:
        return
    results = []
    for mod_id in MOD_IDS:
        try:
            msg = (
                "🔔 *Test de notificación*\n\n"
                "Este mensaje confirma que recibís notificaciones del bot correctamente.\n\n"
                f"_Enviado por mod {update.effective_user.id}_"
            )
            await context.bot.send_message(
                chat_id=mod_id,
                text=msg,
                parse_mode="Markdown"
            )
            results.append(f"✅ {mod_id}")
        except Exception as e:
            results.append(f"❌ {mod_id}: {e}")
    await update.message.reply_text(
        "Resultados:\n" + "\n".join(results),
        parse_mode="Markdown"
    )

async def cmd_resetcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset check-in for testing — solo moderadores"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos.")
        return
    db = load_db()
    uid = str(update.effective_user.id)
    if uid in db:
        db[uid]["last_checkin"] = None
        db[uid]["last_ruleta"] = None
        save_db(db)
        await update.message.reply_text("✅ Check-in y ruleta reseteados. Ya podés probar de nuevo.")
    else:
        await update.message.reply_text("❌ Usuario no encontrado.")

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐆 *CÓMO FUNCIONA LA MANADA PANTHER*\n\n"
        "*Ganás puntos haciendo:*\n"
        "🔥 Check-in diario — mantené la racha\n"
        "👥 Referir amigos al canal\n"
        "📱 Compartir contenido de Panther\n"
        "🎰 Girar la ruleta una vez por día\n\n"
        "*Rachas especiales:*\n"
        "7 días seguidos → +50 pts bonus\n"
        "14 días seguidos → +150 pts bonus\n"
        "30 días seguidos → +500 pts bonus\n\n"
        "*Los niveles:*\n"
        "🐾 Cachorro → 🔍 Rastreador → 🛡️ Guardián\n"
        "🧭 Explorador → ⚡ Embajador → 🦁 Leyenda\n"
        "🔥 Elite → 💎 Diamante → 👑 Rey de la Manada\n"
        "🌕 Lunar → ⚡🐆 Panther Alpha → 🏆 Inmortal → 🌟 Dios de la Manada\n\n"
        "*Premios mensuales ruleta:*\n"
        "💵 USDT: $5, $10 y $50\n"
        "🐾 PNT: 50, 100, 250 y 500 tokens\n"
        "_(Un premio económico por usuario por mes)_\n\n"
        "Usá /niveles para ver la tabla completa\n"
        "Usá /ranking para ver quién va ganando",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ══════════════════════════════════════════════════════════════════════════════
# ── API HTTP para Mini App ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class MiniAppHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Silenciar logs HTTP

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        # ── GET /user?id=123456 ──
        if path == "/user":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = db.get(uid)
            if not data:
                return self.send_json({"error": "User not found"}, 404)

            # Fix: referrals puede estar guardado como int en usuarios viejos
            if not isinstance(data.get("referrals"), list):
                data["referrals"] = []
                db[uid] = data
                save_db(db)

            level = get_level(data["points"])
            next_lv, pts_needed = get_next_level(data["points"])
            today = date.today().isoformat()

            # Calcular nivel index (0-6)
            level_idx = next(
                (i for i, (mn, mx, name) in enumerate(LEVELS) if name == level), 0
            )
            level_max = LEVELS[level_idx][1]
            level_min = LEVELS[level_idx][0]
            xp_pct = round(
                (data["points"] - level_min) / max(level_max - level_min, 1) * 100, 1
            ) if level_max < 999999 else 100

            # Historial reciente (últimas 5 entradas del log si existe)
            history = data.get("history", [])[-5:]

            return self.send_json({
                "id":             uid,
                "username":       data.get("username", ""),
                "first_name":     data.get("first_name", ""),
                "points":         data["points"],
                "streak":         data["streak"],
                "level":          level,
                "level_idx":      level_idx,
                "xp_pct":         xp_pct,
                "level_min":      level_min,
                "level_max":      level_max,
                "next_level":     next_lv,
                "pts_to_next":    pts_needed,
                "referrals":         len(data.get("referrals", [])),
                "referrals_active":  data.get("referrals_active", 0),
                "reel_count_today":   data.get("reel_count_today", 0),
                "story_count_today":  data.get("story_count_today", 0),
                "content_count_today": data.get("content_count_today", 0),
                "referral_code":     data.get("referral_code", ""),
                "checkin_today":  data.get("last_checkin") == today,
                "ruleta_today":   data.get("last_ruleta") == today,
                "ruleta_active":  is_ruleta_active(),
                "ruleta_access":  can_access_ruleta(data),
                "spins_available": get_available_spins(data),
                "spins_used":     data.get("spins_used_this_event", 0),
                "reel_verified":  data.get("reel_verified", False),
                "story_verified": data.get("story_verified", False),
                "follow_ig":      data.get("follow_ig", False),
                "follow_x":       data.get("follow_x", False),
                "follow_tiktok":  data.get("follow_tiktok", False),
                "follow_facebook": data.get("follow_facebook", False),
                "follow_youtube":  data.get("follow_youtube", False),
                "wallet_activated": bool(data.get("wallet_activated", False)),
                "review_store_done": bool(data.get("review_store_done", False)),
                "review_trust_done": bool(data.get("review_trust_done", False)),
                "usdt_won_month": has_won_this_month(data, "usdt"),
                "pnt_won_month":  has_won_this_month(data, "pnt"),
                "history":        history,
            })

        # ── GET /ranking ──
        elif path == "/stats":
            db = load_db()
            users = [v for v in db.values() if isinstance(v, dict) and "points" in v]

            # Check-ins totales por usuario (contando historial)
            checkin_counts = {}
            for u in users:
                uid = u.get("id", "")
                count = sum(1 for h in u.get("history", []) if h.get("type") == "checkin")
                checkin_counts[uid] = count

            # Top 10 por puntos
            top_pts = sorted(users, key=lambda x: x.get("points", 0), reverse=True)[:10]

            # Top 10 por check-ins
            top_checkins = sorted(users, key=lambda x: checkin_counts.get(x.get("id",""), 0), reverse=True)[:10]

            # Ganadores de USDT y PNT
            usdt_winners = [u for u in users if u.get("usdt_won_month")]
            pnt_winners  = [u for u in users if u.get("pnt_won_month")]

            # Usuarios que giraron la ruleta
            spun = [u for u in users if u.get("spins_used", 0) > 0 or u.get("spins_used_this_event", 0) > 0]

            # Misiones de wallet
            wallet_activated = [u for u in users if u.get("wallet_activated")]
            review_store     = [u for u in users if u.get("review_store_done")]
            review_trust     = [u for u in users if u.get("review_trust_done")]

            # Totales generales
            total_pts = sum(u.get("points", 0) for u in users)
            avg_pts   = round(total_pts / len(users)) if users else 0
            max_streak = max((u.get("streak", 0) for u in users), default=0)

            def fmt(u):
                return {
                    "id":         u.get("id"),
                    "username":   u.get("username") or u.get("first_name", "?"),
                    "points":     u.get("points", 0),
                    "level":      u.get("level", get_level(u.get("points", 0))),
                    "streak":     u.get("streak", 0),
                    "checkins":   checkin_counts.get(u.get("id",""), 0),
                }

            return self.send_json({
                "resumen": {
                    "total_usuarios":      len(users),
                    "total_puntos_emitidos": total_pts,
                    "promedio_puntos":     avg_pts,
                    "racha_maxima":        max_streak,
                    "giraron_ruleta":      len(spun),
                    "wallet_activadas":    len(wallet_activated),
                    "reviews_store":       len(review_store),
                    "reviews_trustpilot":  len(review_trust),
                    "ganadores_usdt":      len(usdt_winners),
                    "ganadores_pnt":       len(pnt_winners),
                },
                "top10_puntos":   [fmt(u) for u in top_pts],
                "top10_checkins": [fmt(u) for u in top_checkins],
                "ganadores_usdt": [fmt(u) for u in usdt_winners],
                "ganadores_pnt":  [fmt(u) for u in pnt_winners],
                "wallet_activadas": [{"id": u.get("id"), "username": u.get("username") or u.get("first_name","?")} for u in wallet_activated],
            })

        elif path == "/ranking":
            db    = load_db()
            valid = [u for u in db.values() if isinstance(u, dict) and "points" in u]
            top20 = sorted(valid, key=lambda x: x["points"], reverse=True)[:20]
            return self.send_json([
                {
                    "pos":        i + 1,
                    "id":         u.get("id", ""),
                    "username":   u.get("username", ""),
                    "first_name": u.get("first_name", ""),
                    "points":     u.get("points", 0),
                    "level":      get_level(u.get("points", 0)),
                }
                for i, u in enumerate(top20)
            ])

        # ── GET /evento?id=123456 ──
        elif path == "/evento":
            uid = params.get("id", [None])[0]
            db  = load_db()
            ev  = get_evento_state()

            cazadores_total = get_cazadores_count()
            top5 = get_top_cazadores(5)

            user_data = db.get(uid, {}) if uid else {}
            mis_cazadores = user_data.get("cazadores_evento", 0)
            mi_pnt_estimado = 0
            if mis_cazadores >= 3:
                total_refs = sum(d.get("cazadores_evento", 0) for u2, d in db.items()
                                 if not u2.startswith("_") and isinstance(d, dict)
                                 and d.get("cazadores_evento", 0) >= 3)
                if total_refs > 0:
                    mi_pnt_estimado = round((mis_cazadores / total_refs) * COFRE_PNT, 4)

            top5_list = []
            for i, (ruid, d) in enumerate(top5, 1):
                top5_list.append({
                    "pos":      i,
                    "nombre":   d.get("username") or d.get("first_name") or ruid,
                    "username": d.get("username") or d.get("first_name") or ruid,
                    "refs":     d.get("referrals_active", 0),
                    "referidos": d.get("referrals_active", 0),
                    "uid":      ruid,
                    "es_yo":    ruid == uid,
                })

            end_date = ev.get("end_date")
            dias_restantes = 0
            if end_date:
                dias_restantes = max(0, (datetime.fromisoformat(end_date) - datetime.now()).days)

            return self.send_json({
                "activo":            ev["activo"],
                "cerrado":           ev["cerrado"],
                "cofre_abierto":     ev["cofre_abierto"],
                "total_cazadores":   cazadores_total,
                "meta":              META_CAZADORES,
                "dias_restantes":    dias_restantes,
                "dias_transcurridos": (datetime.fromisoformat(ev["start_date"]) - datetime.now()).days * -1 if ev.get("start_date") else 0,
                "dias_limite":       EVENTO_DIAS_BASE + ev.get("extension", 0),
                "pct_objetivo":      round(cazadores_total / META_CAZADORES * 100, 1),
                "cofre_pnt":         COFRE_PNT,
                "mis_cazadores":     mis_cazadores,
                "mi_pnt_estimado":   mi_pnt_estimado,
                "top5":              top5_list,
                "evento_pnt_ganado": user_data.get("evento_pnt_ganado", 0),
                "usuario": {
                    "referidos_validos": mis_cazadores,
                    "pnt_estimado":      mi_pnt_estimado,
                    "califica":          mis_cazadores >= 3,
                    "min_referidos":     3,
                    "evento_pnt_ganado": user_data.get("evento_pnt_ganado", 0),
                },
            })

        # ── GET /admin/misiones?key=panther2026 ──
        elif path == "/admin/misiones":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Acceso denegado</h2>")
                return

            db   = load_db()
            now  = datetime.now()
            generado = now.strftime("%d/%m/%Y %H:%M:%S")

            tipo_label = {
                "reel":            "🎬 Reel de Panther",
                "story":           "📸 Historia de Panther",
                "historia":        "📸 Historia de Panther",
                "content":         "✏️ Contenido propio",
                "wallet_activate": "👛 Wallet activada",
                "comment_ig":      "💬 Comentario IG",
                "comment_ig_last": "💬 Comentario IG último post",
                "comment_tt":      "💬 Comentario TikTok",
                "comment_tt_last": "💬 Comentario TikTok último video",
                "checkin":         "🔥 Check-in diario",
                "referral":        "🔗 Referido",
                "referral_wallet": "🔗 Referido con wallet",
                "cazador":         "⚔️ Cazador verificado",
                "follow_ig":       "👁 Follow IG",
                "follow_x":        "👁 Follow X",
                "follow_tiktok":   "👁 Follow TikTok",
                "follow_facebook": "👁 Follow Facebook",
                "follow_youtube":  "👁 Follow YouTube",
                "glosario":        "📖 Glosario",
                "ruleta":          "🎰 Ruleta",
            }

            # Recopilar todas las misiones
            filas = []
            for uid, data in db.items():
                if uid.startswith("_") or not isinstance(data, dict):
                    continue
                nombre = str(data.get("username") or data.get("first_name") or uid).replace("_", " ")
                for h in data.get("history", []):
                    tipo  = h.get("type", "otro")
                    label = tipo_label.get(tipo, tipo)
                    pts   = h.get("pts", 0)
                    fecha = h.get("date", "")
                    hora  = h.get("time", "")
                    estado = "✅ Aprobada"
                    filas.append({
                        "fecha":   fecha,
                        "hora":    hora,
                        "nombre":  nombre,
                        "mision":  label,
                        "pts":     pts,
                        "estado":  estado,
                    })

            # Ordenar por fecha+hora descendente
            filas.sort(key=lambda x: x["fecha"] + x["hora"], reverse=True)

            # Pendientes
            pendientes = len(PENDING_MISSIONS)
            if pendientes == 0:
                banner = "<div class=\'banner\'>✅ Ninguna misión pendiente. Todas analizadas.</div>"
            else:
                banner = f"<div class=\'banner pending\'>⏳ {pendientes} misión(es) pendiente(s) de revisión.</div>"

            def build_rows(filas):
                if not filas:
                    return "<tr><td colspan='5' style='text-align:center;color:#AAA;padding:20px'>Sin misiones registradas</td></tr>"
                out = ""
                for i, row in enumerate(filas):
                    bg = "#FAFAFA" if i % 2 == 0 else "#FFFFFF"
                    pts_color = "#FF5A0E" if row["pts"] > 0 else "#AAA"
                    out += (
                        "<tr style='background:" + bg + "'>"
                        "<td>" + row["fecha"] + " " + row["hora"] + "</td>"
                        "<td><b>" + row["nombre"] + "</b></td>"
                        "<td>" + row["mision"] + "</td>"
                        "<td style='color:" + pts_color + ";font-weight:700'>+" + str(row["pts"]) + " pts</td>"
                        "<td>" + row["estado"] + "</td>"
                        "</tr>"
                    )
                return out

            html = f"""<!DOCTYPE html><html><head><meta charset=\'utf-8\'>
<meta name=\'viewport\' content=\'width=device-width,initial-scale=1\'>
<title>Misiones — Manada Panther</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F5F5F5;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;padding:24px;color:#111}}
h1{{color:#FF5A0E;font-size:22px;font-weight:800;margin-bottom:4px}}
.sub{{color:#AAA;font-size:11px;letter-spacing:2px;text-transform:uppercase;margin-bottom:20px}}
.banner{{background:#F0FDF4;border:1px solid #86efac;border-radius:10px;padding:14px 20px;font-size:14px;font-weight:600;color:#166534;margin-bottom:20px}}
.banner.pending{{background:#FFF7ED;border-color:#fed7aa;color:#9a3412}}
.generado{{font-size:11px;color:#AAA;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;background:#FFF;border-radius:12px;overflow:hidden;border:1px solid #EEE;box-shadow:0 2px 8px rgba(0,0,0,0.04)}}
th{{text-align:left;padding:12px 16px;border-bottom:1px solid #F0F0F0;color:#AAA;font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;background:#FAFAFA}}
td{{padding:10px 16px;border-bottom:1px solid #F7F7F7;font-size:13px;color:#333}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#FFF8F5}}
.total{{margin-top:12px;font-size:12px;color:#AAA;text-align:right}}
</style></head><body>
<h1>MISIONES — MANADA PANTHER</h1>
<div class=\'sub\'>Registro de actividad · Uso interno</div>
<div class=\'generado\'>Documento generado el {generado}</div>
{banner}
<table>
<tr><th>Fecha y hora</th><th>Usuario</th><th>Misión</th><th>Puntos</th><th>Estado</th></tr>
{build_rows(filas)}
</table>
<div class=\'total\'>{len(filas)} misiones registradas</div>
</body></html>"""

            html_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        # ── GET /admin/stats?key=panther2026 ──
        elif path == "/admin/stats":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Acceso denegado</h2>")
                return

            db = load_db()
            users = {uid: d for uid, d in db.items() if not uid.startswith("_") and isinstance(d, dict) and "points" in d}

            total        = len(users)
            con_wallet   = sum(1 for d in users.values() if d.get("wallet_activated"))
            sin_wallet   = total - con_wallet
            por_referido = sum(1 for d in users.values() if d.get("referred_by"))
            directo      = total - por_referido

            # Misiones
            mission_counts = {}
            total_missions = 0
            total_pts_emitidos = 0
            for d in users.values():
                for h in d.get("history", []):
                    t = h.get("type", "otro")
                    mission_counts[t] = mission_counts.get(t, 0) + 1
                    total_missions += 1
                    total_pts_emitidos += h.get("pts", 0)

            checkins   = mission_counts.get("checkin", 0)
            contenido  = mission_counts.get("reel", 0) + mission_counts.get("historia", 0) + mission_counts.get("tiktok", 0)
            sociales   = sum(v for k, v in mission_counts.items() if "follow" in k or "social" in k)
            referidos_m = mission_counts.get("referral", 0) + mission_counts.get("referral_wallet", 0)
            glosario   = mission_counts.get("glosario", 0)
            ruleta_m   = mission_counts.get("ruleta", 0)
            otros      = max(0, total_missions - checkins - contenido - sociales - referidos_m - glosario - ruleta_m)

            # Rachas
            rachas = [d.get("streak", 0) for d in users.values()]
            racha_prom = round(sum(rachas) / len(rachas), 1) if rachas else 0
            racha_max  = max(rachas) if rachas else 0

            # Top pts y misiones
            top_pts = max(users.items(), key=lambda x: x[1].get("points", 0), default=(None, {}))
            top_mis = max(users.items(), key=lambda x: len(x[1].get("history", [])), default=(None, {}))

            # Niveles
            nivel_dist = {}
            for d in users.values():
                lv = get_level(d.get("points", 0))
                nivel_dist[lv] = nivel_dist.get(lv, 0) + 1
            nivel_orden = ["Cachorro","Explorador","Guerrero","Cazador","Alfa","Embajador","Leyenda","Dios"]
            nivel_dist_sorted = [(lv, nivel_dist.get(lv, 0)) for lv in nivel_orden if nivel_dist.get(lv, 0) > 0]

            # Dias activos
            from datetime import date as _date
            launch = _date(2026, 4, 28)
            dias_activos = (_date.today() - launch).days

            # Top referidores y recientes
            top_refs = sorted(users.items(), key=lambda x: len(x[1].get("referrals", [])), reverse=True)[:10]
            recientes = sorted(
                [(uid, d) for uid, d in users.items() if d.get("history")],
                key=lambda x: x[1]["history"][-1].get("date", "") + x[1]["history"][-1].get("time", ""),
                reverse=True
            )[:10]

            pct_wallet = round(con_wallet / total * 100) if total else 0
            pct_ref    = round(por_referido / total * 100) if total else 0
            nombre_top_pts = str(top_pts[1].get("username") or top_pts[1].get("first_name") or top_pts[0]) if top_pts[0] else "—"
            nombre_top_mis = str(top_mis[1].get("username") or top_mis[1].get("first_name") or top_mis[0]) if top_mis[0] else "—"

            def td(val, bold=False, color="#333"):
                s = "font-weight:700" if bold else "font-weight:400"
                return f"<td style='padding:10px 16px;border-bottom:1px solid #F7F7F7;font-size:14px;{s};color:{color}'>{val}</td>"

            def ref_rows():
                out = ""
                for i, (uid, d) in enumerate(top_refs):
                    n = str(d.get("username") or d.get("first_name") or uid)
                    refs = len(d.get("referrals", []))
                    act  = d.get("referrals_active", 0)
                    pts  = d.get("points", 0)
                    out += f"<tr>{td(f'#{i+1}',color='#CCC')}{td(n,True,'#111')}{td(str(refs),True,'#FF5A0E')}{td(str(act),color='#16a34a')}{td(str(pts),color='#666')}</tr>"
                return out or "<tr><td colspan='5' style='padding:14px;color:#CCC;text-align:center'>Sin datos</td></tr>"

            def recent_rows():
                out = ""
                for uid, d in recientes:
                    n = str(d.get("username") or d.get("first_name") or uid)
                    w = "<span style='background:#F0FDF4;color:#16a34a;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600'>✅ Activa</span>" if d.get("wallet_activated") else "<span style='color:#CCC'>—</span>"
                    r = "<span style='background:#FFF3EE;color:#FF5A0E;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600'>Referido</span>" if d.get("referred_by") else "<span style='background:#F5F5F5;color:#999;padding:2px 8px;border-radius:10px;font-size:11px'>Directo</span>"
                    last = d["history"][-1]
                    fecha = f"{last.get('date','')} {last.get('time','')}"
                    out += f"<tr>{td(n,True,'#111')}<td style='padding:10px 16px;border-bottom:1px solid #F7F7F7'>{w}</td><td style='padding:10px 16px;border-bottom:1px solid #F7F7F7'>{r}</td>{td(fecha,color='#999')}</tr>"
                return out or "<tr><td colspan='4' style='padding:14px;color:#CCC;text-align:center'>Sin datos</td></tr>"

            def nivel_rows():
                out = ""
                for lv, count in nivel_dist_sorted:
                    pct = round(count / total * 100) if total else 0
                    bar = f"<div style='background:#F0F0F0;border-radius:4px;height:5px;margin-top:4px'><div style='width:{pct}%;height:5px;border-radius:4px;background:#FF5A0E'></div></div><span style='font-size:11px;color:#AAA'>{pct}%</span>"
                    out += f"<tr>{td(lv,True,'#111')}{td(str(count),True,'#FF5A0E')}<td style='padding:10px 16px;border-bottom:1px solid #F7F7F7;min-width:160px'>{bar}</td></tr>"
                return out or "<tr><td colspan='3' style='padding:14px;color:#CCC;text-align:center'>Sin datos</td></tr>"

            html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Manada Panther Stats</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F5F5F5;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:32px 24px;max-width:960px;margin:0 auto}}
h1{{color:#FF5A0E;font-size:26px;font-weight:800;margin-bottom:4px}}
.sub{{color:#AAA;font-size:11px;letter-spacing:2px;margin-bottom:28px;text-transform:uppercase}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:14px}}
.card{{background:#FFF;border:1px solid #E8E8E8;border-radius:14px;padding:20px 16px;box-shadow:0 2px 8px rgba(0,0,0,0.05)}}
.card-val{{font-size:38px;font-weight:800;color:#FF5A0E;line-height:1}}
.card-val.green{{color:#16a34a}}.card-val.gray{{color:#CCC}}.card-val.dark{{color:#111}}
.card-lbl{{font-size:10px;color:#AAA;letter-spacing:2px;margin-top:6px;text-transform:uppercase}}
.card-sub{{font-size:12px;color:#BBB;margin-top:5px}}
.card-name{{font-size:16px;font-weight:700;color:#111;margin-top:6px}}
h2{{font-size:11px;letter-spacing:2px;color:#AAA;margin-bottom:10px;margin-top:28px;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;background:#FFF;border-radius:12px;overflow:hidden;border:1px solid #EEE;box-shadow:0 2px 8px rgba(0,0,0,0.04);margin-bottom:8px}}
th{{text-align:left;padding:12px 16px;border-bottom:1px solid #F0F0F0;color:#AAA;font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;background:#FAFAFA}}
td{{padding:10px 16px;border-bottom:1px solid #F7F7F7;font-size:14px;color:#333}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#FFF8F5}}
.bar-bg{{background:#F0F0F0;border-radius:4px;height:5px;margin-top:8px}}
.bar-fill{{height:5px;border-radius:4px}}
.mis-row{{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #F7F7F7;font-size:14px;background:#FFF}}
.mis-row:last-child{{border-bottom:none}}
footer{{margin-top:48px;padding-bottom:32px;font-size:11px;color:#CCC;text-align:center;letter-spacing:2px;text-transform:uppercase}}
</style></head><body>
<h1>MANADA PANTHER</h1>
<div class='sub'>Community Dashboard &nbsp;·&nbsp; Panther Wallet &nbsp;·&nbsp; {dias_activos} días activos</div>

<h2>Comunidad</h2>
<div class='grid'>
<div class='card'><div class='card-val'>{total}</div><div class='card-lbl'>Miembros totales</div></div>
<div class='card'><div class='card-val green'>{con_wallet}</div><div class='card-lbl'>Con wallet activa</div><div class='bar-bg'><div class='bar-fill' style='width:{pct_wallet}%;background:#16a34a'></div></div><div class='card-sub'>{pct_wallet}% del total</div></div>
<div class='card'><div class='card-val gray'>{sin_wallet}</div><div class='card-lbl'>Sin wallet aún</div></div>
<div class='card'><div class='card-val'>{por_referido}</div><div class='card-lbl'>Vía referido</div><div class='bar-bg'><div class='bar-fill' style='width:{pct_ref}%;background:#FF5A0E'></div></div><div class='card-sub'>{pct_ref}% del total</div></div>
<div class='card'><div class='card-val gray'>{directo}</div><div class='card-lbl'>Acceso directo</div></div>
</div>

<h2>Actividad & Engagement</h2>
<div class='grid'>
<div class='card'><div class='card-val dark'>{total_missions}</div><div class='card-lbl'>Misiones completadas</div></div>
<div class='card'><div class='card-val'>{total_pts_emitidos:,}</div><div class='card-lbl'>Puntos emitidos</div></div>
<div class='card'><div class='card-val dark'>{checkins}</div><div class='card-lbl'>Check-ins totales</div></div>
<div class='card'><div class='card-val dark'>{racha_prom}</div><div class='card-lbl'>Racha promedio</div><div class='card-sub'>Máx: {racha_max} días</div></div>
<div class='card'><div class='card-val dark'>{ruleta_m}</div><div class='card-lbl'>Giros de ruleta</div></div>
</div>

<h2>Misiones por tipo</h2>
<div style='background:#FFF;border-radius:12px;border:1px solid #EEE;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04)'>
<div class='mis-row'><span>🔥 Check-in diario</span><strong style='color:#FF5A0E'>{checkins}</strong></div>
<div class='mis-row'><span>📱 Contenido (reels, historias, TikTok)</span><strong style='color:#FF5A0E'>{contenido}</strong></div>
<div class='mis-row'><span>👥 Sociales (follows)</span><strong style='color:#FF5A0E'>{sociales}</strong></div>
<div class='mis-row'><span>🔗 Referidos</span><strong style='color:#FF5A0E'>{referidos_m}</strong></div>
<div class='mis-row'><span>📖 Glosario crypto</span><strong style='color:#FF5A0E'>{glosario}</strong></div>
<div class='mis-row'><span>🎰 Ruleta</span><strong style='color:#FF5A0E'>{ruleta_m}</strong></div>
<div class='mis-row' style='border-bottom:none'><span style='color:#AAA'>Otros</span><strong style='color:#CCC'>{otros}</strong></div>
</div>

<h2>Usuarios destacados</h2>
<div class='grid'>
<div class='card'><div class='card-lbl'>Mayor puntaje</div><div class='card-name'>{nombre_top_pts}</div><div class='card-sub'>{top_pts[1].get("points",0):,} pts</div></div>
<div class='card'><div class='card-lbl'>Más misiones completadas</div><div class='card-name'>{nombre_top_mis}</div><div class='card-sub'>{len(top_mis[1].get("history",[]))} misiones</div></div>
</div>

<h2>Distribución de niveles</h2>
<table><tr><th>Nivel</th><th>Usuarios</th><th>Distribución</th></tr>{nivel_rows()}</table>

<h2>Top Referidores</h2>
<table><tr><th>#</th><th>Usuario</th><th>Referidos</th><th>Con Wallet</th><th>Puntos</th></tr>{ref_rows()}</table>

<h2>Actividad Reciente</h2>
<table><tr><th>Usuario</th><th>Wallet</th><th>Origen</th><th>Última acción</th></tr>{recent_rows()}</table>

<footer>Manada Panther &nbsp;·&nbsp; Pegando La Vuelta &nbsp;·&nbsp; go.mypanther.io</footer>
</body></html>"""

            html_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        elif path == "/admin/ganadores":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Acceso denegado</h2>")
                return

            db = load_db()
            usdt_winners = []
            pnt_winners  = []
            total_spins  = 0
            all_spins    = []

            for uid, data in db.items():
                if uid.startswith("_") or not isinstance(data, dict):
                    continue
                nombre = str(data.get("username") or data.get("first_name") or uid)
                history = data.get("history", [])
                for h in history:
                    if h.get("type") != "ruleta":
                        continue
                    if h.get("date") == "2026-05-15":
                        total_spins += 1
                        all_spins.append({
                            "nombre": nombre,
                            "uid": uid,
                            "hora": h.get("time", "??:??"),
                            "pts": h.get("pts", 0),
                            "prize": h.get("prize") or "pts"
                        })
                    prize = (h.get("prize") or "").upper()
                    if prize == "USDT" and h.get("date") == "2026-05-15":
                        usdt_winners.append({"nombre": nombre, "uid": uid, "hora": h.get("time", "??:??"), "monto": h.get("prize_amount") or "?"})
                    elif prize == "PNT" and h.get("date") == "2026-05-15":
                        pnt_winners.append({"nombre": nombre, "uid": uid, "hora": h.get("time", "??:??"), "monto": h.get("prize_amount") or "?"})
                # Fallback flags
                if data.get("usdt_won_month") and not any(w["uid"] == uid for w in usdt_winners):
                    usdt_winners.append({"nombre": nombre, "uid": uid, "hora": "desconocida", "monto": "?"})
                if data.get("pnt_won_month") and not any(w["uid"] == uid for w in pnt_winners):
                    pnt_winners.append({"nombre": nombre, "uid": uid, "hora": "desconocida", "monto": "?"})

            def rows(items, cols=["nombre", "uid", "hora"]):
                if not items:
                    return "<tr><td colspan='3' style='color:#888;text-align:center'>Ninguno registrado</td></tr>"
                out = ""
                for r in items:
                    cells = "".join(f"<td style='padding:6px 12px;border-bottom:1px solid #1e1e1e'>{str(r.get(c, '-'))}</td>" for c in cols)
                    out += f"<tr>{cells}</tr>"
                return out

            def spin_rows(items):
                if not items:
                    return "<tr><td colspan='5' style='color:#888;text-align:center'>Sin giros registrados</td></tr>"
                return "".join(
                    f"<tr><td>{r['nombre']}</td><td>{r['uid']}</td><td>{r['hora']}</td><td>{r['pts']}</td><td>{r['prize']}</td></tr>"
                    for r in sorted(items, key=lambda x: x['hora'])
                )

            th = "<th style='text-align:left;padding:8px 12px;border-bottom:1px solid #333'>%s</th>"
            td_style = "style='padding:6px 12px;border-bottom:1px solid #222'"

            html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
            <title>Ganadores Ruleta 15/05</title>
            <style>
              body{{background:#0a0a0a;color:#eee;font-family:sans-serif;padding:24px;}}
              h1{{color:#ff6b1a}}h2{{color:#aaa;font-size:16px;margin-top:28px}}
              table{{border-collapse:collapse;width:100%;max-width:700px;margin-bottom:24px}}
              th{{background:#1a1a1a;color:#ff6b1a;padding:8px 12px;text-align:left;border-bottom:1px solid #333}}
              td{{padding:6px 12px;border-bottom:1px solid #1e1e1e;font-size:14px}}
              .badge{{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700}}
              .usdt{{background:#1a3a1a;color:#4ade80}}.pnt{{background:#1a0a2a;color:#cc88ff}}
              .stat{{font-size:28px;font-weight:700;color:#ff6b1a}}
            </style></head><body>
            <h1>🎰 Ruleta — 15 de mayo 2026</h1>
            <p>Total de giros registrados ese día: <span class='stat'>{total_spins}</span></p>

            <h2>💵 USDT — {len(usdt_winners)} ganador(es)</h2>
            <table><tr>{th%'Usuario'}{th%'ID'}{th%'Hora'}{th%'Monto'}</tr>{rows(usdt_winners, ['nombre','uid','hora','monto'])}</table>

            <h2>🐾 PNT — {len(pnt_winners)} ganador(es)</h2>
            <table><tr>{th%'Usuario'}{th%'ID'}{th%'Hora'}{th%'Monto'}</tr>{rows(pnt_winners, ['nombre','uid','hora','monto'])}</table>

            <h2>📋 Todos los giros del 15 mayo</h2>
            <table><tr>{th%'Usuario'}{th%'ID'}{th%'Hora'}{th%'Pts'}{th%'Premio'}</tr>{spin_rows(all_spins)}</table>
            </body></html>"""

            html_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        # ── GET /missions?id=123456 ──
        elif path == "/ruleta":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = get_user(db, uid)
            today = date.today().isoformat()

            # Check ruleta availability (only days 15 and 30)
            if not is_ruleta_active():
                next_day = 15 if date.today().day < 15 else 30
                return self.send_json({
                    "available": False,
                    "reason": "dates",
                    "message": "La ruleta se habilita el dia 15 o 30 del mes",
                    "next_day": next_day
                })

            # Check access conditions
            if not can_access_ruleta(data):
                streak = data.get("streak", 0)
                missing = [f"racha de 3 días (tenés {streak})"] if streak < 3 else []
                return self.send_json({
                    "available": False,
                    "reason": "missions",
                    "message": f"Necesitás 3 días de check-in seguidos para girar (racha actual: {streak})",
                    "missing": missing
                })

            # Check spins available
            spins_used = data.get("spins_used_this_event", 0)
            spins_available = get_available_spins(data)
            if spins_used >= spins_available:
                return self.send_json({"already_done": True, "points": data["points"]})

            result_label, pts_gain, special, _ = spin_ruleta()
            data["last_ruleta"] = today
            data["spins_used_this_event"] = spins_used + 1

            prize_type = None
            prize_amount = None

            if special == "x2":
                until = datetime.now() + timedelta(hours=24)
                data["double_pts_until"] = until.isoformat()
                prize_type = "x2"
                prize_amount = "x2"

            elif special == "usdt":
                if has_won_this_month(data, "usdt"):
                    pts_gain = 50
                    result_label = f"🎰 USDT → +{pts_gain} pts"
                    special = None
                else:
                    mark_won_month(data, "usdt")
                    prize_type = "USDT"
                    prize_amount = get_usdt_prize()
                    if not prize_amount:
                        prize_amount = "$5"

            elif special == "pnt":
                if has_won_this_month(data, "pnt"):
                    pts_gain = 30
                    result_label = f"🎰 PNT → +{pts_gain} pts"
                    special = None
                else:
                    mark_won_month(data, "pnt")
                    prize_type = "PNT"
                    prize_amount = get_pnt_prize()
                    if not prize_amount:
                        prize_amount = 50
                    prize_amount = str(prize_amount)  # siempre string

            earned = add_points(data, pts_gain)

            if "history" not in data:
                data["history"] = []
            data["history"].append({
                "type": "ruleta",
                "pts": earned,
                "date": today,
                "time": datetime.now().strftime("%H:%M"),
                "prize": prize_type,
                "prize_amount": prize_amount
            })

            db[uid] = data
            save_db(db)

            # Notify mods if economic prize
            if prize_type and CombinedHandler.tg_app:
                username = data.get("username") or data.get("first_name") or uid
                from datetime import datetime as dt
                now_str = dt.now().strftime("%d/%m/%Y %H:%M")
                msg = (
                    f"🎰 *PREMIO DE RULETA*\n\n"
                    f"👤 Usuario: @{username} (ID: `{uid}`)\n"
                    f"🏆 Premio: *{prize_amount} {prize_type}*\n"
                    f"⭐ Puntos actuales: *{data['points']}*\n"
                    f"📅 Fecha/Hora: {now_str}\n\n"
                    f"⚠️ _El usuario debe enviar captura de pantalla al chat para verificar. Plazo de entrega: 5 dias habiles._"
                )
                asyncio.run_coroutine_threadsafe(
                    notify_mods(CombinedHandler.tg_app, msg),
                    CombinedHandler.tg_loop
                )

            return self.send_json({
                "status": "ok",
                "result": result_label,
                "pts_gained": earned,
                "points": data["points"],
                "prize_type": prize_type,
                "prize_amount": prize_amount,
                "already_done": False
            })

        elif path == "/follow":
            uid = body.get("id")
            red = body.get("red")
            if not uid or red not in ["ig", "x", "tiktok", "facebook", "youtube"]:
                return self.send_json({"error": "Invalid params"}, 400)

            db   = load_db()
            data = get_user(db, uid)

            field = f"follow_{red}"
            if data.get(field):
                return self.send_json({"already_done": True, "points": data["points"]})

            earned = add_points(data, PTS[field])
            data[field] = True

            bonus = 0
            if (data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok") 
                and data.get("follow_facebook") and data.get("follow_youtube") 
                and not data.get("follow_all_bonus")):
                bonus = add_points(data, PTS["follow_all_bonus"])
                data["follow_all_bonus"] = True

            db[uid] = data
            save_db(db)

            return self.send_json({
                "status": "ok",
                "earned": earned,
                "bonus": bonus,
                "points": data["points"]
            })

        elif path == "/missions":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = db.get(uid, {})
            today = date.today().isoformat()

            return self.send_json({
                "checkin_done":  data.get("last_checkin") == today,
                "ruleta_done":   data.get("last_ruleta") == today,
                "completed":     sum([
                    data.get("last_checkin") == today,
                    data.get("last_ruleta") == today,
                ]),
                "total": 5,
            })

        elif path == "/app":
            try:
                with open("Manada Panther .html", "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(html.encode())
            except Exception as e:
                self.send_json({"error": f"App not found: {str(e)}"}, 404)

        elif path == "/music":
            try:
                with open("music.mp3", "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_json({"error": f"Music not found: {str(e)}"}, 404)
        elif path == "/debug":
            import os
            db_exists = os.path.exists(DB_FILE)
            db_size = os.path.getsize(DB_FILE) if db_exists else 0
            db = load_db()
            self.send_json({
                "db_file": DB_FILE,
                "db_exists": db_exists,
                "db_size": db_size,
                "user_count": len(db),
                "users": list(db.keys()),
            })
        # ── GET /fix_referrals?referrer=ID&refs=ID1,ID2,ID3 (admin only) ──
        elif path == "/fix_referrals":
            referrer_id = params.get("referrer", [None])[0]
            ref_ids_raw = params.get("refs", [""])[0]
            secret = params.get("secret", [""])[0]
            if secret != "panther_admin_2024":
                return self.send_json({"error": "Unauthorized"}, 403)
            if not referrer_id or not ref_ids_raw:
                return self.send_json({"error": "Missing params"}, 400)
            ref_ids = [r.strip() for r in ref_ids_raw.split(",") if r.strip()]
            db = load_db()
            if referrer_id not in db:
                return self.send_json({"error": "Referrer not found"}, 404)
            referrer_data = db[referrer_id]
            if not isinstance(referrer_data.get("referrals"), list):
                referrer_data["referrals"] = []
            added = []
            skipped = []
            pts_added = 0
            for rid in ref_ids:
                if rid not in db:
                    skipped.append(f"{rid} (not found)")
                    continue
                if rid in referrer_data["referrals"]:
                    skipped.append(f"{rid} (already)")
                    continue
                referrer_data["referrals"].append(rid)
                db[rid]["referred_by"] = referrer_id
                pts = add_points(referrer_data, PTS["referral_join"])
                pts_added += pts
                added.append(rid)
            db[referrer_id] = referrer_data
            save_db(db)
            return self.send_json({
                "status": "ok",
                "added": added,
                "skipped": skipped,
                "pts_added": pts_added,
                "referrer_points_now": referrer_data["points"],
                "referrer_referrals_now": len(referrer_data["referrals"]),
            })

        else:
            self.send_json({"status": "Panther Mini App API", "version": "1.0"})

    def do_POST(self):
        parsed  = urlparse(self.path)
        path    = parsed.path
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        # ── POST /checkin ──
        if path == "/checkin":
            uid = body.get("id")
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = get_user(db, uid)
            today     = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            last      = data.get("last_checkin")

            if last == today:
                return self.send_json({"already_done": True, "points": data["points"]})

            if last == yesterday:
                data["streak"] += 1
            else:
                data["streak"] = 1

            streak   = data["streak"]
            base_pts = PTS["checkin_1_3"] if streak <= 3 else PTS["checkin_4_6"]
            bonus    = 0
            if streak == 7:   bonus = PTS["streak_7"]
            elif streak == 14: bonus = PTS["streak_14"]
            elif streak == 30: bonus = PTS["streak_30"]

            old_pts = data["points"]
            earned  = add_points(data, base_pts + bonus)
            data["last_checkin"] = today

            # Log historial
            if "history" not in data:
                data["history"] = []
            data["history"].append({
                "type": "checkin",
                "pts":  earned,
                "date": today,
                "time": datetime.now().strftime("%H:%M"),
            })
            data["history"] = data["history"][-20:]  # Mantener últimos 20

            old_lv = get_level(old_pts)
            new_lv = get_level(data["points"])
            save_db(db)

            return self.send_json({
                "success":    True,
                "earned":     earned,
                "points":     data["points"],
                "streak":     streak,
                "level":      new_lv,
                "level_up":   old_lv != new_lv,
                "bonus":      bonus,
            })

        # ── POST /set_mission_type — guarda qué misión va a subir el usuario ──
        elif path == "/set_mission_type":
            uid = body.get("id")
            mission_type = body.get("type")
            logger.info(f"set_mission_type: uid={uid} type={mission_type}")
            if not uid or mission_type not in ["reel", "story", "content", "wallet_activate", "review_store", "review_trust", "comment_ig", "comment_ig_last", "comment_tt", "comment_tt_last"]:
                logger.warning(f"set_mission_type INVALID: uid={uid} type={mission_type}")
                return self.send_json({"error": "Invalid params"}, 400)
            PENDING_MISSIONS[uid] = mission_type
            save_pending_missions()
            logger.info(f"set_mission_type OK: uid={uid} type={mission_type}")
            return self.send_json({"status": "ok", "type": mission_type})

        # ── POST /follow ──
        elif path == "/follow":
            uid = body.get("id")
            red = body.get("red")
            if not uid or red not in ["ig", "x", "tiktok", "facebook", "youtube"]:
                return self.send_json({"error": "Invalid params"}, 400)

            db   = load_db()
            data = get_user(db, uid)

            field = f"follow_{red}"
            if data.get(field):
                return self.send_json({"already_done": True, "points": data["points"]})

            earned = add_points(data, PTS[field])
            data[field] = True

            # Log historial
            if "history" not in data:
                data["history"] = []
            data["history"].append({
                "type":  f"follow_{red}",
                "pts":   earned,
                "date":  date.today().isoformat(),
                "time":  datetime.now().strftime("%H:%M"),
            })
            data["history"] = data["history"][-20:]

            bonus = 0
            if (data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok")
                    and data.get("follow_facebook") and data.get("follow_youtube")
                    and not data.get("follow_all_bonus")):
                bonus = add_points(data, PTS["follow_all_bonus"])
                data["follow_all_bonus"] = True

            db[uid] = data
            save_db(db)

            return self.send_json({
                "status": "ok",
                "earned": earned,
                "bonus":  bonus,
                "points": data["points"],
            })

        else:
            self.send_json({"error": "Not found"}, 404)

def run_http_server():
    """Corre el servidor HTTP en un thread separado"""
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), MiniAppHandler)
    logger.info(f"🌐 API HTTP corriendo en puerto {port}")
    server.serve_forever()

class CombinedHandler(MiniAppHandler):
    """Handler that serves both API and passes Telegram updates to the app"""
    tg_app = None
    tg_loop = None

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        token_path = f"/webhook/{TOKEN}"

        if path == token_path:
            # Telegram webhook update
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            if CombinedHandler.tg_app and CombinedHandler.tg_loop:
                try:
                    update = Update.de_json(json.loads(body), CombinedHandler.tg_app.bot)
                    asyncio.run_coroutine_threadsafe(
                        CombinedHandler.tg_app.process_update(update),
                        CombinedHandler.tg_loop
                    )
                except Exception as e:
                    logger.error(f"Error procesando update: {e}")
        else:
            super().do_POST()


# ══════════════════════════════════════════════════════════════════════════════
# ── Main ──────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not TOKEN:
        print("❌ Falta BOT_TOKEN en las variables de entorno")
        return

    # Descargar fuentes y inicializar SQLite
    download_fonts()
    init_db()
    load_pending_missions()
    print("✅ Base de datos SQLite inicializada")

    # Test escritura en volumen
    db_dir = os.path.dirname(DB_FILE)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
            print(f"✅ Directorio {db_dir} creado")
        except Exception as e:
            print(f"❌ No se pudo crear {db_dir}: {e}")
    try:
        with open(DB_FILE, "a") as f:
            pass
        print(f"✅ DB accesible en {DB_FILE}")
    except Exception as e:
        print(f"❌ No se puede escribir en {DB_FILE}: {e}")

    from telegram.ext import JobQueue
    app = Application.builder().token(TOKEN).build()

    # Scheduler del evento con asyncio (sin job-queue extra)
    async def evento_scheduler():
        while True:
            await asyncio.sleep(86400)  # cada 24 horas
            try:
                await check_evento_dia(app)
            except Exception as e:
                logger.warning(f"Error en evento scheduler: {e}")

    asyncio.get_event_loop().create_task(evento_scheduler()) if False else None

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("checkin",    cmd_checkin))
    app.add_handler(CommandHandler("puntos",     cmd_puntos))
    app.add_handler(CommandHandler("ranking",    cmd_ranking))
    app.add_handler(CommandHandler("niveles",    cmd_niveles))
    app.add_handler(CommandHandler("referido",   cmd_referido))
    app.add_handler(CommandHandler("ruleta",     cmd_ruleta))
    app.add_handler(CommandHandler("misiones",   cmd_misiones))
    app.add_handler(CommandHandler("compartir",  cmd_compartir))
    app.add_handler(CommandHandler("ayuda",      cmd_ayuda))
    app.add_handler(CommandHandler("aprobar",    cmd_aprobar))
    app.add_handler(CommandHandler("resetcheck", cmd_resetcheck))
    app.add_handler(CommandHandler("dar_puntos", cmd_dar_puntos))
    app.add_handler(CommandHandler("reset_ruleta",  cmd_reset_ruleta))
    app.add_handler(CommandHandler("ganadores_ruleta", cmd_ganadores_ruleta))
    app.add_handler(CommandHandler("stats_referidos", cmd_stats_referidos))
    app.add_handler(CommandHandler("verificar_cazador", cmd_verificar_cazador))
    app.add_handler(CommandHandler("misiones_recientes", cmd_misiones_recientes))
    app.add_handler(CommandHandler("links_campana",   cmd_links_campana))
    app.add_handler(CommandHandler("evento_start",    cmd_evento_start))
    app.add_handler(CommandHandler("estado_cofre",    cmd_estado_cofre))
    app.add_handler(CommandHandler("cazadores",       cmd_cazadores))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.GROUPS, handle_nuevo_cazador))
    app.add_handler(CallbackQueryHandler(handle_cazador_callback, pattern="^cazador_"))
    app.add_handler(CommandHandler("star",          cmd_star))
    app.add_handler(CommandHandler("award",         cmd_award))
    app.add_handler(CommandHandler("leaderboard",    cmd_leaderboard))
    app.add_handler(CommandHandler("mis_estrellas",  cmd_mis_estrellas))
    app.add_handler(CommandHandler("buscar",           cmd_buscar))
    app.add_handler(CommandHandler("recompensa_todos", cmd_recompensa_todos))
    app.add_handler(CommandHandler("pingmods",   cmd_pingmods))
    app.add_handler(CommandHandler("mi_badge",   cmd_mi_badge))
    app.add_handler(CommandHandler("enviar_badges", cmd_enviar_badges))
    app.add_handler(CommandHandler("ruleta_on",  cmd_ruleta_on))
    app.add_handler(CommandHandler("ruleta_off", cmd_ruleta_off))
    app.add_handler(CommandHandler("ruleta_auto", cmd_ruleta_auto))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))

    port = int(os.environ.get("PORT", 8080))

    if WEBHOOK_URL:
        webhook_path = f"/webhook/{TOKEN}"
        full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
        print(f"🐆 Panther Bot iniciando en modo WEBHOOK: {full_webhook_url}")

        # Create event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Initialize telegram app
        async def init_app():
            await app.initialize()
            await app.start()
            # Set webhook
            await app.bot.set_webhook(
                url=full_webhook_url,
                drop_pending_updates=True
            )
            print(f"✅ Webhook registrado: {full_webhook_url}")
            # Lanzar scheduler del evento
            asyncio.create_task(evento_daily_scheduler(app))

        async def evento_daily_scheduler(application):
            while True:
                await asyncio.sleep(86400)
                try:
                    await check_evento_dia(application)
                except Exception as e:
                    logger.warning(f"Error en evento scheduler: {e}")

        loop.run_until_complete(init_app())

        # Store references
        CombinedHandler.tg_app = app
        CombinedHandler.tg_loop = loop

        # Start HTTP server in main thread (Railway needs this to be responsive)
        server = HTTPServer(("0.0.0.0", port), CombinedHandler)
        print(f"🌐 Servidor HTTP corriendo en puerto {port}")

        # Run loop in background thread to process telegram updates
        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        # Serve HTTP in main thread
        server.serve_forever()
    else:
        # POLLING MODE fallback
        print("🐆 Panther Bot iniciando en modo POLLING...")
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
