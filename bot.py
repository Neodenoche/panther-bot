#!/usr/bin/env python3
"""
PANTHER WALLET — MANADA PANTHER GAME BOT
Módulo completo: Bot + API HTTP para Mini App
"""

import os, json, logging, random, asyncio, threading, sqlite3, hashlib, base64, io
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
# from sorteo import *  # ❌ ELIMINADO: sorteo del iPhone 16 (sorteo.py) — se reemplaza por el nuevo sorteo semanal de La Manada

# Webhook configuration
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # e.g. https://panther-bot-production.up.railway.app

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN   = os.environ.get("BOT_TOKEN", "")
DB_FILE  = "/data/panther_db.json"   # JSON legacy (para migración)
SQLITE_FILE = "/data/panther.db"

# ── Perfil — fotos de usuario, guardadas en el volumen persistente ──
AVATAR_DIR = "/data/avatars"
os.makedirs(AVATAR_DIR, exist_ok=True)
AVATAR_MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB antes de decodificar (base64)
NICKNAME_MAX_LEN = 20
BIO_MAX_LEN = 140

# ── Moderadores ───────────────────────────────────────────────────────────────
MOD_IDS = [int(x) for x in os.environ.get("MOD_IDS", "8234467845,8249484524,1769405650,5605380987,1781826630").split(",") if x.strip()]
# Tesoreria — unico(s) ID(s) que pueden confirmar "Ya pague" en un retiro
# (mueve plata real). El resto de los mods sigue pudiendo Rechazar, que
# no mueve nada y solo devuelve el saldo al usuario.
TREASURY_IDS = [int(x) for x in os.environ.get("TREASURY_IDS", "8234467845").split(",") if x.strip()]
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
    "game":      "PNT Defender",
    "game-defender": "PNT Defender",
}
PENDING_MISSIONS: dict = {}  # uid -> tipo de misión pendiente de subir

# ── Comandos que solo funcionan en privado ────────────────────────────────────
PRIVATE_ONLY_COMMANDS = {
    "start", "checkin", "puntos", "referido", "ruleta",
    "misiones", "compartir", "ayuda",
}

