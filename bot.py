#!/usr/bin/env python3
"""
PANTHER WALLET — MANADA PANTHER GAME BOT
v2.0 — Incluye Operación 1,000 Cazadores
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

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN       = os.environ.get("BOT_TOKEN", "")
DB_FILE     = "/data/panther_db.json"
SQLITE_FILE = "/data/panther.db"
MOD_IDS     = [int(x) for x in os.environ.get("MOD_IDS", "8234467845,8249484524,1769405650,5605380987,1781826630").split(",") if x.strip()]
MOD_GROUP_ID = int(os.environ.get("MOD_GROUP_ID", "-3777494908"))

CAMPAIGN_SOURCES = {"camp_ig":"Instagram","camp_mail":"Email","camp_tk":"TikTok","camp_web":"Sitio Web"}
PENDING_MISSIONS: dict = {}
STAR_COOLDOWN: dict = {}
CHAT_STARS: dict = {}
CHAT_AWARDS: dict = {}

PTS = {
    "checkin_1_3":5,"checkin_4_6":10,"streak_7":50,"streak_14":150,"streak_30":500,
    "referral_join":25,"referral_wallet":150,"share_reel":30,"follow_ig":15,
    "follow_x":15,"follow_tiktok":15,"follow_facebook":15,"follow_youtube":15,
    "follow_all_bonus":20,"share_story":20,"own_content":100,"wallet_activate":175,
    "review_store":175,"review_trust":175,
}

LEVELS = [
    (0,149,"🐾 Cachorro"),(150,499,"🔍 Rastreador"),(500,999,"🛡️ Guardián"),
    (1000,2999,"🧭 Explorador"),(3000,6999,"⚡ Embajador"),(7000,14999,"🦁 Leyenda"),
    (15000,29999,"🔥 Elite"),(30000,59999,"💎 Diamante"),(60000,124999,"👑 Rey de la Manada"),
    (125000,249999,"🌕 Lunar"),(250000,499999,"⚡🐆 Panther Alpha"),
    (500000,999999,"🏆 Inmortal"),(1000000,99999999,"🌟 Dios de la Manada"),
]

RULETA = [
    ("+50 puntos",50,None,35),("+100 puntos",100,None,20),("+200 puntos",200,None,12),
    ("×2 puntos",0,"x2",10),("USDT",0,"usdt",3),("PNT",0,"pnt",8),("+15 puntos",15,None,12),
]
USDT_POOL = [{"amount":"$50","qty":1},{"amount":"$10","qty":5},{"amount":"$5","qty":20}]
PNT_POOL  = [{"amount":500,"qty":3},{"amount":250,"qty":5},{"amount":100,"qty":10},{"amount":50,"qty":30}]

def spin_ruleta():
    pool = []
    for item in RULETA: pool.extend([item]*item[3])
    return random.choice(pool)

def get_pnt_prize():
    weights = [p["qty"] for p in PNT_POOL]; total = sum(weights); r = random.random()*total
    for i,p in enumerate(PNT_POOL):
        r -= weights[i]
        if r<=0: return p["amount"]
    return PNT_POOL[-1]["amount"]

def get_usdt_prize():
    for p in reversed(USDT_POOL):
        if p["qty"]>0: return p["amount"]
    return None

DB_LOCK = threading.Lock()
FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def get_conn():
    conn = sqlite3.connect(SQLITE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def get_db_connection(): return get_conn()

def download_fonts():
    try:
        import subprocess
        subprocess.run(["apt-get","install","-y","fonts-dejavu-core"],capture_output=True,timeout=30)
    except Exception as e: logger.error(f"Error fuentes: {e}")

def init_db():
    db_dir = os.path.dirname(SQLITE_FILE)
    if db_dir and not os.path.exists(db_dir): os.makedirs(db_dir,exist_ok=True)
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
            points INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, last_checkin TEXT,
            last_ruleta TEXT, double_pts_until TEXT, referral_code TEXT DEFAULT '',
            referred_by TEXT, referrals TEXT DEFAULT '[]', referrals_active INTEGER DEFAULT 0,
            joined_at TEXT, usdt_won_month TEXT, pnt_won_month TEXT,
            reel_verified INTEGER DEFAULT 0, story_verified INTEGER DEFAULT 0,
            follow_ig INTEGER DEFAULT 0, follow_x INTEGER DEFAULT 0,
            follow_tiktok INTEGER DEFAULT 0, follow_facebook INTEGER DEFAULT 0,
            follow_youtube INTEGER DEFAULT 0, follow_all_bonus INTEGER DEFAULT 0,
            has_virtual_card INTEGER DEFAULT 0, has_physical_card INTEGER DEFAULT 0,
            big_transaction INTEGER DEFAULT 0, wallet_activated INTEGER DEFAULT 0,
            pending_wallet_proof INTEGER DEFAULT 0, spins_used_this_event INTEGER DEFAULT 0,
            history TEXT DEFAULT '[]', extra TEXT DEFAULT '{}')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS globals (key TEXT PRIMARY KEY, value TEXT)""")
        conn.commit()
    new_columns = [
        ("reel_count_today","INTEGER DEFAULT 0"),("story_count_today","INTEGER DEFAULT 0"),
        ("content_count_today","INTEGER DEFAULT 0"),("last_mission_date","TEXT"),
        ("review_store_done","INTEGER DEFAULT 0"),("review_trust_done","INTEGER DEFAULT 0"),
        ("founder_number","INTEGER"),("cazador_verificado","INTEGER DEFAULT 0"),
        ("evento_pnt_ganado","REAL DEFAULT 0"),
    ]
    with get_conn() as conn:
        for col,defn in new_columns:
            try: conn.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")
            except: pass
        conn.commit()
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE,"r",encoding="utf-8") as f: old=json.load(f)
            migrated=0
            with get_conn() as conn:
                for uid,data in old.items():
                    if uid=="_global":
                        for k,v in data.items():
                            conn.execute("INSERT OR IGNORE INTO globals(key,value) VALUES(?,?)",(k,json.dumps(v)))
                        continue
                    if not isinstance(data,dict) or "points" not in data: continue
                    if conn.execute("SELECT id FROM users WHERE id=?",(uid,)).fetchone(): continue
                    refs=data.get("referrals",[]); history=data.get("history",[])
                    conn.execute("""INSERT OR IGNORE INTO users
                        (id,username,first_name,points,streak,last_checkin,last_ruleta,
                         double_pts_until,referral_code,referred_by,referrals,referrals_active,
                         joined_at,usdt_won_month,pnt_won_month,reel_verified,story_verified,
                         follow_ig,follow_x,follow_tiktok,follow_facebook,follow_youtube,
                         follow_all_bonus,wallet_activated,history)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                        uid,data.get("username",""),data.get("first_name",""),
                        data.get("points",0),data.get("streak",0),data.get("last_checkin"),
                        data.get("last_ruleta"),data.get("double_pts_until"),
                        data.get("referral_code",uid[-6:]),data.get("referred_by"),
                        json.dumps(refs if isinstance(refs,list) else []),
                        data.get("referrals_active",0),data.get("joined_at",datetime.now().isoformat()),
                        data.get("usdt_won_month"),data.get("pnt_won_month"),
                        int(data.get("reel_verified",False)),int(data.get("story_verified",False)),
                        int(data.get("follow_ig",False)),int(data.get("follow_x",False)),
                        int(data.get("follow_tiktok",False)),int(data.get("follow_facebook",False)),
                        int(data.get("follow_youtube",False)),int(data.get("follow_all_bonus",False)),
                        int(data.get("wallet_activated",False)),json.dumps(history),))
                    migrated+=1
                conn.commit()
            if migrated>0:
                logger.info(f"✅ Migrados {migrated} usuarios")
                os.rename(DB_FILE,DB_FILE+".migrated")
        except Exception as e: logger.error(f"Error migración: {e}")

def load_chat_stars():
    global CHAT_STARS,CHAT_AWARDS
    try:
        conn=get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS chat_stars (uid TEXT PRIMARY KEY, data TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS chat_awards (uid TEXT PRIMARY KEY, data TEXT)")
        conn.commit()
        for row in conn.execute("SELECT uid,data FROM chat_stars"): CHAT_STARS[row[0]]=json.loads(row[1])
        for row in conn.execute("SELECT uid,data FROM chat_awards"): CHAT_AWARDS[row[0]]=json.loads(row[1])
        conn.close()
    except Exception as e: logger.error(f"Error chat_stars: {e}")

def save_chat_stars():
    try:
        conn=get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS chat_stars (uid TEXT PRIMARY KEY, data TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS chat_awards (uid TEXT PRIMARY KEY, data TEXT)")
        for uid,data in CHAT_STARS.items():
            conn.execute("INSERT OR REPLACE INTO chat_stars(uid,data) VALUES(?,?)",(uid,json.dumps(data)))
        for uid,data in CHAT_AWARDS.items():
            conn.execute("INSERT OR REPLACE INTO chat_awards(uid,data) VALUES(?,?)",(uid,json.dumps(data)))
        conn.commit(); conn.close()
    except Exception as e: logger.error(f"Error save chat_stars: {e}")

def _row_to_dict(row):
    if row is None: return None
    d=dict(row)
    for field in ("referrals","history"):
        try: d[field]=json.loads(d.get(field) or "[]")
        except: d[field]=[]
    for field in ("reel_verified","story_verified","follow_ig","follow_x","follow_tiktok",
                  "follow_facebook","follow_youtube","follow_all_bonus","has_virtual_card",
                  "has_physical_card","big_transaction","wallet_activated","pending_wallet_proof",
                  "cazador_verificado"):
        d[field]=bool(d.get(field,0))
    return d

def load_db():
    with get_conn() as conn:
        rows=conn.execute("SELECT * FROM users").fetchall()
        db={row["id"]:_row_to_dict(row) for row in rows}
        g_rows=conn.execute("SELECT key,value FROM globals").fetchall()
        if g_rows: db["_global"]={r["key"]:json.loads(r["value"]) for r in g_rows}
    return db

def save_db(db):
    with DB_LOCK:
        with get_conn() as conn:
            for uid,data in db.items():
                if uid=="_global":
                    for k,v in data.items():
                        conn.execute("INSERT OR REPLACE INTO globals(key,value) VALUES(?,?)",(k,json.dumps(v)))
                    continue
                if not isinstance(data,dict) or "id" not in data: continue
                refs=data.get("referrals",[]); history=data.get("history",[])
                conn.execute("""INSERT OR REPLACE INTO users
                    (id,username,first_name,points,streak,last_checkin,last_ruleta,
                     double_pts_until,referral_code,referred_by,referrals,referrals_active,
                     joined_at,usdt_won_month,pnt_won_month,reel_verified,story_verified,
                     follow_ig,follow_x,follow_tiktok,follow_facebook,follow_youtube,
                     follow_all_bonus,has_virtual_card,has_physical_card,big_transaction,
                     wallet_activated,pending_wallet_proof,spins_used_this_event,
                     reel_count_today,story_count_today,content_count_today,
                     last_mission_date,cazador_verificado,evento_pnt_ganado,history)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                    data["id"],sanitize_name(data.get("username","")),sanitize_name(data.get("first_name","")),
                    data.get("points",0),data.get("streak",0),data.get("last_checkin"),data.get("last_ruleta"),
                    data.get("double_pts_until"),data.get("referral_code",""),data.get("referred_by"),
                    json.dumps(refs if isinstance(refs,list) else []),data.get("referrals_active",0),
                    data.get("joined_at",datetime.now().isoformat()),data.get("usdt_won_month"),
                    data.get("pnt_won_month"),int(data.get("reel_verified",False)),
                    int(data.get("story_verified",False)),int(data.get("follow_ig",False)),
                    int(data.get("follow_x",False)),int(data.get("follow_tiktok",False)),
                    int(data.get("follow_facebook",False)),int(data.get("follow_youtube",False)),
                    int(data.get("follow_all_bonus",False)),int(data.get("has_virtual_card",False)),
                    int(data.get("has_physical_card",False)),int(data.get("big_transaction",False)),
                    int(data.get("wallet_activated",False)),int(data.get("pending_wallet_proof",False)),
                    data.get("spins_used_this_event",0),data.get("reel_count_today",0),
                    data.get("story_count_today",0),data.get("content_count_today",0),
                    data.get("last_mission_date"),int(data.get("cazador_verificado",False)),
                    data.get("evento_pnt_ganado",0),json.dumps(history),))
            conn.commit()

