# 🐆 PANTHER GAME BOT — Manada Panther

Bot de Telegram con sistema de puntos, niveles, racha diaria, ruleta y referidos.

---

## ✅ Comandos disponibles

| Comando | Descripción |
|---|---|
| `/start` | Registro y panel principal |
| `/checkin` | Check-in diario (racha de puntos) |
| `/puntos` | Ver tu perfil, nivel y puntos |
| `/ranking` | Top 20 del leaderboard |
| `/referido` | Tu código único de referido |
| `/ruleta` | Ruleta diaria (puntos o premios) |
| `/misiones` | Misiones disponibles del día |
| `/compartir` | Instrucciones para verificar capturas |
| `/aprobar` | (Solo mods) Aprobar misión de usuario |

---

## 🚀 Setup en Railway.app

### 1. Subir el código

Creá un repo en GitHub con estos archivos y conectalo a Railway.

### 2. Variables de entorno en Railway

En el panel de Railway, ir a **Variables** y agregar:

```
BOT_TOKEN=tu_token_de_botfather_aqui
MOD_IDS=123456789,987654321
```

- `BOT_TOKEN` → el token que te dio BotFather
- `MOD_IDS` → los IDs de Telegram de los moderadores, separados por coma

**¿Cómo saber tu ID de Telegram?**
Escribile a @userinfobot en Telegram y te dice tu ID.

### 3. Comando de inicio

En Railway, en **Settings → Start Command** poner:
```
python bot.py
```

---

## 📁 Archivos

```
panther_bot/
├── bot.py           # Código principal del bot
├── requirements.txt # Dependencias
├── README.md        # Este archivo
└── panther_db.json  # Base de datos (se crea automáticamente)
```

---

## 🎮 Sistema de puntos

| Acción | Puntos |
|---|---|
| Check-in días 1–3 | +5 pts/día |
| Check-in días 4–6 | +10 pts/día |
| Racha 7 días | +50 pts bonus |
| Racha 14 días | +150 pts bonus |
| Racha 30 días | +500 pts bonus |
| Referido se une | +25 pts |
| Referido activa wallet | +150 pts |
| Compartir reel | +30 pts |
| Compartir historia | +20 pts |
| Contenido propio | +100 pts |

---

## 🏅 Niveles

| Nivel | Puntos |
|---|---|
| 🐾 Cachorro | 0–99 |
| 🔍 Rastreador | 100–299 |
| 🛡️ Guardián | 300–599 |
| 🧭 Explorador | 600–999 |
| ⚡ Embajador | 1,000–2,000 |
| 🐆 Alfa | 2,001–5,000 |
| 👑 Leyenda | 5,000+ |

---

## 📝 Próximos módulos

- [ ] Módulo 2: Leaderboard automático (publica rankings los lunes)
- [ ] Módulo 3: Misiones encadenadas (rachas semanales)
- [ ] Módulo 4: Integración con API de Panther Wallet (referidos reales)
- [ ] Módulo 5: Panel de admin para gestionar premios