async def redirect_to_private(update: Update) -> bool:
    """
    Si el mensaje viene de un grupo y es un comando privado,
    borra el mensaje del usuario, manda un reply corto en el grupo
    y retorna True para que el handler no continúe.
    """
    if update.effective_chat.type not in ("group", "supergroup"):
        return False

    user = update.effective_user
    bot_username = (await update.get_bot().get_me()).username
    url = f"https://t.me/{bot_username}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🐆 Abrir bot en privado", url=url)
    ]])

    # Intentar borrar el mensaje del comando para no ensuciar el grupo
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        sent = await update.effective_chat.send_message(
            f"👋 {user.mention_html()}, los comandos funcionan en privado 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        # Auto-borrar el aviso después de 8 segundos
        asyncio.get_event_loop().call_later(
            8, lambda: asyncio.create_task(_delete_msg(sent))
        )
    except Exception:
        pass

    return True

async def _delete_msg(msg):
    try:
        await msg.delete()
    except Exception:
        pass

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

# ── Antiflood ─────────────────────────────────────────────────────────────────
FLOOD_TRACKER: dict = {}    # uid -> list of timestamps
FLOOD_MUTED: dict = {}      # uid -> datetime de fin de mute

FLOOD_MAX_MSGS  = 5         # máx mensajes permitidos...
FLOOD_WINDOW    = 8         # ...en X segundos
FLOOD_MUTE_SECS = 300       # duración del mute automático (5 minutos)

async def antiflood_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detecta flood: si un usuario manda más de FLOOD_MAX_MSGS mensajes
    en FLOOD_WINDOW segundos, lo mutea por FLOOD_MUTE_SECS segundos.
    Solo actúa en grupos. Ignora a mods y admins.
    """
    if not update.message or update.effective_chat.type not in ("group", "supergroup"):
        return

    user = update.effective_user
    if not user or user.id in MOD_IDS:
        return

    uid = user.id
    now = datetime.now()

    # Si ya está muteado y el mute sigue vigente, borrar el mensaje
    if uid in FLOOD_MUTED:
        if now < FLOOD_MUTED[uid]:
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        else:
            del FLOOD_MUTED[uid]

    # Registrar timestamp actual
    timestamps = FLOOD_TRACKER.get(uid, [])
    timestamps.append(now)

    # Limpiar timestamps fuera de la ventana
    cutoff = now - timedelta(seconds=FLOOD_WINDOW)
    timestamps = [t for t in timestamps if t > cutoff]
    FLOOD_TRACKER[uid] = timestamps

    # Evaluar si superó el límite
    if len(timestamps) >= FLOOD_MAX_MSGS:
        FLOOD_MUTED[uid] = now + timedelta(seconds=FLOOD_MUTE_SECS)
        FLOOD_TRACKER[uid] = []

        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=uid,
                permissions={"can_send_messages": False},
                until_date=int(FLOOD_MUTED[uid].timestamp()),
            )
        except Exception as e:
            logger.warning(f"Antiflood: no se pudo mutear a {uid}: {e}")

        try:
            await update.message.delete()
        except Exception:
            pass

        nombre = user.first_name or "Usuario"
        aviso = await update.effective_chat.send_message(
            f"⚠️ {nombre} fue muteado 5 minutos por flood. Respira y vuelve con buena energía 🐆"
        )
        # Auto-borrar aviso después de 10 segundos
        asyncio.get_event_loop().call_later(
            10, lambda: asyncio.create_task(_delete_msg(aviso))
        )

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
    "checkin_1_3":          5,
    "checkin_4_6":         10,
    "streak_7":            50,
    "streak_14":          150,
    "streak_30":          500,
    "referral_join":       25,
    "referral_wallet":    150,
    "share_reel":          30,
    "follow_ig":           15,
    "follow_x":            15,
    "follow_tiktok":       15,
    "follow_facebook":     15,
    "follow_youtube":      15,
    "follow_all_bonus":    20,
    "share_story":         20,
    "own_content":         40,
    "wallet_activate":    175,
    "review_store":       175,
    "review_trust":       175,
    "follow_emb_emi":      35,
    "follow_emb_lorena":   35,
    "story_mention":       20,
    "first_deposit":      100,
    "emoji_tg":            20,
}

DAILY_LIMIT_MISSIONS = {
    "share_reel", "share_story", "own_content",
    "comment_ig", "comment_ig_last", "comment_tt", "comment_tt_last",
    "story_mention",
}
DAILY_LIMIT = 3

# Create & Earn (own_content) acredita USDT real ademas de puntos: se limita
# a 1 por dia para que valga la pena cuidar la calidad en vez de spamear.
DAILY_LIMIT_OVERRIDE = {
    "own_content": 1,
}

def get_daily_limit(mission_type: str) -> int:
    return DAILY_LIMIT_OVERRIDE.get(mission_type, DAILY_LIMIT)

ONCE_MISSIONS = {
    "wallet_activate", "review_store", "review_trust",
    "follow_emb_emi", "follow_emb_lorena",
    "first_deposit", "emoji_tg",
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

# ── Integración Milton / Mundial — ❌ ELIMINADA (el Mundial ya pasó, nunca se activó) ──

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_scores (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  TEXT,
                nick     TEXT NOT NULL,
                score    INTEGER NOT NULL,
                ts       INTEGER NOT NULL
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
        ("cazadores_evento",    "INTEGER DEFAULT 0"),
        ("cazador_verificado",  "INTEGER DEFAULT 0"),
        ("source",              "TEXT DEFAULT 'directo'"),
        ("evento_pnt_ganado",   "REAL DEFAULT 0"),
        ("panther_uid",         "TEXT DEFAULT ''"),
        ("last_game",           "TEXT"),
        ("comment_ig_count",    "INTEGER DEFAULT 0"),
        ("comment_tt_count",    "INTEGER DEFAULT 0"),
        ("story_mention_count", "INTEGER DEFAULT 0"),
        ("follow_emb_emi",      "INTEGER DEFAULT 0"),
        ("follow_emb_lorena",   "INTEGER DEFAULT 0"),
        ("first_deposit_done",  "INTEGER DEFAULT 0"),
        ("emoji_tg_done",       "INTEGER DEFAULT 0"),
        # ── La Manada v2 — saldo separado del XP/puntos ──
        ("manada_usdt_balance",     "REAL DEFAULT 0"),
        ("manada_pnt_balance",      "REAL DEFAULT 0"),
        ("manada_usdt_month",       "REAL DEFAULT 0"),
        ("manada_pnt_month",        "REAL DEFAULT 0"),
        ("manada_month_ref",        "TEXT DEFAULT ''"),
        ("manada_week_ref",         "TEXT DEFAULT ''"),
        ("manada_checkins_semana",  "INTEGER DEFAULT 0"),
        ("manada_quiz_semana",      "INTEGER DEFAULT 0"),
        ("manada_last_quiz_date",   "TEXT"),
        ("manada_retiro_pendiente", "INTEGER DEFAULT 0"),
        ("manada_stake_semana",     "INTEGER DEFAULT 0"),
        # Weekly Hunt: snapshot de la semana recien terminada, tomado justo
        # antes de resetear los contadores en curso (ver
        # manada_reset_periods_if_needed). Sin esto, si un usuario hace
        # check-in apenas empieza la semana nueva, se perderian sus
        # totales de la semana anterior antes de que un mod pueda sortear.
        ("manada_last_week_ref",       "TEXT DEFAULT ''"),
        ("manada_last_week_checkins",  "INTEGER DEFAULT 0"),
        ("manada_last_week_quiz",      "INTEGER DEFAULT 0"),
        # Intro de bienvenida a La Manada v2 (tarjeta estilo terminal que
        # explica las misiones nuevas) — se muestra una sola vez.
        ("seen_intro_v2",              "INTEGER DEFAULT 0"),
        # Perfil — apodo, bio y version de la foto (para invalidar cache al
        # subir una nueva). La foto en si se guarda en disco (AVATAR_DIR),
        # no en la base de datos.
        ("nickname",                "TEXT DEFAULT ''"),
        ("bio",                     "TEXT DEFAULT ''"),
        ("avatar_version",          "INTEGER DEFAULT 0"),
        # Retiro de saldo La Manada — manada_retiro_pendiente ya existia
        # como columna (sin usar); agregamos el monto congelado al momento
        # de pedir el retiro para poder devolverlo si un mod lo rechaza.
        ("manada_retiro_usdt",      "REAL DEFAULT 0"),
        ("manada_retiro_pnt",       "REAL DEFAULT 0"),
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
# init_sorteo_db()  # ❌ ELIMINADO junto con el sorteo del iPhone
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
                  "big_transaction", "wallet_activated", "pending_wallet_proof",
                  "cazador_verificado"):
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
                    reel_count_today, story_count_today, content_count_today, last_mission_date,
                    cazadores_evento, cazador_verificado, source, evento_pnt_ganado, panther_uid,
                    manada_usdt_balance, manada_pnt_balance, manada_usdt_month, manada_pnt_month,
                    manada_month_ref, manada_week_ref, manada_checkins_semana, manada_quiz_semana,
                    manada_last_quiz_date, manada_retiro_pendiente, manada_stake_semana,
                    manada_last_week_ref, manada_last_week_checkins, manada_last_week_quiz,
                    seen_intro_v2, nickname, bio, avatar_version, manada_retiro_usdt, manada_retiro_pnt,
                    history)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    data.get("cazadores_evento", 0),
                    int(data.get("cazador_verificado", False)),
                    data.get("source", "directo"),
                    data.get("evento_pnt_ganado", 0),
                    data.get("panther_uid", ""),
                    data.get("manada_usdt_balance", 0),
                    data.get("manada_pnt_balance", 0),
                    data.get("manada_usdt_month", 0),
                    data.get("manada_pnt_month", 0),
                    data.get("manada_month_ref", ""),
                    data.get("manada_week_ref", ""),
                    data.get("manada_checkins_semana", 0),
                    data.get("manada_quiz_semana", 0),
                    data.get("manada_last_quiz_date"),
                    int(data.get("manada_retiro_pendiente", False)),
                    data.get("manada_stake_semana", 0),
                    data.get("manada_last_week_ref", ""),
                    data.get("manada_last_week_checkins", 0),
                    data.get("manada_last_week_quiz", 0),
                    int(data.get("seen_intro_v2", False)),
                    data.get("nickname", ""),
                    data.get("bio", ""),
                    data.get("avatar_version", 0),
                    data.get("manada_retiro_usdt", 0),
                    data.get("manada_retiro_pnt", 0),
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

async def notify_retiro_request(app, uid: str, nombre: str, panther_uid: str, usdt: float, pnt: float):
    """Avisa a los mods que un usuario pidio retirar su saldo de La Manada,
    con botones para marcarlo pagado o rechazarlo (devuelve el saldo).
    Incluye el UID de Panther Wallet porque tesoreria lo necesita para
    poder mandar el pago — sin esto no se puede procesar."""
    texto = (
        f"💸 *Solicitud de retiro*\n\n"
        f"Usuario: {nombre} (ID: {uid})\n"
        f"UID Panther Wallet: `{panther_uid}`\n"
        f"Monto: *{usdt} USDT* + *{pnt} PNT*\n\n"
        f"Sin monto maximo definido por ahora — el limite real es el tope "
        f"de {MANADA_MONTHLY_CAP_USDT} USDT que puede ganar por mes.\n\n"
        f"Solo tesorería puede confirmar el pago."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya pague", callback_data=f"retiroOk_{uid}")],
        [InlineKeyboardButton("❌ Rechazar (devolver saldo)", callback_data=f"retiroNo_{uid}")],
    ])
    notified = False
    try:
        await app.bot.send_message(
            chat_id=MOD_GROUP_ID,
            text=texto,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        notified = True
    except Exception as e:
        logger.error(f"Error notificando retiro al grupo de mods: {e}")
    if not notified:
        for mod_id in MOD_IDS:
            try:
                await app.bot.send_message(
                    chat_id=mod_id,
                    text=texto,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Error notificando retiro a mod {mod_id}: {e}")


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


# ═══════════════════════════════════════════════════════════════════════════
# LA MANADA v2 — saldo de USDT/PNT separado del XP (points).
# Reglas: tope de 10 USDT acreditados por mes por usuario. El PNT no tiene
# tope propio por ahora. El retiro (mínimo 5 USDT) se pide aparte y lo
# aprueba un mod a mano (ver /retiro y /aprobar_retiro).
# ═══════════════════════════════════════════════════════════════════════════
MANADA_MONTHLY_CAP_USDT = 10.0
MANADA_MIN_RETIRO_USDT  = 5.0


def _manada_month_ref() -> str:
    return date.today().strftime("%Y-%m")


def _manada_week_ref() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def manada_reset_periods_if_needed(data: dict):
    """Resetea los contadores mensuales/semanales de La Manada si cambió el período.
    Llamar siempre antes de leer o sumar cualquier campo manada_*."""
    mref = _manada_month_ref()
    if data.get("manada_month_ref") != mref:
        data["manada_month_ref"]  = mref
        data["manada_usdt_month"] = 0
        data["manada_pnt_month"]  = 0

    wref = _manada_week_ref()
    if data.get("manada_week_ref") != wref:
        # Weekly Hunt: guardar snapshot de la semana que termina antes de
        # resetear los contadores, para que el sorteo semanal (que un mod
        # dispara a mano, no necesariamente justo al cambiar de semana)
        # todavia pueda leer los totales reales de esa semana.
        data["manada_last_week_ref"]      = data.get("manada_week_ref", "")
        data["manada_last_week_checkins"] = data.get("manada_checkins_semana", 0) or 0
        data["manada_last_week_quiz"]     = data.get("manada_quiz_semana", 0) or 0

        data["manada_week_ref"]        = wref
        data["manada_checkins_semana"] = 0
        data["manada_quiz_semana"]     = 0
        data["manada_stake_semana"]    = 0


# ═══════════════════════════════════════════════════════════════════════════
# LA MANADA v2 — Weekly Hunt: reemplaza a la Ruleta diaria. No es un juego de
# azar individual, es un pool semanal fijo repartido por sorteo entre quienes
# cumplen el requisito de actividad de la semana (3 check-ins + 1 quiz
# acertado). El sorteo lo dispara un mod a mano desde /admin/weekly_hunt.
# ═══════════════════════════════════════════════════════════════════════════
WEEKLY_HUNT_CHECKINS_REQUIRED = 3
WEEKLY_HUNT_QUIZ_REQUIRED     = 1
WEEKLY_HUNT_POOL_USDT         = 10.0
WEEKLY_HUNT_POOL_PNT          = 100.0
WEEKLY_HUNT_WINNERS           = 5


def get_weekly_hunt_status(data: dict, week_ref: str = None):
    """Devuelve (checkins, quiz_correctos, elegible) para la semana indicada
    (por defecto la semana en curso). Soporta tanto a un usuario que sigue
    en esa semana (contadores en vivo) como a uno que ya roto a la semana
    siguiente (usa el snapshot manada_last_week_*, ver
    manada_reset_periods_if_needed)."""
    if week_ref is None:
        week_ref = _manada_week_ref()

    if data.get("manada_week_ref") == week_ref:
        checkins = data.get("manada_checkins_semana", 0) or 0
        quiz     = data.get("manada_quiz_semana", 0) or 0
    elif data.get("manada_last_week_ref") == week_ref:
        checkins = data.get("manada_last_week_checkins", 0) or 0
        quiz     = data.get("manada_last_week_quiz", 0) or 0
    else:
        checkins = 0
        quiz     = 0

    eligible = (
        checkins >= WEEKLY_HUNT_CHECKINS_REQUIRED
        and quiz >= WEEKLY_HUNT_QUIZ_REQUIRED
    )
    return checkins, quiz, eligible


def _previous_week_ref() -> str:
    """Semana ISO anterior a la actual — la semana 'recien terminada' que un
    mod normalmente quiere sortear."""
    y, w, _ = (date.today() - timedelta(days=7)).isocalendar()
    return f"{y}-W{w:02d}"


def get_weekly_hunt_eligible_uids(db: dict, week_ref: str):
    """Lista de uids elegibles para el sorteo de la semana indicada."""
    eligibles = []
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        _, _, eligible = get_weekly_hunt_status(data, week_ref)
        if eligible:
            eligibles.append(uid)
    return eligibles


def run_weekly_hunt_draw(db: dict, week_ref: str, mod_name: str) -> dict:
    """Sortea ganadores para la semana indicada y acredita el premio.
    Idempotente: si esa semana ya fue sorteada, devuelve el resultado
    guardado sin volver a sortear ni acreditar de nuevo."""
    draws = db.get("_global", {}).get("weekly_hunt_draws", {})
    if week_ref in draws:
        return draws[week_ref]

    eligibles = get_weekly_hunt_eligible_uids(db, week_ref)
    n_winners = min(WEEKLY_HUNT_WINNERS, len(eligibles))
    winners_uids = random.sample(eligibles, n_winners) if n_winners > 0 else []

    usdt_per_winner = round(WEEKLY_HUNT_POOL_USDT / WEEKLY_HUNT_WINNERS, 4)
    pnt_per_winner  = round(WEEKLY_HUNT_POOL_PNT / WEEKLY_HUNT_WINNERS, 4)

    winners = []
    for uid in winners_uids:
        wdata = db[uid]
        usdt_acreditado = add_manada_usdt(wdata, usdt_per_winner)
        pnt_acreditado  = add_manada_pnt(wdata, pnt_per_winner)
        winners.append({
            "uid":    uid,
            "nombre": wdata.get("username") or wdata.get("first_name") or uid,
            "usdt":   usdt_acreditado,
            "pnt":    pnt_acreditado,
        })
        if "history" not in wdata:
            wdata["history"] = []
        wdata["history"].append({
            "type": "weekly_hunt",
            "usdt": usdt_acreditado,
            "pnt":  pnt_acreditado,
            "date": date.today().isoformat(),
            "time": datetime.now().strftime("%H:%M"),
        })
        wdata["history"] = wdata["history"][-20:]

    if "_global" not in db:
        db["_global"] = {}
    if "weekly_hunt_draws" not in db["_global"]:
        db["_global"]["weekly_hunt_draws"] = {}

    result = {
        "winners":        winners,
        "eligible_count": len(eligibles),
        "drawn_at":       datetime.now().isoformat(),
        "drawn_by":       mod_name,
    }
    db["_global"]["weekly_hunt_draws"][week_ref] = result
    return result


async def notify_weekly_hunt_winner(app, uid: str, usdt: float, pnt: float, week_ref: str):
    """Notifica por Telegram a un ganador del sorteo semanal (el momento de
    'revelado' fuera de la mini app; dentro de la mini app se refleja en la
    pantalla de Weekly Hunt la próxima vez que la abra)."""
    try:
        await app.bot.send_message(
            chat_id=int(uid),
            text=(
                f"🏆 *¡Ganaste el sorteo semanal de La Manada!*\n\n"
                f"Semana: `{week_ref}`\n"
                f"➕ *+{usdt} USDT* y *+{pnt} PNT* a tu saldo de La Manada 🐆\n\n"
                f"_Cumpliste los 3 check-ins + 1 quiz acertado de la semana. "
                f"Seguí así para entrar al próximo sorteo_ 🐾"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error notificando ganador de Weekly Hunt {uid}: {e}")


def add_manada_usdt(data: dict, amount: float) -> float:
    """Acredita USDT al saldo de La Manada respetando el tope mensual.
    Devuelve lo realmente acreditado (puede ser menos que `amount` si se
    llegó al tope, o 0 si ya estaba en el tope)."""
    manada_reset_periods_if_needed(data)
    ya_ganado   = data.get("manada_usdt_month", 0) or 0
    disponible  = max(0.0, MANADA_MONTHLY_CAP_USDT - ya_ganado)
    acreditado  = round(min(amount, disponible), 4)
    if acreditado > 0:
        data["manada_usdt_balance"] = round((data.get("manada_usdt_balance", 0) or 0) + acreditado, 4)
        data["manada_usdt_month"]   = round(ya_ganado + acreditado, 4)
    return acreditado


def add_manada_pnt(data: dict, amount: float) -> float:
    """Acredita PNT al saldo de La Manada (sin tope mensual propio por ahora)."""
    manada_reset_periods_if_needed(data)
    amount = round(amount, 4)
    data["manada_pnt_balance"] = round((data.get("manada_pnt_balance", 0) or 0) + amount, 4)
    data["manada_pnt_month"]   = round((data.get("manada_pnt_month", 0) or 0) + amount, 4)
    return amount


def get_daily_hunt_bonus_usdt(streak: int) -> float:
    """Bonus en USDT del Daily Hunt según la racha de check-in (días consecutivos)."""
    if streak >= 7:
        return 0.05
    elif streak >= 4:
        return 0.03
    else:
        return 0.01


# ═══════════════════════════════════════════════════════════════════════════
# LA MANADA v2 — Learn & Earn: quiz corto de 1 pregunta por día, verificación
# automática (sin mod). Recompensa en USDT o PNT, alternada al azar entre
# quienes acierten. Reutiliza las columnas manada_quiz_semana /
# manada_last_quiz_date ya creadas para el modelo de datos de La Manada.
# ═══════════════════════════════════════════════════════════════════════════
QUIZ_USDT_MIN = 0.02
QUIZ_USDT_MAX = 0.10
QUIZ_PNT_MIN  = 0.1
QUIZ_PNT_MAX  = 1.0

QUIZ_BANK = [
    {"cat": "Fundamentos Blockchain", "dif": "Fácil", "q": "¿Qué es una blockchain?", "opts": {"A": "Una base de datos centralizada controlada por un banco", "B": "Un registro de transacciones distribuido entre muchas computadoras", "C": "Un tipo de tarjeta de crédito", "D": "Una moneda física"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Fácil", "q": "¿Qué significa que una red sea 'descentralizada'?", "opts": {"A": "Que una sola empresa controla todo", "B": "Que no existe una autoridad central única, sino una red de participantes", "C": "Que solo los bancos pueden participar", "D": "Que las transacciones se hacen en efectivo"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Fácil", "q": "¿Qué es un 'bloque' en una blockchain?", "opts": {"A": "Un grupo de transacciones agrupadas y validadas en conjunto", "B": "Una billetera perdida", "C": "Un tipo de moneda", "D": "Un error del sistema"}, "correct": "A"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué es un 'hash'?", "opts": {"A": "Un tipo de criptomoneda", "B": "Un código único que identifica y protege un bloque de datos", "C": "Una comisión de red", "D": "Una wallet de papel"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué significa 'minar' criptomonedas en una red de prueba de trabajo (Proof of Work)?", "opts": {"A": "Cavar de forma literal en busca de monedas", "B": "Usar poder computacional para validar transacciones y crear nuevos bloques", "C": "Comprar criptomonedas con tarjeta", "D": "Transferir criptomonedas entre wallets"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué es un 'nodo' en una red blockchain?", "opts": {"A": "Una computadora que participa validando y almacenando una copia de la red", "B": "Un tipo de moneda", "C": "Una wallet de hardware", "D": "Un exchange"}, "correct": "A"},
    {"cat": "Fundamentos Blockchain", "dif": "Fácil", "q": "¿Quién creó Bitcoin?", "opts": {"A": "Elon Musk", "B": "Un desarrollador o grupo bajo el seudónimo Satoshi Nakamoto", "C": "El gobierno de Estados Unidos", "D": "Vitalik Buterin"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Difícil", "q": "¿En qué año se publicó el whitepaper de Bitcoin?", "opts": {"A": "2005", "B": "2008", "C": "2013", "D": "2017"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué blockchain popularizó los contratos inteligentes?", "opts": {"A": "Bitcoin", "B": "Ethereum", "C": "Dogecoin", "D": "Litecoin"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué es un contrato inteligente (smart contract)?", "opts": {"A": "Un contrato en papel firmado de forma digital", "B": "Un programa que ejecuta acciones de manera automática cuando se cumplen ciertas condiciones", "C": "Un tipo de préstamo bancario", "D": "Un NFT"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Fácil", "q": "¿Qué significa que una blockchain sea 'inmutable'?", "opts": {"A": "Que se puede editar libremente", "B": "Que, una vez confirmada, una transacción no se puede alterar ni eliminar", "C": "Que no tiene ningún costo", "D": "Que solo funciona un día"}, "correct": "B"},
    {"cat": "Fundamentos Blockchain", "dif": "Medio", "q": "¿Qué es un explorador de bloques (block explorer)?", "opts": {"A": "Una wallet física", "B": "Una herramienta web para consultar transacciones y direcciones públicas de una blockchain", "C": "Un exchange centralizado", "D": "Un tipo de minero"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Qué es una 'seed phrase' o frase semilla?", "opts": {"A": "Una contraseña que se puede cambiar cuando se quiera", "B": "Un conjunto de palabras que permite recuperar el acceso completo a una wallet", "C": "El nombre de la wallet", "D": "Un código que entrega el exchange"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Con quién se debe compartir la seed phrase o la clave privada?", "opts": {"A": "Con soporte técnico si la solicita", "B": "Con nadie, nunca", "C": "Con un moderador de confianza", "D": "Con familiares cercanos"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una wallet 'no custodial'?", "opts": {"A": "Una wallet en la que un tercero guarda las claves por el usuario", "B": "Una wallet en la que el usuario controla por sí mismo las claves privadas", "C": "Una wallet solo para exchanges", "D": "Una wallet que no necesita ninguna clave"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una wallet 'custodial'?", "opts": {"A": "Una en la que el usuario controla el cien por ciento de las claves privadas", "B": "Una en la que un tercero, como un exchange, guarda las claves por el usuario", "C": "Una wallet de hardware", "D": "Una wallet sin conexión a internet"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una 'cold wallet' o billetera fría?", "opts": {"A": "Una wallet conectada a internet todo el tiempo", "B": "Una wallet que almacena las claves sin conexión a internet", "C": "Una wallet solo para stablecoins", "D": "Una wallet de un exchange"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una 'hot wallet' o billetera caliente?", "opts": {"A": "Una wallet física guardada en una caja fuerte", "B": "Una wallet conectada a internet, más cómoda pero más expuesta a ataques", "C": "Una wallet que no se puede usar", "D": "Un tipo de tarjeta de crédito"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "Si se pierde la seed phrase y no existe una copia de respaldo, ¿qué ocurre?", "opts": {"A": "Se puede solicitar una nueva al soporte técnico", "B": "Se pierde el acceso a los fondos de esa wallet de forma definitiva", "C": "El dinero regresa de forma automática a la cuenta bancaria", "D": "No ocurre nada, se puede restablecer con el correo electrónico"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Qué es la autenticación de dos factores (2FA)?", "opts": {"A": "Usar dos contraseñas iguales", "B": "Una capa adicional de seguridad que solicita un segundo código además de la contraseña", "C": "Un tipo de wallet", "D": "Un impuesto sobre las criptomonedas"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Cuál es una buena práctica para guardar una seed phrase?", "opts": {"A": "Tomarle una foto y publicarla en redes sociales", "B": "Escribirla en papel y guardarla en un lugar físico seguro", "C": "Enviarla por mensaje a un amigo", "D": "Guardarla en un documento en línea de acceso público"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es la clave pública de una wallet?", "opts": {"A": "La contraseña secreta que nunca se debe compartir", "B": "La dirección que se puede compartir para recibir fondos", "C": "El nombre de usuario del exchange", "D": "Un tipo de NFT"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una clave privada?", "opts": {"A": "El código secreto que otorga control total sobre los fondos de una wallet", "B": "La dirección pública para recibir pagos", "C": "El nombre de la blockchain", "D": "Un código de descuento"}, "correct": "A"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Por qué es importante verificar bien una dirección antes de enviar criptomonedas?", "opts": {"A": "No es importante, siempre se puede revertir el envío", "B": "Porque las transacciones en blockchain son irreversibles", "C": "Porque las direcciones cambian por sí solas", "D": "Porque el banco cobra una multa"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué es una wallet de hardware?", "opts": {"A": "Una aplicación en el teléfono", "B": "Un dispositivo físico diseñado para almacenar claves privadas sin conexión", "C": "Una tarjeta de crédito común", "D": "Un tipo de exchange"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "Si una wallet o una persona pide la seed phrase para 'verificar la cuenta', ¿qué corresponde hacer?", "opts": {"A": "Escribirla porque es un trámite habitual", "B": "Sospechar: ninguna aplicación o soporte legítimo la solicita jamás", "C": "Compartirla solo en un mensaje privado", "D": "Enviarla por correo electrónico"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Difícil", "q": "¿Qué significa la expresión 'not your keys, not your coins'?", "opts": {"A": "Que las monedas no tienen dueño", "B": "Que si no se controlan las claves privadas, no se controla realmente el fondo", "C": "Que las claves privadas no tienen ninguna utilidad", "D": "Que todas las wallets son iguales"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Difícil", "q": "¿Qué es una wallet 'multisig' o de múltiples firmas?", "opts": {"A": "Una wallet que requiere más de una firma o aprobación para autorizar una transacción", "B": "Un tipo de moneda", "C": "Una firma digital falsa", "D": "Un exchange centralizado"}, "correct": "A"},
    {"cat": "Wallets y Seguridad", "dif": "Fácil", "q": "¿Es recomendable usar la misma contraseña en la wallet y en otras cuentas?", "opts": {"A": "Sí, es más fácil de recordar", "B": "No, aumenta el riesgo si alguna de esas cuentas es vulnerada", "C": "Solo si la contraseña es larga", "D": "No tiene ninguna importancia en criptomonedas"}, "correct": "B"},
    {"cat": "Wallets y Seguridad", "dif": "Medio", "q": "¿Qué se debe revisar antes de conectar una wallet a una aplicación o sitio web?", "opts": {"A": "Nada, siempre es seguro", "B": "Que el sitio sea legítimo y comprender qué permisos se le otorgan a la wallet", "C": "El clima", "D": "El saldo de la cuenta bancaria"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Medio", "q": "¿Qué es el 'gas fee' en Ethereum?", "opts": {"A": "Un impuesto anual", "B": "La comisión que se paga a la red por procesar una transacción o un contrato", "C": "El costo de crear una wallet", "D": "Una recompensa por hacer staking"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Medio", "q": "¿Por qué pueden subir las comisiones de red en ciertos momentos?", "opts": {"A": "Por decisión de un banco central", "B": "Por congestión, cuando hay mucha demanda de transacciones en la red", "C": "Nunca cambian", "D": "Porque las monedas se vuelven más pesadas"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Fácil", "q": "¿Qué sucede si se envían criptomonedas a una dirección incorrecta?", "opts": {"A": "El sistema las devuelve de forma automática", "B": "Por lo general se pierden, ya que las transacciones son irreversibles", "C": "El exchange reembolsa el monto", "D": "No es posible enviar a una dirección incorrecta"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Fácil", "q": "¿Qué es una transacción 'on-chain'?", "opts": {"A": "Una transacción registrada directamente en la blockchain", "B": "Una transacción solo entre amigos", "C": "Una transacción en efectivo", "D": "Una transacción cancelada"}, "correct": "A"},
    {"cat": "Transacciones y Comisiones", "dif": "Fácil", "q": "¿Qué es el tiempo de 'confirmación' de una transacción?", "opts": {"A": "El tiempo que tarda la red en validar e incluir la transacción en un bloque", "B": "El tiempo que tarda en llegar un correo electrónico", "C": "El tiempo que permanece activa una wallet", "D": "Un plazo legal impuesto por el gobierno"}, "correct": "A"},
    {"cat": "Transacciones y Comisiones", "dif": "Difícil", "q": "¿Qué son las redes de 'capa 2' (Layer 2)?", "opts": {"A": "Blockchains sin ninguna relación entre sí", "B": "Soluciones construidas sobre una blockchain principal para hacerla más rápida y económica", "C": "Un tipo de wallet", "D": "Un exchange centralizado"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Medio", "q": "Al enviar criptomonedas, ¿por qué es fundamental elegir la red correcta?", "opts": {"A": "No tiene importancia, todas las redes son compatibles entre sí", "B": "Porque enviar por la red equivocada puede provocar la pérdida de los fondos", "C": "Porque cambia el color de la wallet", "D": "Porque afecta el nombre de la moneda"}, "correct": "B"},
    {"cat": "Transacciones y Comisiones", "dif": "Fácil", "q": "¿Qué es una 'transacción pendiente'?", "opts": {"A": "Una transacción cancelada", "B": "Una transacción enviada que todavía no fue confirmada por la red", "C": "Una transacción ilegal", "D": "Un tipo de estafa"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué significa 'HODL' en la jerga cripto?", "opts": {"A": "Un tipo de exchange", "B": "Mantener una inversión a largo plazo en lugar de vender de forma apresurada", "C": "Una orden de venta automática", "D": "Un tipo de wallet"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué significa 'FOMO'?", "opts": {"A": "Miedo a perderse una oportunidad ('fear of missing out')", "B": "Un tipo de moneda", "C": "Una wallet fría", "D": "Un protocolo de seguridad"}, "correct": "A"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué significa 'FUD'?", "opts": {"A": "Un tipo de token", "B": "Miedo, incertidumbre y duda difundidos para influir en el mercado", "C": "Una wallet de hardware", "D": "Una comisión de red"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué es una 'ballena' (whale) en el mundo cripto?", "opts": {"A": "Un tipo de moneda", "B": "Una persona que posee una cantidad muy grande de una criptomoneda", "C": "Un exchange pequeño", "D": "Un error del sistema"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué significa la sigla 'DYOR'?", "opts": {"A": "Un tipo de wallet", "B": "Investigar por cuenta propia antes de invertir", "C": "Una orden de compra automática", "D": "Un protocolo de consenso"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es un 'rug pull'?", "opts": {"A": "Una promoción de descuentos", "B": "Una estafa en la que los creadores de un proyecto lo abandonan y se quedan con los fondos de los inversores", "C": "Un tipo de wallet segura", "D": "Una actualización de red"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué es un 'altcoin'?", "opts": {"A": "Bitcoin", "B": "Cualquier criptomoneda que no sea Bitcoin", "C": "Una wallet alternativa", "D": "Un tipo de NFT"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es la capitalización de mercado (market cap) de una criptomoneda?", "opts": {"A": "El precio de una sola unidad", "B": "El valor total de todas las unidades en circulación (precio por cantidad)", "C": "La cantidad de wallets existentes", "D": "El costo de crear la moneda"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué significa 'airdrop' en el ámbito cripto?", "opts": {"A": "Una caída del precio", "B": "Una distribución gratuita de tokens a ciertas wallets, frecuentemente como promoción", "C": "Un tipo de estafa siempre", "D": "Un ataque a una wallet"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es un 'token'?", "opts": {"A": "Una unidad de valor creada sobre una blockchain existente, distinta de su moneda nativa", "B": "Únicamente Bitcoin", "C": "Una wallet física", "D": "Un tipo de exchange"}, "correct": "A"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué es un 'exchange' de criptomonedas?", "opts": {"A": "Una wallet de hardware", "B": "Una plataforma donde se pueden comprar, vender e intercambiar criptomonedas", "C": "Un tipo de blockchain", "D": "Un contrato inteligente"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es el 'DCA' o promedio de costo en dólares (dollar-cost averaging)?", "opts": {"A": "Invertir todo el capital de una sola vez", "B": "Invertir montos fijos de manera periódica para reducir el impacto de la volatilidad", "C": "Retirar todo el dinero de golpe", "D": "Un tipo de wallet"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es 'KYC' (Know Your Customer)?", "opts": {"A": "Un tipo de moneda", "B": "El proceso de verificación de identidad que exigen muchos exchanges", "C": "Una wallet anónima", "D": "Un protocolo de minería"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Fácil", "q": "¿Qué significa que un proyecto sea de 'código abierto' (open source)?", "opts": {"A": "Que su uso siempre es gratuito", "B": "Que su código es público y cualquiera puede revisarlo", "C": "Que no tiene ningún dueño legal", "D": "Que solo funciona en una wallet"}, "correct": "B"},
    {"cat": "Términos y Jerga", "dif": "Medio", "q": "¿Qué es una 'ICO' (Initial Coin Offering)?", "opts": {"A": "Una forma en que un proyecto nuevo recauda fondos vendiendo sus tokens al público", "B": "Un tipo de wallet fría", "C": "Un impuesto sobre las criptomonedas", "D": "Una estafa siempre"}, "correct": "A"},
    {"cat": "Stablecoins y Tokens", "dif": "Fácil", "q": "¿Qué es una 'stablecoin'?", "opts": {"A": "Una moneda que sube de precio todos los días", "B": "Una criptomoneda diseñada para mantener un valor estable, por lo general vinculado a una moneda como el dólar", "C": "Un tipo de NFT", "D": "Una wallet de hardware"}, "correct": "B"},
    {"cat": "Stablecoins y Tokens", "dif": "Fácil", "q": "¿A qué está vinculado normalmente el valor del USDT?", "opts": {"A": "Al oro", "B": "Al dólar estadounidense", "C": "Al euro", "D": "A Bitcoin"}, "correct": "B"},
    {"cat": "Stablecoins y Tokens", "dif": "Medio", "q": "¿Qué es un 'token de utilidad' (utility token)?", "opts": {"A": "Un token que otorga acceso a funciones específicas dentro de un ecosistema o una aplicación", "B": "Únicamente una moneda especulativa", "C": "Un tipo de NFT coleccionable", "D": "Una wallet"}, "correct": "A"},
    {"cat": "Stablecoins y Tokens", "dif": "Fácil", "q": "¿Qué es un NFT?", "opts": {"A": "Una criptomoneda estable", "B": "Un token único y no intercambiable que representa la propiedad de un activo digital", "C": "Un tipo de wallet", "D": "Una comisión de red"}, "correct": "B"},
    {"cat": "Stablecoins y Tokens", "dif": "Medio", "q": "¿Qué significa que un token sea 'fungible'?", "opts": {"A": "Que cada unidad es única e irremplazable", "B": "Que cada unidad es idéntica e intercambiable por otra igual, como el dinero", "C": "Que carece de valor", "D": "Que solo existe una unidad"}, "correct": "B"},
    {"cat": "Stablecoins y Tokens", "dif": "Difícil", "q": "¿Qué riesgo debe considerarse con las stablecoins?", "opts": {"A": "Ninguno, son totalmente infalibles", "B": "Que dependen de que el emisor realmente cuente con las reservas que respaldan su valor", "C": "Que solo sirven para NFTs", "D": "Que no se pueden transferir"}, "correct": "B"},
    {"cat": "Stablecoins y Tokens", "dif": "Medio", "q": "¿Qué son los 'tokens de gobernanza'?", "opts": {"A": "Tokens que otorgan derecho a votar sobre decisiones de un protocolo o proyecto", "B": "Tokens que solo sirven para pagar impuestos", "C": "Un tipo de NFT", "D": "Una wallet institucional"}, "correct": "A"},
    {"cat": "Stablecoins y Tokens", "dif": "Medio", "q": "¿Qué significa 'quemar' (burn) tokens?", "opts": {"A": "Venderlos todos de una sola vez", "B": "Eliminarlos de circulación de forma permanente, reduciendo la oferta total", "C": "Transferirlos a un exchange", "D": "Convertirlos en un NFT"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Medio", "q": "¿Qué significa 'DeFi'?", "opts": {"A": "Finanzas centralizadas", "B": "Finanzas descentralizadas: servicios financieros sin intermediarios tradicionales", "C": "Un tipo de wallet", "D": "Un impuesto sobre las criptomonedas"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Fácil", "q": "¿Qué es el 'staking'?", "opts": {"A": "Vender criptomonedas con rapidez", "B": "Bloquear criptomonedas para ayudar a validar la red y recibir recompensas a cambio", "C": "Un tipo de estafa", "D": "Transferir criptomonedas entre exchanges"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Difícil", "q": "¿Qué es 'Proof of Stake' (prueba de participación)?", "opts": {"A": "Un mecanismo de consenso en el que los validadores bloquean monedas para poder validar transacciones", "B": "Un tipo de minería con computadoras potentes", "C": "Un impuesto sobre las ganancias", "D": "Una wallet fría"}, "correct": "A"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Difícil", "q": "¿Qué es 'Proof of Work' (prueba de trabajo)?", "opts": {"A": "Un mecanismo de consenso en el que los mineros compiten resolviendo cálculos con poder computacional", "B": "Un contrato laboral", "C": "Un tipo de stablecoin", "D": "Una wallet custodial"}, "correct": "A"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Medio", "q": "¿Qué es un 'pool de staking'?", "opts": {"A": "Una piscina física", "B": "Un grupo de usuarios que combina sus fondos para hacer staking en conjunto y compartir recompensas", "C": "Un tipo de exchange", "D": "Una wallet de hardware"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Difícil", "q": "¿Qué es el 'yield farming'?", "opts": {"A": "Cultivar alimentos con criptomonedas", "B": "Mover fondos entre distintos protocolos DeFi para maximizar el rendimiento obtenido", "C": "Un tipo de minería", "D": "Una estafa siempre"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Medio", "q": "¿Qué es una 'DAO' u organización autónoma descentralizada?", "opts": {"A": "Una empresa tradicional con un director ejecutivo", "B": "Una organización gobernada por reglas en código y decisiones votadas por sus miembros", "C": "Un tipo de wallet", "D": "Un exchange centralizado"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Medio", "q": "¿Qué riesgo tiene el staking que conviene conocer?", "opts": {"A": "Ninguno, es totalmente seguro", "B": "Que los fondos pueden quedar bloqueados por un tiempo y el valor de la moneda puede bajar mientras tanto", "C": "Que se convierte automáticamente en un NFT", "D": "Que pierde validez legal"}, "correct": "B"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Difícil", "q": "¿Qué es un 'protocolo' en el contexto de DeFi?", "opts": {"A": "Un conjunto de reglas y contratos inteligentes que definen el funcionamiento de una aplicación descentralizada", "B": "Un tipo de moneda", "C": "Una wallet de hardware", "D": "Un documento legal firmado en papel"}, "correct": "A"},
    {"cat": "DeFi, Staking y Consenso", "dif": "Difícil", "q": "¿Qué es un 'liquidity pool' o fondo de liquidez?", "opts": {"A": "Un fondo de tokens bloqueados que permite realizar intercambios en un exchange descentralizado", "B": "Una wallet fría", "C": "Un tipo de NFT", "D": "Un impuesto sobre las transacciones"}, "correct": "A"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "Si alguien escribe por mensaje privado ofreciendo 'duplicar una inversión' en criptomonedas, ¿qué es lo más probable?", "opts": {"A": "Una oportunidad única", "B": "Una estafa", "C": "Un obsequio oficial de la plataforma", "D": "Un airdrop legítimo"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "¿Qué es el 'phishing'?", "opts": {"A": "Un tipo de moneda", "B": "Un engaño para robar datos o claves haciéndose pasar por una fuente confiable", "C": "Una técnica de minería", "D": "Un tipo de staking"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Medio", "q": "¿Cómo se puede identificar un sitio web falso que imita a uno real?", "opts": {"A": "Es imposible detectarlo", "B": "Revisando con cuidado la dirección web, el dominio y si cuenta con certificado de seguridad", "C": "Por el color de la página", "D": "Por la cantidad de anuncios"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "Si un supuesto 'soporte técnico' solicita acceso remoto a la computadora o a la wallet, ¿qué corresponde hacer?", "opts": {"A": "Otorgar el acceso porque indica que es urgente", "B": "Desconfiar y cortar la comunicación: el soporte legítimo nunca solicita esto", "C": "Pedirle que llame por otra vía", "D": "Aceptar si su foto de perfil parece oficial"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Medio", "q": "¿Qué es un contrato inteligente malicioso?", "opts": {"A": "Un contrato que siempre es seguro", "B": "Un contrato programado para robar o bloquear los fondos de quienes interactúan con él", "C": "Un tipo de wallet", "D": "Un protocolo de consenso"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "¿Qué señal de alerta debería generar dudas sobre un proyecto cripto?", "opts": {"A": "Que cuente con un sitio web", "B": "Que prometa ganancias garantizadas y sin ningún riesgo", "C": "Que tenga redes sociales", "D": "Que cuente con un whitepaper"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Difícil", "q": "¿Qué es el 'pig butchering' o estafa de engorde?", "opts": {"A": "Un tipo de staking", "B": "Una estafa en la que se construye una relación de confianza a largo plazo antes de solicitar una inversión", "C": "Un protocolo DeFi legítimo", "D": "Una wallet de hardware"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "Si aparece un enlace acortado o sospechoso en un chat pidiendo conectar la wallet, ¿cuál es la opción más segura?", "opts": {"A": "Conectarla de todos modos, no representa un riesgo", "B": "No hacer clic y verificar la fuente por canales oficiales", "C": "Compartirlo con más personas", "D": "Conectar solo una parte de la wallet"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Medio", "q": "¿Qué es un 'soporte falso' en Telegram o Discord?", "opts": {"A": "Una cuenta oficial verificada", "B": "Una cuenta que se hace pasar por soporte oficial para robar datos o dinero", "C": "Un bot legítimo del proyecto", "D": "Un tipo de NFT"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Difícil", "q": "¿Por qué resulta riesgoso aprobar (approve) un contrato sin revisarlo con detenimiento?", "opts": {"A": "No implica ningún riesgo", "B": "Porque se le puede estar dando permiso a un contrato para mover los tokens sin pedir confirmación cada vez", "C": "Porque cambia el color de la wallet", "D": "Porque elimina la cuenta de Telegram"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Medio", "q": "¿Qué corresponde hacer si existe la sospecha de haber caído en una estafa cripto?", "opts": {"A": "No comentarlo con nadie", "B": "Dejar de interactuar con esa fuente, informar a la comunidad o al soporte oficial y revisar la seguridad de la wallet", "C": "Compartir la seed phrase para que 'reviertan' la transacción", "D": "Esperar a que la situación se resuelva sola"}, "correct": "B"},
    {"cat": "Estafas y Phishing", "dif": "Fácil", "q": "¿Qué es un 'giveaway' o sorteo falso?", "opts": {"A": "Un sorteo oficial verificado", "B": "Una estafa que solicita enviar criptomonedas primero para 'recibir el doble' después", "C": "Un airdrop real", "D": "Un tipo de staking"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "¿Cómo se llama la comunidad de Telegram de Panther Wallet?", "opts": {"A": "Panther Squad", "B": "La Manada Panther", "C": "Panther Army", "D": "Wallet Warriors"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "¿Qué token tiene Panther Wallet además de aceptar USDT?", "opts": {"A": "PNT", "B": "PTH", "C": "PAW", "D": "PANT"}, "correct": "A"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "¿Cuál es el juego integrado dentro de la mini aplicación de Panther?", "opts": {"A": "Panther Runner", "B": "PNT Defender", "C": "Crypto Jump", "D": "Wallet Wars"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "Dentro de la mini aplicación, ¿qué sección ayuda a aprender términos cripto?", "opts": {"A": "Glosario Crypto", "B": "Panther Academy", "C": "Crypto School", "D": "Learn Center"}, "correct": "A"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Cómo se sube de nivel en el ranking de Panther (por ejemplo, de 'Cachorro' a 'Rastreador')?", "opts": {"A": "Pagando una suscripción", "B": "Acumulando puntos mediante actividades como el check-in diario, referidos y misiones", "C": "Comprando NFTs", "D": "Haciendo staking exclusivamente"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué se obtiene al hacer el check-in diario en la mini aplicación de Panther?", "opts": {"A": "Nada", "B": "Puntos, y con una racha activa un pequeño bono en USDT (Daily Hunt)", "C": "Un NFT gratuito", "D": "Acceso a soporte VIP"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué tipo de producto físico o virtual puede tener una persona usuaria de Panther Wallet?", "opts": {"A": "Solo una wallet de papel", "B": "Una tarjeta virtual o física", "C": "Un token no fungible personal", "D": "Un préstamo bancario"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "¿Qué se utiliza para invitar a otra persona a Panther Wallet?", "opts": {"A": "La contraseña personal", "B": "El código de referido único", "C": "La seed phrase", "D": "La clave privada"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿En qué plataformas tiene presencia oficial Panther Wallet?", "opts": {"A": "Solo en Telegram", "B": "Instagram, YouTube, TikTok, sitio web y Telegram", "C": "Solo en TikTok", "D": "Solo en su sitio web"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "Si alguien escribe por mensaje privado en Telegram diciendo ser 'soporte de Panther' y solicita la seed phrase, ¿qué corresponde hacer?", "opts": {"A": "Entregarla porque asegura ser soporte oficial", "B": "No compartirla nunca: se trata de una estafa, ningún canal oficial la solicita", "C": "Pedirle que la confirme por audio", "D": "Compartirla solo si su foto de perfil parece de Panther"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué busca aportar 'La Manada' además de los puntos de nivel?", "opts": {"A": "Nada adicional, solo puntos", "B": "Un saldo acumulable en USDT o PNT mediante distintas misiones", "C": "Descuentos en tiendas físicas", "D": "Acciones de la empresa"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "Además del check-in diario, ¿qué tipo de actividad puede otorgar puntos en Panther?", "opts": {"A": "Compartir reels o historias mencionando a Panther", "B": "Comprar acciones", "C": "Hacer staking de manera obligatoria", "D": "Ninguna otra actividad otorga puntos"}, "correct": "A"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué programa de Panther está pensado para las personas más activas que buscan recompensas mayores, más allá de La Manada?", "opts": {"A": "Partners", "B": "VIP Club", "C": "Panther Elite", "D": "Founders Only"}, "correct": "A"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué verifica un moderador antes de acreditar puntos o USDT por una misión con captura de pantalla en Panther?", "opts": {"A": "Nada, se acredita de forma automática siempre", "B": "Que la captura corresponda realmente a la acción solicitada, por ejemplo un reel o una reseña genuina", "C": "El horario exacto al segundo", "D": "El modelo de teléfono utilizado"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Medio", "q": "¿Qué ocurre si se comparte contenido de baja calidad solo para intentar obtener más recompensas en Panther?", "opts": {"A": "Se aprueba sin ningún inconveniente", "B": "Un moderador puede rechazarlo o acreditar un monto menor, ya que la aprobación es manual y a su criterio", "C": "Se premia siempre con el monto máximo de forma automática", "D": "No tiene ninguna consecuencia"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Difícil", "q": "¿Por qué Panther separa el saldo de 'La Manada' (USDT o PNT) de los puntos de nivel o ranking?", "opts": {"A": "Porque son exactamente lo mismo con otro nombre", "B": "Porque cumplen funciones distintas: los puntos miden actividad y ranking, y el saldo es una recompensa económica acumulable", "C": "Por un error de programación", "D": "Porque los puntos no tienen ninguna utilidad"}, "correct": "B"},
    {"cat": "Panther Wallet", "dif": "Fácil", "q": "¿Cuál es una buena costumbre para aprovechar mejor las misiones diarias de Panther, como Daily Hunt?", "opts": {"A": "Ingresar una sola vez al mes", "B": "Ingresar todos los días para no perder la racha, que incrementa la recompensa", "C": "Compartir la seed phrase con la comunidad", "D": "Ignorar las misiones automáticas"}, "correct": "B"},
]

def get_daily_quiz_index(uid: str, day: str) -> int:
    """Elige una pregunta determinística por usuario y por día (misma pregunta
    todo el día para ese usuario, cambia al día siguiente)."""
    h = hashlib.sha256(f"{uid}-{day}".encode()).hexdigest()
    return int(h, 16) % len(QUIZ_BANK)


def get_daily_quiz_question(uid: str, day: str) -> dict:
    return QUIZ_BANK[get_daily_quiz_index(uid, day)]


def grade_quiz_answer(data: dict, uid: str, answer: str):
    """Corrige la respuesta del quiz diario y acredita la recompensa si es
    correcta. Devuelve un dict con el resultado. Idempotente: si ya se
    respondió hoy, devuelve already_done=True sin volver a acreditar."""
    today = date.today().isoformat()
    if data.get("manada_last_quiz_date") == today:
        return {"already_done": True}

    question = get_daily_quiz_question(uid, today)
    correcto = str(answer).strip().upper() == question["correct"]

    data["manada_last_quiz_date"] = today
    manada_reset_periods_if_needed(data)

    resultado = {
        "already_done":  False,
        "correct":       correcto,
        "correct_answer": question["correct"],
        "manada_usdt_earned": 0,
        "manada_pnt_earned":  0,
    }

    if correcto:
        data["manada_quiz_semana"] = (data.get("manada_quiz_semana", 0) or 0) + 1
        if random.random() < 0.5:
            monto = round(random.uniform(QUIZ_USDT_MIN, QUIZ_USDT_MAX), 2)
            resultado["manada_usdt_earned"] = add_manada_usdt(data, monto)
        else:
            monto = round(random.uniform(QUIZ_PNT_MIN, QUIZ_PNT_MAX), 2)
            resultado["manada_pnt_earned"] = add_manada_pnt(data, monto)

        if "history" not in data:
            data["history"] = []
        data["history"].append({
            "type": "quiz",
            "correct": True,
            "usdt": resultado["manada_usdt_earned"],
            "pnt":  resultado["manada_pnt_earned"],
            "date": today,
            "time": datetime.now().strftime("%H:%M"),
        })
        data["history"] = data["history"][-20:]

    return resultado


DAILY_COUNT_FIELD = {
    "share_reel":       "reel_count_today",
    "share_story":      "story_count_today",
    "own_content":      "content_count_today",
    "comment_ig":       "comment_ig_count",
    "comment_ig_last":  "comment_ig_count",
    "comment_tt":       "comment_tt_count",
    "comment_tt_last":  "comment_tt_count",
    "story_mention":    "story_mention_count",
}

def reset_daily_counts_if_needed(data):
    today = date.today().isoformat()
    if data.get("last_mission_date") != today:
        for field in DAILY_COUNT_FIELD.values():
            data[field] = 0
        data["last_mission_date"] = today

def can_do_daily_mission(data, mission_type):
    """Retorna True si el usuario puede hacer esta misión hoy (límite según get_daily_limit)."""
    reset_daily_counts_if_needed(data)
    field = DAILY_COUNT_FIELD.get(mission_type)
    if not field:
        return True
    return data.get(field, 0) < get_daily_limit(mission_type)

def register_daily_mission(data, mission_type):
    """Incrementa el contador diario de la misión."""
    field = DAILY_COUNT_FIELD.get(mission_type)
    if field:
        data[field] = data.get(field, 0) + 1

def is_once_mission_done(data, mission_type):
    """Retorna True si una misión de una sola vez ya fue completada."""
    field_map = {
        "wallet_activate":  "wallet_activated",
        "review_store":     "review_store_done",
        "review_trust":     "review_trust_done",
        "follow_emb_emi":   "follow_emb_emi",
        "follow_emb_lorena":"follow_emb_lorena",
        "first_deposit":    "first_deposit_done",
        "emoji_tg":         "emoji_tg_done",
    }
    field = field_map.get(mission_type)
    return bool(data.get(field)) if field else False

def check_emoji_tg(user) -> bool:
    """Verifica si el usuario tiene 🐆 o 🐾 en su nombre de Telegram."""
    name = (user.first_name or "") + (user.last_name or "")
    return "🐆" in name or "🐾" in name

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
    # ── DESACTIVADA: la Ruleta fue reemplazada por Weekly Hunt (ver
    # get_weekly_hunt_status / run_weekly_hunt_draw / /admin/weekly_hunt).
    # El código de la ruleta se deja intacto abajo por si se quisiera
    # reactivar en el futuro; alcanza con revertir este return.
    return False
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
                f"🏆 *¡Eres Fundador de la Manada!*\n\n"
                f"Guardaste tu lugar entre los primeros 500 miembros "
                f"de la Manada Panther.\n\n"
                f"Guarda tu badge y compártelo en tus historias 🐆\n\n"
                f"_Panther Wallet — Tu dinero, tus reglas._"
            ),
            parse_mode="Markdown"
        )
        return True
    except Exception as e:
        logger.error(f"Error enviando badge a {uid}: {e}")
        return False


# ── Bienvenida a nuevos miembros ──────────────────────────────────────────────
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta cuando alguien se une al grupo y lo saluda en el chat general."""
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        uid = str(member.id)
        db  = load_db()
        data = db.get(uid, {})

        # Borrar el mensaje de sistema "X se unió al grupo"
        try:
            await update.message.delete()
        except Exception:
            pass

        # Construir mención y mensaje
        mention = member.mention_html()
        bot_username = (await context.bot.get_me()).username
        bot_url = f"https://t.me/{bot_username}"

        referred_by = data.get("referred_by")
        if referred_by:
            ref_data = db.get(str(referred_by), {})
            ref_nombre = ref_data.get("username") or ref_data.get("first_name") or ""
            ref_line = f"\nTraído por <b>@{ref_nombre}</b> 🐾" if ref_nombre else ""
        else:
            ref_line = ""

        texto = (
            f"🐆 ¡Bienvenido a la Manada, {mention}!\n"
            f"Ya eres parte de los cazadores de Panther Wallet.{ref_line}\n\n"
            f"Escribile al bot para empezar a ganar puntos y participar en el evento 👇"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Empezar en el bot", url=bot_url)
        ]])

        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=texto,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"Error enviando bienvenida a {uid}: {e}")


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
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
            f"Envía tu captura de pantalla aquí directamente 👇\n\n"
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
            f"📸 *Envía tu captura de {tipo_label}*\n\n"
            f"1️⃣ Comparte el {tipo_label} de Panther\n"
            f"2️⃣ Toma una captura de pantalla\n"
            f"3️⃣ Envíala *aquí en este chat* como foto 👇\n\n"
            f"Si se aprueba recibes *+{pts} pts* 🎉",
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
        asyncio.create_task(send_welcome_sequence(context.bot, uid, user.first_name or "Cazador", source=data.get("source", "")))
    else:
        save_db(db)

    level = get_level(data["points"])
    next_lv, pts_needed = get_next_level(data["points"])

    app_url = f"https://go.mypanther.io/app?id={uid}&v=3"

    if is_new:
        text = f"🐆 La Manada te espera, {user.first_name}. Revisa los mensajes que te envié para empezar 👇"
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
            f"Vuelve mañana para no perderla.",
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

    # ── La Manada — Daily Hunt: bonus en USDT según racha + contador semanal ──
    manada_reset_periods_if_needed(data)
    data["manada_checkins_semana"] = (data.get("manada_checkins_semana", 0) or 0) + 1
    usdt_bonus     = get_daily_hunt_bonus_usdt(streak)
    usdt_acreditado = add_manada_usdt(data, usdt_bonus)

    old_lv = get_level(old_pts)
    new_lv = get_level(data["points"])
    lvl_msg = f"\n\n⬆️ *¡SUBISTE DE NIVEL!*\n{old_lv} → *{new_lv}*" if old_lv != new_lv else ""

    next_lv, pts_needed = get_next_level(data["points"])
    save_db(db)

    usdt_msg = f"\n🐆 Daily Hunt: *+{usdt_acreditado:.2f} USDT*" if usdt_acreditado > 0 else \
               f"\n🐆 Daily Hunt: tope mensual de La Manada alcanzado este mes"

    text = (
        f"✅ *¡Check-in completado!*\n\n"
        f"🔥 Racha: *{streak} día{'s' if streak > 1 else ''}*\n"
        f"➕ Ganaste: *+{earned} puntos*{bonus_msg}"
        f"{usdt_msg}\n"
        f"⭐ Total: *{data['points']} puntos*\n"
        f"🏅 Nivel: *{new_lv}*"
        f"{lvl_msg}\n\n"
        f"{'📈 Próximo: *' + next_lv + '* — faltan *' + str(pts_needed) + ' pts*' if next_lv else ''}"
    )
    return text, True