def sanitize_name(name:str)->str:
    if not name: return ""
    try: return name.encode("utf-8",errors="ignore").decode("utf-8")
    except: return "Usuario"

def escape_md(text:str)->str:
    if not text: return ""
    for ch in ['_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']:
        text=text.replace(ch,f'\\{ch}')
    return text

def get_user(db,uid:str,user=None):
    if uid not in db:
        code=uid[-6:] if len(uid)>=6 else uid
        db[uid]={"id":uid,"username":sanitize_name(user.username if user else ""),
            "first_name":sanitize_name(user.first_name if user else ""),
            "points":0,"streak":0,"last_checkin":None,"last_ruleta":None,
            "double_pts_until":None,"referral_code":code,"referred_by":None,
            "referrals":[],"referrals_active":0,"joined_at":datetime.now().isoformat(),
            "usdt_won_month":None,"pnt_won_month":None,"reel_verified":False,
            "story_verified":False,"follow_ig":False,"follow_x":False,"follow_tiktok":False,
            "follow_facebook":False,"follow_youtube":False,"follow_all_bonus":False,
            "wallet_activated":False,"pending_wallet_proof":False,"spins_used_this_event":0,
            "cazador_verificado":False,"evento_pnt_ganado":0,"history":[]}
    elif user:
        db[uid]["username"]=sanitize_name(user.username or db[uid].get("username",""))
        db[uid]["first_name"]=sanitize_name(user.first_name or db[uid].get("first_name",""))
    defaults=[("usdt_won_month",None),("pnt_won_month",None),("referrals_active",0),
        ("reel_verified",False),("story_verified",False),("follow_ig",False),("follow_x",False),
        ("follow_tiktok",False),("follow_facebook",False),("follow_youtube",False),
        ("follow_all_bonus",False),("wallet_activated",False),("pending_wallet_proof",False),
        ("spins_used_this_event",0),("founder_number",None),("history",[]),
        ("cazador_verificado",False),("evento_pnt_ganado",0)]
    for field,default in defaults:
        if field not in db[uid]: db[uid][field]=default
    if not isinstance(db[uid].get("referrals"),list): db[uid]["referrals"]=[]
    return db[uid]

def get_level(pts:int):
    for mn,mx,name in LEVELS:
        if mn<=pts<=mx: return name
    return "👑 Leyenda"

def get_next_level(pts:int):
    for i,(mn,mx,name) in enumerate(LEVELS):
        if mn<=pts<=mx:
            if i+1<len(LEVELS): return LEVELS[i+1][2],LEVELS[i+1][0]-pts
    return None,0

def add_points(data,amount:int):
    multiplier=1
    if data.get("double_pts_until"):
        try:
            until=datetime.fromisoformat(data["double_pts_until"])
            if datetime.now()<until: multiplier=2
            else: data["double_pts_until"]=None
        except: data["double_pts_until"]=None
    data["points"]+=amount*multiplier
    return amount*multiplier

def has_won_this_month(data,prize_type):
    won=data.get(f"{prize_type}_won_month")
    return won==date.today().strftime("%Y-%m") if won else False

def mark_won_month(data,prize_type):
    data[f"{prize_type}_won_month"]=date.today().strftime("%Y-%m")

def is_ruleta_active():
    db=load_db()
    override=db.get("_global",{}).get("ruleta_override")
    if override=="on": return True
    if override=="off": return False
    return date.today().day in [15,30]

def can_access_ruleta(data): return True
def get_available_spins(data): return 3

async def notify_mods(app,msg:str):
    try: await app.bot.send_message(chat_id=MOD_GROUP_ID,text=msg,parse_mode="Markdown")
    except Exception as e: logger.error(f"Error notif mods: {e}")

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check-in diario",callback_data="checkin")],
        [InlineKeyboardButton("📊 Mis puntos",callback_data="puntos"),
         InlineKeyboardButton("🏆 Ranking",callback_data="ranking")],
        [InlineKeyboardButton("🎰 Ruleta",callback_data="ruleta"),
         InlineKeyboardButton("📋 Misiones",callback_data="misiones")],
        [InlineKeyboardButton("🎫 Mi código referido",callback_data="referido")],
        [InlineKeyboardButton("🏅 Tabla de niveles",callback_data="niveles")],
    ])

# ══════════════════════════════════════════════════════════════════════════════
# ── OPERACIÓN 1,000 CAZADORES ─────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

EVENTO_CONFIG = {"nombre":"Operación 1,000 Cazadores","objetivo":1000,"pool_pnt":1125,
    "min_referidos":3,"dias_base":20,"extensiones":[
        {"min":800,"max":999,"dias_extra":5},{"min":600,"max":799,"dias_extra":10},
        {"min":0,"max":599,"dias_extra":15}]}

def get_evento_globals(db): return db.get("_global",{}).get("evento",{})

def save_evento_globals(db,ev):
    if "_global" not in db: db["_global"]={}
    db["_global"]["evento"]=ev; save_db(db)

def evento_activo(db): return bool(get_evento_globals(db).get("activo",False))

def get_total_cazadores(db):
    return sum(1 for uid,d in db.items()
        if not uid.startswith("_") and isinstance(d,dict) and d.get("cazador_verificado"))

def get_referidos_evento(db,uid):
    data=db.get(uid,{}); refs=data.get("referrals",[])
    if not isinstance(refs,list): return 0
    return sum(1 for rid in refs if db.get(str(rid),{}).get("cazador_verificado"))

def get_total_referidos_activos_campana(db):
    total=0
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        total+=get_referidos_evento(db,uid)
    return total

def calcular_pnt_usuario(db,uid):
    ev=get_evento_globals(db); pool=ev.get("pool_pnt",1125); min_refs=ev.get("min_referidos",3)
    refs_u=get_referidos_evento(db,uid); total_refs=get_total_referidos_activos_campana(db)
    califica=refs_u>=min_refs
    if not califica or total_refs==0: return 0.0,refs_u,total_refs,califica
    return round((refs_u/total_refs)*pool,4),refs_u,total_refs,califica

def get_fecha_cierre(db):
    ev=get_evento_globals(db); inicio_str=ev.get("fecha_inicio")
    if not inicio_str: return None
    try:
        inicio=date.fromisoformat(inicio_str)
        return inicio+timedelta(days=ev.get("dias_base",20)+ev.get("dias_extra",0))
    except: return None

def dias_transcurridos(db):
    ev=get_evento_globals(db); inicio_str=ev.get("fecha_inicio")
    if not inicio_str: return -1
    try: return (date.today()-date.fromisoformat(inicio_str)).days
    except: return -1

def calcular_extension(total_cazadores):
    for ext in EVENTO_CONFIG["extensiones"]:
        if ext["min"]<=total_cazadores<=ext["max"]: return ext["dias_extra"]
    return 0

def get_top_referidores(db,n=5):
    scores=[]
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        refs=get_referidos_evento(db,uid)
        if refs>0:
            nombre=data.get("username") or data.get("first_name") or uid
            scores.append((uid,nombre,refs,data.get("points",0)))
    scores.sort(key=lambda x:x[2],reverse=True); return scores[:n]

async def check_alertas_evento(app):
    db=load_db(); ev=get_evento_globals(db)
    if not ev.get("activo"): return
    dias=dias_transcurridos(db)
    if dias<0: return
    alertas=ev.get("alertas_enviadas",[]); total_caz=get_total_cazadores(db)
    grupo_id=ev.get("grupo_id",MOD_GROUP_ID)

    if dias>=7 and "dia7" not in alertas:
        top5=get_top_referidores(db,5); medals=["🥇","🥈","🥉","4️⃣","5️⃣"]
        lines=["🏆 *TOP 5 CAZADORES — Semana 1*\n"]
        for i,(uid,nombre,refs,pts) in enumerate(top5):
            lines.append(f"{medals[i]} @{nombre} — *{refs} cazadores*")
        lines.append(f"\n🎯 Progreso: *{total_caz}/1,000* cazadores")
        lines.append("_¡Seguí invitando para ganar más PNT del cofre! 🐾_")
        try: await app.bot.send_message(chat_id=grupo_id,text="\n".join(lines),parse_mode="Markdown")
        except Exception as e: logger.error(f"Error alerta día7: {e}")
        alertas.append("dia7"); ev["alertas_enviadas"]=alertas; save_evento_globals(db,ev)

    if dias>=15 and "dia15" not in alertas:
        faltan=max(0,1000-total_caz); pct=round(total_caz/1000*100)
        if faltan==0:
            msg=("🎉 *¡YA LLEGAMOS A 1,000 CAZADORES!*\n\nEl cofre comunitario está listo 🔓\n"
                "_Esperamos al cierre oficial para repartir los 1,125 PNT 🐾_")
        else:
            msg=(f"⚔️ *Operación 1,000 Cazadores — Día 15*\n\n"
                f"📊 Progreso: *{total_caz}/1,000* ({pct}%)\n"
                f"🎯 Faltan: *{faltan} cazadores* para abrir el cofre\n\n"
                f"Quedan 5 días. ¡Es momento de llamar refuerzos! 🐆\n_Compartí tu link_ 🔗")
        try: await app.bot.send_message(chat_id=grupo_id,text=msg,parse_mode="Markdown")
        except Exception as e: logger.error(f"Error alerta día15: {e}")
        alertas.append("dia15"); ev["alertas_enviadas"]=alertas; save_evento_globals(db,ev)

    if dias>=20 and "dia20" not in alertas:
        if total_caz>=1000:
            msg=(f"🎊 *¡MISIÓN CUMPLIDA — OPERACIÓN 1,000 CAZADORES!*\n\n"
                f"Llegamos a *{total_caz} cazadores* 🏆\n\n"
                f"💰 El cofre de *1,125 PNT* está listo para repartirse.\n"
                f"Los premios se distribuyen al cierre oficial 🐾🔥")
        else:
            dias_extra=calcular_extension(total_caz); faltan=1000-total_caz
            if total_caz>=800: razon=f"¡Estamos muy cerca! Solo faltan {faltan}"
            elif total_caz>=600: razon=f"Buen progreso. Faltan {faltan} para el cofre"
            else: razon=f"La Manada necesita refuerzos. Faltan {faltan}"
            msg=(f"⏰ *Día 20 — Operación 1,000 Cazadores*\n\n"
                f"📊 Progreso: *{total_caz}/1,000*\n{razon}\n\n"
                f"⚡ *El evento se extiende {dias_extra} días más*\n"
                f"¡La Manada no se rinde! 🐆\n_Compartí tu link_ 🔗")
            ev["dias_extra"]=ev.get("dias_extra",0)+dias_extra
        try: await app.bot.send_message(chat_id=grupo_id,text=msg,parse_mode="Markdown")
        except Exception as e: logger.error(f"Error alerta día20: {e}")
        alertas.append("dia20"); ev["alertas_enviadas"]=alertas; save_evento_globals(db,ev)

async def cmd_evento_start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    args=context.args or []
    if not args:
        await update.message.reply_text("Uso: /evento_start YYYY-MM-DD [grupo_id]"); return
    fecha_str=args[0]; grupo_id=int(args[1]) if len(args)>1 else MOD_GROUP_ID
    try: date.fromisoformat(fecha_str)
    except: await update.message.reply_text("❌ Fecha inválida. Formato: YYYY-MM-DD"); return
    db=load_db()
    ev={"activo":True,"fecha_inicio":fecha_str,"dias_base":20,"dias_extra":0,
        "pool_pnt":1125,"min_referidos":3,"grupo_id":grupo_id,"alertas_enviadas":[],"cerrado":False}
    save_evento_globals(db,ev)
    await update.message.reply_text(
        f"✅ *Operación 1,000 Cazadores — ACTIVADA*\n\n📅 Inicio: {fecha_str}\n"
        f"🎯 Objetivo: 1,000 cazadores en 20 días\n💰 Pool: 1,125 PNT\n"
        f"📢 Grupo: {grupo_id}\n\nComandos:\n/evento_status /evento_stop\n"
        f"/evento_preview_premios /evento_cerrar\n/forzar_alerta dia7|dia15|dia20",
        parse_mode="Markdown")

