# BullCore Capital — Sistema de Alertas de Flujo Institucional

## Descripción del Proyecto

Sistema de monitoreo en tiempo real del flujo de opciones institucional sobre futuros `/ES` (E-mini S&P 500) y `/GC` (Oro). Conectado directamente al feed DXFeed de Tastytrade vía WebSocket, detecta patrones de flujo — Sweep Burst, Block Print, Pressure Cooker — y los envía como alertas a Telegram.

El sistema corre en Railway con pausa automática de fin de semana (viernes 18:00 ET → domingo 18:00 ET).

---

## Stack Técnico

| Componente | Tecnología |
|-----------|-----------|
| Lenguaje | Python 3.12+ |
| Feed de mercado | Tastytrade DXLinkStreamer (DXFeed WebSocket) |
| SDK | `tastytrade==9.13` |
| Scheduler | `APScheduler==3.11.2` (AsyncIOScheduler) |
| Notificaciones | Telegram Bot API via `httpx==0.28.1` |
| Deploy | Railway (cloud, redeploy automático por push) |
| Config | `python-dotenv==1.2.2` |

---

## Horario de Operación

| Periodo | Estado |
|---------|--------|
| Domingo 18:00 ET → Viernes 18:00 ET | ✅ Stream activo |
| Viernes 18:00 ET → Domingo 18:00 ET | ⏸ Stream pausado (proceso vivo, sesión renovándose) |

El proceso **nunca muere** durante el fin de semana — permanece en espera para evitar que Railway lo reinicie en loop. La renovación de sesión (`renew_session`) y el watchdog de expiración siguen corriendo durante la pausa.

---

## Arquitectura — Flujo de un Trade

```
CME (Exchange)
    │  trade ejecutado en pit electrónico
    ▼
DXFeed (data vendor de Tastytrade)
    │  WebSocket feed en tiempo real
    ▼
DXLinkStreamer (tastytrade SDK)
    │  eventos: Trade, Quote, Greeks
    ▼
tasty_stream.py — TastyAlertSystem
    │
    ├─ _handle_trades()     → VolumeTracker → BlockPrintEngine
    │                                       → PressureCookerEngine
    ├─ _handle_quotes()     → actualiza bid/ask en _contract_meta
    ├─ _handle_greeks()     → actualiza delta, IV en _contract_meta
    └─ _periodic_checks()   → SweepBurstEngine (snapshot cada 15s)
                            → heartbeat de diagnóstico cada 60s
    │
    ▼
alert_engine.py — detección de patrones
    │
    ├─ Canal Telegram (TELEGRAM_CHAT_ID)     → alertas de trading
    └─ Chat privado (TELEGRAM_PRIVATE_CHAT_ID) → estado del sistema
```

### Ciclo de vida de la sesión

```
Arranque
  → _is_weekend_now()     (si es fin de semana, entra en modo pausa directamente)
  → _make_session()       (prioridad: TT_SESSION_JSON env → session.json → user/pass)
  → DXLinkStreamer abre WebSocket
  → load_chain()          (carga cadena 0DTE, registra símbolos, obtiene precio futuro)
  → subscribe(Trade, Quote, Greeks) para todos los contratos
  → asyncio.create_task(run_session(), name="run_session")
  → asyncio.gather() lanza 4 handlers concurrentes

Viernes 18:00 ET
  → _enter_weekend()      (cancela la task "run_session", pone _weekend.set())
  → main loop espera en bucle de 5 min hasta que _weekend se limpie

16:00 ET (lun-vie)
  → reload_chain()        (usa el MISMO streamer principal, re-suscribe nuevos contratos)

Cada 8 horas (24/7, incluyendo fin de semana)
  → renew_session.py      (renueva token, actualiza Railway via GraphQL)

Cada 1 hora (24/7)
  → _expiry_watchdog()    (renueva si la sesión expira en < 10h)

18:05 ET (lun-vie)
  → verify_stream()       (confirma contratos activos)

Domingo 18:00 ET
  → _exit_weekend()       (limpia _weekend, main loop reconecta solo)
```

---

## Los Tres Triggers

### 1. Sweep Burst `🌊`

Detecta ráfagas coordinadas: múltiples strikes de la **misma dirección** con volumen elevado en una ventana de 60 segundos. Señal de institucional distribuyendo en varios strikes simultáneamente.

