"""
╔══════════════════════════════════════════════════════════════════╗
║         SORTEO MANADA PANTHER — Módulo completo                 ║
║  Integrar en bot.py:                                            ║
║    1. from sorteo import *  (al inicio de bot.py)               ║
║    2. init_sorteo_db()      (dentro de init_db())               ║
║    3. Registrar handlers    (dentro de main(), ver abajo)       ║
╚══════════════════════════════════════════════════════════════════╝

HANDLERS A AGREGAR EN main() de bot.py:
────────────────────────────────────────
    app.add_handler(CommandHandler("sorteo",          cmd_sorteo_info))
    app.add_handler(CommandHandler("sorteo_entrar",   cmd_sorteo_entrar))
    app.add_handler(CommandHandler("sorteo_estado",   cmd_sorteo_estado))
    app.add_handler(CommandHandler("sorteo_lista",    cmd_sorteo_lista))
    app.add_handler(CommandHandler("sorteo_activar",  cmd_sorteo_activar))
    app.add_handler(CommandHandler("sorteo_cancelar", cmd_sorteo_cancelar))
    app.add_handler(CallbackQueryHandler(handle_sorteo_callback, pattern="^sorteo_"))

VARIABLE DE ENTORNO NECESARIA:
────────────────────────────────
    ADMIN_SORTEO_ID  → tu Telegram user_id (Emi) para aprobar capturas
                       si no se setea, usa el primer MOD_ID
"""

import random
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ── Configuración del sorteo ──────────────────────────────────────────────────
SORTEO_MIN_PARTICIPANTES = 50
SORTEO_USDT_POR_TICKET   = 100   # cada 100 USDT en PNT = 1 ticket
SORTEO_PREMIOS = {
    1: "📱 iPhone 16 (~$800 USD)",
    2: "🧥 Merch Premium: buzo + termo (~$150 USD)",
    3: "👕 Merch: camiseta + termo (~$100 USD)",
}

# ── Estados del sorteo ────────────────────────────────────────────────────────
SORTEO_ESTADO_ABIERTO    = "abierto"
SORTEO_ESTADO_CERRADO    = "cerrado"
SORTEO_ESTADO_FINALIZADO = "finalizado"

# ── Tipos de captura pendiente ────────────────────────────────────────────────
SORTEO_FOTO_COMPRA   = "sorteo_compra"
SORTEO_FOTO_STAKING  = "sorteo_staking"


