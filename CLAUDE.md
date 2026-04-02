# BullCore Capital — Sistema de Alertas de Flujo Institucional

## Descripción del Proyecto

Sistema de monitoreo en tiempo real del flujo de opciones institucional sobre futuros `/ES` (E-mini S&P 500) y `/GC` (Oro). Conectado directamente al feed DXFeed de Tastytrade vía WebSocket, detecta patrones de flujo — Sweep Burst, Block Print, Pressure Cooker — y los envía como alertas a Telegram.

El sistema corre 24/7 en Railway con renovación automática de sesión cada 20 horas.

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
    ▼
telegram_notifier.py — formateo y envío
    │
    ▼
Telegram (canal privado)
```

### Ciclo de vida de la sesión

```
Arranque
  → _make_session()  (prioridad: TT_SESSION_JSON env → session.json → user/pass)
  → DXLinkStreamer abre WebSocket
  → load_chain()     (carga cadena 0DTE, registra símbolos, obtiene precio futuro)
  → subscribe(Trade, Quote, Greeks)  para todos los contratos
  → asyncio.gather() lanza los 4 handlers concurrentes

17:15 ET diario
  → reload_chain()   (usa el MISMO streamer principal, re-suscribe nuevos contratos)

Cada 20 horas
  → renew_session.py (renueva token via remember_token, actualiza Railway via GraphQL)

18:05 ET
  → verify_stream()  (confirma contratos activos, envía mensaje Telegram)
```

---

## Los Tres Triggers

### 1. Sweep Burst `🌊`

Detecta ráfagas coordinadas: múltiples strikes de la **misma dirección** con volumen elevado en una ventana de 60 segundos. Señal de institucional distribuyendo en varios strikes simultáneamente.

**Condición A** (amplitud normal):
- ≥ `SWEEP_BURST_MIN_CONTRACTS` = **5** contratos distintos
- Cada uno con ≥ `SWEEP_BURST_MIN_VOL` = **50** contratos vol en 60s
- `|delta|` ≥ `MIN_DELTA` = **0.30**

**Condición B** (amplitud reducida, vol alto):
- ≥ `SWEEP_BURST_MIN_CONTRACTS_B` = **3** contratos distintos
- Cada uno con ≥ `SWEEP_BURST_MIN_VOL_B` = **75** contratos vol en 60s
- `|delta|` ≥ `MIN_DELTA` = **0.30**

**Cooldown:** `ALERT_COOLDOWN_SECONDS` = **120s** por dirección (CALL/PUT independiente).

**Evaluación:** `_periodic_checks()` cada 15s pasa un snapshot `{symbol → vol_1min}` al engine.

---

### 2. Block Print `🖨️`

Detecta acumulación de volumen en un **único contrato** — señal de una sola entidad tomando posición grande.

**Lógica de disparo:**
- `vol_1min` ≥ `BLOCK_PRINT_MIN_VOL` = **100** contratos en ventana 60s
- `cum_vol` (acumulado de sesión) ≥ `último_disparo_vol + 100` (umbral incremental: dispara en 100, 200, 300...)
- `BLOCK_PRINT_MIN_DELTA` = **0.40** ≤ `|delta|` ≤ `BLOCK_PRINT_MAX_DELTA` = **0.90**
- Cooldown: `BLOCK_PRINT_COOLDOWN_SECONDS` = **120s** por símbolo

**Evaluación:** event-driven — se llama en cada Trade recibido.

---

### 3. Pressure Cooker `🔥`

Detecta flujo sostenido en un único contrato a lo largo del tiempo — acumulación silenciosa que delata convicción direccional.

**Ventana 2 minutos:**
- `vol_2min` ≥ `PRESSURE_COOKER_2MIN_VOL` = **250** contratos
- Re-dispara cuando supera `último_vol_disparado + 250`
- Cooldown: `PRESSURE_COOKER_COOLDOWN_SECONDS` = **120s** por símbolo

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
| `TELEGRAM_CHAT_ID` | ID del chat/canal destino de alertas |

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
| `SWEEP_BURST_MIN_VOL_B` | `75` | Sweep cond B |
| `ALERT_COOLDOWN_SECONDS` | `120` | Sweep |
| `MIN_DELTA` | `0.30` | Sweep + Pressure Cooker |
| `MAX_DELTA` | `0.95` | Sweep + Pressure Cooker |
| `BLOCK_PRINT_MIN_VOL` | `100` | Block Print |
| `BLOCK_PRINT_MIN_DELTA` | `0.40` | Block Print |
| `BLOCK_PRINT_MAX_DELTA` | `0.90` | Block Print |
| `BLOCK_PRINT_COOLDOWN_SECONDS` | `120` | Block Print |
| `PRESSURE_COOKER_2MIN_VOL` | `250` | Pressure Cooker |
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

### Renovación automática (cada 20 horas)

`renew_session.py` se ejecuta automáticamente desde el scheduler. Estrategia:

1. **remember_token** (funciona desde Railway): extrae el `remember_token` del `TT_SESSION_JSON` actual y autentica sin password.
2. **username/password** (fallback, solo desde Mac — dispositivo autorizado).

Tras renovar, actualiza `TT_SESSION_JSON` en Railway via GraphQL API y notifica a Telegram.

### Renovación manual (si falla la automática)

```bash
# Desde Mac:
python renew_session.py
```

Si Railway no se actualiza automáticamente, copiar el nuevo valor de `TT_SESSION_JSON` del log y pegarlo manualmente en las variables de entorno de Railway.

### Prioridad de autenticación en `_make_session()`

1. `TT_SESSION_JSON` env var (base64) — Railway production
2. `session.json` local — desarrollo / fallback
3. Login con `TT_USERNAME` + `TT_PASSWORD` — solo dispositivo conocido (Mac)

---

## Deploy en Railway

### Variables mínimas para funcionar

```
TT_USERNAME=...
TT_PASSWORD=...
TT_SESSION_JSON=<base64 generado con python login.py>
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
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
Sesión cargada desde TT_SESSION_JSON (expira ...)
=== load_chain() iniciado ===
  Hora ET: HH:MM:SS | today=YYYY-MM-DD | today_expired=False
  WATCH_SYMBOLS: ['/ES']