**Condición de disparo:**
- ≥ `SWEEP_BURST_MIN_CONTRACTS_B` = **3** contratos distintos
- Cada uno con ≥ `SWEEP_BURST_MIN_VOL_B` = **50** contratos vol en ask en 60s
- `|delta|` ≥ `MIN_DELTA` = **0.30**

**Cooldown:** `ALERT_COOLDOWN_SECONDS` = **120s** por dirección (CALL/PUT independiente).

**Evaluación:** `_periodic_checks()` cada 15s pasa un snapshot `{symbol → vol_1min}` al engine.

---

### 2. Block Print `🖨️`

Detecta una única transacción ≥ umbral en el T&S. No acumula — una orden, un disparo.

**Lógica de disparo:**
- Mercado (9:01–17:59 ET): `trade_size` ≥ **150**, `|delta|` ≥ **0.40**
- Fuera de mercado (18:00–9:00 ET): `trade_size` ≥ **100**, `|delta|` ≥ **0.30**
- `|delta|` ≤ `BLOCK_PRINT_MAX_DELTA` = **0.90** (ambos horarios)
- Cooldown: `BLOCK_PRINT_COOLDOWN_SECONDS` = **120s** por símbolo

**Evaluación:** event-driven — se llama en cada Trade recibido.

---

### 3. Pressure Cooker `🔥`

Detecta flujo sostenido en un único contrato a lo largo del tiempo — acumulación silenciosa que delata convicción direccional.

**Ventana 5 minutos:**
- `vol_5min` ≥ `PRESSURE_COOKER_5MIN_VOL` = **500** contratos
- Re-dispara cuando supera `último_vol_disparado + 500`
- Cooldown: **120s** por símbolo

**Filtro delta (ambas ventanas):** `MIN_DELTA` = **0.30** ≤ `|delta|` ≤ `MAX_DELTA` = **0.95**

**Evaluación:** `_periodic_checks()` cada 15s itera `_active_symbols`.

---

## Filtros Globales

Aplicados en `_handle_trades()` antes de pasar al engine:

| Filtro | Variable | Default | Descripción |
|--------|----------|---------|-------------|
| Precio mínimo de contrato | `MIN_CONTRACT_PRICE` | `$7.00` | Ignora contratos muy baratos (deep OTM) |
| Distancia máxima al strike | `MAX_STRIKE_DISTANCE_PCT` | `15%` | Solo carga strikes dentro del ±15% del precio del futuro |
| Delta mínimo (Sweep/PC) | `MIN_DELTA` | `0.30` | Filtra opciones muy OTM |
| Delta máximo (Sweep/PC) | `MAX_DELTA` | `0.95` | Filtra opciones muy ITM (no son flujo especulativo) |
| Delta mínimo (Block) | `BLOCK_PRINT_MIN_DELTA` | `0.40` | Más estricto para bloques |
| Delta máximo (Block) | `BLOCK_PRINT_MAX_DELTA` | `0.90` | |
| Vol máximo por ciclo | `MAX_VOL_DELTA_PER_CYCLE` | `500` | Cap de seguridad contra spikes de data |

---

## Variables de Entorno

### Requeridas (sin estas el sistema no arranca)

| Variable | Descripción |
|----------|-------------|
| `TT_USERNAME` | Email de la cuenta Tastytrade |
| `TT_PASSWORD` | Contraseña Tastytrade (para renovación desde Mac) |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram (`@BotFather`) |
| `TELEGRAM_CHAT_ID` | ID del canal de alertas (suscriptores) |
| `TELEGRAM_PRIVATE_CHAT_ID` | ID del chat privado (notificaciones de sistema y errores) |

### Sesión (Railway production)

| Variable | Descripción |
|----------|-------------|
| `TT_SESSION_JSON` | Sesión Tastytrade serializada en base64. Se genera con `python login.py` y se actualiza automáticamente por `renew_session.py`. |

### Railway (para auto-renovación de sesión)