# ══════════════════════════════════════════════════════════════════════════════
# ── Base de datos ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def init_sorteo_db():
    """
    Crea las 3 tablas del sorteo si no existen.
    Llamar dentro de init_db() en bot.py.
    """
    from bot import get_conn  # importa la conexión compartida del bot
    with get_conn() as conn:
        # ── Participantes ──────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sorteo_participantes (
                user_id         TEXT PRIMARY KEY,
                username        TEXT DEFAULT '',
                first_name      TEXT DEFAULT '',
                tickets         INTEGER DEFAULT 0,
                usdt_declarados REAL DEFAULT 0,
                pnt_cantidad    REAL DEFAULT 0,
                wallet          TEXT DEFAULT '',
                status          TEXT DEFAULT 'pendiente',
                -- pendiente | aprobado | rechazado | descalificado
                foto_compra_id  TEXT DEFAULT '',
                foto_staking_id TEXT DEFAULT '',
                foto_compra_ok  INTEGER DEFAULT 0,
                foto_staking_ok INTEGER DEFAULT 0,
                joined_at       TEXT,
                aprobado_at     TEXT,
                notas_admin     TEXT DEFAULT ''
            )
        """)
        # ── Configuración global del sorteo ───────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sorteo_config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                estado      TEXT DEFAULT 'cerrado',
                inicio_at   TEXT,
                cierre_at   TEXT,
                min_partic  INTEGER DEFAULT 50,
                activo_por  TEXT DEFAULT 'Emiliano Torres'
            )
        """)
        # Insertar fila única de config si no existe
        conn.execute("""
            INSERT OR IGNORE INTO sorteo_config (id, estado, min_partic)
            VALUES (1, 'cerrado', 50)
        """)
        # ── Ganadores ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sorteo_ganadores (
                lugar       INTEGER PRIMARY KEY,
                user_id     TEXT,
                username    TEXT,
                first_name  TEXT,
                tickets     INTEGER,
                premio      TEXT,
                sorteado_at TEXT
            )
        """)
        conn.commit()
    logger.info("✅ Tablas del sorteo inicializadas")


def _get_config():
    """Retorna la config actual del sorteo como dict."""
    from bot import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sorteo_config WHERE id=1").fetchone()
        return dict(row) if row else {}


def _set_config(**kwargs):
    """Actualiza campos en sorteo_config."""
    from bot import get_conn
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values())
    with get_conn() as conn:
        conn.execute(f"UPDATE sorteo_config SET {sets} WHERE id=1", vals)
        conn.commit()


def _get_participante(user_id: str):
    from bot import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sorteo_participantes WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def _upsert_participante(user_id, **kwargs):
    from bot import get_conn
    kwargs["user_id"] = user_id
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" * len(kwargs))
    updates = ", ".join(f"{k}=excluded.{k}" for k in kwargs if k != "user_id")
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO sorteo_participantes ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {updates}",
            list(kwargs.values())
        )
        conn.commit()


def _count_aprobados():
    from bot import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM sorteo_participantes WHERE status='aprobado'"
        ).fetchone()
        return row["c"] if row else 0


def _get_all_aprobados():
    from bot import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sorteo_participantes WHERE status='aprobado'"
        ).fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# ── Helpers ───────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _es_admin(user_id: int) -> bool:
    """Verifica si el usuario es admin del sorteo."""
    import os
    from bot import MOD_IDS
    admin_id = int(os.environ.get("ADMIN_SORTEO_ID", MOD_IDS[0] if MOD_IDS else 0))
    return user_id == admin_id or user_id in MOD_IDS


def _nombre(user) -> str:
    return user.first_name or user.username or str(user.id)


# ══════════════════════════════════════════════════════════════════════════════
# ── Comandos públicos ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_sorteo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo — Muestra info completa del sorteo, reglas y estado actual.
    Funciona en privado y en grupos.
    """
    config = _get_config()
    estado = config.get("estado", "cerrado")
    aprobados = _count_aprobados()
    faltan = max(0, SORTEO_MIN_PARTICIPANTES - aprobados)

    estado_emoji = {
        "abierto":    "🟢 ABIERTO",
        "cerrado":    "🔴 CERRADO",
        "finalizado": "🏆 FINALIZADO",
    }.get(estado, "⚪ Desconocido")

    premios_txt = "\n".join(
        f"  {lugar}° lugar → {premio}"
        for lugar, premio in SORTEO_PREMIOS.items()
    )

    if estado == "abierto":
        participacion = (
            f"👥 Participantes confirmados: *{aprobados}*\n"
            f"{'✅ ¡Mínimo alcanzado! El sorteo está asegurado.' if aprobados >= SORTEO_MIN_PARTICIPANTES else f'⏳ Faltan {faltan} para confirmar el sorteo.'}"
        )
        cta = "\n\n👉 ¿Querés participar? Usá /sorteo\\_entrar"
    else:
        participacion = f"👥 Participantes confirmados: *{aprobados}*"
        cta = "\n\n⏳ El sorteo no está activo aún. Seguí atento a la Manada 🐆"

    texto = f"""🏆 *SORTEO MANADA PANTHER*
━━━━━━━━━━━━━━━━━━━━━
Estado: *{estado_emoji}*

🎁 *PREMIOS*
{premios_txt}

📋 *CÓMO PARTICIPAR*
  • Comprá PNT por mínimo *100 USDT*
  • Poné esos PNT en *staking* en la app
  • Registrate con /sorteo\\_entrar y subí 2 capturas
  • Cada 100 USDT invertidos = 1 ticket 🎫
  • Podés acumular tickets comprando más

⚠️ *REGLAS*
  • El staking debe mantenerse *60 días completos*
  • Retiro anticipado = pérdida automática de tickets
  • Mínimo {SORTEO_MIN_PARTICIPANTES} participantes para activar el sorteo
  • Sorteo público y en vivo en Telegram

{participacion}{cta}"""

    await update.effective_message.reply_text(texto, parse_mode="Markdown")