Cargando cadena /ES...
  Chain OK — option_chains=1, futures=N
  Expiraciones disponibles (N): [...]
  Expiración activa: YYYY-MM-DD (N strikes totales)
  Futuro front-month: /ESM5
  Precio /ESM5: XXXX.XX | Filtro distancia: ±15%
  /ES: NNN contratos registrados (M strikes descartados por distancia)
=== load_chain() completado: NNN contratos totales ===
Registrando suscripción Trade para NNN símbolos...
  Trade OK. Muestra: ['.../ES...', ...]
Registrando suscripción Quote para NNN símbolos...
  Quote OK.
Registrando suscripción Greeks para NNN símbolos...
  Greeks OK.
=== Streaming activo — NNN contratos suscritos. Esperando eventos... ===
[DIAGNÓSTICO] Primer Quote recibido: sym=..., bid=..., ask=...
[DIAGNÓSTICO] Primer Trade recibido: sym=..., size=..., price=...
[HEARTBEAT] contratos=NNN | trades_total=X (+Y/min) | quotes_total=X (+Y/min) | greeks_total=X (+Y/min)
```

**Señal de problema:** Si el heartbeat muestra `trades_total=0` con `contratos>0` después de varios minutos en horario de mercado, hay un problema de suscripción.

---

## Bugs Conocidos y Fixes Aplicados

### Bug 1 — `reload_chain()` creaba un streamer paralelo (CRÍTICO, fix aplicado)

**Síntoma:** Tras el reload de las 17:15 ET, el stream quedaba ciego para los contratos del nuevo día. Los logs mostraban "Recarga completada" pero nunca llegaban eventos de los nuevos símbolos.

**Causa:** `reload_chain()` abría un `DXLinkStreamer` nuevo, cargaba la cadena (actualizando `_contract_meta` y `_active_symbols`), y cerraba el nuevo streamer **sin registrar suscripciones en el streamer principal**. El streamer principal continuaba con suscripciones a los símbolos del día anterior.

**Fix:** `reload_chain()` ahora usa `self._streamer` (referencia al streamer principal activo) para cargar la cadena y llamar a `subscribe(Trade/Quote/Greeks)` en el mismo WebSocket.

### Bug 2 — Falta de visibilidad en el flujo (fix aplicado)

**Síntoma:** El stream conectaba (keepalives cada 30s) pero no había forma de saber si los eventos llegaban pero eran filtrados, o si directamente no había suscripciones activas.

**Fix:** Se añadieron:
- Logs detallados en `load_chain()` con cada paso de la carga (expiración elegida, precio del futuro, strikes antes/después del filtro, muestra de símbolos).
- Logs de confirmación para cada `subscribe()` en `run_session()` y `reload_chain()`.
- Log del **primer evento recibido** de cada tipo (Trade, Quote, Greeks), incluyendo el símbolo — aunque no esté en `_contract_meta`.
- **Heartbeat cada 60s** en `_periodic_checks()` con conteo de eventos por tipo y advertencia si hay 0 trades con contratos activos.

### Nota — Renovación automática de sesión

La renovación automática (cada 20h vía `renew_session.py`) actualiza `TT_SESSION_JSON` en Railway via GraphQL pero **no provoca redeploy**. El proceso en memoria usa la nueva sesión directamente (`os.environ["TT_SESSION_JSON"] = new_b64`). El streamer principal no se reinicia — la renovación solo afecta a futuras llamadas a `_make_session()`.

---

## Formato de Alertas Telegram

### Sweep Burst `🌊`

```
🌊 SWEEP  /ES  🟢 CALL  320 vol  6 strikes