| Variable | Dónde encontrarla |
|----------|------------------|
| `RAILWAY_API_TOKEN` | railway.app → Account Settings → Tokens → Create Token |
| `RAILWAY_PROJECT_ID` | railway.app → Project → Settings → General → Project ID |
| `RAILWAY_SERVICE_ID` | railway.app → Service → Settings → Service ID |
| `RAILWAY_ENVIRONMENT_ID` | railway.app → Environments → (hover sobre el env) → Copy ID |

### Subyacentes

| Variable | Default | Descripción |
|----------|---------|-------------|
| `WATCH_SYMBOLS` | `/ES` | Símbolos a monitorear, separados por coma. Ejemplo: `/ES,/GC` |

### Triggers (todos tienen defaults razonables)

| Variable | Default | Trigger |
|----------|---------|---------|
| `SWEEP_BURST_WINDOW_SECONDS` | `60` | Sweep |
| `SWEEP_BURST_MIN_CONTRACTS` | `5` | Sweep cond A |
| `SWEEP_BURST_MIN_VOL` | `50` | Sweep cond A |
| `SWEEP_BURST_MIN_CONTRACTS_B` | `3` | Sweep cond B |
| `SWEEP_BURST_MIN_VOL_B` | `50` | Sweep cond B |
| `ALERT_COOLDOWN_SECONDS` | `120` | Sweep |
| `MIN_DELTA` | `0.30` | Sweep + Pressure Cooker |
| `MAX_DELTA` | `0.95` | Sweep + Pressure Cooker |
| `BLOCK_PRINT_MIN_VOL` | `100` | Block Print |
| `BLOCK_PRINT_MIN_DELTA` | `0.40` | Block Print |
| `BLOCK_PRINT_MAX_DELTA` | `0.90` | Block Print |
| `BLOCK_PRINT_COOLDOWN_SECONDS` | `120` | Block Print |
| `PRESSURE_COOKER_5MIN_VOL` | `500` | Pressure Cooker |
| `PRESSURE_COOKER_COOLDOWN_SECONDS` | `120` | Pressure Cooker |

### Filtros de contrato

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MIN_CONTRACT_PRICE` | `7.00` | Precio mínimo del contrato en dólares |
| `MAX_STRIKE_DISTANCE_PCT` | `0.15` | Distancia máxima al strike como fracción del precio del futuro |

### Modo Raw (debugging / calibración)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `RAW_ALERT_MODE` | `false` | Si `true`, bypassa los engines y alerta por vol simple |
| `RAW_MIN_PRICE` | `10.00` | Precio mínimo en modo raw |
| `RAW_MIN_VOL_1MIN` | `50` | Vol mínimo en 60s en modo raw |
| `RAW_COOLDOWN_SECONDS` | `120` | Cooldown en modo raw |

---

## Autenticación Tastytrade — Sistema TT_SESSION_JSON

Tastytrade usa sesiones con expiración. En Railway (donde no se puede hacer login interactivo), la sesión se serializa como JSON y se pasa como variable de entorno en base64.

### Generar sesión inicial (desde Mac)

```bash
python login.py
# Imprime el valor base64 para pegar como TT_SESSION_JSON en Railway
```

### Renovación automática desde Mac (LaunchAgent)

`auto_renew.py` corre como LaunchAgent macOS cada **6 horas** via `com.bullcore.autorenew.plist`.
- Autentica con username/password (Mac es dispositivo autorizado — sin OTP)
- Actualiza `session.json` local
- Actualiza `TT_SESSION_JSON` en Railway via GraphQL API
- Notifica el resultado al chat privado de Telegram

**Importante — macOS Sequoia TCC:** `launchd` no puede escribir en `~/Desktop/` para `StandardOutPath`. Los logs del agente van a `~/Library/Logs/bullcore_autorenew.log` (no al directorio del proyecto).

```bash
# Ver logs del LaunchAgent
cat ~/Library/Logs/bullcore_autorenew.log

