# 🛒 Amazon Bargain Bot

Bot en Python que vigila Amazon.es en busca de **chollos y errores de precio** en tres nichos
(💻 Tecnología, 🎮 Consolas y Videojuegos, 🃏 Magic: The Gathering) y los muestra en un
**menú interactivo de Telegram**. Los errores de precio se envían además como alerta inmediata.

Pensado para funcionar 24/7 como Web Service en [Render](https://render.com) con una estrategia de
**baja carga**: pocas peticiones, pausas aleatorias y cortacircuitos ante bloqueos.

## Características

- **Menú en Telegram** (`/start`, `/ofertas`): secciones por nicho, "🔥 Ver Todo" (descuento > 40%),
  paginación ◀️ ▶️ editando el mismo mensaje y botón 🔄 para forzar un escaneo.
- **Alertas push solo para lo urgente**: 🚨 *posible error de precio* (≥ 60% de descuento), con enlace a su sección.
- **Detección de descuentos** contra el precio recomendado de Amazon, la mediana del historial propio y el precio anterior.
- **Filtro de vendedor**: solo productos vendidos por Amazon (o enviados por Amazon si la fuente no filtra).
- **Anti-bloqueo**: `curl_cffi` con huella TLS de navegador real, sesión con cookies, backoff exponencial con jitter,
  pausas aleatorias entre fuentes y cortacircuitos.
- **Servidor FastAPI** con `/` y `/health` para los health checks de Render.

## Estructura

```
├── main.py               # FastAPI (app) + CLI local + bucle de escaneo
├── config.json           # nichos, fuentes y parámetros de baja carga
├── render.yaml           # Blueprint de Render
├── requirements.txt
├── .python-version       # versión de Python para Render
├── .env.example          # plantilla de variables de entorno
└── avisador/
    ├── scraper.py        # cliente HTTP (curl_cffi), reintentos, cortacircuitos
    ├── parsers.py        # HTML de búsquedas, /deals y fichas (precio, vendedor)
    ├── detector.py       # filtros por nicho, descuentos, errores de precio, vendedor
    ├── storage.py        # historial de precios y deduplicación (JSON)
    ├── active.py         # caché de ofertas activas para el menú
    ├── views.py          # textos y teclados del menú
    ├── telegram_bot.py   # handlers de Telegram (python-telegram-bot)
    ├── notifier.py       # envío de alertas (Bot API)
    └── config.py         # carga y validación de config.json
```

---

## 1. Puesta en marcha local

Requisitos: Python 3.11+ (Render usa 3.13).

```powershell
git clone https://github.com/<tu-usuario>/<tu-repo>.git
cd <tu-repo>
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

### Configurar Telegram

1. En Telegram, habla con **@BotFather** → `/newbot` → copia el token en `TELEGRAM_TOKEN` del `.env`.
2. Abre tu bot y envíale `/start`.
3. Obtén tu chat ID y cópialo en `CHAT_ID`:
   ```powershell
   python main.py --get-chat-id
   ```
4. Comprueba que todo funciona:
   ```powershell
   python main.py --test-telegram
   ```

### Ejecutar

| Comando | Qué hace |
|---|---|
| `uvicorn main:app --port 8000` | Igual que en Render: servidor web + bot + escaneo |
| `python main.py` | Bot de Telegram + escaneo (sin servidor web) |
| `python main.py --once --dry-run` | Un único escaneo, sin enviar alertas (para probar) |
| `python main.py --test-telegram` | Mensaje de prueba |
| `python main.py --get-chat-id` | Muestra tu chat ID (con el bot parado) |

> ⚠️ Ejecuta **una sola instancia** a la vez (local *o* Render). Telegram solo permite un proceso
> recibiendo actualizaciones por bot; el segundo falla con `Conflict`.

---

## 2. Configuración (`config.json`)

### `bot_settings`: estrategia de baja carga

```json
"bot_settings": {
  "scan_interval_minutes": 25,
  "delay_between_sources_seconds": { "min": 12, "max": 28 },
  "max_retries_per_source": 2,
  "backoff_factor_seconds": 15
}
```

| Clave | Efecto |
|---|---|
| `scan_interval_minutes` | Tiempo entre ciclos completos (con ±10% de jitter) |
| `delay_between_sources_seconds` | Pausa aleatoria entre fuentes (se registra: `⏳ Esperando 18.4s...`) |
| `max_retries_per_source` | Reintentos por fuente ante 503/429/CAPTCHA (3 intentos en total con 2) |
| `backoff_factor_seconds` | Backoff exponencial con jitter: ~15 s, ~30 s… |

### `settings`: resto de parámetros

| Clave | Por defecto | Descripción |
|---|---|---|
| `push_mode` | `errors` | Alertas directas: `errors`, `all` o `none` |
| `top_percent` | `40` | Umbral de "🔥 Ver Todo" |
| `page_size` | `5` | Ofertas por página en el menú |
| `price_error_percent` | `60` | Descuento a partir del cual se marca 🚨 error de precio |
| `dedup_hours` | `36` | Horas sin repetir la alerta del mismo producto |
| `circuit_breaker_failures` | `2` | Fuentes bloqueadas seguidas que detienen el ciclo |
| `cooldown_minutes` | `45` | Pausa de seguridad tras el cortacircuitos (luego empieza el intervalo normal) |
| `seller_filter` | `true` | Verifica el vendedor de las fuentes sin filtro `emi` |
| `active_deal_ttl_hours` | `3` | Cuánto se mantiene una oferta en el menú si su fuente falla |

### `categories`: nichos y fuentes

```json
{
  "id": "mtg",
  "label": "🃏 MAGIC THE GATHERING",          // etiqueta en las alertas
  "menu_label": "🃏 Magic: The Gathering",    // botón del menú
  "min_drop_percent": 25,
  "price_error_percent": 60,
  "min_price": 5,
  "include_keywords": ["magic the gathering", "mtg"],
  "exclude_keywords": ["fundas", "sleeves", "pokemon"],
  "sources": [
    { "type": "search", "name": "MTG en oferta", "pages": 2,
      "url": "https://www.amazon.es/s?k=magic+the+gathering&rh=p_n_deal_type%3A26902953031&emi=A1AT7YVPFBWXBL" }
  ]
}
```

Tipos de fuente: `search` (cualquier URL de búsqueda o categoría), `deals` (página `/deals` por
`departments`) y `product` (una ficha con `target_price`).

**Parámetros útiles en las URLs de Amazon.es**

| Parámetro | Significado |
|---|---|
| `emi=A1AT7YVPFBWXBL` | Solo **vendido por Amazon.es** (verificado). Estas fuentes no necesitan abrir fichas para comprobar el vendedor |
| `rh=p_n_deal_type%3A26902953031` | Solo productos con descuento ("Todos los ahorros") |
| `i=computers` / `i=videogames` / `i=toys` | Limitar a un departamento |
| `s=date-desc-rank` | Ordenar por novedades |

> `emi=A1ATTZ506680M` **no** es Amazon.es (devuelve 0 resultados); si aparece, el bot lo corrige solo.

Los umbrales también se pueden cambiar con variables de entorno: `MIN_DROP_<ID>`, `PRICE_ERROR_<ID>`,
`PRICE_ERROR_PERCENT`, `DEDUP_HOURS`, `PUSH_MODE`, `TOP_PERCENT`.

---

## 3. Variables de entorno

Se leen primero del sistema (Render) y, si no existen, del archivo `.env` local.

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Token de @BotFather (`TELEGRAM_BOT_TOKEN` también se acepta) |
| `CHAT_ID` | ✅ | Chat(s) autorizados, separados por comas (`TELEGRAM_CHAT_ID` también se acepta) |
| `PUSH_MODE` | | `errors` / `all` / `none` |
| `PROXY_URL` | | Proxy para el scraping (`http://user:pass@host:port`) |
| `AMAZON_AFFILIATE_TAG` | | Tag de afiliado añadido a los enlaces |
| `LOG_LEVEL` | | `INFO` por defecto |
| `LOG_TO_FILE` | | `false` en Render |
| `SELF_PING` | | `true`: se hace ping a sí mismo cada ~10 min (plan Free) |
| `DRY_RUN` | | `true`: no envía alertas push |

---

## 4. Despliegue en Render

### 4.1 Subir a GitHub

```bash
git remote add origin https://github.com/<tu-usuario>/<tu-repo>.git
git push -u origin main
```

> Comprueba antes con `git status` que **`.env` no aparece**: está en `.gitignore`.

### 4.2 Crear el servicio desde el Blueprint

1. En el [Dashboard de Render](https://dashboard.render.com): **New → Blueprint**.
2. Conecta tu cuenta de GitHub y elige el repositorio. Render detecta `render.yaml`.
3. Te pedirá los secretos marcados con `sync: false`: **`TELEGRAM_TOKEN`** y **`CHAT_ID`**.
4. **Apply**. Render instala dependencias (`pip install -r requirements.txt`) y arranca
   `uvicorn main:app --host 0.0.0.0 --port $PORT`.
5. Comprueba `https://<tu-servicio>.onrender.com/health` → `{"status": "healthy", ...}`.
   En Telegram recibirás el menú de bienvenida.

Cada `git push` a `main` vuelve a desplegar automáticamente (`autoDeployTrigger: commit`).

### 4.3 Limitaciones a tener en cuenta

- **Plan Free**: Render apaga el servicio tras 15 min sin tráfico HTTP entrante. El bot se hace
  *self-ping* a `/health` cada ~10 min (`SELF_PING=true`) para seguir despierto; las 750 h gratuitas
  al mes bastan para un servicio 24/7. Alternativa: un monitor externo (p. ej. UptimeRobot) contra `/health`.
- **Disco efímero**: el historial de precios (`data/`) se pierde en cada despliegue o reinicio. Para
  conservarlo, usa un plan de pago (`0.5c-512mb`) con un [disco persistente](https://render.com/docs/disks)
  montado en `data/`.
- **IP de centro de datos**: Amazon bloquea con más facilidad las IP de servidores que las domésticas.
  Si en los logs ves el cortacircuitos a menudo, configura `PROXY_URL` con un proxy residencial.
- **Una sola instancia**: no ejecutes el bot en local mientras esté desplegado (conflicto de Telegram).

---

## Aviso legal

Las condiciones de uso de Amazon prohíben el scraping automatizado. Este proyecto está pensado para
uso personal y con volumen bajo; úsalo bajo tu responsabilidad.
