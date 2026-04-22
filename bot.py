#!/usr/bin/env python3
"""
PANTHER WALLET — MANADA PANTHER GAME BOT
Módulo completo: Bot + API HTTP para Mini App
"""

import os, json, logging, random, asyncio, threading
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
DB_FILE = "/data/panther_db.json"

# ── Moderadores ───────────────────────────────────────────────────────────────
MOD_IDS = [int(x) for x in os.environ.get("MOD_IDS", "8234467845,8249484524").split(",") if x.strip()]

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
}

# ── Niveles actualizados ──────────────────────────────────────────────────────
LEVELS = [
    (0,     149,   "🐾 Cachorro"),
    (150,   499,   "🔍 Rastreador"),
    (500,   999,   "🛡️ Guardián"),
    (1000,  1999,  "🧭 Explorador"),
    (2000,  4999,  "⚡ Embajador"),
    (5000,  9999,  "🐆 Alfa"),
    (10000, 999999,"👑 Leyenda"),
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

# ── DB ────────────────────────────────────────────────────────────────────────
DB_LOCK = threading.Lock()

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    with DB_LOCK:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(db, uid: str, user=None):
    if uid not in db:
        code = uid[-6:] if len(uid) >= 6 else uid
        db[uid] = {
            "id": uid,
            "username": user.username if user else "",
            "first_name": user.first_name if user else "",
            "points": 0,
            "streak": 0,
            "last_checkin": None,
            "last_ruleta": None,
            "double_pts_until": None,
            "referral_code": code,
            "referred_by": None,
            "referrals": [],
            "joined_at": datetime.now().isoformat(),
            "usdt_won_month": None,
            "pnt_won_month": None,
        }
    elif user:
        db[uid]["username"] = user.username or db[uid].get("username","")
        db[uid]["first_name"] = user.first_name or db[uid].get("first_name","")
    # Asegurar campos nuevos en usuarios existentes
    if "usdt_won_month" not in db[uid]:
        db[uid]["usdt_won_month"] = None
    if "pnt_won_month" not in db[uid]:
        db[uid]["pnt_won_month"] = None
    # Fix referrals if stored as int instead of list
    if not isinstance(db[uid].get("referrals"), list):
        db[uid]["referrals"] = []
    return db[uid]

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
    return (
        data.get("reel_verified", False) and
        data.get("story_verified", False) and
        data.get("referrals_active", 0) >= 1
    )

def get_available_spins(data):
    base = 1
    bonus = 0
    if data.get("has_virtual_card"): bonus += 2
    if data.get("has_physical_card"): bonus += 3
    if data.get("big_transaction"): bonus += 4
    return min(base + bonus, 3)

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

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    is_new = uid not in db
    data = get_user(db, uid, user)

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
        # Match both numeric code and full PANTH-XXXXXX format
        for rid, rdata in db.items():
            r_code = rdata.get("referral_code", "")
            if (r_code == ref_code or r_code == f"PANTH-{ref_code}") and rid != uid:
                data["referred_by"] = rid
                if uid not in rdata["referrals"]:
                    rdata["referrals"].append(uid)
                    earned = add_points(rdata, PTS["referral_join"])
                    db[rid] = rdata

                    # Notify referrer
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

                    # Check milestone and notify group
                    total = len([u for u in db.values() if isinstance(u, dict) and "points" in u])
                    await check_member_milestone(context.bot, total)
                break

    save_db(db)

    level = get_level(data["points"])
    next_lv, pts_needed = get_next_level(data["points"])

    app_url = f"https://go.mypanther.io/app?id={uid}"

    if is_new:
        text = (
            f"🐆 *¡Bienvenido a la Manada Panther, {user.first_name}!*\n\n"
            f"🏅 Nivel: *{level}*\n"
            f"⭐ Puntos: *{data['points']}*\n\n"
            f"📢 Canal oficial: t.me/pantherwalletoficial\n"
            f"💬 Chat comunidad: t.me/manadapanther\n\n"
            f"_Completa misiones, refiere amigos y gana premios en PNT y USDT 💰_"
        )
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
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"
    save_db(db)
    await update.message.reply_text("✅ Ruleta ACTIVADA manualmente")

async def cmd_ruleta_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "off"
    save_db(db)
    await update.message.reply_text("🔴 Ruleta DESACTIVADA manualmente")

async def cmd_ruleta_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = None
    save_db(db)
    await update.message.reply_text("🔄 Ruleta en modo AUTOMÁTICO (días 15 y 30)")

# ── /broadcast (moderadores) ──────────────────────────────────────────────────
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
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

    msg = f"🎰 *¡GIRASTE LA RULETA!*\n\n"

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
            f"⚡ *¡PUNTOS DOBLES POR 24 HORAS!*\n"
            f"Todas tus acciones de hoy valen el doble 🔥\n"
            f"⭐ Puntos actuales: *{data['points']}*"
        )
    elif special == "usdt":
        if has_won_this_month(data, "usdt"):
            # Ya ganó USDT este mes — dar puntos en su lugar
            earned = add_points(data, 50)
            msg += (
                f"⭐ *+{earned} puntos*\n"
                f"⭐ Total: *{data['points']} puntos*"
            )
        else:
            prize = get_usdt_prize()
            if prize:
                mark_won_month(data, "usdt")
                msg += (
                    f"💵 *¡PREMIO EN EFECTIVO!*\n\n"
                    f"Ganaste: *{prize} USDT*\n\n"
                    f"El equipo de Panther te va a contactar para coordinar el pago.\n"
                    f"Guardá este mensaje como comprobante 🎉\n\n"
                    f"_⚠️ Solo podés ganar USDT una vez por mes._"
                )
                # Notificar a moderadores
                for mod_id in MOD_IDS:
                    try:
                        name = user.username or user.first_name
                        await context.bot.send_message(
                            chat_id=mod_id,
                            text=f"💵 *Premio USDT ganado*\n\n"
                                 f"Usuario: @{name} (ID: {uid})\n"
                                 f"Premio: *{prize} USDT*\n"
                                 f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.warning(f"No se pudo notificar mod {mod_id}: {e}")
            else:
                earned = add_points(data, 50)
                msg += f"⭐ *+{earned} puntos*\n⭐ Total: *{data['points']} puntos*"

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
                f"Los tokens serán acreditados en tu Panther Wallet. "
                f"El equipo te contactará para confirmar 🎉\n\n"
                f"_⚠️ Solo podés ganar PNT una vez por mes._"
            )
            for mod_id in MOD_IDS:
                try:
                    name = user.username or user.first_name
                    await context.bot.send_message(
                        chat_id=mod_id,
                        text=f"🐾 *Premio PNT ganado*\n\n"
                             f"Usuario: @{name} (ID: {uid})\n"
                             f"Premio: *{pnt_amount} PNT*\n"
                             f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
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
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "on"
    save_db(db)
    await update.message.reply_text("✅ Ruleta ACTIVADA manualmente")

async def cmd_ruleta_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = "off"
    save_db(db)
    await update.message.reply_text("🔴 Ruleta DESACTIVADA manualmente")

async def cmd_ruleta_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
        return
    db = load_db()
    if "_global" not in db:
        db["_global"] = {}
    db["_global"]["ruleta_override"] = None
    save_db(db)
    await update.message.reply_text("🔄 Ruleta en modo AUTOMÁTICO (días 15 y 30)")

# ── /broadcast (moderadores) ──────────────────────────────────────────────────
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in MOD_IDS:
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
    
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)

    name = f"@{user.username}" if user.username else user.first_name

    # Check if this is a wallet activation proof
    if data.get("pending_wallet_proof"):
        data["pending_wallet_proof"] = False
        save_db(db)

        await update.message.reply_text(
            f"✅ *¡Captura recibida!* Gracias {name}.\n\n"
            f"Un moderador verificará tu activación de wallet en las próximas 24h.\n\n"
            f"_Cuando se apruebe, tu referidor recibirá sus puntos_ 🐆",
            parse_mode="Markdown"
        )

        # Notify mods with wallet proof buttons
        referred_by = data.get("referred_by")
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(
                    chat_id=mod_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"✅ Aprobar wallet (+150 pts al referidor)",
                        callback_data=f"wallet_{uid}_{referred_by}"
                    )],
                    [InlineKeyboardButton(
                        "❌ Rechazar",
                        callback_data=f"reject_{uid}"
                    )]
                ])
                await context.bot.send_message(
                    chat_id=mod_id,
                    text=f"🔐 *Prueba de wallet*\n\n"
                         f"Usuario: {name} (ID: {uid})\n"
                         f"Referido por: {referred_by or 'N/A'}\n\n"
                         f"¿Aprobar activación de wallet?",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Error notifying mod: {e}")
        return

    save_db(db)

    await update.message.reply_text(
        f"📸 ¡Captura recibida! Gracias {name}.\n\n"
        f"Un moderador verificará tu misión y acreditará los puntos en las próximas 24h.\n\n"
        f"_Seguí acumulando con /checkin y /ruleta mientras tanto 🐾_",
        parse_mode="Markdown"
    )

    # Notificar a moderadores con botones inline
    for mod_id in MOD_IDS:
        try:
            await context.bot.forward_message(
                chat_id=mod_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"✅ Reel (+{PTS['share_reel']} pts)",
                        callback_data=f"approve_{uid}_reel"
                    ),
                    InlineKeyboardButton(
                        f"✅ Historia (+{PTS['share_story']} pts)",
                        callback_data=f"approve_{uid}_story"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"✅ Contenido (+{PTS['own_content']} pts)",
                        callback_data=f"approve_{uid}_content"
                    ),
                    InlineKeyboardButton(
                        "❌ Rechazar",
                        callback_data=f"reject_{uid}"
                    ),
                ]
            ])
            await context.bot.send_message(
                chat_id=mod_id,
                text=f"📸 *Captura de verificación*\n"
                     f"Usuario: {name} (ID: `{uid}`)\n"
                     f"Puntos actuales: *{data['points']}*\n\n"
                     f"Seleccioná la acción:",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.warning(f"No se pudo notificar al mod {mod_id}: {e}")

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
    pts_map    = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"]}

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
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data

    # ── Aprobar wallet (moderadores) ──
    if cb.startswith("wallet_"):
        parts = cb.split("_")
        target_uid = parts[1]
        referrer_uid = parts[2] if len(parts) > 2 else None

        db = load_db()

        # Mark wallet activated for referred user
        if target_uid in db:
            db[target_uid]["wallet_activated"] = True

        # Give +150 pts to referrer
        if referrer_uid and referrer_uid in db:
            earned = add_points(db[referrer_uid], PTS["referral_wallet"])
            db[referrer_uid]["referrals_active"] = db[referrer_uid].get("referrals_active", 0) + 1
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
        if query.from_user.id not in MOD_IDS:
            await query.edit_message_text("❌ No tenés permisos de moderador.")
            return

        parts = data_str.split("_")
        action = parts[0]
        target_uid = parts[1]
        tipo = parts[2] if len(parts) > 2 else None

        db = load_db()
        if target_uid not in db:
            await query.edit_message_text("❌ Usuario no encontrado.")
            return

        if action == "approve" and tipo:
            pts_map = {"reel": PTS["share_reel"], "story": PTS["share_story"], "content": PTS["own_content"]}
            earned = add_points(db[target_uid], pts_map.get(tipo, 0))
            save_db(db)

            tipo_label = {"reel": "Reel", "story": "Historia", "content": "Contenido"}
            await query.edit_message_text(
                f"✅ *{tipo_label.get(tipo, tipo)} aprobado*\n"
                f"Usuario: `{target_uid}`\n"
                f"Puntos acreditados: *+{earned}*",
                parse_mode="Markdown"
            )
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

        elif action == "reject":
            save_db(db)
            await query.edit_message_text(
                f"❌ *Captura rechazada*\nUsuario: `{target_uid}`",
                parse_mode="Markdown"
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=f"❌ Tu captura no pudo ser verificada.\n\n"
                         f"Asegurate de que se vea claramente el contenido de Panther y volvé a intentarlo 🐾",
                )
            except Exception:
                pass
        return

    # ── Navegación del menú principal ──
    handlers = {
        "checkin":  cmd_checkin,
        "puntos":   cmd_puntos,
        "ranking":  cmd_ranking,
        "ruleta":   cmd_ruleta,
        "compartir": cmd_compartir,
        "broadcast":  cmd_broadcast,
        "ruleta_on":  cmd_ruleta_on,
        "verificar_follow": cmd_verificar_follow,
        "ruleta_off": cmd_ruleta_off,
        "ruleta_auto": cmd_ruleta_auto,
        "misiones": cmd_misiones,
        "referido": cmd_referido,
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
        "🐾 Cachorro (0-149) → 🔍 Rastreador (150-499)\n"
        "🛡️ Guardián (500-999) → 🧭 Explorador (1K-1.9K)\n"
        "⚡ Embajador (2K-4.9K) → 🐆 Alfa (5K-9.9K) → 👑 Leyenda (10K+)\n\n"
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
                "next_level":     next_lv,
                "pts_to_next":    pts_needed,
                "referrals":         len(data.get("referrals", [])),
                "referrals_active":  data.get("referrals_active", 0),
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
                "usdt_won_month": has_won_this_month(data, "usdt"),
                "pnt_won_month":  has_won_this_month(data, "pnt"),
                "history":        history,
            })

        # ── GET /ranking ──
        elif path == "/ranking":
            db      = load_db()
            sorted_ = sorted(db.values(), key=lambda x: x["points"], reverse=True)
            top20   = sorted_[:20]
            # Filter out _global key
            valid = [u for u in sorted_ if isinstance(u, dict) and "points" in u]
            top20 = valid[:20]
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

        # ── GET /missions?id=123456 ──
        elif path == "/ruleta":
            uid = body.get("id")
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
                missing = []
                if not data.get("reel_verified"): missing.append("reel verificado")
                if not data.get("story_verified"): missing.append("historia verificada")
                if data.get("referrals_active", 0) < 1: missing.append("1 referido activo")
                return self.send_json({
                    "available": False,
                    "reason": "missions",
                    "message": "Completa todas las misiones para desbloquear la ruleta",
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

            if special == "usdt":
                if data.get("usdt_won_month"):
                    # Ya ganó USDT este mes → puntos en su lugar
                    pts_gain = 50
                    result_label = f"🎰 {result_label} → +{pts_gain} pts"
                    special = None
                else:
                    data["usdt_won_month"] = True
                    prize_type = "USDT"
                    prize_amount = result_label
            elif special == "pnt":
                if data.get("pnt_won_month"):
                    pts_gain = 30
                    result_label = f"🎰 {result_label} → +{pts_gain} pts"
                    special = None
                else:
                    data["pnt_won_month"] = True
                    prize_type = "PNT"
                    prize_amount = result_label

            earned = add_points(data, pts_gain)

            if "history" not in data:
                data["history"] = []
            data["history"].append({
                "type": "ruleta",
                "pts": earned,
                "date": today,
                "time": datetime.now().strftime("%H:%M"),
                "prize": prize_type
            })

            db[uid] = data
            save_db(db)

            # Notify mods if economic prize
            if prize_type and CombinedHandler.tg_app:
                username = data.get("username") or data.get("first_name") or uid
                msg = (
                    f"🎰 *Premio de Ruleta*\n\n"
                    f"👤 Usuario: {username} (ID: {uid})\n"
                    f"🏆 Premio: *{prize_amount} {prize_type}*\n"
                    f"📅 Fecha: {today}\n\n"
                    f"_Verificar y procesar el pago_"
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

    app = Application.builder().token(TOKEN).build()

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