# Recargar el agente tras cambios al plist
launchctl unload ~/Library/LaunchAgents/com.bullcore.autorenew.plist
launchctl load  ~/Library/LaunchAgents/com.bullcore.autorenew.plist
```

### Renovación manual (si falla la automática)

```bash
# Desde Mac:
python renew_session.py
```

Si Railway no se actualiza automáticamente, el chat privado de Telegram recibirá el base64 para pegarlo manualmente.

### Prioridad de autenticación en `_make_session()`

1. `TT_SESSION_JSON` env var (base64) — Railway production
2. `session.json` local — desarrollo / fallback
3. `remember_token` extraído de la sesión expirada
4. Login con `TT_USERNAME` + `TT_PASSWORD` — solo dispositivo conocido (Mac)

---

## Deploy en Railway

### Variables mínimas para funcionar

```
TT_USERNAME=...
TT_PASSWORD=...
TT_SESSION_JSON=<base64 generado con python login.py>
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_PRIVATE_CHAT_ID=...
```

### Variables para auto-renovación de sesión

```
RAILWAY_API_TOKEN=...
RAILWAY_PROJECT_ID=...
RAILWAY_SERVICE_ID=...
RAILWAY_ENVIRONMENT_ID=...
```

### Redeploy

Railway hace redeploy automático con cada `git push origin main`. También se puede forzar desde el dashboard de Railway → Service → Deploy.

### Logs en Railway

Los logs muestran la secuencia de arranque completa. Secuencia esperada al arrancar:

```
Iniciando sistema de alertas Tastytrade...
Scheduler activo — reload 16:00 ET (lun-vie), verificación 18:05 ET (lun-vie), ...
Sesión cargada desde TT_SESSION_JSON (expira ...)
=== load_chain() iniciado ===
  Hora ET: HH:MM:SS | today=YYYY-MM-DD | today_expired=False
  WATCH_SYMBOLS: ['/ES']
Cargando cadena /ES...
  Chain OK — option_chains=1, futures=N
  Expiración activa: YYYY-MM-DD (N strikes totales)
  Futuro front-month: /ESM5
  Precio /ESM5: XXXX.XX | Filtro distancia: ±15%
  /ES: NNN contratos registrados (M strikes descartados por distancia)
=== load_chain() completado: NNN contratos totales ===
=== Streaming activo — NNN contratos suscritos. Esperando eventos... ===
[DIAGNÓSTICO] Primer Trade recibido: sym=..., size=..., price=...
[HEARTBEAT] contratos=NNN | trades_total=X (+Y/min) | ...
```

**Señal de problema:** Si el heartbeat muestra `trades_total=0` con `contratos>0` después de varios minutos en horario de mercado, hay un problema de suscripción.

**Fin de semana:** Los logs mostrarán `Modo fin de semana — stream pausado` con checks cada 5 min. Normal.

---

## Bugs Conocidos y Fixes Aplicados

### Bug 1 — `reload_chain()` creaba un streamer paralelo (CRÍTICO, fix aplicado)

**Síntoma:** Tras el reload de las 16:00 ET, el stream quedaba ciego para los contratos del nuevo día.

**Causa:** `reload_chain()` abría un `DXLinkStreamer` nuevo, cargaba la cadena y cerraba el nuevo streamer sin registrar suscripciones en el streamer principal.

**Fix:** `reload_chain()` usa `self._streamer` (referencia al streamer principal activo) para cargar la cadena y suscribir en el mismo WebSocket.

### Bug 2 — Falta de visibilidad en el flujo (fix aplicado)

**Síntoma:** El stream conectaba pero no había forma de saber si los eventos llegaban o si las suscripciones estaban activas.

**Fix:** Logs detallados en `load_chain()`, confirmación de cada `subscribe()`, log del primer evento de cada tipo, heartbeat cada 60s con conteo de eventos por tipo.

### Bug 3 — `RuntimeError: Event loop stopped before Future completed.` en Python 3.14 (fix aplicado)

**Síntoma:** Al recibir SIGTERM (e.g. Railway redeploy), el proceso terminaba con `RuntimeError` en lugar de salir limpiamente.

**Causa (a):** El shutdown handler llamaba `_stop.set()` pero no cancelaba las tareas async activas. En Python 3.14, `asyncio.run()` lanza `RuntimeError` si quedan futures pendientes cuando el loop para.

**Causa (b):** El `try/except` exterior solo capturaba `KeyboardInterrupt` y `SystemExit`, dejando `RuntimeError` sin capturar.

**Fix:**
```python
# En _shutdown(): cancelar todas las tasks activas
for task in asyncio.all_tasks(loop):
    task.cancel()

# En __main__:
except (KeyboardInterrupt, SystemExit, RuntimeError):
    pass
