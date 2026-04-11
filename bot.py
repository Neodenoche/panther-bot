#!/usr/bin/env python3
"""
PANTHER WALLET — MANADA PANTHER GAME BOT
Módulo 1: Base, registro, puntos, check-in con racha
"""

import os, json, logging, random
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN   = os.environ.get("BOT_TOKEN", "")
DB_FILE = "panther_db.json"

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
    "share_story":      20,
    "own_content":     100,
}

# ── Niveles ───────────────────────────────────────────────────────────────────
LEVELS = [
    (0,    99,    "🐾 Cachorro"),
    (100,  299,   "🔍 Rastreador"),
    (300,  599,   "🛡️ Guardián"),
    (600,  999,   "🧭 Explorador"),
    (1000, 2000,  "⚡ Embajador"),
    (2001, 5000,  "🐆 Alfa"),
    (5001, 999999,"👑 Leyenda"),
]

# ── Ruleta ────────────────────────────────────────────────────────────────────
RULETA = [
    ("+50 puntos",                    50,  None,  40),
    ("+100 puntos",                  100,  None,  25),
    ("+200 puntos",                  200,  None,  15),
    ("⚡ Puntos dobles por 24h",       0,  "x2",  12),
    ("💵 Premio $5 USDT",              0,  "$5",   5),
    ("💵 Premio $25 USDT",             0,  "$25",  2),
    ("💵 Premio $50 USDT",             0,  "$50",  1),
]

def spin_ruleta():
    pool = []
    for item in RULETA:
        pool.extend([item] * item[3])
    return random.choice(pool)

# ── DB ────────────────────────────────────────────────────────────────────────
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
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
        }
    elif user:
        db[uid]["username"] = user.username or db[uid].get("username","")
        db[uid]["first_name"] = user.first_name or db[uid].get("first_name","")
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
    """Suma puntos respetando doble puntos si está activo"""
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