async def cmd_evento_stop(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db(); ev=get_evento_globals(db); ev["activo"]=False
    save_evento_globals(db,ev)
    await update.message.reply_text("⏸️ Evento pausado.")

async def cmd_evento_status(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db(); ev=get_evento_globals(db)
    if not ev: await update.message.reply_text("❌ No hay evento. Usá /evento_start."); return
    dias=dias_transcurridos(db); total_caz=get_total_cazadores(db)
    total_refs=get_total_referidos_activos_campana(db); fecha_cierre=get_fecha_cierre(db)
    top5=get_top_referidores(db,5); medals=["🥇","🥈","🥉","4️⃣","5️⃣"]
    top_lines="\n".join(f"  {medals[i]} @{nombre} — {refs} cazadores"
        for i,(uid,nombre,refs,pts) in enumerate(top5))
    activo=ev.get("activo",False); cerrado=ev.get("cerrado",False)
    estado="🔴 Cerrado" if cerrado else ("🟢 Activo" if activo else "⏸️ Pausado")
    await update.message.reply_text(
        f"⚔️ *Operación 1,000 Cazadores*\n\nEstado: {estado}\n"
        f"Día: *{dias}*/{ev.get('dias_base',20)+ev.get('dias_extra',0)}\n"
        f"Cierre: {fecha_cierre or 'N/A'}\n\n🎯 Cazadores: *{total_caz}/1,000*\n"
        f"🔗 Denominador: *{total_refs}*\n💰 Pool: *{ev.get('pool_pnt',1125)} PNT*\n\n"
        f"*Top 5:*\n{top_lines or '  Sin datos'}\n\nAlertas: {ev.get('alertas_enviadas',[])}",
        parse_mode="Markdown")

async def cmd_evento_preview_premios(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db(); ev=get_evento_globals(db)
    if not ev.get("activo") and not ev.get("cerrado"):
        await update.message.reply_text("❌ No hay evento activo."); return
    total_refs=get_total_referidos_activos_campana(db)
    min_refs=ev.get("min_referidos",3); pool=ev.get("pool_pnt",1125)
    ganadores=[]
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        refs_u=get_referidos_evento(db,uid)
        if refs_u<min_refs: continue
        pnt=round((refs_u/total_refs)*pool,4) if total_refs>0 else 0
        nombre=data.get("username") or data.get("first_name") or uid
        ganadores.append((uid,nombre,refs_u,pnt))
    ganadores.sort(key=lambda x:x[3],reverse=True)
    if not ganadores:
        await update.message.reply_text(f"⚠️ Nadie califica aún. Mínimo: {min_refs} cazadores."); return
    total_pnt=sum(g[3] for g in ganadores)
    lines=[f"💰 *Preview del cofre*\nPool: {pool} PNT | Denominador: {total_refs}\n"]
    for i,(uid,nombre,refs,pnt) in enumerate(ganadores[:20]):
        lines.append(f"{i+1}. @{nombre} — {refs} refs → *{pnt} PNT*")
    if len(ganadores)>20: lines.append(f"...y {len(ganadores)-20} más")
    lines.append(f"\nTotal: *{round(total_pnt,2)} PNT*")
    await update.message.reply_text("\n".join(lines),parse_mode="Markdown")

async def cmd_evento_cerrar(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    args=context.args or []
    if "confirmar" not in args:
        db=load_db(); total_caz=get_total_cazadores(db)
        await update.message.reply_text(
            f"⚠️ *¿Confirmar cierre?*\n\nCazadores: *{total_caz}*\n"
            f"Esto distribuye los premios. *Irreversible.*\n\nSi estás seguro:\n`/evento_cerrar confirmar`",
            parse_mode="Markdown"); return
    db=load_db(); ev=get_evento_globals(db)
    total_caz=get_total_cazadores(db); total_refs=get_total_referidos_activos_campana(db)
    min_refs=ev.get("min_referidos",3); pool=ev.get("pool_pnt",1125)
    ganadores=[]
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        refs_u=get_referidos_evento(db,uid)
        if refs_u<min_refs: continue
        pnt=round((refs_u/total_refs)*pool,4) if total_refs>0 else 0
        if pnt>0: ganadores.append((uid,refs_u,pnt))
    notificados=0
    for uid,refs_u,pnt in ganadores:
        db[uid]["evento_pnt_ganado"]=pnt
        try:
            nombre=db[uid].get("username") or db[uid].get("first_name") or "Cazador"
            await context.bot.send_message(chat_id=int(uid),
                text=(f"🎊 *¡El cofre se abrió, {nombre}!*\n\n"
                    f"⚔️ Operación 1,000 Cazadores — Cierre oficial\n\n"
                    f"🔗 Cazadores referidos: *{refs_u}*\n💰 Tu premio: *{pnt} PNT*\n\n"
                    f"Tokens acreditados en tu Panther Wallet en los próximos 5 días hábiles 🐾\n"
                    f"_Gracias por ser parte de la Manada 🐆_"),parse_mode="Markdown")
            notificados+=1
        except Exception as e: logger.warning(f"No se pudo notificar {uid}: {e}")
    ev["activo"]=False; ev["cerrado"]=True; save_evento_globals(db,ev); save_db(db)
    total_pnt=round(sum(g[2] for g in ganadores),2)
    await update.message.reply_text(
        f"✅ *Evento cerrado*\n\n👥 Cazadores: {total_caz}\n🏆 Ganadores: {len(ganadores)}\n"
        f"💰 PNT distribuidos: {total_pnt}\n📨 Notificados: {notificados}",parse_mode="Markdown")

async def cmd_forzar_alerta(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    args=context.args or []
    if not args or args[0] not in ["dia7","dia15","dia20"]:
        await update.message.reply_text("Uso: /forzar_alerta dia7|dia15|dia20"); return
    alerta=args[0]; db=load_db(); ev=get_evento_globals(db)
    alertas=ev.get("alertas_enviadas",[])
    if alerta in alertas: alertas.remove(alerta)
    ev["alertas_enviadas"]=alertas
    dias_map={"dia7":7,"dia15":15,"dia20":20}
    fecha_original=ev.get("fecha_inicio")
    ev["fecha_inicio"]=(date.today()-timedelta(days=dias_map[alerta])).isoformat()
    save_evento_globals(db,ev)
    await check_alertas_evento(context.application)
    db2=load_db(); ev2=get_evento_globals(db2)
    ev2["fecha_inicio"]=fecha_original; save_evento_globals(db2,ev2)
    await update.message.reply_text(f"✅ Alerta {alerta} enviada.")

# ══════════════════════════════════════════════════════════════════════════════
# ── COMANDOS USUARIOS ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def generate_founder_badge(name:str,number:int)->bytes:
    try:
        from PIL import Image,ImageDraw,ImageFont
        import math,io
        W,H=1080,1080; NEGRO="#0A0A0A"; NARANJA="#FF5C1A"; ORO="#FFD700"
        img=Image.new("RGB",(W,H),NEGRO); d=ImageDraw.Draw(img)
        for row in range(-1,18):
            for col in range(-1,18):
                cx=col*78+(39 if row%2 else 0); cy=row*68
                pts=[(cx+34*math.cos(math.radians(60*i-30)),cy+34*math.sin(math.radians(60*i-30))) for i in range(6)]
                d.polygon(pts,outline="#181818",fill=NEGRO)
        d.rounded_rectangle([30,30,W-30,H-30],radius=30,outline=ORO,width=4,fill=NEGRO)
        def ft(path,size):
            try: return ImageFont.truetype(path,size) if path and os.path.exists(path) else ImageFont.load_default()
            except: return ImageFont.load_default()
        f_badge=ft(FONT_BOLD,48); f_name=ft(FONT_BOLD,80); f_sub=ft(FONT_REGULAR,42); f_small=ft(FONT_REGULAR,36)
        titulo="✦ FUNDADOR DE LA MANADA ✦"
        bb=d.textbbox((0,0),titulo,font=f_badge); d.text(((W-(bb[2]-bb[0]))//2,88),titulo,font=f_badge,fill=ORO)
        d.rectangle([80,125,W-80,127],fill=ORO)
        display_name=name[:22]+"..." if len(name)>22 else name
        bb=d.textbbox((0,0),display_name,font=f_name); d.text(((W-(bb[2]-bb[0]))//2,575),display_name,font=f_name,fill="#FFFFFF")
        d.rectangle([200,648,W-200,650],fill=NARANJA)
        sub="Entre los primeros 500 en la Manada Panther"
        bb=d.textbbox((0,0),sub,font=f_sub); d.text(((W-(bb[2]-bb[0]))//2,668),sub,font=f_sub,fill="#aaaaaa")
        num_text=f"# {number:04d}"
        d.rounded_rectangle([W//2-120,730,W//2+120,800],radius=20,fill="#1a0800",outline="#7a2d0d",width=1)
        bb=d.textbbox((0,0),num_text,font=f_badge); d.text(((W-(bb[2]-bb[0]))//2,748),num_text,font=f_badge,fill=NARANJA)
        fecha="2026"
        bb=d.textbbox((0,0),fecha,font=f_small); d.text(((W-(bb[2]-bb[0]))//2,830),fecha,font=f_small,fill="#555555")
        d.rectangle([30,H-50,W-30,H-30],fill=NARANJA)
        out=io.BytesIO(); img.save(out,format="PNG"); return out.getvalue()
    except Exception as e: logger.error(f"Error badge: {e}"); return None

async def send_founder_badge(bot,uid:str,name:str,number:int):
    badge_bytes=generate_founder_badge(name,number)
    if not badge_bytes: return False
    try:
        import io
        await bot.send_photo(chat_id=int(uid),photo=io.BytesIO(badge_bytes),
            caption=f"🏆 *¡Sos Fundador de la Manada!*\n\nGuardaste tu lugar entre los primeros 500 miembros.\nGuardá tu badge y compartilo 🐆",
            parse_mode="Markdown")
        return True
    except Exception as e: logger.error(f"Error enviando badge {uid}: {e}"); return False

async def cmd_start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); is_new=uid not in db
    data=get_user(db,uid,user)
    if context.args and context.args[0]=='mission':
        save_db(db)
        mission_type=PENDING_MISSIONS.get(uid)
        tipo_labels={"wallet_activate":"🔐 Activación de Wallet","review_store":"⭐ Review en Tienda",
            "review_trust":"🌟 Review en Trustpilot","content":"✏️ Contenido propio",
            "reel":"🎬 Reel de Panther","story":"📸 Historia de Panther"}
        tipo_label=tipo_labels.get(mission_type,"📎 Tu misión")
        await update.message.reply_text(
            f"📸 *¡Listo {user.first_name}!*\n\nMisión: *{tipo_label}*\n\n"
            f"Enviá tu captura de pantalla acá 👇\n\n_Un moderador la verificará en las próximas 24h 🐾_",
            parse_mode="Markdown"); return
    if context.args and is_new:
        ref_code=context.args[0]
        if ref_code in CAMPAIGN_SOURCES: data["source"]=ref_code
        else:
            data["source"]="referral"
            for rid,rdata in db.items():
                r_code=rdata.get("referral_code","") if isinstance(rdata,dict) else ""
                if (r_code==ref_code or r_code==f"PANTH-{ref_code}") and rid!=uid:
                    data["referred_by"]=rid
                    if uid not in rdata["referrals"]:
                        rdata["referrals"].append(uid)
                        earned=add_points(rdata,PTS["referral_join"]); db[rid]=rdata
                        try:
                            await context.bot.send_message(chat_id=int(rid),
                                text=f"🎉 *¡Nuevo miembro!*\n\n*{user.first_name}* se unió con tu código 🐆\n*+{earned} puntos* 🐾",
                                parse_mode="Markdown")
                        except: pass
                    break
    if is_new:
        db2=load_db(); user_count=len([u for u in db2.keys() if not u.startswith("_")])
        if user_count<=500: data["founder_number"]=user_count
        db[uid]=data; save_db(db)
        if user_count<=500:
            fname=user.first_name or user.username or "Miembro"
            asyncio.create_task(send_founder_badge(context.bot,uid,fname,user_count))
    else: save_db(db)
    level=get_level(data["points"]); next_lv,pts_needed=get_next_level(data["points"])
    app_url=f"https://go.mypanther.io/app?id={uid}&v=3"
    from telegram import WebAppInfo
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton("🐆 Abrir Manada Panther",web_app=WebAppInfo(url=app_url))],
        [InlineKeyboardButton("✅ Check-in diario",callback_data="checkin")],
        [InlineKeyboardButton("📊 Mis puntos",callback_data="puntos"),
         InlineKeyboardButton("🏆 Ranking",callback_data="ranking")],
        [InlineKeyboardButton("🎰 Ruleta",callback_data="ruleta"),
         InlineKeyboardButton("📋 Misiones",callback_data="misiones")],
        [InlineKeyboardButton("🎫 Mi código referido",callback_data="referido")],
        [InlineKeyboardButton("🏅 Tabla de niveles",callback_data="niveles")],
    ])
    if is_new:
        text=(f"🐆 *¡Bienvenido a la Manada Panther, {user.first_name}!*\n\n"
            f"🏅 Nivel: *{level}*\n⭐ Puntos: *{data['points']}*\n\n"
            f"📢 Canal: t.me/pantherwalletoficial\n💬 Chat: t.me/manadapanther\n\n"
            f"_Completá misiones, referí amigos y ganá premios 💰_")
    else:
        text=(f"🐾 *¡Hola, {user.first_name}!*\n\n🏅 Nivel: *{level}*\n"
            f"⭐ Puntos: *{data['points']}*\n🔥 Racha: *{data['streak']} días*\n"
            f"{'📈 Próximo: *'+next_lv+'* — '+str(pts_needed)+' pts' if next_lv else '👑 Nivel máximo'}")
    await update.message.reply_text(text,parse_mode="Markdown",reply_markup=keyboard)

async def cmd_checkin(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); data=get_user(db,uid,user)
    today=date.today().isoformat(); yesterday=(date.today()-timedelta(days=1)).isoformat()
    last=data.get("last_checkin")
    if last==today:
        await update.message.reply_text(
            f"⏰ Ya hiciste tu check-in hoy.\n\n🔥 Racha: *{data['streak']} días*\nVolvé mañana.",
            parse_mode="Markdown",reply_markup=main_keyboard()); return
    data["streak"]=(data["streak"]+1) if last==yesterday else 1; streak=data["streak"]
    base_pts=PTS["checkin_1_3"] if streak<=3 else PTS["checkin_4_6"]; bonus=0; bonus_msg=""
    if streak==7: bonus=PTS["streak_7"]; bonus_msg=f"\n🎉 *¡RACHA DE 7 DÍAS!* +{bonus} pts"
    elif streak==14: bonus=PTS["streak_14"]; bonus_msg=f"\n🎉 *¡RACHA DE 14 DÍAS!* +{bonus} pts"
    elif streak==30: bonus=PTS["streak_30"]; bonus_msg=f"\n🎉 *¡RACHA DE 30 DÍAS!* +{bonus} pts"
    old_pts=data["points"]; earned=add_points(data,base_pts+bonus); data["last_checkin"]=today
    if "history" not in data: data["history"]=[]
    data["history"].append({"type":"checkin","pts":earned,"date":today,"time":datetime.now().strftime("%H:%M")})
    data["history"]=data["history"][-20:]
    old_lv=get_level(old_pts); new_lv=get_level(data["points"])
    lvl_msg=f"\n\n⬆️ *¡SUBISTE DE NIVEL!*\n{old_lv} → *{new_lv}*" if old_lv!=new_lv else ""
    next_lv,pts_needed=get_next_level(data["points"]); save_db(db)
    await update.message.reply_text(
        f"✅ *¡Check-in completado!*\n\n🔥 Racha: *{streak} día{'s' if streak>1 else ''}*\n"
        f"➕ Ganaste: *+{earned} puntos*{bonus_msg}\n⭐ Total: *{data['points']} puntos*\n"
        f"🏅 Nivel: *{new_lv}*{lvl_msg}\n\n"
        f"{'📈 Próximo: *'+next_lv+'* — faltan *'+str(pts_needed)+' pts*' if next_lv else ''}",
        parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_puntos(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); data=get_user(db,uid,user); save_db(db)
    level=get_level(data["points"]); next_lv,pts_needed=get_next_level(data["points"])
    refs=len(data.get("referrals",[]))
    await update.message.reply_text(
        f"📊 *Tu perfil — Manada Panther*\n\n👤 {user.first_name}\n🏅 Nivel: *{level}*\n"
        f"⭐ Puntos: *{data['points']}*\n🔥 Racha: *{data['streak']} días*\n"
        f"👥 Referidos: *{refs}*\n🎫 Código: `{data['referral_code']}`\n\n"
        f"{'📈 Próximo: *'+next_lv+'* — faltan *'+str(pts_needed)+' pts*' if next_lv else '👑 ¡Sos Leyenda!'}",
        parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_niveles(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id)
    data=get_user(db,uid,user); save_db(db); current=get_level(data["points"])
    lines=["🏅 *NIVELES — MANADA PANTHER*\n"]
    for mn,mx,name in LEVELS:
        marker=" ✅ ← estás aquí" if name==current else ""
        pts_range=f"{mn:,} – {mx:,} pts" if mx<999999 else f"{mn:,}+ pts"
        lines.append(f"{name}{marker}\n_{pts_range}_\n")
    lines.append(f"⭐ *Tus puntos: {data['points']}*")
    await update.message.reply_text("\n".join(lines),parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_ranking(update:Update,context:ContextTypes.DEFAULT_TYPE):
    db=load_db(); uid=str(update.effective_user.id)
    sorted_=[u for u in db.values() if isinstance(u,dict) and "points" in u]
    sorted_.sort(key=lambda x:x.get("points",0),reverse=True)
    top20=sorted_[:20]; medals=["🥇","🥈","🥉"]
    lines=["🏆 *LEADERBOARD — MANADA PANTHER*\n"]
    for i,u in enumerate(top20):
        prefix=medals[i] if i<3 else f"{i+1}."
        name=u.get("username") or u.get("first_name") or "Anónimo"
        lv=get_level(u["points"]); me=" ← vos" if u["id"]==uid else ""
        lines.append(f"{prefix} @{name} — *{u['points']} pts* {lv}{me}")
    my_pos=next((i+1 for i,u in enumerate(sorted_) if u["id"]==uid),None)
    if my_pos and my_pos>20: lines.append(f"\n📍 Tu posición: *#{my_pos}* — {db[uid]['points']} pts")
    await update.message.reply_text("\n".join(lines),parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_referido(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id)
    data=get_user(db,uid,user); save_db(db)
    me=await context.bot.get_me(); link=f"https://t.me/{me.username}?start={data['referral_code']}"
    refs=len(data.get("referrals",[]))
    await update.message.reply_text(
        f"🎫 *Tu código de referido*\n\nCódigo: `{data['referral_code']}`\nLink: {link}\n\n"
        f"👥 Referidos: *{refs}*\n\n*Por cada referido:*\n├ Se une: *+{PTS['referral_join']} pts*\n"
        f"└ Activa wallet: *+{PTS['referral_wallet']} pts*",
        parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_ruleta(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); data=get_user(db,uid,user)
    today=date.today().isoformat()
    if data.get("last_ruleta")==today:
        await update.message.reply_text("🎰 Ya giraste la ruleta hoy. Volvé mañana 🐾"); return
    result_label,pts_gain,special,_=spin_ruleta(); data["last_ruleta"]=today
    msg="🎰 *¡GIRASTE LA RULETA!*\n\n"
    if pts_gain>0:
        earned=add_points(data,pts_gain)
        msg+=f"🎊 Resultado: *{result_label}*\n➕ Ganaste: *+{earned} puntos*\n⭐ Total: *{data['points']} puntos*"
    elif special=="x2":
        until=datetime.now()+timedelta(hours=24); data["double_pts_until"]=until.isoformat()
        msg+=f"⚡ *¡PUNTOS DOBLES POR 24 HORAS!* 🔥\n⭐ Puntos: *{data['points']}*"
    elif special in ("usdt","pnt"):
        if has_won_this_month(data,special):
            earned=add_points(data,50 if special=="usdt" else 30)
            msg+=f"⭐ *+{earned} puntos*\n⭐ Total: *{data['points']} puntos*"
        else:
            mark_won_month(data,special)
            prize=get_usdt_prize() if special=="usdt" else f"{get_pnt_prize()} PNT"
            tipo="💵 USDT" if special=="usdt" else "🐾 PNT"
            msg+=(f"{tipo} *¡PREMIO!*\n\nGanaste: *{prize}*\n\n"
                f"📸 Tomá captura y enviala al chat. Un mod te contacta.\n_⚠️ Un premio por mes._")
            name=user.username or user.first_name
            for mod_id in MOD_IDS:
                try: await context.bot.send_message(chat_id=mod_id,
                    text=f"🎰 Premio {special.upper()}\n@{name} (ID:{uid})\nPremio: {prize}")
                except: pass
    save_db(db)
    await update.message.reply_text(msg,parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_misiones(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id); app_url=f"https://go.mypanther.io/app?id={uid}&v=3"
    from telegram import WebAppInfo
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton("🐆 Abrir Misiones",web_app=WebAppInfo(url=app_url))]])
    await update.message.reply_text("Las misiones están en la Mini App 👇",reply_markup=keyboard)

async def cmd_compartir(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 *Verificación de contenido*\n\n1️⃣ Compartí el reel o historia de Panther\n"
        "2️⃣ Tomá una captura\n3️⃣ Enviá la captura acá directamente\n\nVerificamos en menos de 24h 🐾",
        parse_mode="Markdown")

async def cmd_verificar_follow(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); data=get_user(db,uid,user)
    args=context.args or []; red=args[0].lower() if args else ""
    valid={"ig":"follow_ig","x":"follow_x","tiktok":"follow_tiktok"}
    if red not in valid: await update.message.reply_text("Uso: /verificar_follow ig | x | tiktok"); return
    field=valid[red]
    if data.get(field): await update.message.reply_text("✅ Ya verificaste esta red social."); return
    earned=add_points(data,PTS[field]); data[field]=True; bonus_msg=""
    if data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok") and not data.get("follow_all_bonus"):
        bonus=add_points(data,PTS["follow_all_bonus"]); data["follow_all_bonus"]=True
        bonus_msg=f"\n\n🎉 *¡Bonus por seguir todas!* +{bonus} pts"
    db[uid]=data; save_db(db)
    red_names={"ig":"Instagram","x":"X (Twitter)","tiktok":"TikTok"}
    await update.message.reply_text(
        f"✅ *¡Misión completada!*\nSeguiste a Panther en {red_names[red]}\n*+{earned} pts* 🐆{bonus_msg}",
        parse_mode="Markdown")

async def cmd_ayuda(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐆 *CÓMO FUNCIONA LA MANADA PANTHER*\n\n*Ganás puntos:*\n🔥 Check-in diario\n"
        "👥 Referir amigos\n📱 Compartir contenido\n🎰 Ruleta\n\n*Rachas:*\n"
        "7 días → +50 pts\n14 días → +150 pts\n30 días → +500 pts\n\nUsá /niveles para la tabla.",
        parse_mode="Markdown",reply_markup=main_keyboard())

async def cmd_mi_badge(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; db=load_db(); uid=str(user.id); data=db.get(uid,{})
    founder_number=data.get("founder_number")
    if not founder_number: await update.message.reply_text("❌ No tenés badge de Fundador."); return
    await update.message.reply_text("🏆 Generando tu badge...")
    fname=user.first_name or user.username or "Miembro"
    success=await send_founder_badge(context.bot,uid,fname,founder_number)
    if not success: await update.message.reply_text("❌ Error generando el badge.")

# ══════════════════════════════════════════════════════════════════════════════
# ── COMANDOS MODERADORES ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_aprobar(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: await update.message.reply_text("❌ Sin permisos."); return
    if len(context.args)<2: await update.message.reply_text("Uso: /aprobar USER_ID reel|story|content"); return
    target_uid=context.args[0]; tipo=context.args[1].lower()
    pts_map={"reel":PTS["share_reel"],"story":PTS["share_story"],"content":PTS["own_content"],
        "wallet_activate":175,"review_store":175,"review_trust":175,
        "comment_ig":5,"comment_ig_last":30,"comment_tt":5,"comment_tt_last":30}
    if tipo not in pts_map: await update.message.reply_text("Tipo inválido."); return
    db=load_db()
    if target_uid not in db: await update.message.reply_text("Usuario no encontrado."); return
    earned=add_points(db[target_uid],pts_map[tipo]); save_db(db)
    await update.message.reply_text(f"✅ +{earned} pts a {target_uid}")
    try: await context.bot.send_message(chat_id=int(target_uid),
        text=f"✅ *¡Misión verificada!*\n\n➕ *+{earned} puntos* 🐾\n⭐ Total: *{db[target_uid]['points']} puntos*",
        parse_mode="Markdown")
    except: pass

async def cmd_dar_puntos(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: await update.message.reply_text("Sin permisos."); return
    if len(context.args)<2: await update.message.reply_text("Uso: /dar_puntos USER_ID cantidad motivo"); return
    target_uid=context.args[0]
    try: amount=int(context.args[1])
    except: await update.message.reply_text("❌ Cantidad debe ser número."); return
    if amount<=0 or amount>500: await update.message.reply_text("❌ Entre 1 y 500."); return
    motivo=" ".join(context.args[2:]) if len(context.args)>2 else "Bonus especial"
    db=load_db()
    if target_uid not in db: await update.message.reply_text("❌ Usuario no encontrado."); return
    earned=add_points(db[target_uid],amount); save_db(db)
    name=db[target_uid].get("username") or db[target_uid].get("first_name") or target_uid
    await update.message.reply_text(f"✅ +{earned} pts a @{name}\nMotivo: {motivo}\nTotal: {db[target_uid]['points']} pts")
    try: await context.bot.send_message(chat_id=int(target_uid),
        text=f"🎉 Bonus!\n\n+{earned} puntos\nMotivo: {motivo}\nTotal: {db[target_uid]['points']} pts 🐾")
    except: pass

async def cmd_reset_ruleta(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db(); count=0
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        data["spins_used_this_event"]=0; data["spins_available"]=3; count+=1
    save_db(db); await update.message.reply_text(f"✅ Giros reseteados para {count} usuarios.")

async def cmd_ruleta_on(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db()
    if "_global" not in db: db["_global"]={}
    db["_global"]["ruleta_override"]="on"; save_db(db)
    await update.message.reply_text("✅ Ruleta ACTIVADA")

async def cmd_ruleta_off(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db()
    if "_global" not in db: db["_global"]={}
    db["_global"]["ruleta_override"]="off"; save_db(db)
    await update.message.reply_text("🔴 Ruleta DESACTIVADA")

async def cmd_ruleta_auto(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db()
    if "_global" not in db: db["_global"]={}
    db["_global"]["ruleta_override"]=None; save_db(db)
    await update.message.reply_text("🔄 Ruleta AUTOMÁTICA (días 15 y 30)")

async def cmd_broadcast(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: await update.message.reply_text("❌ Sin permisos."); return
    if not context.args: await update.message.reply_text("Uso: /broadcast mensaje"); return
    msg=" ".join(context.args); db=load_db()
    users=[u for u in db.keys() if not u.startswith("_")]
    await update.message.reply_text(f"📤 Enviando a {len(users)} usuarios...")
    sent=failed=0
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=int(user_id),
                text=f"📢 *Mensaje de Panther Wallet*\n\n{msg}",parse_mode="Markdown")
            sent+=1
        except: failed+=1
    await update.message.reply_text(f"✅ Enviados: {sent} | Fallidos: {failed}")

async def cmd_ganadores_ruleta(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db(); usdt_w=[]; pnt_w=[]
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict): continue
        nombre=str(data.get("username") or data.get("first_name") or uid)
        for h in data.get("history",[]):
            prize=(h.get("prize") or "").upper()
            if prize=="USDT": usdt_w.append(f"- {nombre} (ID:{uid}) {h.get('date','')} {h.get('time','')}")
            elif prize=="PNT": pnt_w.append(f"- {nombre} (ID:{uid}) {h.get('date','')} {h.get('time','')}")
    lines=["Ganadores Ruleta\n",f"USDT — {len(usdt_w)} ganador(es)"]
    lines.extend(usdt_w or ["- Ninguno"])
    lines.append(f"\nPNT — {len(pnt_w)} ganador(es)")
    lines.extend(pnt_w or ["- Ninguno"])
    await update.message.reply_text("\n".join(lines))

async def cmd_stats_referidos(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db()
    users={uid:d for uid,d in db.items() if not uid.startswith("_") and isinstance(d,dict) and "points" in d}
    total=len(users); con_wallet=sum(1 for d in users.values() if d.get("wallet_activated"))
    por_referido=sum(1 for d in users.values() if d.get("referred_by"))
    cazadores=sum(1 for d in users.values() if d.get("cazador_verificado"))
    top=sorted(users.items(),key=lambda x:len(x[1].get("referrals",[])),reverse=True)[:5]
    lines=["STATS MANADA PANTHER\n",f"Total usuarios: {total}",f"Con wallet: {con_wallet}",
        f"Por referido: {por_referido}",f"Cazadores: {cazadores}","\nTOP REFERIDORES"]
    for uid,d in top:
        n=str(d.get("username") or d.get("first_name") or uid)
        lines.append(f"- {n}: {len(d.get('referrals',[]))} refs ({d.get('referrals_active',0)} activos)")
    await update.message.reply_text("\n".join(lines))

async def cmd_links_campana(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    base="https://t.me/ManadaPantherBot?start="
    lines=["Links de campaña:\n",f"Instagram:\n{base}camp_ig",f"\nEmail:\n{base}camp_mail",
        f"\nTikTok:\n{base}camp_tk",f"\nSitio Web:\n{base}camp_web"]
    await update.message.reply_text("\n".join(lines))

async def cmd_pingmods(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    results=[]
    for mod_id in MOD_IDS:
        try: await context.bot.send_message(chat_id=mod_id,text="🔔 Test OK"); results.append(f"✅ {mod_id}")
        except Exception as e: results.append(f"❌ {mod_id}: {e}")
    await update.message.reply_text("Resultados:\n"+"\n".join(results))

async def cmd_resetcheck(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: await update.message.reply_text("❌ Sin permisos."); return
    db=load_db(); uid=str(update.effective_user.id)
    if uid in db:
        db[uid]["last_checkin"]=None; db[uid]["last_ruleta"]=None; save_db(db)
        await update.message.reply_text("✅ Check-in y ruleta reseteados.")
    else: await update.message.reply_text("❌ Usuario no encontrado.")

async def cmd_buscar(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    if not context.args: await update.message.reply_text("Uso: /buscar @username"); return
    q=context.args[0].lstrip("@").lower(); db=load_db()
    found=[d for uid,d in db.items() if not uid.startswith("_") and isinstance(d,dict)
        and (q in (d.get("username") or "").lower() or q in (d.get("first_name") or "").lower())]
    if not found: await update.message.reply_text("No encontrado."); return
    lines=["Usuarios encontrados:\n"]
    for u in found[:10]:
        n=u.get("username") or u.get("first_name") or "?"
        lines.append(f"@{n} — ID: {u.get('id','?')} — {u.get('points',0)} pts")
    await update.message.reply_text("\n".join(lines))

async def cmd_recompensa_todos(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    if not context.args: await update.message.reply_text("Uso: /recompensa_todos cantidad motivo"); return
    try: amount=int(context.args[0])
    except: await update.message.reply_text("La cantidad debe ser un número."); return
    if amount<=0 or amount>10000: await update.message.reply_text("Entre 1 y 10000."); return
    motivo=" ".join(context.args[1:]) if len(context.args)>1 else "Recompensa especial"
    db=load_db(); count=0
    for uid,data in db.items():
        if uid.startswith("_") or not isinstance(data,dict) or "points" not in data: continue
        add_points(data,amount); count+=1
        try: await context.bot.send_message(chat_id=int(uid),
            text=f"🎁 +{amount} puntos\nMotivo: {motivo}\nTotal: {data['points']} pts 🐾")
        except: pass
    save_db(db); await update.message.reply_text(f"✅ +{amount} pts a {count} usuarios.")

async def cmd_enviar_badges(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: return
    db=load_db()
    users=[(uid,d) for uid,d in db.items() if not uid.startswith("_") and isinstance(d,dict) and "points" in d]
    await update.message.reply_text(f"📤 Enviando badges a {len(users)} usuarios...")
    sent=failed=0
    for i,(uid,data) in enumerate(users):
        number=data.get("founder_number",i+1)
        if not data.get("founder_number"): data["founder_number"]=i+1; db[uid]=data
        fname=data.get("first_name") or data.get("username") or "Miembro"
        success=await send_founder_badge(context.bot,uid,fname,number)
        if success: sent+=1
        else: failed+=1
        await asyncio.sleep(0.3)
    save_db(db); await update.message.reply_text(f"✅ Badges: {sent} enviados, {failed} fallidos.")

async def cmd_award(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS: await update.message.reply_text("Sin permisos."); return
    if len(context.args)<2: await update.message.reply_text("Uso: /award @usuario cantidad razon"); return
    username=context.args[0].lstrip("@")
    try: amount=int(context.args[1])
    except: await update.message.reply_text("La cantidad debe ser un número."); return
    if amount<=0 or amount>500: await update.message.reply_text("Entre 1 y 500."); return
    reason=" ".join(context.args[2:]) if len(context.args)>2 else "Premio especial"
    mod_name=f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
    uid_found=next((uid for uid,d in CHAT_STARS.items() if d.get("username","").lower()==username.lower()),None)
    if not uid_found:
        uid_found=f"@{username}"; CHAT_STARS[uid_found]={"stars":0,"pts":0,"username":username,"first_name":username}
    CHAT_STARS[uid_found]["pts"]+=amount
    if uid_found not in CHAT_AWARDS: CHAT_AWARDS[uid_found]=[]
    CHAT_AWARDS[uid_found].append({"pts":amount,"reason":reason,"mod":mod_name})
    save_chat_stars()
    await update.message.reply_text(
        f"🏆 {mod_name} le otorgó +{amount} pts a @{username}\nMotivo: {reason}\nTotal: {CHAT_STARS[uid_found]['pts']} pts")

# ══════════════════════════════════════════════════════════════════════════════
# ── HANDLERS (fotos, callbacks, web_app, chat stars) ──────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def handle_photo(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private": return
    user=update.effective_user; db=load_db(); uid=str(user.id); data=get_user(db,uid,user)
    raw_name=f"@{user.username}" if user.username else user.first_name
    name_md=escape_md(raw_name)
    if data.get("pending_wallet_proof"):
        data["pending_wallet_proof"]=False; save_db(db)
        await update.message.reply_text(
            f"✅ *¡Captura recibida!* Gracias {name_md}.\nUn moderador verifica en las próximas 24h 🐾",
            parse_mode="Markdown")
        referred_by=data.get("referred_by")
        keyboard_wallet=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Aprobar wallet (+150 pts al referidor)",callback_data=f"wallet_{uid}_{referred_by}")],
            [InlineKeyboardButton("❌ Rechazar",callback_data=f"reject_{uid}")]])
        wallet_text=(f"🔐 *Prueba de wallet*\n\nUsuario: {raw_name} (ID: {uid})\n"
            f"Referido por: {referred_by or 'N/A'}")
        try:
            await context.bot.forward_message(chat_id=MOD_GROUP_ID,from_chat_id=update.effective_chat.id,message_id=update.message.message_id)
            await context.bot.send_message(chat_id=MOD_GROUP_ID,text=wallet_text,parse_mode="Markdown",reply_markup=keyboard_wallet)
        except Exception as e:
            logger.error(f"Error mod grupo: {e}")
            for mod_id in MOD_IDS:
                try:
                    await context.bot.forward_message(chat_id=mod_id,from_chat_id=update.effective_chat.id,message_id=update.message.message_id)
                    await context.bot.send_message(chat_id=mod_id,text=wallet_text,parse_mode="Markdown",reply_markup=keyboard_wallet)
                except: pass
        return
    today=date.today().isoformat()
    if data.get("last_mission_date")!=today:
        data["reel_count_today"]=data["story_count_today"]=data["content_count_today"]=0
        data["last_mission_date"]=today
    mission_type=PENDING_MISSIONS.pop(uid,None); MAX_DAILY=3
    tipo_labels={"reel":"🎬 Reel de Panther","story":"📸 Historia de Panther","content":"✏️ Contenido propio",
        "wallet_activate":"🔐 Activación de Wallet","review_store":"⭐ Review en Tienda",
        "review_trust":"🌟 Review en Trustpilot","comment_ig":"💬 Comentario IG",
        "comment_ig_last":"💬 Comentario Último Post IG","comment_tt":"💬 Comentario TikTok",
        "comment_tt_last":"💬 Comentario Último Video TikTok",None:"📎 Sin clasificar"}
    tipo_label=tipo_labels.get(mission_type,"📎 Sin clasificar")
    wallet_missions=["wallet_activate","review_store","review_trust","comment_ig","comment_ig_last","comment_tt","comment_tt_last"]
    if mission_type in wallet_missions: count_key=None
    elif mission_type in ["reel","story","content"]: count_key=f"{mission_type}_count_today"
    else: mission_type="content"; count_key="content_count_today"
    current_count=data.get(count_key,0) if count_key else 0
    if count_key and current_count>=MAX_DAILY:
        type_name={"reel":"reels","story":"historias","content":"contenidos"}.get(mission_type,"misiones")
        await update.message.reply_text(f"⚠️ Límite de {MAX_DAILY} {type_name} por día. Volvé mañana 🐾"); return
    if count_key:
        data[count_key]=current_count+1; remaining=MAX_DAILY-data[count_key]
        counter_msg=f"\n\n📊 {tipo_label}: *{data[count_key]}/{MAX_DAILY}* hoy · quedan *{remaining}*."
    else: counter_msg=""
    save_db(db)
    await update.message.reply_text(
        f"📸 ¡Captura recibida! Gracias {raw_name}.\nMisión: *{tipo_label}*{counter_msg}\n"
        f"\nUn moderador verifica en las próximas 24h 🐾",parse_mode="Markdown")
    mission_keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Reel (+{PTS['share_reel']})",callback_data=f"approve_{uid}_reel"),
         InlineKeyboardButton(f"✅ Historia (+{PTS['share_story']})",callback_data=f"approve_{uid}_story")],
        [InlineKeyboardButton(f"✅ Contenido (+{PTS['own_content']})",callback_data=f"approve_{uid}_content"),
         InlineKeyboardButton("✅ Wallet (+175)",callback_data=f"approve_{uid}_wallet_activate")],
        [InlineKeyboardButton("✅ Review Store (+175)",callback_data=f"approve_{uid}_review_store"),
         InlineKeyboardButton("✅ Review Trust (+175)",callback_data=f"approve_{uid}_review_trust")],
        [InlineKeyboardButton("💬 Comment IG (+5)",callback_data=f"approve_{uid}_comment_ig"),
         InlineKeyboardButton("💬 Último IG (+30)",callback_data=f"approve_{uid}_comment_ig_last")],
        [InlineKeyboardButton("💬 Comment TT (+5)",callback_data=f"approve_{uid}_comment_tt"),
         InlineKeyboardButton("💬 Último TT (+30)",callback_data=f"approve_{uid}_comment_tt_last")],
        [InlineKeyboardButton("❌ Rechazar",callback_data=f"reject_{uid}")],
    ])
    mission_text=(f"📸 *Captura de verificación*\nTipo: *{tipo_label}*\n"
        f"Usuario: {name_md} (ID: `{uid}`)\nPuntos: *{data['points']}*")
    notified=False
    try:
        await context.bot.forward_message(chat_id=MOD_GROUP_ID,from_chat_id=update.effective_chat.id,message_id=update.message.message_id)
        await context.bot.send_message(chat_id=MOD_GROUP_ID,text=mission_text,parse_mode="Markdown",reply_markup=mission_keyboard)
        notified=True
    except Exception as e: logger.error(f"Error mod grupo: {e}")
    if not notified:
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(chat_id=mod_id,from_chat_id=update.effective_chat.id,message_id=update.message.message_id)
                await context.bot.send_message(chat_id=mod_id,text=mission_text,parse_mode="Markdown",reply_markup=mission_keyboard)
            except: pass

async def handle_nuevo_cazador(update:Update,context:ContextTypes.DEFAULT_TYPE):
    msg=update.message
    if not msg or not msg.photo: return
    if update.effective_chat.type not in ("group","supergroup"): return
    caption=(msg.caption or "").lower()
    if "#nuevocazador" not in caption: return
    user=update.effective_user; uid=str(user.id); db=load_db(); data=get_user(db,uid,user)
    nombre=f"@{user.username}" if user.username else user.first_name
    if data.get("cazador_verificado"):
        try: await msg.reply_text(f"🐆 {nombre}, tu ritual ya fue verificado.")
        except: pass
        return
    referred_by=data.get("referred_by")
    if referred_by:
        ref_data=db.get(str(referred_by),{})
        ref_nombre=ref_data.get("username") or ref_data.get("first_name") or str(referred_by)
        ref_txt=f"Referido por: @{ref_nombre} (ID: {referred_by})"
    else: ref_txt="Sin referidor"
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar cazador",callback_data=f"cazador_ok_{uid}")],
        [InlineKeyboardButton("❌ Rechazar",callback_data=f"cazador_no_{uid}")]])
    mod_text=(f"🎯 *Nuevo Cazador — verificación pendiente*\n\nUsuario: {nombre} (ID: {uid})\n{ref_txt}")
    try: await msg.reply_text(f"Captura recibida {nombre}. Un moderador verifica 🐾")
    except: pass
    try:
        await context.bot.forward_message(chat_id=MOD_GROUP_ID,from_chat_id=update.effective_chat.id,message_id=msg.message_id)
        await context.bot.send_message(chat_id=MOD_GROUP_ID,text=mod_text,parse_mode="Markdown",reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"Error notif mods cazador: {e}")
        for mod_id in MOD_IDS:
            try:
                await context.bot.forward_message(chat_id=mod_id,from_chat_id=update.effective_chat.id,message_id=msg.message_id)
                await context.bot.send_message(chat_id=mod_id,text=mod_text,parse_mode="Markdown",reply_markup=keyboard)
            except: pass

async def handle_cazador_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    if update.effective_user.id not in MOD_IDS: await query.answer("Sin permisos.",show_alert=True); return
    data_str=query.data; db=load_db()
    if data_str.startswith("cazador_ok_"):
        target_uid=data_str.replace("cazador_ok_",""); target_data=db.get(target_uid)
        if not target_data: await query.edit_message_text("Error: usuario no encontrado."); return
        nombre=target_data.get("username") or target_data.get("first_name") or target_uid
        target_data["cazador_verificado"]=True; target_data["wallet_activated"]=True
        referred_by=target_data.get("referred_by"); ref_msg=""
        if referred_by:
            ref_data=db.get(str(referred_by),{})
            if ref_data:
                if target_uid not in ref_data.get("referrals",[]): ref_data.setdefault("referrals",[]).append(target_uid)
                ref_data["referrals_active"]=ref_data.get("referrals_active",0)+1; db[str(referred_by)]=ref_data
                ref_nombre=ref_data.get("username") or ref_data.get("first_name") or referred_by
                ref_msg=f"\nReferidor @{ref_nombre} actualizado."
                try: await context.bot.send_message(chat_id=int(referred_by),
                    text=f"Tu referido {nombre} completó el ritual de cazador 🐾")
                except: pass
        db[target_uid]=target_data; save_db(db)
        try: await context.bot.send_message(chat_id=int(target_uid),
            text="Tu ritual fue verificado. Sos oficialmente un Cazador de la Manada 🐆")
        except: pass
        await query.edit_message_text(f"✅ Cazador aprobado: @{nombre}{ref_msg}")
    elif data_str.startswith("cazador_no_"):
        target_uid=data_str.replace("cazador_no_",""); target_data=db.get(target_uid,{})
        nombre=target_data.get("username") or target_data.get("first_name") or target_uid
        try: await context.bot.send_message(chat_id=int(target_uid),
            text="Tu captura no pudo ser verificada. Asegurate que muestre Panther Wallet instalada.")
        except: pass
        await query.edit_message_text(f"❌ Cazador rechazado: @{nombre}")

async def cmd_star(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("⭐ Respondé el mensaje del usuario al que querés dar una estrella."); return
    giver=update.effective_user; receiver=update.message.reply_to_message.from_user
    if not receiver or receiver.id==giver.id: await update.message.reply_text("No podés darte estrellas 😄"); return
    if receiver.is_bot: await update.message.reply_text("Los bots no reciben estrellas 🤖"); return
    now=datetime.now().timestamp(); uid=str(giver.id)
    if uid not in STAR_COOLDOWN: STAR_COOLDOWN[uid]=[]
    STAR_COOLDOWN[uid]=[t for t in STAR_COOLDOWN[uid] if now-t<3600]
    if len(STAR_COOLDOWN[uid])>=5:
        secs=int(3600-(now-STAR_COOLDOWN[uid][0]))
        await update.message.reply_text(f"⏳ Ya diste 5 estrellas esta hora. Podés dar más en {secs//60} min."); return
    STAR_COOLDOWN[uid].append(now); pts=5 if update.message.reply_to_message.reply_to_message else 3
    rid=str(receiver.id)
    if rid not in CHAT_STARS: CHAT_STARS[rid]={"stars":0,"pts":0,"username":receiver.username or "","first_name":receiver.first_name or "Usuario"}
    CHAT_STARS[rid]["stars"]+=1; CHAT_STARS[rid]["pts"]+=pts
    giver_name=f"@{giver.username}" if giver.username else giver.first_name
    receiver_name=f"@{receiver.username}" if receiver.username else receiver.first_name
    await update.message.reply_text(
        f"⭐ {giver_name} le dio una estrella a {receiver_name}!\n+{pts} pts 🐾\nTotal: {CHAT_STARS[rid]['stars']} ⭐ · {CHAT_STARS[rid]['pts']} pts")
    try: await context.bot.send_message(chat_id=int(rid),text=f"⭐ Recibiste una estrella de {giver_name}!\n+{pts} pts.")
    except: pass

async def cmd_leaderboard(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not CHAT_STARS: await update.message.reply_text("🌟 Aún no hay estrellas. Usá /star!"); return
    sorted_users=sorted(CHAT_STARS.items(),key=lambda x:x[1]["pts"],reverse=True)[:10]
    medals=["🥇","🥈","🥉"]; lines=["🏆 Ranking de la Manada 🏆\n"]
    for i,(uid,data) in enumerate(sorted_users):
        medal=medals[i] if i<3 else f"{i+1}."
        name=f"@{data['username']}" if data.get("username") else data.get("first_name","Usuario")
        lines.append(f"{medal} {name} — {data.get('stars',0)} ⭐ · {data.get('pts',0)} pts")
    await update.message.reply_text("\n".join(lines))

async def cmd_mis_estrellas(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if uid in CHAT_STARS:
        d=CHAT_STARS[uid]
        await update.message.reply_text(
            f"Tus estrellas en la Manada\n\nEstrellas: {d.get('stars',0)}\nPuntos: {d.get('pts',0)}\n\nUsá /leaderboard para el ranking.")
    else: await update.message.reply_text("Todavía no tenés estrellas. Participá en el chat 🐾")

async def handle_web_app_data(update:Update,context:ContextTypes.DEFAULT_TYPE):
    try:
        data=json.loads(update.effective_message.web_app_data.data); action=data.get("action"); tipo=data.get("type","reel")
        if action=="share":
            tipo_label="reel de Instagram" if tipo=="reel" else "historia de Instagram"
            pts=PTS["share_reel"] if tipo=="reel" else PTS["share_story"]
            await update.message.reply_text(
                f"📸 *Enviá tu captura de {tipo_label}*\n\nEnviala acá 👇\nSi se aprueba: *+{pts} pts* 🎉",
                parse_mode="Markdown")
    except Exception as e: logger.error(f"Error web_app_data: {e}")

async def handle_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); data_str=query.data
    if data_str.startswith("wallet_"):
        if query.from_user.id not in MOD_IDS: await query.answer("❌ Sin permisos.",show_alert=True); return
        parts=data_str.split("_"); target_uid=parts[1]; referrer_uid=parts[2] if len(parts)>2 else None
        db=load_db()
        if target_uid in db: db[target_uid]["wallet_activated"]=True
        if referrer_uid and referrer_uid in db:
            earned=add_points(db[referrer_uid],PTS["referral_wallet"])
            db[referrer_uid]["referrals_active"]=db[referrer_uid].get("referrals_active",0)+1
            save_db(db)
            try: await context.bot.send_message(chat_id=int(referrer_uid),
                text=f"🎉 *¡Tu referido activó su wallet!*\n\n*+{earned} puntos* 🐆",parse_mode="Markdown")
            except: pass
        else: save_db(db)
        await query.edit_message_text("✅ Wallet aprobada."); return
    if data_str.startswith("approve_") or data_str.startswith("reject_"):
        if query.from_user.id not in MOD_IDS: await query.answer("❌ Sin permisos.",show_alert=True); return
        parts=data_str.split("_"); action=parts[0]; target_uid=parts[1]; tipo="_".join(parts[2:]) if len(parts)>2 else None
        db=load_db()
        if target_uid not in db: await query.edit_message_text("❌ Usuario no encontrado."); return
        mod_name=query.from_user.first_name or str(query.from_user.id)
        if action=="approve" and tipo:
            pts_map={"reel":PTS["share_reel"],"story":PTS["share_story"],"content":PTS["own_content"],
                "wallet_activate":175,"review_store":175,"review_trust":175,
                "comment_ig":5,"comment_ig_last":30,"comment_tt":5,"comment_tt_last":30}
            earned=add_points(db[target_uid],pts_map.get(tipo,0))
            if tipo=="wallet_activate":
                db[target_uid]["wallet_activated"]=True
                referrer_uid=db[target_uid].get("referred_by")
                if referrer_uid and referrer_uid in db:
                    ref_earned=add_points(db[referrer_uid],PTS["referral_wallet"])
                    db[referrer_uid]["referrals_active"]=db[referrer_uid].get("referrals_active",0)+1
                    try: await context.bot.send_message(chat_id=int(referrer_uid),
                        text=f"🎉 *¡Tu referido activó su wallet!*\n\n*+{ref_earned} pts* 🐆",parse_mode="Markdown")
                    except: pass
            elif tipo=="review_store": db[target_uid]["review_store_done"]=True
            elif tipo=="review_trust": db[target_uid]["review_trust_done"]=True
            save_db(db); await query.answer(f"✅ Aprobado +{earned} pts")
            try: await query.edit_message_text(f"✅ Aprobado ({tipo}) — +{earned} pts\nPor: {mod_name}")
            except: pass
            try: await context.bot.send_message(chat_id=int(target_uid),
                text=f"✅ *¡Misión verificada!*\n\n➕ *+{earned} puntos* 🐾\n⭐ Total: *{db[target_uid]['points']}*",
                parse_mode="Markdown")
            except: pass
        elif action=="reject":
            save_db(db); await query.answer("❌ Rechazado")
            try: await query.edit_message_text(f"❌ Rechazado\nPor: {mod_name}")
            except: pass
            try: await context.bot.send_message(chat_id=int(target_uid),text="❌ Tu captura no fue verificada. Intentá de nuevo 🐾")
            except: pass
        return
    async def redirect_app(upd,ctx):
        uid=str(upd.effective_user.id); app_url=f"https://go.mypanther.io/app?id={uid}&v=3"
        from telegram import WebAppInfo
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🐆 Abrir Manada Panther",web_app=WebAppInfo(url=app_url))]])
        await upd.message.reply_text("Tocá el botón para abrir la Mini App.",reply_markup=kb)
    handlers_map={"checkin":redirect_app,"puntos":redirect_app,"ranking":redirect_app,
        "ruleta":redirect_app,"misiones":redirect_app,"referido":redirect_app}
    if data_str in handlers_map:
        fake=type("Update",(),{"effective_user":query.from_user,"effective_chat":query.message.chat,
            "message":query.message,"callback_query":query})()
        await handlers_map[data_str](fake,context)
    elif data_str=="niveles":
        fake=type("Update",(),{"effective_user":query.from_user,"effective_chat":query.message.chat,
            "message":query.message,"callback_query":query})()
        await cmd_niveles(fake,context)

# ══════════════════════════════════════════════════════════════════════════════
# ── API HTTP MINI APP ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class MiniAppHandler(BaseHTTPRequestHandler):
    def log_message(self,format,*args): pass

    def send_json(self,data,status=200):
        body=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; params=parse_qs(parsed.query)

        if path=="/user":
            uid=params.get("id",[None])[0]
            if not uid: return self.send_json({"error":"Missing id"},400)
            db=load_db(); data=db.get(uid)
            if not data: return self.send_json({"error":"User not found"},404)
            if not isinstance(data.get("referrals"),list): data["referrals"]=[]; db[uid]=data; save_db(db)
            level=get_level(data["points"]); next_lv,pts_needed=get_next_level(data["points"])
            today=date.today().isoformat()
            level_idx=next((i for i,(mn,mx,name) in enumerate(LEVELS) if name==level),0)
            level_max=LEVELS[level_idx][1]; level_min=LEVELS[level_idx][0]
            xp_pct=round((data["points"]-level_min)/max(level_max-level_min,1)*100,1) if level_max<999999 else 100
            history=data.get("history",[])[-5:]
            return self.send_json({
                "id":uid,"username":data.get("username",""),"first_name":data.get("first_name",""),
                "points":data["points"],"streak":data["streak"],"level":level,"level_idx":level_idx,
                "xp_pct":xp_pct,"level_min":level_min,"level_max":level_max,
                "next_level":next_lv,"pts_to_next":pts_needed,
                "referrals":len(data.get("referrals",[])),
                "referrals_active":data.get("referrals_active",0),
                "referral_code":data.get("referral_code",""),
                "checkin_today":data.get("last_checkin")==today,
                "ruleta_today":data.get("last_ruleta")==today,
                "ruleta_active":is_ruleta_active(),"ruleta_access":can_access_ruleta(data),
                "spins_available":get_available_spins(data),"spins_used":data.get("spins_used_this_event",0),
                "reel_verified":data.get("reel_verified",False),"story_verified":data.get("story_verified",False),
                "follow_ig":data.get("follow_ig",False),"follow_x":data.get("follow_x",False),
                "follow_tiktok":data.get("follow_tiktok",False),"follow_facebook":data.get("follow_facebook",False),
                "follow_youtube":data.get("follow_youtube",False),
                "wallet_activated":bool(data.get("wallet_activated",False)),
                "review_store_done":bool(data.get("review_store_done",False)),
                "review_trust_done":bool(data.get("review_trust_done",False)),
                "cazador_verificado":bool(data.get("cazador_verificado",False)),
                "evento_pnt_ganado":data.get("evento_pnt_ganado",0),"history":history})

        elif path=="/ranking":
            db=load_db(); valid=[u for u in db.values() if isinstance(u,dict) and "points" in u]
            top20=sorted(valid,key=lambda x:x["points"],reverse=True)[:20]
            return self.send_json([{"pos":i+1,"id":u.get("id",""),"username":u.get("username",""),
                "first_name":u.get("first_name",""),"points":u.get("points",0),"level":get_level(u.get("points",0))}
                for i,u in enumerate(top20)])

        elif path=="/evento":
            uid=params.get("id",[None])[0]; db=load_db(); ev=get_evento_globals(db)
            total_caz=get_total_cazadores(db); total_refs=get_total_referidos_activos_campana(db)
            dias=dias_transcurridos(db); fecha_cierre=get_fecha_cierre(db); usuario_data={}
            if uid:
                refs_u=get_referidos_evento(db,uid)
                pnt,_,_,califica=calcular_pnt_usuario(db,uid)
                usuario_data={"referidos_validos":refs_u,"califica":califica,"pnt_estimado":pnt,
                    "min_referidos":ev.get("min_referidos",3)}
            top5=get_top_referidores(db,5)
            return self.send_json({
                "activo":ev.get("activo",False),"cerrado":ev.get("cerrado",False),
                "total_cazadores":total_caz,"objetivo":ev.get("objetivo",1000),
                "pool_pnt":ev.get("pool_pnt",1125),"dias_transcurridos":dias,
                "dias_limite":ev.get("dias_base",20)+ev.get("dias_extra",0),
                "fecha_cierre":str(fecha_cierre) if fecha_cierre else None,
                "pct_objetivo":round(total_caz/1000*100,1),"usuario":usuario_data,
                "top5":[{"uid":u,"username":nombre,"referidos":refs} for u,nombre,refs,pts in top5]})

        elif path=="/ruleta":
            uid=params.get("id",[None])[0]
            if not uid: return self.send_json({"error":"Missing id"},400)
            db=load_db(); data=get_user(db,uid); today=date.today().isoformat()
            if not is_ruleta_active():
                return self.send_json({"available":False,"reason":"dates","message":"La ruleta se habilita el día 15 o 30"})
            if not can_access_ruleta(data):
                return self.send_json({"available":False,"reason":"missions","message":f"Necesitás racha de 3 días"})
            spins_used=data.get("spins_used_this_event",0); spins_available=get_available_spins(data)
            if spins_used>=spins_available: return self.send_json({"already_done":True,"points":data["points"]})
            result_label,pts_gain,special,_=spin_ruleta()
            data["last_ruleta"]=today; data["spins_used_this_event"]=spins_used+1
            prize_type=prize_amount=None
            if special=="x2":
                until=datetime.now()+timedelta(hours=24); data["double_pts_until"]=until.isoformat()
                prize_type="x2"; prize_amount="x2"
            elif special=="usdt":
                if has_won_this_month(data,"usdt"): pts_gain=50; special=None
                else:
                    mark_won_month(data,"usdt"); prize_type="USDT"; prize_amount=get_usdt_prize() or "$5"
            elif special=="pnt":
                if has_won_this_month(data,"pnt"): pts_gain=30; special=None
                else:
                    mark_won_month(data,"pnt"); prize_type="PNT"; prize_amount=str(get_pnt_prize() or 50)
            earned=add_points(data,pts_gain)
            if "history" not in data: data["history"]=[]
            data["history"].append({"type":"ruleta","pts":earned,"date":today,"time":datetime.now().strftime("%H:%M"),
                "prize":prize_type,"prize_amount":prize_amount})
            db[uid]=data; save_db(db)
            if prize_type and CombinedHandler.tg_app:
                username=data.get("username") or data.get("first_name") or uid
                asyncio.run_coroutine_threadsafe(
                    notify_mods(CombinedHandler.tg_app,f"🎰 *PREMIO RULETA*\n\n👤 @{username} (ID:`{uid}`)\n🏆 {prize_amount} {prize_type}"),
                    CombinedHandler.tg_loop)
            return self.send_json({"status":"ok","result":result_label,"pts_gained":earned,
                "points":data["points"],"prize_type":prize_type,"prize_amount":prize_amount,"already_done":False})

        elif path=="/app":
            try:
                with open("Manada Panther .html","r",encoding="utf-8") as f: html=f.read()
                self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Cache-Control","no-cache")
                self.end_headers(); self.wfile.write(html.encode())
            except Exception as e: self.send_json({"error":str(e)},404)

        elif path=="/debug":
            db=load_db(); return self.send_json({"user_count":len(db),"users":list(db.keys())[:10]})

        elif path=="/admin/stats":
            key=params.get("key",[None])[0]
            if key!="panther2026": self.send_response(403); self.end_headers(); self.wfile.write(b"Acceso denegado"); return
            db=load_db(); users=[v for v in db.values() if isinstance(v,dict) and "points" in v]
            total=len(users); con_wallet=sum(1 for u in users if u.get("wallet_activated"))
            por_referido=sum(1 for u in users if u.get("referred_by"))
            total_pts=sum(u.get("points",0) for u in users)
            cazadores=sum(1 for u in users if u.get("cazador_verificado"))
            top5_refs=sorted(users,key=lambda x:len(x.get("referrals",[])),reverse=True)[:5]
            html=(f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Stats</title>"
                f"<style>body{{background:#0a0a0a;color:#eee;font-family:sans-serif;padding:24px;max-width:800px;margin:0 auto}}"
                f"h1{{color:#FF5C1A}}table{{border-collapse:collapse;width:100%;margin:16px 0}}"
                f"th{{background:#1a1a1a;color:#FF5C1A;padding:8px 12px;text-align:left}}"
                f"td{{padding:8px 12px;border-bottom:1px solid #1e1e1e}}</style></head><body>"
                f"<h1>🐆 Manada Panther Stats</h1>"
                f"<table><tr><th>Métrica</th><th>Valor</th></tr>"
                f"<tr><td>Total usuarios</td><td><b>{total}</b></td></tr>"
                f"<tr><td>Con wallet activa</td><td><b>{con_wallet}</b></td></tr>"
                f"<tr><td>Por referido</td><td><b>{por_referido}</b></td></tr>"
                f"<tr><td>Cazadores</td><td><b>{cazadores}</b></td></tr>"
                f"<tr><td>Total pts emitidos</td><td><b>{total_pts:,}</b></td></tr></table>"
                f"<h2>Top 5 Referidores</h2><table><tr><th>Usuario</th><th>Referidos</th><th>Puntos</th></tr>"
                + "".join(f"<tr><td>@{u.get('username') or u.get('first_name','?')}</td><td>{len(u.get('referrals',[]))}</td><td>{u.get('points',0)}</td></tr>" for u in top5_refs)
                + "</table></body></html>")
            html_bytes=html.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(html_bytes))); self.end_headers(); self.wfile.write(html_bytes)

        else: self.send_json({"status":"Panther Mini App API","version":"2.0"})

    def do_POST(self):
        parsed=urlparse(self.path); path=parsed.path
        length=int(self.headers.get("Content-Length",0))
        body=json.loads(self.rfile.read(length)) if length else {}

        if path=="/checkin":
            uid=body.get("id")
            if not uid: return self.send_json({"error":"Missing id"},400)
            db=load_db(); data=get_user(db,uid)
            today=date.today().isoformat(); yesterday=(date.today()-timedelta(days=1)).isoformat()
            last=data.get("last_checkin")
            if last==today: return self.send_json({"already_done":True,"points":data["points"]})
            data["streak"]=(data["streak"]+1) if last==yesterday else 1; streak=data["streak"]
            base_pts=PTS["checkin_1_3"] if streak<=3 else PTS["checkin_4_6"]; bonus=0
            if streak==7: bonus=PTS["streak_7"]
            elif streak==14: bonus=PTS["streak_14"]
            elif streak==30: bonus=PTS["streak_30"]
            old_pts=data["points"]; earned=add_points(data,base_pts+bonus); data["last_checkin"]=today
            if "history" not in data: data["history"]=[]
            data["history"].append({"type":"checkin","pts":earned,"date":today,"time":datetime.now().strftime("%H:%M")})
            data["history"]=data["history"][-20:]
            old_lv=get_level(old_pts); new_lv=get_level(data["points"]); save_db(db)
            return self.send_json({"success":True,"earned":earned,"points":data["points"],
                "streak":streak,"level":new_lv,"level_up":old_lv!=new_lv,"bonus":bonus})

        elif path=="/set_mission_type":
            uid=body.get("id"); mission_type=body.get("type")
            valid_types=["reel","story","content","wallet_activate","review_store","review_trust",
                "comment_ig","comment_ig_last","comment_tt","comment_tt_last"]
            if not uid or mission_type not in valid_types: return self.send_json({"error":"Invalid params"},400)
            PENDING_MISSIONS[uid]=mission_type
            return self.send_json({"status":"ok","type":mission_type})

        elif path=="/follow":
            uid=body.get("id"); red=body.get("red")
            if not uid or red not in ["ig","x","tiktok","facebook","youtube"]:
                return self.send_json({"error":"Invalid params"},400)
            db=load_db(); data=get_user(db,uid); field=f"follow_{red}"
            if data.get(field): return self.send_json({"already_done":True,"points":data["points"]})
            earned=add_points(data,PTS[field]); data[field]=True
            if "history" not in data: data["history"]=[]
            data["history"].append({"type":f"follow_{red}","pts":earned,"date":date.today().isoformat(),"time":datetime.now().strftime("%H:%M")})
            data["history"]=data["history"][-20:]
            bonus=0
            if (data.get("follow_ig") and data.get("follow_x") and data.get("follow_tiktok")
                    and data.get("follow_facebook") and data.get("follow_youtube") and not data.get("follow_all_bonus")):
                bonus=add_points(data,PTS["follow_all_bonus"]); data["follow_all_bonus"]=True
            db[uid]=data; save_db(db)
            return self.send_json({"status":"ok","earned":earned,"bonus":bonus,"points":data["points"]})

        else: self.send_json({"error":"Not found"},404)


class CombinedHandler(MiniAppHandler):
    tg_app=None; tg_loop=None
    def do_POST(self):
        parsed=urlparse(self.path); path=parsed.path; token_path=f"/webhook/{TOKEN}"
        if path==token_path:
            length=int(self.headers.get("Content-Length",0)); body=self.rfile.read(length)
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(b'{"ok":true}')
            if CombinedHandler.tg_app and CombinedHandler.tg_loop:
                try:
                    upd=Update.de_json(json.loads(body),CombinedHandler.tg_app.bot)
                    asyncio.run_coroutine_threadsafe(CombinedHandler.tg_app.process_update(upd),CombinedHandler.tg_loop)
                except Exception as e: logger.error(f"Error procesando update: {e}")
        else: super().do_POST()

def run_http_server():
    port=int(os.environ.get("PORT",8000)); server=HTTPServer(("0.0.0.0",port),MiniAppHandler)
    logger.info(f"🌐 API HTTP en puerto {port}"); server.serve_forever()

# ══════════════════════════════════════════════════════════════════════════════
# ── MAIN ──────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not TOKEN: print("❌ Falta BOT_TOKEN"); return
    download_fonts(); init_db(); load_chat_stars(); print("✅ DB inicializada")
    app=Application.builder().token(TOKEN).build()

    # Usuarios
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("checkin",cmd_checkin))
    app.add_handler(CommandHandler("puntos",cmd_puntos))
    app.add_handler(CommandHandler("ranking",cmd_ranking))
    app.add_handler(CommandHandler("niveles",cmd_niveles))
    app.add_handler(CommandHandler("referido",cmd_referido))
    app.add_handler(CommandHandler("ruleta",cmd_ruleta))
    app.add_handler(CommandHandler("misiones",cmd_misiones))
    app.add_handler(CommandHandler("compartir",cmd_compartir))
    app.add_handler(CommandHandler("ayuda",cmd_ayuda))
    app.add_handler(CommandHandler("mi_badge",cmd_mi_badge))
    app.add_handler(CommandHandler("mis_estrellas",cmd_mis_estrellas))
    app.add_handler(CommandHandler("leaderboard",cmd_leaderboard))
    app.add_handler(CommandHandler("star",cmd_star))
    app.add_handler(CommandHandler("verificar_follow",cmd_verificar_follow))

    # Mods — generales
    app.add_handler(CommandHandler("aprobar",cmd_aprobar))
    app.add_handler(CommandHandler("dar_puntos",cmd_dar_puntos))
    app.add_handler(CommandHandler("reset_ruleta",cmd_reset_ruleta))
    app.add_handler(CommandHandler("ruleta_on",cmd_ruleta_on))
    app.add_handler(CommandHandler("ruleta_off",cmd_ruleta_off))
    app.add_handler(CommandHandler("ruleta_auto",cmd_ruleta_auto))
    app.add_handler(CommandHandler("broadcast",cmd_broadcast))
    app.add_handler(CommandHandler("ganadores_ruleta",cmd_ganadores_ruleta))
    app.add_handler(CommandHandler("stats_referidos",cmd_stats_referidos))
    app.add_handler(CommandHandler("links_campana",cmd_links_campana))
    app.add_handler(CommandHandler("pingmods",cmd_pingmods))
    app.add_handler(CommandHandler("resetcheck",cmd_resetcheck))
    app.add_handler(CommandHandler("buscar",cmd_buscar))
    app.add_handler(CommandHandler("recompensa_todos",cmd_recompensa_todos))
    app.add_handler(CommandHandler("enviar_badges",cmd_enviar_badges))
    app.add_handler(CommandHandler("award",cmd_award))

    # Mods — Operación 1,000 Cazadores
    app.add_handler(CommandHandler("evento_start",cmd_evento_start))
    app.add_handler(CommandHandler("evento_stop",cmd_evento_stop))
    app.add_handler(CommandHandler("evento_status",cmd_evento_status))
    app.add_handler(CommandHandler("evento_preview_premios",cmd_evento_preview_premios))
    app.add_handler(CommandHandler("evento_cerrar",cmd_evento_cerrar))
    app.add_handler(CommandHandler("forzar_alerta",cmd_forzar_alerta))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_cazador_callback,pattern="^cazador_"))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Mensajes
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.GROUPS,handle_nuevo_cazador))
    app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA,handle_web_app_data))

    port=int(os.environ.get("PORT",8080))

    if WEBHOOK_URL:
        webhook_path=f"/webhook/{TOKEN}"; full_webhook_url=f"{WEBHOOK_URL}{webhook_path}"
        print(f"🐆 Panther Bot v2.0 — WEBHOOK: {full_webhook_url}")
        loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        async def init_app():
            await app.initialize(); await app.start()
            await app.bot.set_webhook(url=full_webhook_url,drop_pending_updates=True)
            print("✅ Webhook registrado")
        loop.run_until_complete(init_app())
        CombinedHandler.tg_app=app; CombinedHandler.tg_loop=loop
        server=HTTPServer(("0.0.0.0",port),CombinedHandler)
        print(f"🌐 HTTP en puerto {port}")
        def run_loop():
            async def job_alertas():
                while True:
                    try: await check_alertas_evento(app)
                    except Exception as e: logger.error(f"Error job alertas: {e}")
                    await asyncio.sleep(3600)
            asyncio.run_coroutine_threadsafe(job_alertas(),loop)
            loop.run_forever()
        loop_thread=threading.Thread(target=run_loop,daemon=True)
        loop_thread.start(); server.serve_forever()
    else:
        print("🐆 Panther Bot v2.0 — POLLING")
        http_thread=threading.Thread(target=run_http_server,daemon=True)
        http_thread.start(); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()