async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
    user = update.effective_user
    text, _ = await do_checkin(str(user.id), user, context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

# ── /puntos ───────────────────────────────────────────────────────────────────
async def cmd_puntos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
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
        f"{'📈 Próximo: *' + next_lv + '* — faltan *' + str(pts_needed) + ' pts*' if next_lv else '👑 ¡Eres Leyenda!'}",
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
        me     = " ← tú" if u["id"] == uid else ""
        lines.append(f"{prefix} @{name} — *{u['points']} pts* {lv}{me}")

    my_pos = next((i+1 for i,u in enumerate(sorted_) if u["id"] == uid), None)
    if my_pos and my_pos > 20:
        lines.append(f"\n📍 Tu posición: *#{my_pos}* — {db[uid]['points']} pts")

    lines.append(f"\n_Actualizado: {datetime.now().strftime('%d/%m %H:%M')}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())

# ── /referido ─────────────────────────────────────────────────────────────────
async def cmd_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
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
        f"_Compartí tu link y acumula puntos 🚀_",
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

    # Leer horas del argumento: /ruleta_on 8
    horas = 8  # default
    if context.args:
        try:
            horas = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ Uso: /ruleta_on <horas> (ej: /ruleta_on 8)")
            return

    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"

    # Guardar hora de fin para el countdown
    from datetime import datetime, timedelta
    ruleta_end = (datetime.utcnow() + timedelta(hours=horas)).isoformat()
    db["_global"]["ruleta_end"] = ruleta_end

    # Resetear giros de todos los usuarios al activar
    count = 0
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        data["spins_used_this_event"] = 0
        count += 1
    save_db(db)

    # Mandar mensaje inicial al grupo con countdown
    end_dt = datetime.fromisoformat(ruleta_end)

    def fmt_hora(dt, offset):
        local = dt + timedelta(hours=offset)
        return local.strftime("%H:%M")


    mx  = fmt_hora(end_dt, -6)
    col = fmt_hora(end_dt, -5)
    ar  = fmt_hora(end_dt, -3)
    es  = fmt_hora(end_dt, +2)
    horas_zonas = (
        "🇲🇽 México: " + mx + "\n"
        + "🇨🇴🇪🇨🇵🇪 Col/Ecu/Perú: " + col + "\n"
        + "🇦🇷 Argentina: " + ar + "\n"
        + "🇪🇸 España: " + es
    )
    msg = (
        "🎰 *¡LA RULETA ESTÁ ABIERTA, MANADA!* 🐾\n\n"
        + f"Tienen *{horas} horas* para girar. 3 giros por usuario.\n\n"
        + "💰 Premios reales en USDT y PNT esperando.\n\n"
        + "👉 Abre el bot y gira ahora → @ManadaPantherBot\n\n"
        + "⏳ *Cierra a las:*\n" + horas_zonas
    )
    try:
        sent = await context.bot.send_message(
            chat_id=MAIN_GROUP_ID,
            text=msg,
            parse_mode="Markdown"
        )
        # Guardar message_id para editarlo después
        db = load_db()
        db["_global"]["ruleta_countdown_msg_id"] = sent.message_id
        save_db(db)
    except Exception as e:
        logger.warning(f"No se pudo enviar mensaje de ruleta al grupo: {e}")

    # Lanzar tarea de countdown (actualiza cada 30 min)
    asyncio.create_task(ruleta_countdown_task(context.bot, horas))

    await update.message.reply_text(
        f"✅ Ruleta ACTIVADA por {horas}h. Giros reseteados para {count} usuarios. Mensaje enviado al grupo 🐾"
    )


async def ruleta_countdown_task(bot, horas_total: int):
    """Edita el mensaje del grupo cada 30 minutos con el tiempo restante."""
    from datetime import datetime, timedelta
    interval = 30 * 60  # 30 minutos
    steps = (horas_total * 60) // 30  # cuántas actualizaciones
    for i in range(steps):
        await asyncio.sleep(interval)
        db = load_db()
        msg_id = db.get("_global", {}).get("ruleta_countdown_msg_id")
        ruleta_end_str = db.get("_global", {}).get("ruleta_end")
        if not msg_id or not ruleta_end_str:
            break
        end_dt = datetime.fromisoformat(ruleta_end_str)
        now = datetime.utcnow()
        remaining = end_dt - now
        if remaining.total_seconds() <= 0:
            # Tiempo agotado
            try:
                await bot.edit_message_text(
                    chat_id=MAIN_GROUP_ID,
                    message_id=msg_id,
                    text="🎰 *¡La Ruleta se cerró!* 🐾\n\nGracias a todos los que giraron. Hasta la próxima 🐆",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Error editando mensaje cierre ruleta: {e}")
            break
        total_mins = int(remaining.total_seconds() // 60)
        horas_left = total_mins // 60
        mins_left = total_mins % 60
        tiempo_str = f"{horas_left}h {mins_left}m" if horas_left > 0 else f"{mins_left}m"
        def fmt_hora(dt, offset):
            local = dt + timedelta(hours=offset)
            return local.strftime("%H:%M")

        mx  = fmt_hora(end_dt, -6)
        col = fmt_hora(end_dt, -5)
        ar  = fmt_hora(end_dt, -3)
        es  = fmt_hora(end_dt, +2)
        horas_zonas = (
            "🇲🇽 México: " + mx + "\n"
            "🇨🇴🇪🇨🇵🇪 Col/Ecu/Perú: " + col + "\n"
            "🇦🇷 Argentina: " + ar + "\n"
            "🇪🇸 España: " + es
        )
        msg_edit = (
            "🎰 *¡LA RULETA ESTÁ ABIERTA, MANADA!* 🐾\n\n"
            f"Tienen *{horas_total} horas* para girar. 3 giros por usuario.\n\n"
            "💰 Premios reales en USDT y PNT esperando.\n\n"
            "👉 Abre el bot y gira ahora → @ManadaPantherBot\n\n"
            f"⏳ Cierra en *{tiempo_str}* a las:\n" + horas_zonas
        )
        try:
            await bot.edit_message_text(
                chat_id=MAIN_GROUP_ID,
                message_id=msg_id,
                text=msg_edit,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Error actualizando countdown ruleta: {e}")

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
        await update.message.reply_text("❌ No tienes permisos.")
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
        f"📸 *Envía tu captura de {tipo_label}*\n\n"
        f"1️⃣ Comparte el {tipo_label} de Panther\n"
        f"2️⃣ Toma una captura de pantalla\n"
        f"3️⃣ Envíala *aquí en este chat* como foto 👇\n\n"
        f"Si se aprueba recibes *+{pts} pts* 🎉",
        parse_mode="Markdown"
    )

# ── /ruleta ───────────────────────────────────────────────────────────────────
async def cmd_ruleta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    today = date.today().isoformat()
    if data.get("last_ruleta") == today:
        await update.message.reply_text(
            "🎰 Ya giraste la ruleta hoy.\n\nVuelve mañana para otro giro 🐾",
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
                f"📸 Toma captura de esta pantalla y envíala al chat general "
                f"o al bot en privado. Un moderador te contactará para coordinar el pago.\n\n"
                f"_⚠️ Solo puedes ganar USDT una vez por mes._"
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
                f"📸 Toma captura de esta pantalla y envíala al chat general "
                f"o al bot en privado. Los tokens serán acreditados en tu Panther Wallet.\n\n"
                f"_⚠️ Solo puedes ganar PNT una vez por mes._"
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
    if await redirect_to_private(update):
        return
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
        "Las misiones están disponibles en la Mini App. Toca el botón para abrirla.",
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

    # Leer horas del argumento: /ruleta_on 8
    horas = 8  # default
    if context.args:
        try:
            horas = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ Uso: /ruleta_on <horas> (ej: /ruleta_on 8)")
            return

    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"

    # Guardar hora de fin para el countdown
    from datetime import datetime, timedelta
    ruleta_end = (datetime.utcnow() + timedelta(hours=horas)).isoformat()
    db["_global"]["ruleta_end"] = ruleta_end

    # Resetear giros de todos los usuarios al activar
    count = 0
    for uid, data in db.items():
        if uid.startswith("_") or not isinstance(data, dict):
            continue
        data["spins_used_this_event"] = 0
        count += 1
    save_db(db)

    # Mandar mensaje inicial al grupo con countdown
    end_dt = datetime.fromisoformat(ruleta_end)

    def fmt_hora(dt, offset):
        local = dt + timedelta(hours=offset)
        return local.strftime("%H:%M")

    mx  = fmt_hora(end_dt, -6)
    col = fmt_hora(end_dt, -5)
    ar  = fmt_hora(end_dt, -3)
    es  = fmt_hora(end_dt, +2)
    horas_zonas = (
        "🇲🇽 México: " + mx + "\n"
        + "🇨🇴🇪🇨🇵🇪 Col/Ecu/Perú: " + col + "\n"
        + "🇦🇷 Argentina: " + ar + "\n"
        + "🇪🇸 España: " + es
    )
    msg = (
        "🎰 *¡LA RULETA ESTÁ ABIERTA, MANADA!* 🐾\n\n"
        + f"Tienen *{horas} horas* para girar. 3 giros por usuario.\n\n"
        + "💰 Premios reales en USDT y PNT esperando.\n\n"
        + "👉 Abre el bot y gira ahora → @ManadaPantherBot\n\n"
        + "⏳ *Cierra a las:*\n" + horas_zonas
    )
    try:
        sent = await context.bot.send_message(
            chat_id=MAIN_GROUP_ID,
            text=msg,
            parse_mode="Markdown"
        )
        # Guardar message_id para editarlo después
        db = load_db()
        db["_global"]["ruleta_countdown_msg_id"] = sent.message_id
        save_db(db)
    except Exception as e:
        logger.warning(f"No se pudo enviar mensaje de ruleta al grupo: {e}")

    # Lanzar tarea de countdown (actualiza cada 30 min)
    asyncio.create_task(ruleta_countdown_task(context.bot, horas))

    await update.message.reply_text(
        f"✅ Ruleta ACTIVADA por {horas}h. Giros reseteados para {count} usuarios. Mensaje enviado al grupo 🐾"
    )


async def ruleta_countdown_task(bot, horas_total: int):
    """Edita el mensaje del grupo cada 30 minutos con el tiempo restante."""
    from datetime import datetime, timedelta
    interval = 30 * 60  # 30 minutos
    steps = (horas_total * 60) // 30  # cuántas actualizaciones
    for i in range(steps):
        await asyncio.sleep(interval)
        db = load_db()
        msg_id = db.get("_global", {}).get("ruleta_countdown_msg_id")
        ruleta_end_str = db.get("_global", {}).get("ruleta_end")
        if not msg_id or not ruleta_end_str:
            break
        end_dt = datetime.fromisoformat(ruleta_end_str)
        now = datetime.utcnow()
        remaining = end_dt - now
        if remaining.total_seconds() <= 0:
            # Tiempo agotado
            try:
                await bot.edit_message_text(
                    chat_id=MAIN_GROUP_ID,
                    message_id=msg_id,
                    text="🎰 *¡La Ruleta se cerró!* 🐾\n\nGracias a todos los que giraron. Hasta la próxima 🐆",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Error editando mensaje cierre ruleta: {e}")
            break
        total_mins = int(remaining.total_seconds() // 60)
        horas_left = total_mins // 60
        mins_left = total_mins % 60
        tiempo_str = f"{horas_left}h {mins_left}m" if horas_left > 0 else f"{mins_left}m"
        def fmt_hora(dt, offset):
            local = dt + timedelta(hours=offset)
            return local.strftime("%H:%M")

        mx  = fmt_hora(end_dt, -6)
        col = fmt_hora(end_dt, -5)
        ar  = fmt_hora(end_dt, -3)
        es  = fmt_hora(end_dt, +2)
        horas_zonas = (
            "🇲🇽 México: " + mx + "\n"
            "🇨🇴🇪🇨🇵🇪 Col/Ecu/Perú: " + col + "\n"
            "🇦🇷 Argentina: " + ar + "\n"
            "🇪🇸 España: " + es
        )
        msg_edit = (
            "🎰 *¡LA RULETA ESTÁ ABIERTA, MANADA!* 🐾\n\n"
            f"Tienen *{horas_total} horas* para girar. 3 giros por usuario.\n\n"
            "💰 Premios reales en USDT y PNT esperando.\n\n"
            "👉 Abre el bot y gira ahora → @ManadaPantherBot\n\n"
            f"⏳ Cierra en *{tiempo_str}* a las:\n" + horas_zonas
        )
        try:
            await bot.edit_message_text(
                chat_id=MAIN_GROUP_ID,
                message_id=msg_id,
                text=msg_edit,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Error actualizando countdown ruleta: {e}")

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
        await update.message.reply_text("❌ No tienes permisos.")
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
    if await redirect_to_private(update):
        return
    await update.message.reply_text(
        f"📸 *Verificación de contenido*\n\n"
        f"Para acreditar tus puntos:\n\n"
        f"1️⃣ Comparte el reel o historia de Panther\n"
        f"2️⃣ Toma una captura de pantalla\n"
        f"3️⃣ Envía la captura *directamente aquí* en el chat\n\n"
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
                f"📸 *Envía tu captura de {tipo_label}*\n\n"
                f"1️⃣ Comparte el {tipo_label} de Panther\n"
                f"2️⃣ Toma una captura de pantalla\n"
                f"3️⃣ Envíala *aquí en este chat* como foto 👇\n\n"
                f"Si se aprueba recibes *+{pts} pts* 🎉",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error handling web_app_data: {e}")

# ── Manejo de fotos (capturas de misiones) ────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only handle photos in private chats
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)
    # Detectar #NuevoCazador en privado
    caption = (update.message.caption or "").lower()
    if "#nuevocazador" in caption:
        await handle_nuevo_cazador_privado(update, context)
        return


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

    # ── Foto sin contexto — rechazar con explicación ──
    if mission_type is None:
        await update.message.reply_text(
            "⚠️ Esta imagen no fue enviada desde una misión del gamebot.\n\n"
            "Para que cuente, tienes que entrar a la Mini App → Misiones → "
            "seleccionar la misión correspondiente y subir la captura desde ahí.\n\n"
            "Las imágenes enviadas sin contexto no son válidas. "
            "Las misiones sociales tienen un límite de 3 capturas por día."
        )
        return

    tipo_labels = {
        "reel":             "🎬 Reel de Panther",
        "story":            "📸 Historia de Panther",
        "content":          "✏️ Contenido propio",
        "wallet_activate":  "🔐 Activación de Wallet",
        "review_store":     "⭐ Review en Tienda (Play/App Store)",
        "review_trust":     "🌟 Review en Trustpilot",
        "comment_ig":       "💬 Comentario en Instagram",
        "comment_ig_last":  "💬 Comentario en Último Post IG (+30 pts)",
        "comment_tt":       "💬 Comentario en TikTok",
        "comment_tt_last":  "💬 Comentario en Último Video TikTok (+30 pts)",
        "follow_emb_emi":   "🐆 Seguir Embajador @neodenoche",
        "follow_emb_lorena":"🐆 Seguir Embajadora @pegandolavuelta",
        "story_mention":    "📣 Historia mencionando a un amigo",
        "first_deposit":    "💰 Primer depósito en Panther Wallet",
        "stake":            "💰 Stake Challenge",
        None:               "📎 Sin clasificar",
    }
    tipo_label = tipo_labels.get(mission_type, "📎 Sin clasificar")

    # ── Verificar misiones de una sola vez ──
    if mission_type in ONCE_MISSIONS and mission_type != "emoji_tg":
        if is_once_mission_done(data, mission_type):
            await update.message.reply_text(
                f"⚠️ Ya completaste la misión *{tipo_label}* anteriormente.\n"
                "Solo se puede hacer una vez 🐾",
                parse_mode="Markdown"
            )
            return

    # ── Stake Challenge: límite semanal (1x por semana, no diario) ──
    if mission_type == "stake":
        if not can_do_stake_this_week(data):
            await update.message.reply_text(
                "⚠️ Ya enviaste tu captura de Stake Challenge esta semana.\n"
                "Vuelve la próxima semana para seguir ganando PNT 🐾"
            )
            return
        # Registrar el intento ahora (antes de la revisión del mod), mismo
        # criterio que Create & Earn: que cueste, no que se pueda spamear.
        data["manada_stake_semana"] = (data.get("manada_stake_semana", 0) or 0) + 1
        try:
            save_db(db)
        except Exception as e:
            logger.error(f"Error guardando manada_stake_semana en handle_photo: {e}")

    # ── Verificar límite diario ──
    mission_key = mission_type
    if mission_type in ["reel", "story", "content"]:
        mission_key = {"reel": "share_reel", "story": "share_story", "content": "own_content"}[mission_type]

    daily_limit_for_mission = get_daily_limit(mission_key)

    if mission_key in DAILY_LIMIT_MISSIONS:
        reset_daily_counts_if_needed(data)
        if not can_do_daily_mission(data, mission_key):
            await update.message.reply_text(
                f"⚠️ Ya alcanzaste el límite de {daily_limit_for_mission} "
                f"captura{'s' if daily_limit_for_mission != 1 else ''} para esta misión hoy.\n"
                "Vuelve mañana para seguir ganando puntos 🐾"
            )
            return
        # Registrar el intento ahora: antes esto no se hacía y el contador
        # nunca subía, así que el límite diario no se aplicaba de verdad.
        register_daily_mission(data, mission_key)

    count_key = DAILY_COUNT_FIELD.get(mission_key)
    current_count = data.get(count_key, 0) if count_key else 0
    remaining = max(0, daily_limit_for_mission - current_count) if count_key else None

    try:
        save_db(db)
    except Exception as e:
        logger.error(f"Error guardando contadores en handle_photo: {e}")

    if count_key and remaining is not None:
        counter_msg = f"\n\n📊 {tipo_label}: *{current_count}/{daily_limit_for_mission}* hoy · te quedan *{remaining}* restantes."
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
            InlineKeyboardButton("✅ Wallet (+175 pts)", callback_data=f"approve_{uid}_wallet_activate"),
        ],
        [
            InlineKeyboardButton(f"🎨 Contenido $0.05 (+{PTS['own_content']}pts)", callback_data=f"approve_{uid}_content|0.05"),
        ],
        [
            InlineKeyboardButton("🎨 $0.10", callback_data=f"approve_{uid}_content|0.10"),
            InlineKeyboardButton("🎨 $0.15", callback_data=f"approve_{uid}_content|0.15"),
            InlineKeyboardButton("🎨 $0.20", callback_data=f"approve_{uid}_content|0.20"),
        ],
        [
            InlineKeyboardButton("🎨 $0.25", callback_data=f"approve_{uid}_content|0.25"),
            InlineKeyboardButton("🎨 $0.30", callback_data=f"approve_{uid}_content|0.30"),
        ],
        [
            InlineKeyboardButton("💰 Stake 0.5 PNT", callback_data=f"approve_{uid}_stake|0.5"),
            InlineKeyboardButton("💰 1 PNT", callback_data=f"approve_{uid}_stake|1"),
            InlineKeyboardButton("💰 2 PNT", callback_data=f"approve_{uid}_stake|2"),
        ],
        [
            InlineKeyboardButton("💰 3 PNT", callback_data=f"approve_{uid}_stake|3"),
            InlineKeyboardButton("💰 4 PNT", callback_data=f"approve_{uid}_stake|4"),
            InlineKeyboardButton("💰 5 PNT", callback_data=f"approve_{uid}_stake|5"),
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
        f"Selecciona la acción:"
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
# Create & Earn: rango de USDT que un mod puede acreditar a mano por /aprobar
CREATE_EARN_USDT_MIN = 0.05
CREATE_EARN_USDT_MAX = 0.30

# Stake Challenge: rango de PNT que un mod puede acreditar a mano, y límite
# de 1 captura por semana (el staking no cambia todos los días).
STAKE_PNT_MIN = 0.5
STAKE_PNT_MAX = 5.0

def can_do_stake_this_week(data: dict) -> bool:
    manada_reset_periods_if_needed(data)
    return (data.get("manada_stake_semana", 0) or 0) < 1

async def cmd_aprobar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tienes permisos.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: /aprobar USER_ID reel|story|content|stake [monto]\n\n"
            f"Para 'content' (Create & Earn) el monto es obligatorio, entre "
            f"{CREATE_EARN_USDT_MIN} y {CREATE_EARN_USDT_MAX} USDT según la calidad.\n"
            f"Para 'stake' (Stake Challenge) el monto es obligatorio, entre "
            f"{STAKE_PNT_MIN} y {STAKE_PNT_MAX} PNT según la calidad.\n"
            "Ej: /aprobar 123456789 content 0.20\n"
            "Ej: /aprobar 123456789 stake 2"
        )
        return

    target_uid = context.args[0]
    tipo       = context.args[1].lower()
    pts_map = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"], "wallet_activate": PTS["wallet_activate"], "review_store": PTS["review_store"], "review_trust": PTS["review_trust"], "comment_ig": 5, "comment_ig_last": 30, "comment_tt": 5, "comment_tt_last": 30, "follow_emb_emi": PTS["follow_emb_emi"], "follow_emb_lorena": PTS["follow_emb_lorena"], "story_mention": PTS["story_mention"], "first_deposit": PTS["first_deposit"], "stake": 0}

    if tipo not in pts_map:
        await update.message.reply_text("Tipo inválido. Usa: reel, story, content o stake")
        return

    # ── Create & Earn: el mod define el monto USDT según la calidad ──
    monto_usdt = 0.0
    if tipo == "content":
        if len(context.args) < 3:
            await update.message.reply_text(
                f"⚠️ Para 'content' tienes que indicar el monto en USDT "
                f"({CREATE_EARN_USDT_MIN}–{CREATE_EARN_USDT_MAX}).\n"
                "Ej: /aprobar USER_ID content 0.20"
            )
            return
        try:
            monto_usdt = round(float(context.args[2].replace(",", ".")), 2)
        except ValueError:
            await update.message.reply_text("Monto inválido. Usa un número, ej: 0.20")
            return
        if not (CREATE_EARN_USDT_MIN <= monto_usdt <= CREATE_EARN_USDT_MAX):
            await update.message.reply_text(
                f"El monto debe estar entre {CREATE_EARN_USDT_MIN} y {CREATE_EARN_USDT_MAX} USDT."
            )
            return

    # ── Stake Challenge: el mod define el monto PNT según la calidad ──
    monto_pnt = 0.0
    if tipo == "stake":
        if len(context.args) < 3:
            await update.message.reply_text(
                f"⚠️ Para 'stake' tienes que indicar el monto en PNT "
                f"({STAKE_PNT_MIN}–{STAKE_PNT_MAX}).\n"
                "Ej: /aprobar USER_ID stake 2"
            )
            return
        try:
            monto_pnt = round(float(context.args[2].replace(",", ".")), 2)
        except ValueError:
            await update.message.reply_text("Monto inválido. Usa un número, ej: 2")
            return
        if not (STAKE_PNT_MIN <= monto_pnt <= STAKE_PNT_MAX):
            await update.message.reply_text(
                f"El monto debe estar entre {STAKE_PNT_MIN} y {STAKE_PNT_MAX} PNT."
            )
            return

    db = load_db()
    if target_uid not in db:
        await update.message.reply_text("Usuario no encontrado.")
        return

    earned = add_points(db[target_uid], pts_map[tipo])

    acreditado_usdt = 0.0
    usdt_mod_line = ""
    usdt_user_line = ""
    if tipo == "content":
        acreditado_usdt = add_manada_usdt(db[target_uid], monto_usdt)
        usdt_mod_line = f"\n💰 +{acreditado_usdt} USDT (Manada) acreditados"
        if acreditado_usdt < monto_usdt:
            usdt_mod_line += " — tope mensual de 10 USDT alcanzado, se acreditó parcial"
        if acreditado_usdt > 0:
            usdt_user_line = f"\n💰 *+{acreditado_usdt} USDT* a tu saldo de La Manada 🐆"

    acreditado_pnt = 0.0
    pnt_mod_line = ""
    pnt_user_line = ""
    if tipo == "stake":
        acreditado_pnt = add_manada_pnt(db[target_uid], monto_pnt)
        pnt_mod_line = f"\n🐾 +{acreditado_pnt} PNT (Manada) acreditados"
        if acreditado_pnt > 0:
            pnt_user_line = f"\n🐾 *+{acreditado_pnt} PNT* a tu saldo de La Manada 🐆"

    save_db(db)

    await update.message.reply_text(f"✅ +{earned} pts acreditados al usuario {target_uid}{usdt_mod_line}{pnt_mod_line}")

    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text=f"✅ *¡Misión verificada!*\n\n"
                 f"Tu captura fue aprobada.\n"
                 f"➕ *+{earned} puntos* acreditados 🐾"
                 f"{usdt_user_line}"
                 f"{pnt_user_line}\n"
                 f"⭐ Total: *{db[target_uid]['points']} puntos*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ── /transferir — traspaso de puntos entre usuarios (solo mods) ──────────────
async def cmd_transferir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Uso: <code>/transferir &lt;id_origen&gt; &lt;id_destino&gt; &lt;puntos|all&gt;</code>\n"
            "Ejemplo: <code>/transferir 5251081083 7836597271 all</code>",
            parse_mode="HTML"
        )
        return

    origen_id  = context.args[0].strip()
    destino_id = context.args[1].strip()
    cantidad   = context.args[2].strip().lower()

    db = load_db()

    if origen_id not in db:
        await update.message.reply_text(f"❌ Usuario origen <code>{origen_id}</code> no encontrado.", parse_mode="HTML")
        return

    if destino_id not in db:
        await update.message.reply_text(f"❌ Usuario destino <code>{destino_id}</code> no encontrado.", parse_mode="HTML")
        return

    data_origen  = db[origen_id]
    data_destino = db[destino_id]
    puntos_disponibles = data_origen.get("points", 0)

    if cantidad == "all":
        puntos_a_mover = puntos_disponibles
    else:
        try:
            puntos_a_mover = int(cantidad)
        except ValueError:
            await update.message.reply_text("❌ La cantidad debe ser un número entero o <code>all</code>.", parse_mode="HTML")
            return

    if puntos_a_mover <= 0:
        await update.message.reply_text("⚠️ El usuario origen tiene 0 puntos. No hay nada que transferir.")
        return

    if puntos_a_mover > puntos_disponibles:
        await update.message.reply_text(
            f"⚠️ El origen solo tiene <b>{puntos_disponibles} puntos</b> y quieres mover <b>{puntos_a_mover}</b>.",
            parse_mode="HTML"
        )
        return

    def _h(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    nombre_origen  = _h(data_origen.get("username") or data_origen.get("first_name") or origen_id)
    nombre_destino = _h(data_destino.get("username") or data_destino.get("first_name") or destino_id)
    pts_destino_antes = data_destino.get("points", 0)

    data_origen["points"]  -= puntos_a_mover
    data_destino["points"] += puntos_a_mover
    save_db(db)

    await update.message.reply_text(
        f"✅ <b>Traspaso completado</b>\n\n"
        f"📤 <b>Origen:</b> {nombre_origen} (<code>{origen_id}</code>)\n"
        f"   {puntos_disponibles} → <b>{data_origen['points']} pts</b>\n\n"
        f"📥 <b>Destino:</b> {nombre_destino} (<code>{destino_id}</code>)\n"
        f"   {pts_destino_antes} → <b>{data_destino['points']} pts</b>\n\n"
        f"💰 Transferidos: <b>{puntos_a_mover} puntos</b>",
        parse_mode="HTML"
    )

    try:
        await context.bot.send_message(
            chat_id=int(destino_id),
            text=f"🎉 <b>¡Recibiste puntos!</b>\n\n"
                 f"Un administrador transfirió <b>{puntos_a_mover} puntos</b> a tu cuenta.\n"
                 f"⭐ Tu nuevo saldo: <b>{data_destino['points']} puntos</b> 🐾",
            parse_mode="HTML"
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
        "La ruleta solo está disponible en la Mini App. Toca el botón para abrirla.",
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
        "Las misiones solo están disponibles en la Mini App. Toca el botón para abrirla.",
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
                         f"Tu activación fue aprobada. Ya puedes acceder a todas las misiones 🐆",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        else:
            save_db(db)

        await query.edit_message_text(f"✅ Wallet aprobada. +150 pts enviados al referidor.")
        return

    # ── Retiro de saldo La Manada (moderadores) ──
    if data_str.startswith("retiroOk_") or data_str.startswith("retiroNo_"):
        aprobado = data_str.startswith("retiroOk_")
        # "Ya pague" mueve plata real — solo tesoreria. "Rechazar" no mueve
        # nada (solo devuelve el saldo), lo puede usar cualquier mod.
        if aprobado and query.from_user.id not in TREASURY_IDS:
            await query.answer("❌ Solo tesorería puede confirmar un pago.", show_alert=True)
            return
        if not aprobado and query.from_user.id not in MOD_IDS:
            await query.answer("❌ No tienes permisos de moderador.", show_alert=True)
            return
        target_uid = data_str.split("_", 1)[1]

        db = load_db()
        if target_uid not in db:
            await query.edit_message_text("Usuario no encontrado.")
            return

        udata = db[target_uid]
        usdt = udata.get("manada_retiro_usdt", 0) or 0
        pnt  = udata.get("manada_retiro_pnt", 0) or 0

        udata["manada_retiro_pendiente"] = False
        udata["manada_retiro_usdt"] = 0
        udata["manada_retiro_pnt"]  = 0

        if not aprobado:
            # Rechazado — el saldo vuelve a estar disponible para el usuario
            udata["manada_usdt_balance"] = (udata.get("manada_usdt_balance", 0) or 0) + usdt
            udata["manada_pnt_balance"]  = (udata.get("manada_pnt_balance", 0) or 0) + pnt
        save_db(db)

        try:
            if aprobado:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=f"✅ *¡Retiro pagado!*\n\n"
                         f"Se acreditaron *{usdt} USDT* y *{pnt} PNT* fuera de la app 🐆",
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=f"❌ *Retiro rechazado*\n\n"
                         f"Tu saldo de *{usdt} USDT* y *{pnt} PNT* volvió a tu cuenta. "
                         f"Escribile a un mod si tenés dudas.",
                    parse_mode="Markdown"
                )
        except Exception:
            pass

        estado = "pagado ✅" if aprobado else "rechazado (saldo devuelto) ❌"
        await query.edit_message_text(f"Retiro de {target_uid} — {usdt} USDT / {pnt} PNT — {estado}")
        return

    # ── Aprobar/rechazar captura (moderadores) ──
    if data_str.startswith("approve_") or data_str.startswith("reject_"):
        logger.info(f"Callback mod check: from_user.id={query.from_user.id} type={type(query.from_user.id)} MOD_IDS={MOD_IDS}")
        if query.from_user.id not in MOD_IDS:
            await query.answer("❌ No tienes permisos de moderador.", show_alert=True)
            logger.warning(f"ID {query.from_user.id} no está en MOD_IDS {MOD_IDS}")
            return

        parts = data_str.split("_")
        action = parts[0]
        target_uid = parts[1]
        tipo = "_".join(parts[2:]) if len(parts) > 2 else None

        # Create & Earn: los botones de "Contenido" codifican el monto USDT
        # elegido por el mod como "content|0.05" (ver mission_keyboard arriba).
        monto_usdt = None
        if tipo and tipo.startswith("content|"):
            try:
                monto_usdt = round(float(tipo.split("|", 1)[1]), 2)
            except ValueError:
                monto_usdt = None
            tipo = "content"

        # Stake Challenge: los botones codifican el monto PNT elegido por el
        # mod como "stake|0.5" (ver mission_keyboard arriba).
        monto_pnt = None
        if tipo and tipo.startswith("stake|"):
            try:
                monto_pnt = round(float(tipo.split("|", 1)[1]), 2)
            except ValueError:
                monto_pnt = None
            tipo = "stake"

        db = load_db()
        if target_uid not in db:
            await query.edit_message_text("❌ Usuario no encontrado.")
            return

        mod_name = query.from_user.first_name or str(query.from_user.id)

        if action == "approve" and tipo:
            pts_map = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"], "wallet_activate": PTS["wallet_activate"], "review_store": PTS["review_store"], "review_trust": PTS["review_trust"], "comment_ig": 5, "comment_ig_last": 30, "comment_tt": 5, "comment_tt_last": 30, "follow_emb_emi": PTS["follow_emb_emi"], "follow_emb_lorena": PTS["follow_emb_lorena"], "story_mention": PTS["story_mention"], "first_deposit": PTS["first_deposit"], "stake": 0}
            earned = add_points(db[target_uid], pts_map.get(tipo, 0))

            acreditado_usdt = 0.0
            usdt_mod_line = ""
            usdt_user_line = ""
            if tipo == "content" and monto_usdt:
                acreditado_usdt = add_manada_usdt(db[target_uid], monto_usdt)
                usdt_mod_line = f"\nUSDT acreditados (Manada): *+{acreditado_usdt}*"
                if acreditado_usdt < monto_usdt:
                    usdt_mod_line += " (tope mensual de 10 USDT alcanzado)"
                if acreditado_usdt > 0:
                    usdt_user_line = f"\n💰 *+{acreditado_usdt} USDT* a tu saldo de La Manada 🐆"

            acreditado_pnt = 0.0
            pnt_mod_line = ""
            pnt_user_line = ""
            if tipo == "stake" and monto_pnt:
                acreditado_pnt = add_manada_pnt(db[target_uid], monto_pnt)
                pnt_mod_line = f"\nPNT acreditados (Manada): *+{acreditado_pnt}*"
                if acreditado_pnt > 0:
                    pnt_user_line = f"\n🐾 *+{acreditado_pnt} PNT* a tu saldo de La Manada 🐆"

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
            elif tipo == "follow_emb_emi":
                db[target_uid]["follow_emb_emi"] = True
            elif tipo == "follow_emb_lorena":
                db[target_uid]["follow_emb_lorena"] = True
            elif tipo == "first_deposit":
                db[target_uid]["first_deposit_done"] = True
            elif tipo == "story_mention":
                register_daily_mission(db[target_uid], "story_mention")

            # ── Guardar en historial con tipo correcto ──
            today = date.today().isoformat()
            now_time = datetime.now().strftime("%H:%M")
            if "history" not in db[target_uid]:
                db[target_uid]["history"] = []
            db[target_uid]["history"].append({
                "type": tipo,
                "pts":  earned,
                "date": today,
                "time": now_time,
            })

            save_db(db)

            tipo_label = {"reel": "Reel", "story": "Historia", "content": "Contenido", "wallet_activate": "Activacion de Wallet", "review_store": "Review Store", "review_trust": "Review Trustpilot", "comment_ig": "Comentario IG", "comment_ig_last": "Comentario Ultimo Post IG", "comment_tt": "Comentario TikTok", "comment_tt_last": "Comentario Ultimo Video TikTok", "follow_emb_emi": "Seguir @neodenoche", "follow_emb_lorena": "Seguir @pegandolavuelta", "story_mention": "Historia con mención", "first_deposit": "Primer Depósito", "stake": "Stake Challenge"}
            approve_text = (
                f"✅ *{tipo_label.get(tipo, tipo)} aprobado*\n"
                f"Usuario: `{target_uid}`\n"
                f"Puntos acreditados: *+{earned}*"
                f"{usdt_mod_line}"
                f"{pnt_mod_line}"
            )
            # Confirmar el tap inmediatamente
            answer_text = f"✅ {tipo_label.get(tipo, tipo)} aprobado — +{earned} pts"
            if tipo == "content" and monto_usdt:
                answer_text += f" +{acreditado_usdt} USDT"
            if tipo == "stake" and monto_pnt:
                answer_text += f" +{acreditado_pnt} PNT"
            await query.answer(answer_text)
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
                        f"➕ *+{earned} puntos* acreditados 🐾"
                        f"{usdt_user_line}"
                        f"{pnt_user_line}\n"
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
                        "Asegúrate de que se vea claramente el contenido "
                        "de Panther y vuelve a intentarlo 🐾"
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
            "Todas las misiones y funciones están en la Mini App. Toca el botón para abrirla.",
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
            "❌ No tienes badge de Fundador.\n\n"
            "El badge es exclusivo para los primeros 500 miembros de la Manada 🐾"
        )
        return
    
    await update.message.reply_text("🏆 Generando tu badge...")
    fname = user.first_name or user.username or "Miembro"
    success = await send_founder_badge(context.bot, uid, fname, founder_number)
    if not success:
        await update.message.reply_text("❌ Error generando el badge. Intenta de nuevo.")

async def cmd_enviar_badges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía badges a todos los usuarios existentes — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await query.answer("No tienes permisos.", show_alert=True)
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
                    "Eres oficialmente un Cazador de la Manada\n"
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
                    "Asegúrate de que la imagen muestre Panther Wallet instalada "
                    "y vuelve a mandarla con #NuevoCazador."
                )
            )
        except Exception:
            pass

        nombre_safe2 = str(nombre).replace("_", " ").replace("*", "").replace("`", "")
        await query.edit_message_text(f"❌ Cazador rechazado: @{nombre_safe2} (ID: {target_uid})")



# ═══════════════════════════════════════════════════════════════
# ONBOARDING — Mensajes de bienvenida secuenciales
# ═══════════════════════════════════════════════════════════════

async def send_welcome_sequence(bot, uid: str, first_name: str, source: str = ""):
    """Envía secuencia de bienvenida al usuario nuevo. Si viene del juego, adapta el mensaje."""

    # ── Detectar si viene del juego ──────────────────────────────────────────
    came_from_game = source in ("game", "game-defender")

    # ── MSG 1: Bienvenida + pasos ────────────────────────────────────────────
    if came_from_game:
        intro = (
            f"🎮 *¡Buena partida, {first_name}! Ahora haces parte de la Manada Panther.*\n\n"
            f"PNT Defender no es solo un juego — cada punto que ganas se convierte en "
            f"recompensas reales dentro de la comunidad de Panther Wallet.\n\n"
            f"Para que tus puntos de juego cuenten de verdad, completa estos pasos:\n\n"
        )
    else:
        intro = (
            f"🐆 *¡Bienvenido a la Manada Panther, {first_name}!*\n\n"
            f"Este es el espacio donde la comunidad de Panther Wallet se reúne, "
            f"aprende y gana recompensas reales.\n\n"
            f"Para ser parte oficial de la Manada completa estos pasos:\n\n"
        )

    msg1 = (
        intro +
        f"*Paso 1:* Únete al chat general de la Manada\n"
        f"👉 {LINKS['chat']}\n\n"
        f"*Paso 2:* Descarga Panther Wallet\n"
        f"👉 https://mypanther.io/es/\n\n"
        f"*Paso 3:* Activa tu cuenta y configura el Google 2FA\n"
        f"_(Configuración → Seguridad → Google Authenticator)_\n\n"
        f"*Paso 4:* Toma una captura de pantalla mostrando el 2FA activo\n\n"
        f"*Paso 5:* Envía esa captura aquí al bot con el hashtag *#NuevoCazador*\n\n"
        f"Un moderador la verificará y quedarás oficialmente como Cazador de la Manada 🐾\n\n"
        f"{'🎮 Una vez registrado, tus partidas de PNT Defender acumulan puntos reales automáticamente.' if came_from_game else ''}"
    )

    # ── MSG 2: Reglas (30 seg después) ──────────────────────────────────────
    msg2 = (
        f"📋 *Reglas de la Manada*\n\n"
        f"Para que este espacio funcione bien para todos, seguimos estas reglas:\n\n"
        f"✅ Respeto y buena onda — aquí nos ayudamos entre todos\n"
        f"✅ Las dudas sobre la wallet son bienvenidas — la comunidad responde "
        f"y si no puede, te derivamos al soporte oficial\n"
        f"✅ No spam ni promoción de proyectos externos\n"
        f"✅ Solo contenido relacionado con Panther Wallet y crypto\n"
        f"✅ No FUD, no toxicidad, no comentarios malintencionados\n\n"
        f"⭐ La buena onda se premia — los miembros activos y colaborativos "
        f"acumulan puntos y reconocimiento dentro de la Manada.\n\n"
        f"⚠️ El incumplimiento puede resultar en suspensión del grupo.\n\n"
        f"El equipo de moderación está siempre presente. Ante cualquier duda, escríbenos. 🐆"
    )

    # ── MSG 3: Redes + CTA final (60 seg después) ───────────────────────────
    msg3 = (
        f"🔗 *Síguenos en todas las plataformas*\n\n"
        f"Toda la actividad oficial de Panther Wallet pasa por aquí:\n\n"
        f"🐾 Instagram: {LINKS['ig']}\n"
        f"📺 YouTube: {LINKS['yt']}\n"
        f"🎵 TikTok: {LINKS['tiktok']}\n"
        f"🌐 Sitio web: {LINKS['web']}\n"
        f"📢 Canal oficial: {LINKS['canal']}\n"
        f"💬 Chat general: {LINKS['chat']}\n\n"
        f"Síguenos para no perderte ningún anuncio, sorteo ni novedad 🐆\n\n"
        f"{'🎮 Y cuando quieras jugar de nuevo: go.mypanther.io/game-defender' if came_from_game else ''}"
    )

    try:
        await bot.send_message(chat_id=int(uid), text=msg1, parse_mode="Markdown",
                               disable_web_page_preview=True)
        await asyncio.sleep(30)   # 30 segundos
        await bot.send_message(chat_id=int(uid), text=msg2, parse_mode="Markdown")
        await asyncio.sleep(60)   # 1 minuto
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
    """❌ ELIMINADA junto con la mecánica del evento (ya no se abre cofre)."""
    return {}

# ❌ ELIMINADOS junto con el evento "Operación 1,000 Cazadores":
# cmd_evento_start, cmd_estado_cofre, cmd_cazadores, check_evento_dia,
# evaluar_cierre_evento, abrir_cofre.
# Se mantienen get_evento_state/set_evento_state/get_cazadores_count/get_top_cazadores
# porque los sigue usando el panel de stats y el conteo de referidos verificados.


async def cmd_misiones_recientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra misiones aprobadas/rechazadas de los últimos 2 días — solo mods"""
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("No tienes permisos.")
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


async def cmd_quiensoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Tu ID es: `{user.id}`", parse_mode="Markdown")


async def cmd_emoji_pantera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Misión automática: verifica si el usuario tiene 🐆 o 🐾 en su nombre de TG."""
    if await redirect_to_private(update):
        return
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    if is_once_mission_done(data, "emoji_tg"):
        await update.message.reply_text(
            "✅ Ya completaste esta misión anteriormente.\n"
            "¡Gracias por llevar la Manada en tu nombre! 🐾"
        )
        return

    if check_emoji_tg(user):
        earned = add_points(data, PTS["emoji_tg"])
        data["emoji_tg_done"] = True
        db[uid] = data
        save_db(db)
        await update.message.reply_text(
            f"🐆 *¡Misión completada!*\n\n"
            f"Tienes el emoji de la Manada en tu nombre de Telegram.\n"
            f"*+{earned} puntos* acreditados 🐾\n"
            f"⭐ Total: *{data['points']} puntos*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🐾 *Misión: Emoji Pantera*\n\n"
            "Agrega el emoji 🐆 o 🐾 a tu nombre de Telegram y vuelve a ejecutar /emoji_pantera.\n\n"
            "_Para cambiar tu nombre: Configuración → Editar perfil → Nombre_\n\n"
            "*+20 puntos* por completarla (solo una vez)",
            parse_mode="Markdown"
        )



    if not update.message.reply_to_message:
        await update.message.reply_text("⭐ Responde el mensaje del usuario al que quieres dar una estrella.")
        return

    giver = update.effective_user
    receiver = update.message.reply_to_message.from_user

    if not receiver or receiver.id == giver.id:
        await update.message.reply_text("No puedes darte estrellas a ti mismo 😄")
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
            f"⏳ Ya diste 5 estrellas esta hora. Puedes dar más en {mins} minutos."
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
        await update.message.reply_text("No tienes permisos para usar /award.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("No tienes permisos.")
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
        await update.message.reply_text("Todavia no tienes estrellas. Participa en el chat y otros pueden darte estrellas con /star.")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el ranking del chat general por estrellas"""
    if not CHAT_STARS:
        await update.message.reply_text("🌟 Aún no hay estrellas repartidas. Usa /star para reconocer a alguien!")
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
                "Este mensaje confirma que recibes notificaciones del bot correctamente.\n\n"
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
        await update.message.reply_text("❌ No tienes permisos.")
        return
    db = load_db()
    uid = str(update.effective_user.id)
    if uid in db:
        db[uid]["last_checkin"] = None
        db[uid]["last_ruleta"] = None
        save_db(db)
        await update.message.reply_text("✅ Check-in y ruleta reseteados. Ya puedes probar de nuevo.")
    else:
        await update.message.reply_text("❌ Usuario no encontrado.")

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await redirect_to_private(update):
        return
    await update.message.reply_text(
        "🐆 *CÓMO FUNCIONA LA MANADA PANTHER*\n\n"
        "*Ganas puntos haciendo:*\n"
        "🔥 Check-in diario — mantén la racha\n"
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
        "Usa /niveles para ver la tabla completa\n"
        "Usa /ranking para ver quién va ganando",
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

            weekly_hunt_checkins, weekly_hunt_quiz, weekly_hunt_eligible = get_weekly_hunt_status(data)

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
                "manada_usdt_balance": data.get("manada_usdt_balance", 0),
                "manada_pnt_balance":  data.get("manada_pnt_balance", 0),
                "quiz_today": data.get("manada_last_quiz_date") == today,
                "weekly_hunt_checkins": weekly_hunt_checkins,
                "weekly_hunt_quiz":     weekly_hunt_quiz,
                "weekly_hunt_eligible": weekly_hunt_eligible,
                "seen_intro_v2": bool(data.get("seen_intro_v2", False)),
                "nickname":       data.get("nickname", ""),
                "bio":            data.get("bio", ""),
                "avatar_version": data.get("avatar_version", 0) or 0,
                "manada_retiro_pendiente": bool(data.get("manada_retiro_pendiente", False)),
                "manada_retiro_usdt":      data.get("manada_retiro_usdt", 0),
                "manada_retiro_pnt":       data.get("manada_retiro_pnt", 0),
                "manada_min_retiro_usdt":  MANADA_MIN_RETIRO_USDT,
                "panther_uid":    data.get("panther_uid", ""),
            })

        # ── GET /quiz?id=123456 — Learn & Earn: pregunta del día ──
        elif path == "/quiz":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = db.get(uid)
            if not data:
                return self.send_json({"error": "User not found"}, 404)

            today = date.today().isoformat()
            done  = data.get("manada_last_quiz_date") == today
            question = get_daily_quiz_question(uid, today)

            return self.send_json({
                "already_done": done,
                "question": question["q"],
                "category":  question["cat"],
                "options":   question["opts"],
                "manada_usdt_balance": data.get("manada_usdt_balance", 0),
                "manada_pnt_balance":  data.get("manada_pnt_balance", 0),
            })

        # ── GET /weekly_hunt?id=123456 — Weekly Hunt: progreso + resultado del sorteo pasado ──
        elif path == "/weekly_hunt":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)

            db   = load_db()
            data = db.get(uid)
            if not data:
                return self.send_json({"error": "User not found"}, 404)

            week_ref = _manada_week_ref()
            checkins, quiz, eligible = get_weekly_hunt_status(data, week_ref)

            last_week_ref = _previous_week_ref()
            lw_checkins, lw_quiz, lw_eligible = get_weekly_hunt_status(data, last_week_ref)

            draws = db.get("_global", {}).get("weekly_hunt_draws", {})
            last_week_draw = draws.get(last_week_ref)
            drawn = last_week_draw is not None
            won = False
            usdt_ganado = 0
            pnt_ganado  = 0
            total_winners = 0
            eligible_count = 0
            if drawn:
                total_winners = len(last_week_draw.get("winners", []))
                eligible_count = last_week_draw.get("eligible_count", 0)
                for w in last_week_draw.get("winners", []):
                    if w.get("uid") == uid:
                        won = True
                        usdt_ganado = w.get("usdt", 0)
                        pnt_ganado  = w.get("pnt", 0)
                        break

            return self.send_json({
                "week_ref":  week_ref,
                "checkins":  checkins,
                "checkins_required": WEEKLY_HUNT_CHECKINS_REQUIRED,
                "quiz":      quiz,
                "quiz_required": WEEKLY_HUNT_QUIZ_REQUIRED,
                "eligible":  eligible,
                "pool_usdt": WEEKLY_HUNT_POOL_USDT,
                "pool_pnt":  WEEKLY_HUNT_POOL_PNT,
                "winners_count": WEEKLY_HUNT_WINNERS,
                "last_week": {
                    "week_ref":       last_week_ref,
                    "eligible":       lw_eligible,
                    "drawn":          drawn,
                    "won":            won,
                    "usdt":           usdt_ganado,
                    "pnt":            pnt_ganado,
                    "total_winners":  total_winners,
                    "eligible_count": eligible_count,
                },
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
                    "pos":            i + 1,
                    "id":             u.get("id", ""),
                    "username":       u.get("username", ""),
                    "first_name":     u.get("first_name", ""),
                    "nickname":       u.get("nickname", ""),
                    "avatar_version": u.get("avatar_version", 0) or 0,
                    "points":         u.get("points", 0),
                    "level":          get_level(u.get("points", 0)),
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

        # ── GET /admin/ruleta?key=panther2026 ── ganadores ruleta con campo UID editable
        elif path == "/admin/ruleta":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                self.send_response(403)
                self.end_headers()
                return

            db = load_db()
            # Recolectar todos los giros de ruleta del historial
            all_spins = []
            for uid, data in db.items():
                if uid.startswith("_") or not isinstance(data, dict):
                    continue
                nombre = str(data.get("username") or data.get("first_name") or uid)
                for h in data.get("history", []):
                    if h.get("type") == "ruleta":
                        all_spins.append({
                            "uid":    uid,
                            "nombre": nombre,
                            "fecha":  h.get("date", ""),
                            "hora":   h.get("time", ""),
                            "pts":    h.get("pts", 0),
                            "prize":  h.get("prize", ""),
                            "monto":  h.get("prize_amount", ""),
                        })

            # Filtrar solo giros de la ruleta del dia especificado (param ?fecha=YYYY-MM-DD)
            from datetime import datetime, timedelta
            fecha_param = params.get("fecha", [None])[0]
            if fecha_param:
                fecha_ruleta = fecha_param
            else:
                fecha_ruleta = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
            all_spins = [s for s in all_spins if s["fecha"] == fecha_ruleta]
            # Ordenar por fecha y hora desc
            all_spins.sort(key=lambda x: (x["fecha"], x["hora"]), reverse=True)

            # Agrupar por fecha
            by_date = {}
            for s in all_spins:
                d = s["fecha"] or "sin fecha"
                if d not in by_date:
                    by_date[d] = []
                by_date[d].append(s)

            # Separar por tipo de premio
            usdt_spins = [s for s in all_spins if s["prize"] == "USDT"]
            pnt_spins  = [s for s in all_spins if s["prize"] == "PNT"]
            pts_spins  = [s for s in all_spins if s["prize"] not in ("USDT", "PNT")]

            def build_rows(spins, section, db):
                if not spins:
                    return "<tr><td colspan='6' style='color:#888;text-align:center;padding:16px'>Sin ganadores</td></tr>"
                rows = ""
                for i, s in enumerate(spins):
                    row_id = f"{section}_{i}"
                    if s["prize"] == "USDT":
                        badge = f"<span style='background:#1a3a1a;color:#4ade80;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700'>${s['monto']} USDT</span>"
                    elif s["prize"] == "PNT":
                        badge = f"<span style='background:#1a0a2a;color:#cc88ff;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700'>{s['monto']} PNT</span>"
                    else:
                        badge = f"<span style='background:#1a1a2a;color:#aaa;padding:2px 8px;border-radius:6px;font-size:12px'>+{s['pts']} pts</span>"

                    # UID guardado en DB si existe — leer del usuario, no del spin
                    user_data = db.get(s["uid"], {})
                    saved_uid = user_data.get("panther_uid", "") if isinstance(user_data, dict) else ""
                    uid_style = "background:#111;border:1px solid #333;color:#fff;padding:4px 8px;border-radius:6px;width:160px;font-size:12px"
                    status_html = f"<span id='status_{row_id}' style='font-size:11px;color:" + ("#4ade80" if saved_uid else "#555") + "'>" + ("✅ " + saved_uid if saved_uid else "sin asignar") + "</span>"

                    rows += f"""<tr id='{row_id}'>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee'>{s['fecha']} {s['hora']}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #1e1e1e;font-weight:700'>{s['nombre']}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #1e1e1e;color:#666;font-size:12px'>{s['uid']}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee'>{badge}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee' colspan='2'>
                            <form method='GET' action='/admin/save_ruleta_uid' style='display:flex;gap:6px;align-items:center'>
                                <input type='hidden' name='key' value='panther2026'>
                                <input type='hidden' name='tg_id' value='{s["uid"]}'>
                                <input type='text' name='panther_uid' placeholder='UID Panther Wallet' value='{saved_uid}'
                                    style='background:#fff;border:1px solid #ddd;color:#111;padding:4px 8px;border-radius:6px;width:160px;font-size:12px'>
                                <button type='submit' style='background:#FF5A0E;color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer'>Guardar</button>
                                {"<span style='color:#2e7d32;font-size:11px'>✅ " + saved_uid + "</span>" if saved_uid else ""}
                            </form>
                        </td>
                    </tr>"""
                return rows

            usdt_rows = build_rows(usdt_spins, "usdt", db)
            pnt_rows  = build_rows(pnt_spins, "pnt", db)
            pts_rows  = build_rows(pts_spins, "pts", db)

            th = lambda t: f"<th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>{t}</th>"
            headers = th("Fecha/Hora") + th("Usuario") + th("ID Telegram") + th("Premio") + th("UID Panther Wallet") + th("Estado")

            html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
            <title>Ganadores Ruleta — Manada Panther</title>
            <style>
              body{{background:#fff;color:#111;font-family:sans-serif;padding:24px;max-width:960px;margin:0 auto}}
              h1{{color:#FF5A0E;margin-bottom:4px}}
              h2{{color:#333;font-size:15px;margin:28px 0 12px;padding-bottom:6px;border-bottom:2px solid #FF5A0E}}
              .sub{{color:#888;font-size:13px;margin-bottom:28px}}
              table{{border-collapse:collapse;width:100%;margin-bottom:8px}}
              td{{font-size:13px;color:#111}}
              tr:hover td{{background:#fff8f5}}
              input{{background:#fff;border:1px solid #ddd;color:#111;padding:4px 8px;border-radius:6px;width:160px;font-size:12px}}
              input:focus{{outline:none;border-color:#FF5A0E !important}}
              .saving{{border-color:#FF5A0E !important}}
              .toast{{position:fixed;bottom:20px;right:20px;background:#e6f4ea;color:#2e7d32;padding:10px 18px;border-radius:10px;font-size:13px;display:none;border:1px solid #a5d6a7}}
            </style></head><body>
            <h1>🎰 Ganadores de Ruleta — {fecha_ruleta}</h1>
            <div class='sub'>Manada Panther · {len(all_spins)} giros totales · {len(usdt_spins)} USDT · {len(pnt_spins)} PNT · <a href='/admin/ruleta?key=panther2026&fecha=2026-06-15' style='color:#FF5A0E'>15 Jun</a> · <a href='/admin/ruleta?key=panther2026' style='color:#FF5A0E'>Ayer</a></div>

            <h2>💵 Ganadores USDT — {len(usdt_spins)}</h2>
            <table><tr>{headers}</tr>{usdt_rows}</table>

            <h2>🐾 Ganadores PNT — {len(pnt_spins)}</h2>
            <table><tr>{headers}</tr>{pnt_rows}</table>

            <h2>⭐ Solo puntos — {len(pts_spins)}</h2>
            <table><tr>{headers}</tr>{pts_rows}</table>


            </body></html>"""

            html_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        # ── GET /admin/weekly_hunt?key=panther2026&week=YYYY-Www ── panel del sorteo semanal
        elif path == "/admin/weekly_hunt":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                self.send_response(403)
                self.end_headers()
                return

            db = load_db()
            week_param = params.get("week", [None])[0]
            week_ref = week_param or _previous_week_ref()

            eligibles_uids = get_weekly_hunt_eligible_uids(db, week_ref)
            eligibles_rows = ""
            if eligibles_uids:
                for uid in eligibles_uids:
                    d = db.get(uid, {})
                    nombre = d.get("username") or d.get("first_name") or uid
                    checkins, quiz, _ = get_weekly_hunt_status(d, week_ref)
                    eligibles_rows += f"""<tr>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;font-weight:700'>{nombre}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;color:#666;font-size:12px'>{uid}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center'>{checkins}/{WEEKLY_HUNT_CHECKINS_REQUIRED}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center'>{quiz}/{WEEKLY_HUNT_QUIZ_REQUIRED}</td>
                    </tr>"""
            else:
                eligibles_rows = "<tr><td colspan='4' style='color:#888;text-align:center;padding:16px'>Nadie calificó esta semana</td></tr>"

            draws = db.get("_global", {}).get("weekly_hunt_draws", {})
            ya_sorteado = week_ref in draws

            if ya_sorteado:
                resultado = draws[week_ref]
                winners_rows = ""
                for w in resultado["winners"]:
                    winners_rows += f"""<tr>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;font-weight:700'>🏆 {w['nombre']}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee;color:#666;font-size:12px'>{w['uid']}</td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee'><span style='background:#1a3a1a;color:#4ade80;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700'>+{w['usdt']} USDT</span></td>
                        <td style='padding:8px 12px;border-bottom:1px solid #eee'><span style='background:#1a0a2a;color:#cc88ff;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700'>+{w['pnt']} PNT</span></td>
                    </tr>"""
                if not resultado["winners"]:
                    winners_rows = "<tr><td colspan='4' style='color:#888;text-align:center;padding:16px'>Nadie calificó, no hubo ganadores</td></tr>"
                accion_html = (
                    f"<div class='sub'>✅ Sorteado el {resultado['drawn_at']} por {resultado['drawn_by']} · "
                    f"{resultado['eligible_count']} elegibles</div>"
                    f"<h2>🏆 Ganadores</h2>"
                    f"<table><tr><th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>Usuario</th>"
                    f"<th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>ID Telegram</th>"
                    f"<th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>USDT</th>"
                    f"<th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>PNT</th></tr>{winners_rows}</table>"
                )
            else:
                accion_html = f"""
                    <form method='GET' action='/admin/weekly_hunt_draw' style='margin:16px 0'>
                        <input type='hidden' name='key' value='panther2026'>
                        <input type='hidden' name='week' value='{week_ref}'>
                        <button type='submit' style='background:#FF5A0E;color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:700;cursor:pointer'
                            onclick="return confirm('¿Sortear {WEEKLY_HUNT_WINNERS} ganadores para la semana {week_ref} entre {len(eligibles_uids)} elegibles? Esta acción no se puede deshacer.')">
                            🎲 Sortear ganadores de {week_ref}
                        </button>
                    </form>
                """

            html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
            <title>Weekly Hunt — Manada Panther</title>
            <style>
              body{{background:#fff;color:#111;font-family:sans-serif;padding:24px;max-width:960px;margin:0 auto}}
              h1{{color:#FF5A0E;margin-bottom:4px}}
              h2{{color:#333;font-size:15px;margin:28px 0 12px;padding-bottom:6px;border-bottom:2px solid #FF5A0E}}
              .sub{{color:#888;font-size:13px;margin-bottom:16px}}
              table{{border-collapse:collapse;width:100%;margin-bottom:8px}}
              td{{font-size:13px;color:#111}}
              tr:hover td{{background:#fff8f5}}
            </style></head><body>
            <h1>🏆 Weekly Hunt — {week_ref}</h1>
            <div class='sub'>
                Pool semanal: <b>{WEEKLY_HUNT_POOL_USDT} USDT + {WEEKLY_HUNT_POOL_PNT} PNT</b> ·
                {WEEKLY_HUNT_WINNERS} ganadores al azar
                (≈{round(WEEKLY_HUNT_POOL_USDT/WEEKLY_HUNT_WINNERS,2)} USDT + {round(WEEKLY_HUNT_POOL_PNT/WEEKLY_HUNT_WINNERS,2)} PNT c/u) ·
                requisito: {WEEKLY_HUNT_CHECKINS_REQUIRED} check-ins + {WEEKLY_HUNT_QUIZ_REQUIRED} quiz acertado en la semana ·
                <a href='/admin/weekly_hunt?key=panther2026&week={_previous_week_ref()}' style='color:#FF5A0E'>semana pasada</a>
            </div>

            {accion_html}

            <h2>✅ Elegibles esta semana — {len(eligibles_uids)}</h2>
            <table><tr>
                <th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>Usuario</th>
                <th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>ID Telegram</th>
                <th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>Check-ins</th>
                <th style='background:#fff8f5;color:#FF5A0E;padding:8px 12px;text-align:left;border-bottom:2px solid #FF5A0E;font-size:13px'>Quiz</th>
            </tr>{eligibles_rows}</table>

            </body></html>"""

            html_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        # ── GET /admin/weekly_hunt_draw?key=panther2026&week=YYYY-Www ── dispara el sorteo (accion, redirige) ──
        elif path == "/admin/weekly_hunt_draw":
            key      = params.get("key", [None])[0]
            week_ref = params.get("week", [None])[0]
            if key == "panther2026" and week_ref:
                db = load_db()
                mod_name = "admin_panel"
                already = week_ref in db.get("_global", {}).get("weekly_hunt_draws", {})
                resultado = run_weekly_hunt_draw(db, week_ref, mod_name)
                save_db(db)
                logger.info(f"Weekly Hunt draw for {week_ref}: {len(resultado['winners'])} winners, already_existed={already}")

                if not already and CombinedHandler.tg_app and CombinedHandler.tg_loop:
                    for w in resultado["winners"]:
                        asyncio.run_coroutine_threadsafe(
                            notify_weekly_hunt_winner(CombinedHandler.tg_app, w["uid"], w["usdt"], w["pnt"], week_ref),
                            CombinedHandler.tg_loop
                        )
                    if resultado["winners"]:
                        ganadores_txt = ", ".join(f"{w['nombre']} (+{w['usdt']} USDT, +{w['pnt']} PNT)" for w in resultado["winners"])
                        msg = f"🏆 *Weekly Hunt {week_ref} sorteado*\n\n{resultado['eligible_count']} elegibles.\nGanadores: {ganadores_txt}"
                        asyncio.run_coroutine_threadsafe(
                            notify_mods(CombinedHandler.tg_app, msg),
                            CombinedHandler.tg_loop
                        )
            self.send_response(302)
            self.send_header("Location", f"/admin/weekly_hunt?key=panther2026&week={week_ref}")
            self.end_headers()
            return

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



        # ── GET /admin/save_ruleta_uid ── guarda UID via GET y redirige
        elif path == "/admin/save_ruleta_uid":
            key         = params.get("key", [None])[0]
            tg_id       = params.get("tg_id", [None])[0]
            panther_uid = params.get("panther_uid", [""])[0].strip()
            logger.info(f"save_ruleta_uid GET: tg_id={tg_id} panther_uid={panther_uid}")
            if key == "panther2026" and tg_id:
                db = load_db()
                if tg_id in db:
                    db[tg_id]["panther_uid"] = panther_uid
                    save_db(db)
                    logger.info(f"save_ruleta_uid: saved OK for {tg_id}")
                else:
                    logger.warning(f"save_ruleta_uid: {tg_id} not in DB")
            self.send_response(302)
            self.send_header("Location", "/admin/ruleta?key=panther2026")
            self.end_headers()
            return
        elif path == "/admin/debug":
            key = params.get("key", [None])[0]
            if key != "panther2026":
                return self.send_json({"error": "forbidden"}, 403)
            db = load_db()
            type_counts = {}
            total_history = 0
            for uid, data in db.items():
                if uid.startswith("_") or not isinstance(data, dict):
                    continue
                for h in data.get("history", []):
                    t = h.get("type", "sin_tipo")
                    type_counts[t] = type_counts.get(t, 0) + 1
                    total_history += 1
            return self.send_json({
                "total_history": total_history,
                "total_users": len([u for u in db if not u.startswith("_")]),
                "mission_types": dict(sorted(type_counts.items(), key=lambda x: x[1], reverse=True))
            })

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

            # ── Fallback: reconstruir desde campos booleanos si historial no tiene datos ──
            # Fallback desde historial (nuevas misiones)
            reels_count     = mission_counts.get("reel", 0)
            stories_count   = mission_counts.get("story", 0) + mission_counts.get("historia", 0)
            wallet_count    = mission_counts.get("wallet_activate", 0) or sum(1 for d in users.values() if d.get("wallet_activated"))
            content_count   = mission_counts.get("content", 0)
            follow_ig_count = mission_counts.get("follow_ig", 0) or sum(1 for d in users.values() if d.get("follow_ig"))
            follow_yt_count = mission_counts.get("follow_youtube", 0) or sum(1 for d in users.values() if d.get("follow_youtube"))
            follow_tt_count = mission_counts.get("follow_tiktok", 0) or sum(1 for d in users.values() if d.get("follow_tiktok"))
            follow_x_count  = mission_counts.get("follow_x", 0) or sum(1 for d in users.values() if d.get("follow_x"))
            follow_fb_count = mission_counts.get("follow_facebook", 0) or sum(1 for d in users.values() if d.get("follow_facebook"))
            comment_ig_count = mission_counts.get("comment_ig", 0)
            comment_ig_last  = mission_counts.get("comment_ig_last", 0)
            comment_tt_count = mission_counts.get("comment_tt", 0)
            comment_tt_last  = mission_counts.get("comment_tt_last", 0)

            # Reconstruccion historica desde puntos del historial
            # Buscamos entradas con pts especificos que no sean follow/checkin/ruleta
            pts_30_count = 0  # reels, comentarios IG ultimo, comentarios TT ultimo
            pts_20_count = 0  # historias
            pts_50_count = 0  # contenido propio
            pts_5_count  = 0  # comentarios cortos
            known_types  = {"checkin", "ruleta", "follow_ig", "follow_youtube",
                           "follow_tiktok", "follow_x", "follow_facebook",
                           "reel", "story", "historia", "content", "wallet_activate",
                           "comment_ig", "comment_ig_last", "comment_tt", "comment_tt_last"}
            for d in users.values():
                for h in d.get("history", []):
                    t = h.get("type", "")
                    if t not in known_types and t not in ("referral", "referral_wallet"):
                        pts = h.get("pts", 0)
                        if pts == 30:   pts_30_count += 1
                        elif pts == 20: pts_20_count += 1
                        elif pts == 50: pts_50_count += 1
                        elif pts == 5:  pts_5_count  += 1

            # Usar historial si tiene datos, sino reconstruccion
            if reels_count == 0 and pts_30_count > 0:
                reels_count = pts_30_count
            if stories_count == 0 and pts_20_count > 0:
                stories_count = pts_20_count
            if content_count == 0 and pts_50_count > 0:
                content_count = pts_50_count

            # Ultimo recurso: estimar desde puntos totales restando misiones conocidas
            if reels_count == 0 and stories_count == 0 and content_count == 0:
                pts_checkins  = checkins * 10
                pts_follows   = (follow_ig_count + follow_yt_count + follow_tt_count + follow_x_count + follow_fb_count) * 20
                pts_ruleta    = 0  # ruleta da puntos variables, ignorar
                pts_referidos = sum(d.get("referrals_active", 0) * 150 for d in users.values())
                pts_total     = sum(d.get("points", 0) for d in users.values())
                pts_restantes = max(0, pts_total - pts_checkins - pts_follows - pts_referidos)
                # pts_restantes son misiones de contenido (reels 30, historias 20, comentarios 5-30)
                # Estimacion conservadora
                contenido_estimado = pts_restantes // 25  # promedio ~25 pts por mision de contenido
                if contenido_estimado > 0:
                    reels_count = contenido_estimado

            # ── Referidos del evento ──
            total_cazadores_evento = sum(d.get("cazadores_evento", 0) for d in users.values())
            total_referidos_hist   = sum(d.get("referrals_active", 0) for d in users.values())
            top_evento = sorted(users.items(), key=lambda x: x[1].get("cazadores_evento", 0), reverse=True)[:10]
            top_evento = [(uid, d) for uid, d in top_evento if d.get("cazadores_evento", 0) > 0]

            # ── Origen de usuarios ──
            origen_counts = {}
            for d in users.values():
                src = d.get("source", "directo")
                label = {
                    "camp_ig":   "Instagram",
                    "camp_mail": "Email",
                    "camp_tk":   "TikTok",
                    "camp_web":  "Sitio Web",
                    "referral":  "Referido de usuario",
                    "directo":   "Directo",
                }.get(src, src)
                origen_counts[label] = origen_counts.get(label, 0) + 1

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

            # ── Fechas importantes ──
            ev = get_evento_state()
            fecha_inicio_manada = "28 de abril 2026"
            fecha_inicio_evento = datetime.fromisoformat(ev["start_date"]).strftime("%d de %B %Y") if ev.get("start_date") else "No iniciado"
            dias_evento = (datetime.now() - datetime.fromisoformat(ev["start_date"])).days if ev.get("start_date") else 0
            dias_restantes_evento = max(0, 20 - dias_evento + ev.get("extension", 0))

            # ── Actividad diaria ──
            daily_activity = {}
            for d in users.values():
                for h in d.get("history", []):
                    day = h.get("date", "")
                    if day:
                        daily_activity[day] = daily_activity.get(day, 0) + 1
            top_days = sorted(daily_activity.items(), key=lambda x: x[1], reverse=True)[:7]
            nombre_top_pts = str(top_pts[1].get("username") or top_pts[1].get("first_name") or top_pts[0]) if top_pts[0] else "—"
            nombre_top_mis = str(top_mis[1].get("username") or top_mis[1].get("first_name") or top_mis[0]) if top_mis[0] else "—"

            # Pre-build HTML snippets to avoid backslash in f-strings
            if top_days:
                dias_activos_html = "".join(
                    "<div class='mis-row'><span>" + day + "</span><strong style='color:#FF5A0E'>" + str(count) + " acciones</strong></div>"
                    for day, count in top_days
                )
            else:
                dias_activos_html = "<div class='mis-row'>Sin datos</div>"

            origen_html = "".join(
                "<div class='mis-row'><span>" + label + "</span><strong style='color:#FF5A0E'>" + str(count) + "</strong></div>"
                for label, count in sorted(origen_counts.items(), key=lambda x: x[1], reverse=True)
            ) or "<div class='mis-row'>Sin datos</div>"

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

<h2>Fechas clave</h2>
<div class='grid'>
<div class='card'><div class='card-lbl'>Inicio de la Manada</div><div class='card-name'>{fecha_inicio_manada}</div></div>
<div class='card'><div class='card-lbl'>Inicio del Evento</div><div class='card-name'>{fecha_inicio_evento}</div><div class='card-sub'>Día {dias_evento} · {dias_restantes_evento} días restantes</div></div>
</div>

<h2>Días más activos</h2>
<div style='background:#FFF;border-radius:12px;border:1px solid #EEE;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04)'>
{dias_activos_html}
</div>

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

<h2>Referidos del Evento</h2>
<div class='grid'>
<div class='card'><div class='card-val'>{total_cazadores_evento}</div><div class='card-lbl'>Cazadores del evento</div></div>
<div class='card'><div class='card-val' style='color:#111'>{total_referidos_hist}</div><div class='card-lbl'>Referidos históricos totales</div></div>
</div>
{'<table><tr><th>#</th><th>Usuario</th><th>Cazadores evento</th></tr>' + ''.join(f"<tr><td style='padding:10px 16px;color:#CCC'>{i+1}</td><td style='padding:10px 16px;font-weight:700;color:#111'>{str(d.get('username') or d.get('first_name') or uid).replace('_',' ')}</td><td style='padding:10px 16px;font-weight:700;color:#FF5A0E'>{d.get('cazadores_evento',0)}</td></tr>" for i,(uid,d) in enumerate(top_evento)) + '</table>' if top_evento else "<p style='color:#AAA;font-size:13px'>Sin cazadores verificados aún.</p>"}

<h2>Origen de usuarios</h2>
<div style='background:#FFF;border-radius:12px;border:1px solid #EEE;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04)'>
{origen_html}
</div>

<h2>Misiones por tipo</h2>
<div style='background:#FFF;border-radius:12px;border:1px solid #EEE;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04)'>
<div class='mis-row'><span>🔥 Check-in diario</span><strong style='color:#FF5A0E'>{checkins}</strong></div>
<div class='mis-row'><span>🎰 Ruleta</span><strong style='color:#FF5A0E'>{ruleta_m}</strong></div>
<div class='mis-row'><span>👁 Follow Instagram</span><strong style='color:#FF5A0E'>{follow_ig_count}</strong></div>
<div class='mis-row'><span>👁 Follow YouTube</span><strong style='color:#FF5A0E'>{follow_yt_count}</strong></div>
<div class='mis-row'><span>👁 Follow TikTok</span><strong style='color:#FF5A0E'>{follow_tt_count}</strong></div>
<div class='mis-row'><span>👁 Follow X</span><strong style='color:#FF5A0E'>{follow_x_count}</strong></div>
<div class='mis-row'><span>👁 Follow Facebook</span><strong style='color:#FF5A0E'>{follow_fb_count}</strong></div>
<div class='mis-row'><span>👛 Wallet activada</span><strong style='color:#FF5A0E'>{wallet_count}</strong></div>
<div class='mis-row' style='border-bottom:none'><span>📣 Total misiones sociales (follows + comentarios)</span><strong style='color:#FF5A0E'>{follow_ig_count + follow_yt_count + follow_tt_count + follow_x_count + follow_fb_count + comment_ig_count + comment_ig_last + comment_tt_count + comment_tt_last}</strong></div>
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
                missing = [f"racha de 3 días (tienes {streak})"] if streak < 3 else []
                return self.send_json({
                    "available": False,
                    "reason": "missions",
                    "message": f"Necesitas 3 días de check-in seguidos para girar (racha actual: {streak})",
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

        elif path == "/game-defender":
            try:
                with open("PNT_Defender_v2.html", "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(html.encode())
            except Exception as e:
                self.send_json({"error": f"Game not found: {str(e)}"}, 404)

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

        # ── GET /avatar?id=123456 — sirve la foto de perfil del usuario ──
        elif path == "/avatar":
            uid = params.get("id", [None])[0]
            avatar_path = os.path.join(AVATAR_DIR, f"{uid}.jpg") if uid else None
            if not uid or not os.path.isfile(avatar_path):
                return self.send_json({"error": "No avatar"}, 404)
            try:
                with open(avatar_path, "rb") as f:
                    img_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.send_header("Content-Length", str(len(img_data)))
                self.end_headers()
                self.wfile.write(img_data)
            except Exception as e:
                self.send_json({"error": f"Avatar error: {str(e)}"}, 404)

        # ── GET /profile?id=123456 — perfil PUBLICO de un usuario (lo ven otros) ──
        elif path == "/profile":
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)
            db   = load_db()
            data = db.get(uid)
            if not data:
                return self.send_json({"error": "User not found"}, 404)
            return self.send_json({
                "id":             uid,
                "nickname":       data.get("nickname") or data.get("username") or data.get("first_name") or "Cazador",
                "bio":            data.get("bio", ""),
                "avatar_version": data.get("avatar_version", 0) or 0,
                "points":         data.get("points", 0),
                "level":          get_level(data.get("points", 0)),
                "streak":         data.get("streak", 0),
            })

        elif path == "/music-game":
            try:
                with open("pnt_defender_music.mp3", "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_json({"error": f"Music not found: {str(e)}"}, 404)

        elif path == "/music":
            try:
                with open("music.mp3.mp3", "rb") as f:  # nombre real del archivo en el repo
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_json({"error": f"Music not found: {str(e)}"}, 404)
        # ── /auth/validate — ❌ ELIMINADO junto con la integración Milton/Mundial ──

        elif path == "/debug":
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

        elif path == "/game-leaderboard":
            with get_conn() as conn:
                rows = conn.execute("""
                    SELECT nick, MAX(score) as best, MAX(ts) as last_ts
                    FROM game_scores
                    GROUP BY nick
                    ORDER BY best DESC
                    LIMIT 10
                """).fetchall()
            lb = [{"name": r[0], "score": r[1], "ts": r[2]} for r in rows]
            return self.send_json({"ok": True, "leaderboard": lb})

        elif path == "/user-info":
            # Devuelve el referral_code del usuario para que el juego lo use en el share
            uid = params.get("id", [None])[0]
            if not uid:
                return self.send_json({"ok": False, "error": "no id"})
            db   = load_db()
            data = db.get(uid)
            if not data:
                return self.send_json({"ok": False, "registered": False})
            return self.send_json({
                "ok": True,
                "registered": True,
                "referral_code": data.get("referral_code", ""),
                "name": data.get("first_name", ""),
            })

        else:
            self.send_json({"status": "Panther Mini App API", "version": "1.0"})

    def do_POST(self):
        parsed  = urlparse(self.path)
        path    = parsed.path
        length  = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")
        raw_body = self.rfile.read(length) if length else b""

        # Parse body — JSON or form data
        if "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qs
            form = parse_qs(raw_body.decode("utf-8"))
            body = {k: v[0] for k, v in form.items()}
        else:
            try:
                body = json.loads(raw_body) if raw_body else {}
            except Exception:
                body = {}

        # ── POST /game — PNT Defender ──
        if path == "/game":
            uid     = body.get("id")
            score   = int(body.get("score", 0))
            bot_pts = int(body.get("bot_pts", 0))
            if not uid:
                return self.send_json({"ok": False, "error": "no id"})
            db   = load_db()
            today = date.today().isoformat()
            data = db.get(uid)
            if not data:
                # Usuario jugó pero no está registrado en el bot
                # Le decimos que se una para que sus puntos cuenten
                return self.send_json({
                    "ok": False,
                    "not_registered": True,
                    "join_url": f"https://t.me/ManadaPantherBot?start=game",
                    "message": "¡Buena partida! Uníte a la Manada para que tus puntos cuenten 🐆"
                })
            if data.get("last_game") == today:
                return self.send_json({"ok": False, "already_played": True})
            bot_pts = max(0, min(50, bot_pts))
            earned  = add_points(data, bot_pts)
            data["last_game"] = today
            save_db(db)
            return self.send_json({"ok": True, "earned": earned, "score": score})

        # ── POST /checkin ──
        elif path == "/checkin":
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

            # ── La Manada — Daily Hunt: bonus en USDT según racha + contador semanal ──
            manada_reset_periods_if_needed(data)
            data["manada_checkins_semana"] = (data.get("manada_checkins_semana", 0) or 0) + 1
            usdt_bonus      = get_daily_hunt_bonus_usdt(streak)
            usdt_acreditado = add_manada_usdt(data, usdt_bonus)

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
                "success":       True,
                "earned":        earned,
                "points":        data["points"],
                "streak":        streak,
                "level":         new_lv,
                "level_up":      old_lv != new_lv,
                "bonus":         bonus,
                "manada_usdt_earned":  usdt_acreditado,
                "manada_usdt_balance": data.get("manada_usdt_balance", 0),
            })

        # ── POST /quiz/answer — Learn & Earn: responder la pregunta del día ──
        elif path == "/quiz/answer":
            uid    = body.get("id")
            answer = body.get("answer")
            if not uid or not answer:
                return self.send_json({"error": "Missing id or answer"}, 400)

            db   = load_db()
            data = get_user(db, uid)

            resultado = grade_quiz_answer(data, uid, answer)
            save_db(db)

            resultado["manada_usdt_balance"] = data.get("manada_usdt_balance", 0)
            resultado["manada_pnt_balance"]  = data.get("manada_pnt_balance", 0)
            return self.send_json(resultado)

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

        # ── POST /intro_seen — marca que el usuario ya vio la intro de bienvenida ──
        elif path == "/intro_seen":
            uid = body.get("id")
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)
            db   = load_db()
            data = get_user(db, uid)
            data["seen_intro_v2"] = True
            save_db(db)
            return self.send_json({"status": "ok"})

        # ── POST /profile — actualiza apodo, bio y UID de Panther Wallet ──
        elif path == "/profile":
            uid = body.get("id")
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)
            nickname = (body.get("nickname") or "").strip()[:NICKNAME_MAX_LEN]
            bio      = (body.get("bio") or "").strip()[:BIO_MAX_LEN]
            db   = load_db()
            data = get_user(db, uid)
            data["nickname"] = nickname
            data["bio"]      = bio
            # El UID de Panther Wallet es opcional en este endpoint: solo se
            # actualiza si vino en el body, asi el mismo endpoint sirve para
            # guardar apodo/bio sin pisar el UID por accidente.
            if "panther_uid" in body:
                data["panther_uid"] = (body.get("panther_uid") or "").strip()[:64]
            save_db(db)
            return self.send_json({
                "status": "ok", "nickname": nickname, "bio": bio,
                "panther_uid": data.get("panther_uid", ""),
            })

        # ── POST /profile_photo — sube/reemplaza la foto de perfil ──
        elif path == "/profile_photo":
            uid   = body.get("id")
            image = body.get("image")  # data URL: "data:image/jpeg;base64,...."
            if not uid or not image:
                return self.send_json({"error": "Missing id or image"}, 400)
            try:
                if "," in image:
                    image = image.split(",", 1)[1]
                raw = base64.b64decode(image)
                if len(raw) > AVATAR_MAX_UPLOAD_BYTES:
                    return self.send_json({"error": "Imagen muy pesada (max 2MB)"}, 400)
                img = Image.open(io.BytesIO(raw))
                img = img.convert("RGB")
                # Recortar al centro en cuadrado y redimensionar — todos los
                # avatares quedan del mismo tamano sin importar la foto original.
                w, h = img.size
                side = min(w, h)
                left = (w - side) // 2
                top  = (h - side) // 2
                img = img.crop((left, top, left + side, top + side)).resize((400, 400), Image.LANCZOS)
                out_path = os.path.join(AVATAR_DIR, f"{uid}.jpg")
                img.save(out_path, "JPEG", quality=85)
            except Exception as e:
                logger.error(f"Error procesando avatar de {uid}: {e}")
                return self.send_json({"error": "Imagen invalida"}, 400)

            db   = load_db()
            data = get_user(db, uid)
            data["avatar_version"] = int(data.get("avatar_version", 0) or 0) + 1
            save_db(db)
            return self.send_json({"status": "ok", "avatar_version": data["avatar_version"]})

        # ── POST /request_retiro — pide retirar el saldo acumulado de La Manada ──
        elif path == "/request_retiro":
            uid = body.get("id")
            if not uid:
                return self.send_json({"error": "Missing id"}, 400)
            db   = load_db()
            data = get_user(db, uid)
            if data.get("manada_retiro_pendiente"):
                return self.send_json({"error": "Ya tienes un retiro pendiente"}, 400)
            panther_uid = (data.get("panther_uid") or "").strip()
            if not panther_uid:
                return self.send_json({"error": "Configura tu UID de Panther Wallet en tu Perfil antes de pedir un retiro"}, 400)
            usdt_bal = data.get("manada_usdt_balance", 0) or 0
            pnt_bal  = data.get("manada_pnt_balance", 0) or 0
            if usdt_bal < MANADA_MIN_RETIRO_USDT:
                return self.send_json({"error": f"Necesitas al menos {MANADA_MIN_RETIRO_USDT} USDT para pedir un retiro"}, 400)

            data["manada_retiro_pendiente"] = True
            data["manada_retiro_usdt"] = usdt_bal
            data["manada_retiro_pnt"]  = pnt_bal
            data["manada_usdt_balance"] = 0
            data["manada_pnt_balance"]  = 0
            save_db(db)

            nombre = data.get("nickname") or data.get("username") or data.get("first_name") or uid
            if CombinedHandler.tg_app and CombinedHandler.tg_loop:
                asyncio.run_coroutine_threadsafe(
                    notify_retiro_request(CombinedHandler.tg_app, uid, nombre, panther_uid, usdt_bal, pnt_bal),
                    CombinedHandler.tg_loop
                )
            return self.send_json({"status": "ok", "usdt": usdt_bal, "pnt": pnt_bal})

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

        # ── POST /game-score — Guardar score en leaderboard global ──
        elif path == "/game-score":
            nick  = str(body.get("nick", "AAA"))[:3].upper()
            score = int(body.get("score", 0))
            uid   = str(body.get("id", ""))
            ts    = int(__import__("time").time() * 1000)
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO game_scores (user_id, nick, score, ts) VALUES (?,?,?,?)",
                    (uid, nick, score, ts)
                )
                rows = conn.execute("""
                    SELECT nick, MAX(score) as best, MAX(ts) as last_ts
                    FROM game_scores
                    GROUP BY nick
                    ORDER BY best DESC
                    LIMIT 10
                """).fetchall()
            lb = [{"name": r[0], "score": r[1], "ts": r[2]} for r in rows]
            return self.send_json({"ok": True, "leaderboard": lb})

        # ── /award_points y /auth/token — ❌ ELIMINADOS junto con la integración Milton/Mundial ──

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

    # ── Migración: poblar cazadores_evento desde referidos con wallet activa ──
    def migrate_cazadores_evento():
        EVENTO_START = "2026-05-29"
        db = load_db()
        referidor_counts = {}
        migrated = 0
        for uid, data in db.items():
            if uid.startswith("_") or not isinstance(data, dict):
                continue
            referred_by = data.get("referred_by")
            if not referred_by:
                continue
            if not data.get("wallet_activated"):
                continue
            # Usar joined_at si existe, si no contar igual
            joined_at = data.get("joined_at", "")
            joined_date = joined_at[:10] if joined_at else ""
            # Contar si se unió desde el evento O si no tenemos fecha (beneficio de la duda)
            if not joined_date or joined_date >= EVENTO_START:
                referidor_counts[str(referred_by)] = referidor_counts.get(str(referred_by), 0) + 1
                migrated += 1

        updated = 0
        for ref_uid, count in referidor_counts.items():
            if ref_uid in db:
                if db[ref_uid].get("cazadores_evento", 0) != count:
                    db[ref_uid]["cazadores_evento"] = count
                    updated += 1

        if updated > 0:
            save_db(db)
            logger.info(f"✅ Migración cazadores_evento: {migrated} referidos, {updated} referidores actualizados")
        else:
            logger.info(f"✅ Migración cazadores_evento: sin cambios necesarios")

    migrate_cazadores_evento()
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

    # ❌ ELIMINADO: scheduler del evento (ya estaba deshabilitado con "if False", y el evento ya no existe)

    # ── Antiflood (debe ir PRIMERO, group=-1 para ejecutarse antes que todo) ──
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND,
        antiflood_handler
    ), group=-1)

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
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
    app.add_handler(CommandHandler("transferir", cmd_transferir))
    app.add_handler(CommandHandler("resetcheck", cmd_resetcheck))
    app.add_handler(CommandHandler("dar_puntos", cmd_dar_puntos))
    app.add_handler(CommandHandler("reset_ruleta",  cmd_reset_ruleta))
    app.add_handler(CommandHandler("ganadores_ruleta", cmd_ganadores_ruleta))
    app.add_handler(CommandHandler("stats_referidos", cmd_stats_referidos))
    app.add_handler(CommandHandler("verificar_cazador", cmd_verificar_cazador))
    app.add_handler(CommandHandler("misiones_recientes", cmd_misiones_recientes))
    app.add_handler(CommandHandler("links_campana",   cmd_links_campana))
    # ❌ ELIMINADOS: /evento_start, /estado_cofre, /cazadores (mecánica del evento "Operación 1,000 Cazadores")
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.GROUPS, handle_nuevo_cazador))
    app.add_handler(CallbackQueryHandler(handle_cazador_callback, pattern="^cazador_"))
    app.add_handler(CommandHandler("quiensoy",       cmd_quiensoy))
    app.add_handler(CommandHandler("emoji_pantera",  cmd_emoji_pantera))
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
    # ❌ ELIMINADO: comandos y callback del sorteo del iPhone (sorteo.py)
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
            # ❌ ELIMINADO: scheduler del evento (la mecánica del evento ya no existe)

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