# ── Teclado principal ─────────────────────────────────────────────────────────
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check-in diario", callback_data="checkin")],
        [
            InlineKeyboardButton("📊 Mis puntos", callback_data="puntos"),
            InlineKeyboardButton("🏆 Ranking", callback_data="ranking"),
        ],
        [
            InlineKeyboardButton("🎰 Ruleta", callback_data="ruleta"),
            InlineKeyboardButton("📋 Misiones", callback_data="misiones"),
        ],
        [InlineKeyboardButton("🎫 Mi código referido", callback_data="referido")],
    ])

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    is_new = uid not in db
    data = get_user(db, uid, user)

    # Procesar código de referido
    if context.args and is_new:
        ref_code = context.args[0]
        for rid, rdata in db.items():
            if rdata.get("referral_code") == ref_code and rid != uid:
                data["referred_by"] = rid
                if uid not in rdata["referrals"]:
                    rdata["referrals"].append(uid)
                    earned = add_points(rdata, PTS["referral_join"])
                    try:
                        await context.bot.send_message(
                            chat_id=int(rid),
                            text=f"🎉 *¡Nuevo referido!*\n\n"
                                 f"{user.first_name} se unió con tu código.\n"
                                 f"*+{earned} puntos* para vos 🐾",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                break

    save_db(db)

    level = get_level(data["points"])
    next_lv, pts_needed = get_next_level(data["points"])

    text = (
        f"{'🐆 *¡Bienvenido a la Manada Panther!*' if is_new else f'🐾 *¡Hola, {user.first_name}!*'}\n\n"
        f"🏅 Nivel: *{level}*\n"
        f"⭐ Puntos: *{data['points']}*\n"
        f"🔥 Racha: *{data['streak']} días*\n"
        f"{'📈 Próximo: *' + next_lv + '* — ' + str(pts_needed) + ' pts' if next_lv else '👑 Nivel máximo'}\n\n"
        f"_Hacé check-in cada día, referí amigos y subí en el ranking para ganar recompensas en PNT y USDT 💰_"
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

# ── /checkin ──────────────────────────────────────────────────────────────────
async def do_checkin(uid: str, user, context):
    db   = load_db()
    data = get_user(db, uid, user)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last = data.get("last_checkin")

    if last == today:
        return (
            f"⏰ Ya hiciste tu check-in hoy.\n\n"
            f"🔥 Racha: *{data['streak']} días*\n"
            f"Volvé mañana para no perderla.",
            False
        )

    # Calcular racha
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
    elif special in ("$5", "$25", "$50"):
        amount = special
        msg += (
            f"💵 *¡PREMIO EN EFECTIVO!*\n\n"
            f"Ganaste: *{amount} USDT*\n\n"
            f"El equipo de Panther te va a contactar para coordinar el pago. "
            f"Guardá este mensaje como comprobante 🎉"
        )

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

    checkin_hoy  = "✅" if data.get("last_checkin")  == today else "⬜"
    ruleta_hoy   = "✅" if data.get("last_ruleta")   == today else "⬜"

    await update.message.reply_text(
        f"📋 *MISIONES DE HOY*\n\n"
        f"{checkin_hoy} *Check-in diario* → /checkin\n"
        f"_+5 a +10 pts · bonus por racha_\n\n"
        f"{ruleta_hoy} *Ruleta diaria* → /ruleta\n"
        f"_Puntos, x2 o premios en USDT_\n\n"
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

# ── /compartir — verificación de capturas ─────────────────────────────────────
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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe capturas para verificación de misiones sociales"""
    user = update.effective_user
    db   = load_db()
    uid  = str(user.id)
    data = get_user(db, uid, user)
    save_db(db)

    name = f"@{user.username}" if user.username else user.first_name

    await update.message.reply_text(
        f"📸 ¡Captura recibida! Gracias {name}.\n\n"
        f"Un moderador verificará tu misión y acreditará los puntos en las próximas 24h.\n\n"
        f"_Seguí acumulando con /checkin y /ruleta mientras tanto 🐾_",
        parse_mode="Markdown"
    )

    # Notificar a moderadores (agregar IDs de mods acá)
    MOD_IDS = [int(x) for x in os.environ.get("MOD_IDS", "").split(",") if x.strip()]
    for mod_id in MOD_IDS:
        try:
            await context.bot.forward_message(
                chat_id=mod_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
            await context.bot.send_message(
                chat_id=mod_id,
                text=f"📸 Captura de verificación\n"
                     f"Usuario: {name} (ID: {uid})\n"
                     f"Puntos actuales: {data['points']}\n\n"
                     f"Respondé con:\n"
                     f"`/aprobar {uid} reel` — +{PTS['share_reel']} pts\n"
                     f"`/aprobar {uid} story` — +{PTS['share_story']} pts\n"
                     f"`/aprobar {uid} content` — +{PTS['own_content']} pts",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"No se pudo notificar al mod {mod_id}: {e}")

# ── /aprobar — comando para moderadores ──────────────────────────────────────
async def cmd_aprobar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /aprobar USER_ID tipo"""
    MOD_IDS = [int(x) for x in os.environ.get("MOD_IDS", "").split(",") if x.strip()]
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("❌ No tenés permisos para usar este comando.")
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

# ── Callbacks ─────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    fake = type('obj', (object,), {
        'effective_user': query.from_user,
        'effective_chat': query.message.chat,
        'message':        query.message,
    })()

    handlers = {
        "checkin":  cmd_checkin,
        "puntos":   cmd_puntos,
        "ranking":  cmd_ranking,
        "ruleta":   cmd_ruleta,
        "misiones": cmd_misiones,
        "referido": cmd_referido,
    }

    if query.data in handlers:
        fake_update = type('Update', (), {
            'effective_user': query.from_user,
            'effective_chat': query.message.chat,
            'message':        query.message,
            'callback_query': query,
        })()
        await handlers[query.data](fake_update, context)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("❌ Falta BOT_TOKEN en las variables de entorno")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("checkin",   cmd_checkin))
    app.add_handler(CommandHandler("puntos",    cmd_puntos))
    app.add_handler(CommandHandler("ranking",   cmd_ranking))
    app.add_handler(CommandHandler("referido",  cmd_referido))
    app.add_handler(CommandHandler("ruleta",    cmd_ruleta))
    app.add_handler(CommandHandler("misiones",  cmd_misiones))
    app.add_handler(CommandHandler("compartir", cmd_compartir))
    app.add_handler(CommandHandler("aprobar",   cmd_aprobar))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🐆 Panther Game Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