1 Apr 2025  |  0 DTE
─────────────────────
5800C   Δ 0.52   75 vol   $12.50  ASK
5810C   Δ 0.48   68 vol   $11.00  ASK
5820C   Δ 0.44   55 vol   $9.50   MID
5830C   Δ 0.41   52 vol   $8.25   BID
5840C   Δ 0.37   40 vol   $7.00   BID
5850C   Δ 0.34   30 vol   $6.00   MID
─────────────────────
09:47:23 ET  |  via Tastytrade
```

- `🟢` para CALL, `🔴` para PUT
- Side (BID/ASK/MID) calculado comparando precio de ejecución contra bid/ask
- Strikes ordenados por precio ascendente

### Block Print `🖨️`

```
🖨️ BLOCK  5800🟢  CALL  /ES  250 vol

1 Apr 2025  |  0 DTE  |  Δ 0.52  |  IV 18%
─────────────────────
Bid 12.25  |  Ask 12.75  |  Exec $12.75
─────────────────────
250 contratos en el ASK
10:15:44 ET  |  via Tastytrade
```

- El vol reportado es el de la **ventana 1min** (`get_accumulated`)
- Side calculado comparando `exec_price` vs bid/ask del contrato

### Pressure Cooker `🔥`

```
🔥 FLOW  5800🟢  /ES  380 vol  2m

1 Apr 2025  |  0 DTE  |  Δ 0.52  |  IV 18%
─────────────────────
Tape  2m
10:14:01    45 @ $12.50  ASK
10:14:23    60 @ $12.75  ASK
10:14:55    80 @ $12.50  MID
10:15:12   110 @ $12.75  ASK
10:15:38    85 @ $12.75  ASK
─────────────────────
Total  380 contratos
10:15:44 ET  |  via Tastytrade
```

- Muestra las **últimas 10 transacciones** del tape del contrato
- Formato de tape: `HH:MM:SS    size @ $precio  side`
- Dispara por ventana 2min (≥250 vol) y 5min (≥500 vol) de forma independiente

### Mensajes de sistema

| Mensaje | Cuándo |
|---------|--------|
| `🚀 BullCore Capital V1 — EN VIVO` | Al arrancar `run_session()` |
| `🔄 Cadena recargada — nueva sesión` | Al completar `reload_chain()` |
| `✅ Stream verificado (18:05 ET)` | Verificación automática diaria |
| `🔑 Sesion Tastytrade renovada` | Tras renovación exitosa de sesión |
| `❌ Fallo renovacion sesion` | Si falla la renovación automática |
| `⛔ Sistema de alertas detenido` | Al recibir SIGINT/SIGTERM |