```

### Bug 4 — LaunchAgent sin logs (macOS Sequoia TCC, fix aplicado)

**Síntoma:** `auto_renew.log` dejó de recibir entradas aunque el agente seguía corriendo (session.json se actualizaba).

**Causa:** macOS Sequoia bloquea que `launchd` abra archivos para `StandardOutPath` dentro de `~/Desktop/`. La salida del proceso iba a `/dev/null`.

**Fix:** Mover `StandardOutPath` y `StandardErrorPath` a `~/Library/Logs/bullcore_autorenew.log`. El proceso Python puede escribir en `~/Desktop/` directamente (tiene permisos de usuario), pero `launchd` no puede abrir descriptores de archivo ahí.

### Nota — Renovación automática de sesión

La renovación automática actualiza `TT_SESSION_JSON` en Railway via GraphQL pero **no provoca redeploy**. El proceso en memoria usa la nueva sesión directamente (`os.environ["TT_SESSION_JSON"] = new_b64`). El streamer principal no se reinicia.

---

## Notificaciones Telegram

### Canal de alertas (`TELEGRAM_CHAT_ID`) — para suscriptores

| Alerta | Cuándo |
|--------|--------|
| `🌊 SWEEP BURST` | Ráfaga coordinada multi-strike detectada |
| `🖨️ BLOCK PRINT` | Transacción única ≥ umbral en T&S |
| `🔥 PRESSURE COOKER` | Flujo sostenido acumulado en 5 min |

### Chat privado (`TELEGRAM_PRIVATE_CHAT_ID`) — para el operador

| Mensaje | Cuándo |
|---------|--------|
| `🚀 BullCore Capital V1 — EN VIVO` | Al arrancar `run_session()` |
| `⏸ Sistema pausado — mercado cerrado` | Viernes 18:00 ET |
| `▶️ Sistema reanudado — mercado abierto` | Domingo 18:00 ET |
| `⏸ Arranque en fin de semana` | Si el proceso arranca durante el fin de semana |
| `🔄 Cadena recargada — nueva sesión` | Al completar `reload_chain()` |
| `✅ Stream verificado (18:05 ET)` | Verificación automática diaria |
| `🔑 Sesion Tastytrade renovada` | Tras renovación exitosa de sesión |
| `❌ Fallo renovacion sesion` | Si falla la renovación automática |
| `⚠️ Railway NO actualizado` | Si Railway no pudo actualizarse (incluye base64 para pegar manualmente) |
| `🔐 Tastytrade requiere login manual` | Si se necesita OTP (remember_token expirado) |
| `⛔ Sistema de alertas detenido` | Al recibir SIGINT/SIGTERM |

---

## Formato de Alertas de Trading

### Sweep Burst `🌊`

```
🌊 SWEEP BURST  /ES  🟢 CALL  320 vol  6 strikes

1 Apr 2025  |  0 DTE
─────────────────────
5800C   Δ 0.52   75 vol   $12.50  ASK
5810C   Δ 0.48   68 vol   $11.00  ASK
5820C   Δ 0.44   55 vol   $9.50   MID
5830C   Δ 0.41   52 vol   $8.25   BID
5840C   Δ 0.37   40 vol   $7.00   BID
5850C   Δ 0.34   30 vol   $6.00   MID
─────────────────────
09:47:23 ET  |  via BullCore
```

### Block Print `🖨️`

```
🖨️ BLOCK PRINT  5800🟢  CALL  /ES  250 vol

1 Apr 2025  |  0 DTE  |  Δ 0.52  |  IV 18%
─────────────────────
Bid 12.25  |  Ask 12.75  |  Exec $12.75
─────────────────────
250 contratos en el ASK
10:15:44 ET  |  via BullCore
```

### Pressure Cooker `🔥`

```
🔥 PRESSURE COOKER  5800🟢  /ES  380 vol  5m

1 Apr 2025  |  0 DTE  |  Δ 0.52  |  IV 18%
─────────────────────
Tape  5m
10:14:01    45 @ $12.50  ASK
10:14:23    60 @ $12.75  ASK
10:14:55    80 @ $12.50  MID
10:15:12   110 @ $12.75  ASK
10:15:38    85 @ $12.75  ASK
─────────────────────
Total  380 contratos
10:15:44 ET  |  via BullCore
```