async def cmd_sorteo_entrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_entrar — Inicia el proceso de registro al sorteo.
    Solo en privado. El bot pide las 2 capturas secuencialmente.
    """
    user = update.effective_user
    chat = update.effective_chat

    # Solo en privado
    if chat.type != "private":
        bot_info = await context.bot.get_me()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🐆 Abrir en privado", url=f"https://t.me/{bot_info.username}")
        ]])
        await update.message.reply_text(
            "👋 Este comando funciona en privado 👇",
            reply_markup=kb
        )
        return

    config = _get_config()
    if config.get("estado") != SORTEO_ESTADO_ABIERTO:
        await update.message.reply_text(
            "⏳ El sorteo no está activo en este momento.\n"
            "Seguí atento al grupo para saber cuándo arranca 🐆"
        )
        return

    uid = str(user.id)
    participante = _get_participante(uid)

    # Ya está aprobado — permitir sumar más tickets
    if participante and participante.get("status") == "aprobado":
        tickets = participante.get("tickets", 0)
        await update.message.reply_text(
            f"🎫 Ya tenés *{tickets} ticket{'s' if tickets != 1 else ''}* en el sorteo.\n\n"
            f"Si compraste más PNT, podés sumar tickets enviando las capturas de la nueva compra.\n\n"
            f"📝 *¿Cuántos USDT adicionales compraste?*\n"
            f"Respondé con un número (ej: `100`, `250`)\n\n"
            f"_Ingresá solo el monto de la compra nueva, no el total anterior._",
            parse_mode="Markdown"
        )
        context.user_data["sorteo_step"] = "esperando_usdt"
        context.user_data["sorteo_acumulando"] = True
        return

    # Ya tiene capturas pendientes
    if participante and participante.get("status") == "pendiente":
        await update.message.reply_text(
            "⏳ Tus capturas ya fueron enviadas y están *esperando aprobación*.\n\n"
            "Te avisamos cuando queden confirmadas 🐆",
            parse_mode="Markdown"
        )
        return

    # Iniciar registro — pedir cantidad de USDT
    await update.message.reply_text(
        "🎫 *SORTEO MANADA PANTHER — Registro*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Para participar necesitás:\n"
        "  1️⃣ Haber comprado PNT por mínimo *100 USDT*\n"
        "  2️⃣ Tener esos PNT en *staking* en la app de Panther\n\n"
        "📝 *¿Cuántos USDT invertiste en PNT?*\n"
        "Respondé con un número (ej: `100`, `250`, `500`)\n\n"
        "_Cada 100 USDT = 1 ticket. El mínimo es 100 USDT._",
        parse_mode="Markdown"
    )

    # Guardar estado de conversación
    context.user_data["sorteo_step"] = "esperando_usdt"


async def handle_sorteo_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Maneja la respuesta de texto durante el flujo de registro.
    Llamar desde handle_message en bot.py.
    Retorna True si consumió el mensaje, False si no era del sorteo.
    """
    step = context.user_data.get("sorteo_step")
    if not step or step != "esperando_usdt":
        return False

    user = update.effective_user
    texto = update.message.text.strip().replace(",", ".")

    try:
        usdt = float(texto)
    except ValueError:
        await update.message.reply_text(
            "❌ Ingresá solo el número (ej: `200`)",
            parse_mode="Markdown"
        )
        return True

    if usdt < SORTEO_USDT_POR_TICKET:
        await update.message.reply_text(
            f"❌ El mínimo para participar es *{SORTEO_USDT_POR_TICKET} USDT*.\n"
            f"Comprá más PNT y volvé cuando estés listo 🐆",
            parse_mode="Markdown"
        )
        return True

    tickets = int(usdt // SORTEO_USDT_POR_TICKET)
    context.user_data["sorteo_usdt"] = usdt
    context.user_data["sorteo_tickets"] = tickets
    context.user_data["sorteo_step"] = "esperando_foto_compra"

    await update.message.reply_text(
        f"✅ Con *{usdt:.0f} USDT* te corresponden *{tickets} ticket{'s' if tickets != 1 else ''}* 🎫\n\n"
        "📸 *Paso 1 de 2 — Captura de compra*\n"
        "Enviá una captura de pantalla que muestre la *compra de PNT* "
        f"por {usdt:.0f} USDT o más en la app de Panther Wallet.\n\n"
        "_La imagen debe mostrar claramente el monto y el token PNT._",
        parse_mode="Markdown"
    )
    return True


async def handle_sorteo_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Maneja las fotos enviadas durante el flujo de registro.
    Llamar desde handle_photo en bot.py ANTES de la lógica existente.
    Retorna True si consumió la foto, False si no era del sorteo.
    """
    step = context.user_data.get("sorteo_step")
    if step not in ("esperando_foto_compra", "esperando_foto_staking"):
        return False

    if update.effective_chat.type != "private":
        return False

    import os
    from bot import MOD_IDS, MOD_GROUP_ID, get_conn, DB_LOCK

    user = update.effective_user
    uid = str(user.id)
    foto = update.message.photo[-1]  # la más grande
    file_id = foto.file_id

    if step == "esperando_foto_compra":
        context.user_data["sorteo_foto_compra"] = file_id
        context.user_data["sorteo_step"] = "esperando_foto_staking"

        await update.message.reply_text(
            "✅ *Captura de compra recibida.*\n\n"
            "📸 *Paso 2 de 2 — Captura de staking*\n"
            "Ahora enviá una captura que muestre tus PNT *en staking activo* "
            "dentro de la app de Panther Wallet.\n\n"
            "_La imagen debe mostrar el monto stakeado._",
            parse_mode="Markdown"
        )
        return True

    elif step == "esperando_foto_staking":
        foto_compra_id  = context.user_data.get("sorteo_foto_compra", "")
        foto_staking_id = file_id
        usdt            = context.user_data.get("sorteo_usdt", 0)
        tickets_nuevos  = context.user_data.get("sorteo_tickets", 0)
        acumulando      = context.user_data.get("sorteo_acumulando", False)
        username        = user.username or ""
        first_name      = user.first_name or ""

        # Si está acumulando, sumar tickets al total anterior
        participante_actual = _get_participante(uid)
        if acumulando and participante_actual:
            tickets_anteriores = participante_actual.get("tickets", 0)
            usdt_anteriores    = participante_actual.get("usdt_declarados", 0)
            tickets            = tickets_anteriores + tickets_nuevos
            usdt_total         = usdt_anteriores + usdt
        else:
            tickets      = tickets_nuevos
            usdt_total   = usdt

        # Guardar en DB como pendiente
        _upsert_participante(
            uid,
            username        = username,
            first_name      = first_name,
            tickets         = tickets,
            usdt_declarados = usdt_total,
            wallet          = "",
            status          = "pendiente",
            foto_compra_id  = foto_compra_id,
            foto_staking_id = foto_staking_id,
            foto_compra_ok  = 0,
            foto_staking_ok = 0,
            joined_at       = datetime.now().isoformat(),
        )

        # Limpiar estado de conversación
        for key in ("sorteo_step", "sorteo_usdt", "sorteo_tickets", "sorteo_foto_compra", "sorteo_acumulando"):
            context.user_data.pop(key, None)

        # Notificar al usuario
        if acumulando:
            msg_usuario = (
                f"🎫 *¡Compra adicional enviada!*\n\n"
                f"📊 Resumen:\n"
                f"  • USDT nuevos: *{usdt:.0f} USDT*\n"
                f"  • Tickets nuevos: *+{tickets_nuevos} 🎫*\n"
                f"  • Total de tickets (si se aprueba): *{tickets} 🎫*\n\n"
                f"⏳ Tus capturas están siendo revisadas.\n\n"
                f"_Recordá mantener el staking activo durante 60 días._"
            )
        else:
            msg_usuario = (
                f"🎫 *¡Listo! Tu registro fue enviado.*\n\n"
                f"📊 Resumen:\n"
                f"  • USDT declarados: *{usdt:.0f} USDT*\n"
                f"  • Tickets: *{tickets} 🎫*\n\n"
                f"⏳ Tus capturas están siendo revisadas. "
                f"Te notificamos cuando queden aprobadas.\n\n"
                f"_Recordá mantener el staking activo durante 60 días._"
            )
        await update.message.reply_text(msg_usuario, parse_mode="Markdown")

        # Notificar al grupo de mods con ambas fotos y botones
        nombre_display = f"@{username}" if username else first_name
        tipo_solicitud = "➕ *ACUMULACIÓN DE TICKETS*" if acumulando else "🎫 *SORTEO — Nueva solicitud*"
        caption_compra = (
            f"{tipo_solicitud}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {nombre_display} (`{uid}`)\n"
            f"💰 USDT nuevos: *{usdt:.0f}*\n"
            f"🎫 Tickets nuevos: *+{tickets_nuevos}*\n"
            f"🎟 Total si se aprueba: *{tickets}*\n\n"
            f"📸 *Captura 1/2 — Compra de PNT*"
        )

        kb_compra = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Compra OK", callback_data=f"sorteo_foto_ok_compra_{uid}"),
            InlineKeyboardButton("❌ Rechazar compra", callback_data=f"sorteo_foto_bad_compra_{uid}"),
        ]])

        kb_staking = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Staking OK", callback_data=f"sorteo_foto_ok_staking_{uid}"),
            InlineKeyboardButton("❌ Rechazar staking", callback_data=f"sorteo_foto_bad_staking_{uid}"),
        ], [
            InlineKeyboardButton("✅✅ APROBAR TODO", callback_data=f"sorteo_aprobar_{uid}"),
            InlineKeyboardButton("❌ RECHAZAR TODO", callback_data=f"sorteo_rechazar_{uid}"),
        ]])

        try:
            await context.bot.send_photo(
                chat_id    = MOD_GROUP_ID,
                photo      = foto_compra_id,
                caption    = caption_compra,
                parse_mode = "Markdown",
                reply_markup = kb_compra,
            )
            await context.bot.send_photo(
                chat_id    = MOD_GROUP_ID,
                photo      = foto_staking_id,
                caption    = f"📸 *Captura 2/2 — Staking activo*\n👤 {nombre_display} (`{uid}`)",
                parse_mode = "Markdown",
                reply_markup = kb_staking,
            )
        except Exception as e:
            logger.error(f"Error notificando admin sorteo: {e}")

        return True

    return False


async def cmd_sorteo_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_estado — El usuario ve su estado actual en el sorteo.
    """
    user = update.effective_user
    uid  = str(user.id)

    if update.effective_chat.type != "private":
        bot_info = await context.bot.get_me()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🐆 Ver mi estado en privado", url=f"https://t.me/{bot_info.username}")
        ]])
        await update.message.reply_text("👋 Este comando funciona en privado 👇", reply_markup=kb)
        return

    p = _get_participante(uid)

    if not p:
        await update.message.reply_text(
            "❌ No estás registrado en el sorteo.\n\n"
            "Usá /sorteo para ver las reglas y /sorteo\\_entrar para participar 🎫",
            parse_mode="Markdown"
        )
        return

    status_emoji = {
        "pendiente":      "⏳ Pendiente de aprobación",
        "aprobado":       "✅ Aprobado",
        "rechazado":      "❌ Rechazado",
        "descalificado":  "🚫 Descalificado",
    }.get(p["status"], p["status"])

    texto = (
        f"🎫 *TU ESTADO EN EL SORTEO*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Estado: *{status_emoji}*\n"
        f"USDT declarados: *{p.get('usdt_declarados', 0):.0f} USDT*\n"
        f"Tickets: *{p.get('tickets', 0)} 🎫*\n"
        f"Registrado: {p.get('joined_at', '')[:10]}\n"
    )

    if p.get("notas_admin"):
        texto += f"\n📝 Nota: _{p['notas_admin']}_"

    if p["status"] == "rechazado":
        texto += "\n\nPodés volver a intentarlo con /sorteo\\_entrar"

    await update.message.reply_text(texto, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# ── Comandos admin ─────────────────────────────────────────════════════════════
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_sorteo_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_reset <user_id> — Admin: resetea el estado de un usuario a 'rechazado'
    para que pueda volver a registrarse en el sorteo.
    """
    user = update.effective_user
    if not _es_admin(user.id):
        await update.message.reply_text("❌ Solo admins.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: `/sorteo_reset <user_id>`\nEjemplo: `/sorteo_reset 840016974`",
            parse_mode="Markdown"
        )
        return

    uid = args[0].strip()
    p = _get_participante(uid)

    if not p:
        await update.message.reply_text(f"❌ No se encontró ningún participante con ID `{uid}`.", parse_mode="Markdown")
        return

    _upsert_participante(uid, status="rechazado", notas_admin="Reset manual por admin")
    await update.message.reply_text(
        f"✅ Usuario `{uid}` reseteado.\n"
        f"Ahora puede volver a hacer `/sorteo_entrar` para registrarse de nuevo.",
        parse_mode="Markdown"
    )

async def cmd_sorteo_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_lista — Admin: lista de todos los participantes con su estado.
    """
    user = update.effective_user
    if not _es_admin(user.id):
        await update.message.reply_text("❌ Solo admins.")
        return

    from bot import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sorteo_participantes ORDER BY joined_at DESC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 No hay participantes aún.")
        return

    status_icons = {
        "pendiente":     "⏳",
        "aprobado":      "✅",
        "rechazado":     "❌",
        "descalificado": "🚫",
    }

    aprobados  = sum(1 for r in rows if r["status"] == "aprobado")
    pendientes = sum(1 for r in rows if r["status"] == "pendiente")
    total_tickets = sum(r["tickets"] for r in rows if r["status"] == "aprobado")

    lineas = [
        f"🎫 *PARTICIPANTES DEL SORTEO*\n"
        f"✅ Aprobados: {aprobados}/{SORTEO_MIN_PARTICIPANTES} mínimo | ⏳ Pendientes: {pendientes}\n"
        f"🎟 Total tickets en juego: {total_tickets}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for r in rows:
        nombre = f"@{r['username']}" if r["username"] else r["first_name"] or r["user_id"]
        icon   = status_icons.get(r["status"], "❓")
        lineas.append(
            f"{icon} {nombre} — {r['tickets']} ticket{'s' if r['tickets'] != 1 else ''} "
            f"({r['usdt_declarados']:.0f} USDT)"
        )

    # Telegram tiene límite de 4096 chars, dividir si es necesario
    texto = "\n".join(lineas)
    if len(texto) > 4000:
        chunks = [texto[i:i+4000] for i in range(0, len(texto), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_sorteo_activar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_activar — Admin: abre el sorteo (acepta registros).
    /sorteo_activar correr — corre el sorteo aleatorio y anuncia ganadores.
    """
    user = update.effective_user
    if not _es_admin(user.id):
        await update.message.reply_text("❌ Solo admins.")
        return

    args = context.args or []
    config = _get_config()

    # ── Modo "correr": ejecutar el sorteo ─────────────────────────────────
    if args and args[0].lower() == "correr":
        aprobados = _get_all_aprobados()
        if len(aprobados) < SORTEO_MIN_PARTICIPANTES:
            await update.message.reply_text(
                f"❌ No se puede correr el sorteo.\n"
                f"Participantes aprobados: *{len(aprobados)}/{SORTEO_MIN_PARTICIPANTES}*\n\n"
                f"Faltan {SORTEO_MIN_PARTICIPANTES - len(aprobados)} participantes.",
                parse_mode="Markdown"
            )
            return

        # Construir pool ponderado por tickets
        pool = []
        for p in aprobados:
            pool.extend([p] * p["tickets"])

        ganadores = []
        usados = set()
        for lugar in range(1, 4):
            candidatos = [p for p in pool if p["user_id"] not in usados]
            if not candidatos:
                break
            ganador = random.choice(candidatos)
            usados.add(ganador["user_id"])
            ganadores.append((lugar, ganador))

        # Guardar ganadores
        from bot import get_conn
        ahora = datetime.now().isoformat()
        with get_conn() as conn:
            conn.execute("DELETE FROM sorteo_ganadores")
            for lugar, g in ganadores:
                premio = SORTEO_PREMIOS.get(lugar, "Premio")
                conn.execute("""
                    INSERT OR REPLACE INTO sorteo_ganadores
                    (lugar, user_id, username, first_name, tickets, premio, sorteado_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (lugar, g["user_id"], g["username"], g["first_name"],
                      g["tickets"], premio, ahora))
            conn.commit()

        _set_config(estado=SORTEO_ESTADO_FINALIZADO, cierre_at=ahora)

        # Armar mensaje de ganadores
        import os
        from bot import MOD_IDS, MAIN_GROUP_ID
        lineas_ganadores = []
        for lugar, g in ganadores:
            nombre = f"@{g['username']}" if g["username"] else g["first_name"] or g["user_id"]
            premio = SORTEO_PREMIOS.get(lugar, "Premio")
            lineas_ganadores.append(f"  {lugar}° lugar → {nombre} — {premio}")

        anuncio = (
            f"🏆 *¡GANADORES DEL SORTEO MANADA PANTHER!* 🏆\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n".join(lineas_ganadores) +
            f"\n\n🎫 Total de participantes: *{len(aprobados)}*\n"
            f"🎟 Total de tickets: *{len(pool)}*\n\n"
            f"¡Gracias a toda la Manada por participar! 🐆🔥"
        )

        await update.message.reply_text(anuncio, parse_mode="Markdown")

        # Anunciar en el grupo principal
        try:
            await context.bot.send_message(
                chat_id    = MAIN_GROUP_ID,
                text       = anuncio,
                parse_mode = "Markdown"
            )
        except Exception as e:
            logger.error(f"Error anunciando ganadores en grupo: {e}")

        # Notificar a cada ganador en privado
        for lugar, g in ganadores:
            premio = SORTEO_PREMIOS.get(lugar, "Premio")
            try:
                await context.bot.send_message(
                    chat_id    = int(g["user_id"]),
                    text       = (
                        f"🎉 *¡FELICITACIONES!*\n\n"
                        f"Ganaste el *{lugar}° lugar* en el Sorteo Manada Panther 🏆\n\n"
                        f"Premio: *{premio}*\n\n"
                        f"El equipo de Panther se va a contactar con vos a la brevedad 🐆"
                    ),
                    parse_mode = "Markdown"
                )
            except Exception as e:
                logger.warning(f"No se pudo notificar ganador {g['user_id']}: {e}")

        return

    # ── Modo normal: abrir el sorteo ───────────────────────────────────────
    if config.get("estado") == SORTEO_ESTADO_ABIERTO:
        await update.message.reply_text(
            "ℹ️ El sorteo ya está abierto.\n\n"
            "Para correrlo usá: /sorteo\\_activar correr",
            parse_mode="Markdown"
        )
        return

    _set_config(estado=SORTEO_ESTADO_ABIERTO, inicio_at=datetime.now().isoformat())

    await update.message.reply_text(
        "✅ *Sorteo activado.* Ahora los usuarios pueden registrarse con /sorteo\\_entrar\n\n"
        "Cuando quieras correrlo: /sorteo\\_activar correr",
        parse_mode="Markdown"
    )

    # Anunciar en el grupo
    from bot import MAIN_GROUP_ID
    try:
        await context.bot.send_message(
            chat_id    = MAIN_GROUP_ID,
            text       = (
                "🎫 *¡EL SORTEO MANADA PANTHER ESTÁ ABIERTO!* 🎫\n\n"
                "🏆 *Premios:*\n"
                "  1° lugar → 📱 iPhone 16\n"
                "  2° lugar → 🧥 Merch Premium\n"
                "  3° lugar → 👕 Merch\n\n"
                "📋 *Cómo participar:*\n"
                "  • Comprá PNT por mínimo 100 USDT\n"
                "  • Poné esos PNT en staking en la app\n"
                "  • Mandá /sorteo al bot para registrarte\n\n"
                "Cada 100 USDT = 1 ticket 🎟\n"
                "¡Más tickets = más chances! 🐆🔥"
            ),
            parse_mode = "Markdown"
        )
    except Exception as e:
        logger.error(f"Error anunciando sorteo en grupo: {e}")


async def cmd_sorteo_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sorteo_cancelar — Admin: cierra el sorteo sin correrlo.
    """
    user = update.effective_user
    if not _es_admin(user.id):
        await update.message.reply_text("❌ Solo admins.")
        return

    _set_config(estado=SORTEO_ESTADO_CERRADO)
    await update.message.reply_text("🔴 Sorteo cerrado. Los registros están pausados.")


# ══════════════════════════════════════════════════════════════════════════════
# ── Callback handler (botones de aprobación) ─────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def handle_sorteo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Maneja los botones inline del panel de aprobación de capturas.
    Patrón: ^sorteo_
    """
    query  = update.callback_query
    user   = query.from_user
    data   = query.data

    if not _es_admin(user.id):
        await query.answer("❌ Solo admins pueden aprobar.", show_alert=True)
        return

    await query.answer()

    # ── sorteo_foto_ok_compra_{uid} ───────────────────────────────────────
    if data.startswith("sorteo_foto_ok_compra_"):
        uid = data.replace("sorteo_foto_ok_compra_", "")
        _upsert_participante(uid, foto_compra_ok=1)
        p = _get_participante(uid)
        if p and p.get("foto_staking_ok"):
            _upsert_participante(uid, status="aprobado", aprobado_at=datetime.now().isoformat())
            await query.edit_message_caption(
                caption="✅ Captura de *compra* aprobada — Ambas OK, participante *APROBADO* 🎫",
                parse_mode="Markdown"
            )
            await _notificar_aprobado(context, uid, p)
        else:
            await query.edit_message_caption(
                caption="✅ Captura de *compra* aprobada. Esperando staking.",
                parse_mode="Markdown"
            )

    # ── sorteo_foto_ok_staking_{uid} ──────────────────────────────────────
    elif data.startswith("sorteo_foto_ok_staking_"):
        uid = data.replace("sorteo_foto_ok_staking_", "")
        _upsert_participante(uid, foto_staking_ok=1)
        p = _get_participante(uid)
        if p and p.get("foto_compra_ok"):
            _upsert_participante(uid, status="aprobado", aprobado_at=datetime.now().isoformat())
            await query.edit_message_caption(
                caption="✅ Captura de *staking* aprobada — Ambas OK, participante *APROBADO* 🎫",
                parse_mode="Markdown"
            )
            await _notificar_aprobado(context, uid, p)
        else:
            await query.edit_message_caption(
                caption="✅ Captura de *staking* aprobada. Esperando compra.",
                parse_mode="Markdown"
            )

    # ── sorteo_aprobar_{uid} — aprobar todo de una ────────────────────────
    elif data.startswith("sorteo_aprobar_"):
        uid = data.replace("sorteo_aprobar_", "")
        p   = _get_participante(uid)
        if not p:
            await query.edit_message_caption(caption="❌ Participante no encontrado.")
            return
        _upsert_participante(
            uid,
            foto_compra_ok  = 1,
            foto_staking_ok = 1,
            status          = "aprobado",
            aprobado_at     = datetime.now().isoformat()
        )
        aprobados = _count_aprobados()
        await query.edit_message_caption(
            caption=(
                f"✅✅ *APROBADO* — {p.get('first_name') or p.get('username') or uid}\n"
                f"🎫 Tickets: {p.get('tickets')}\n"
                f"👥 Total aprobados: {aprobados}/{SORTEO_MIN_PARTICIPANTES}"
            ),
            parse_mode="Markdown"
        )
        await _notificar_aprobado(context, uid, p)

    # ── sorteo_rechazar_{uid} ─────────────────────────────────────────────
    elif data.startswith("sorteo_rechazar_"):
        uid = data.replace("sorteo_rechazar_", "")
        p   = _get_participante(uid)
        if not p:
            await query.edit_message_caption(caption="❌ Participante no encontrado.")
            return
        _upsert_participante(uid, status="rechazado")
        nombre = p.get("first_name") or p.get("username") or uid
        await query.edit_message_caption(
            caption=f"❌ *RECHAZADO* — {nombre}",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                chat_id    = int(uid),
                text       = (
                    "❌ *Tu solicitud al sorteo fue rechazada.*\n\n"
                    "Las capturas enviadas no cumplieron los requisitos.\n\n"
                    "Podés volver a intentarlo con /sorteo\\_entrar asegurándote de:\n"
                    "  • Mostrar claramente la compra de PNT\n"
                    "  • Mostrar el staking activo en la app\n\n"
                    "Cualquier duda escribí al grupo 🐆"
                ),
                parse_mode = "Markdown"
            )
        except Exception as e:
            logger.warning(f"No se pudo notificar rechazo a {uid}: {e}")

    # ── sorteo_foto_bad_compra / sorteo_foto_bad_staking ──────────────────
    elif data.startswith("sorteo_foto_bad_"):
        tipo = "compra" if "compra" in data else "staking"
        uid  = data.split("_")[-1]
        await query.edit_message_caption(
            caption=f"⚠️ Captura de {tipo} marcada como inválida.\nUsá 'RECHAZAR TODO' para rechazar al usuario.",
            parse_mode="Markdown"
        )


async def _notificar_aprobado(context, uid: str, participante: dict):
    """Notifica al usuario que fue aprobado."""
    tickets = participante.get("tickets", 0)
    try:
        await context.bot.send_message(
            chat_id    = int(uid),
            text       = (
                f"🎉 *¡Tu registro al sorteo fue APROBADO!* 🎫\n\n"
                f"Tickets asignados: *{tickets} 🎫*\n\n"
                f"⚠️ Importante:\n"
                f"  • Mantené el staking activo durante los *60 días* completos\n"
                f"  • Si retirás antes, perdés los tickets automáticamente\n\n"
                f"¡Mucha suerte en el sorteo! 🐆🔥\n"
                f"Seguí el estado con /sorteo\\_estado"
            ),
            parse_mode = "Markdown"
        )
    except Exception as e:
        logger.warning(f"No se pudo notificar aprobación a {uid}: {e}")
