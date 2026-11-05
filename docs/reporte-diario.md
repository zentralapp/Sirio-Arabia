# Reporte diario por mail

Guía para configurar, probar y operar el reporte diario en PDF que se manda por
Gmail. Está pensada para quien lo instala y lo mantiene (un dev), no para el
usuario final.

Contenido:

1. [Qué hace](#1-qué-hace)
2. [Cómo está armado](#2-cómo-está-armado)
3. [Google Cloud y Gmail, paso a paso](#3-google-cloud-y-gmail-paso-a-paso)
4. [Variables de entorno](#4-variables-de-entorno)
5. [Railway](#5-railway)
6. [Uso del comando](#6-uso-del-comando)
7. [Probar en local](#7-probar-en-local)
8. [Operación](#8-operación)
9. [Rollback](#9-rollback)
10. [Dependencias](#10-dependencias)
11. [Apéndice: VPS con systemd](#11-apéndice-vps-con-systemd)
12. [Checklist de puesta en producción](#12-checklist-de-puesta-en-producción)

---

## 1. Qué hace

De lunes a viernes a las **08:00 hora argentina** se genera un PDF con
cumpleaños y cobranzas, y se manda adjunto por mail. En Railway se programa
con el cron `0 11 * * 1-5`, porque Railway usa UTC. Argentina no tiene horario
de verano, así que las 11:00 UTC son siempre las 08:00 ART.

El reporte tiene **dos ritmos**, según el día de la semana de la fecha de
negocio:

- **Lunes: extendido.** Es el resumen semanal: hoy, la semana hasta el
  domingo, lo que vence más adelante, los pagos parciales, el **total
  general** y la **regularidad de compra**. Un lunes feriado sale igual,
  extendido.
- **Martes a viernes: del día.** Solo lo de hoy: cumpleaños, vencimientos,
  ATRASADAS, pagadas sin registrar y sin vencimiento. **El viernes, "hoy"
  cubre de viernes a domingo**, para que nada del fin de semana se pierda
  entre el viernes y el lunes.

El modo depende **solo** de la fecha (`modo_reporte` en
`backend/services/reporte_diario/fechas.py`): un `--fecha` de un lunes sale
extendido aunque se corra un jueves. Un sábado o domingo forzado sale "del
día", con "hoy" hasta el domingo.

- **Formato:** el mail trae solo el PDF adjunto
  (`reporte-diario-AAAA-MM-DD.pdf`). El asunto es
  `Reporte diario – jue 29/10/2026` y el cuerpo es
  `Adjunto el reporte del día.`
- **Casilla:** la misma cuenta Gmail **general del negocio** manda el mail y
  lo recibe. No se usa la casilla personal de nadie.
- **Alcance:** global. Entran todos los pedidos de todos los usuarios, sin
  filtrar por vendedor.

### Secciones del PDF (en este orden)

El orden sale de `SECCIONES_POR_MODO` (`backend/services/reporte_diario/modelo.py`).

**Martes a viernes (del día)**

| Sección | Qué entra |
|---|---|
| Cumpleaños de hoy | Cumpleaños cuya fecha cae hoy. El viernes se titula "Cumpleaños de hoy y del fin de semana" y entra vie..dom. |
| Cobranzas que vencen hoy | Vencimiento = hoy. El viernes se titula "Cobranzas que vencen hoy y el fin de semana" y entra vie..dom. |
| ATRASADAS | Vencimiento < hoy, con días de atraso (contados contra hoy; el viernes, contra el viernes). Las más atrasadas van primero. |
| Pagadas sin registrar cobro | Ver la regla abajo. Aparecen SOLO acá. Se muestra siempre ("Sin registros" si está vacía). |
| Sin fecha de vencimiento | Aparece solo si tiene filas. |

Martes a viernes **no** hay secciones de la semana, ni pagos parciales, ni
"vencen más adelante", ni total general, ni regularidad. Una deuda que vence
después de hoy no aparece hasta su día (o hasta el lunes).

**Lunes (extendido)**

| Sección | Qué entra |
|---|---|
| Cumpleaños de hoy | Cumpleaños cuya fecha cae hoy. |
| Cumpleaños de la semana | Desde mañana hasta el domingo inclusive. |
| Cobranzas que vencen hoy | Vencimiento = hoy. |
| Cobranzas que vencen en la semana | Desde mañana hasta el domingo inclusive. |
| ATRASADAS | Vencimiento < hoy, con días de atraso. |
| Cobranzas que vencen más adelante (después del domingo dd/mm) | Vencimiento posterior al domingo. Antes se descartaban; ahora se ven una vez por semana. Con total. |
| Sin fecha de vencimiento | Aparece solo si tiene filas. |
| Pagos parciales | Tienen pagos o notas de crédito y les queda saldo > 0,01. Es una sección transversal: un parcial también aparece en su sección por vencimiento. |
| Pagadas sin registrar cobro | Igual que de martes a viernes. |
| **Total general de deuda vigente** | Una línea, no una tabla. Ver la regla abajo. |
| Regularidad de compra (clientes que se pasaron de su ritmo habitual) | Última sección. Ver la regla abajo. |

**Subtítulos:** martes a jueves `mar 10/11/2026`; viernes
`vie 13/11/2026 · incluye sáb y dom hasta dom 15/11/2026`; lunes
`lun 09/11/2026 · resumen semanal hasta dom 15/11/2026`. El título del
documento, el asunto y el nombre del adjunto no cambian según el modo.

Una sección vacía dice "Sin registros". Si están todas las del modo vacías, el
PDF dice "Sin novedades". Un lunes con solo regularidad **no** es "Sin
novedades".

### Reglas (resumen; la fuente de verdad es el código)

- **"La semana"** (solo el lunes) va desde mañana hasta el domingo siguiente
  inclusive. Código: `fin_de_semana` en
  `backend/services/reporte_diario/fechas.py`.
- **"Hoy"** es solo hoy, salvo el viernes (y un sábado o domingo forzado),
  donde llega hasta el domingo. Código: `fin_de_hoy` en el mismo archivo.
- **Partición exclusiva por vencimiento:** cada cobranza abierta cae en una
  sola de hoy / semana / ATRASADAS / vencen más adelante / sin vencimiento.
  Martes a viernes solo se muestran las del modo; el resto se ve el lunes.
- **Umbral de saldo 0,01:** un saldo ≤ 0,01 cuenta como 0 en todo el reporte
  (clasificación, celdas y totales). Código: `UMBRAL_SALDO` en
  `backend/services/saldos.py`.
- **Sin monto:** un pedido sin monto en ninguna de las tres fuentes (cobranza,
  precio final, logística) aparece en su sección por vencimiento. En Monto y
  Saldo dice "Sin monto" y **no suma** a ningún total. Nunca es parcial ni
  "pagada sin registrar".
- **Pagadas sin registrar cobro:** son pedidos con monto > 0 y pagos > 0 cuyo
  saldo es ≤ 0,01, pero que no tienen el cobro marcado. Van **solo** a su
  sección, no aparecen en ATRASADAS ni en hoy/semana, y **no suman** a ningún
  total.
- **Totales por sección:** solo hay total de saldo en "vencen hoy", "vencen en
  la semana", ATRASADAS, "vencen más adelante" y "Pagos parciales", y solo si
  la sección tiene filas.
- **Total general (solo el lunes):** suma el saldo de vencen hoy + semana +
  ATRASADAS + vencen más adelante + **sin vencimiento**. **No** suma los
  parciales (ya están en su sección por vencimiento: no se duplican) ni las
  pagadas sin registrar. Los "Sin monto" **no suman**, pero se cuentan:
  `Total general de deuda vigente: $3.900,00 (2 pedidos sin monto cargado no
  incluidos)`. Código: `total_general` en `clasificar.py`.
- **Cumpleaños:** se proyectan al año en curso. El 29/2 se muestra el 28/2 en
  los años no bisiestos. Funciona también cuando la semana cruza de año.
- **Clientes archivados:** sus **cobranzas sí aparecen**, porque la deuda es
  deuda. Sus **cumpleaños no**. En la **regularidad no** aparecen (ver abajo).

Código de la clasificación: `backend/services/reporte_diario/clasificar.py`.
Las queries están en `backend/services/reporte_diario/datos.py`.

#### Regularidad de compra (solo el lunes)

Lista los pares **cliente + empresa** que se pasaron de su ritmo habitual de
compra. La query (`leer_compras_por_par` en `datos.py`) corre **solo** los
lunes; martes a viernes ni se ejecuta. El cálculo es puro y está en
`backend/services/reporte_diario/regularidad.py`.

- **Días de compra distintos por par.** La fecha de cada pedido es
  `coalesce(LogisticsStatus.fecha_compra, Order.created_at)::date`. Varios
  pedidos del mismo día cuentan como **una** compra. No cuentan los pedidos
  borrados (`deleted_at`) ni los borradores (`OrderDraft`). Los días
  posteriores a la fecha del reporte (retrofechados al futuro) se ignoran.
- **Ojo con `fecha_compra`:** la columna tiene default `datetime.utcnow`
  (`backend/models.py`), así que casi siempre tiene valor. Es la fecha de
  carga en UTC salvo que alguien la haya editado. El `coalesce` solo cae a
  `created_at` si alguien la puso en NULL a propósito.
- **Mínimo de 3 días distintos.** Con menos, el par no tiene ritmo y no se
  evalúa.
- **Ventana:** solo los últimos **12 intervalos** (los 13 días más
  recientes).
- **Ritmo P:** la **mediana** de esos intervalos en días, redondeada half-up
  a entero (con cantidad par: `(a + b + 1) // 2`), con mínimo 1. La mediana
  hace que un outlier (unas vacaciones) no mueva el ritmo.
- **Días sin comprar N:** fecha del reporte − último día de compra.
- **Dispara si `P ≤ N ≤ min(3·P, 365)`.** Más de 3 veces el ritmo (o más de un
  año) se da por perdido y deja de aparecer, para que la lista no se llene de
  clientes que ya no compran.
- **Clientes y empresas archivadas excluidos:** no entran los pares cuyo **cliente o empresa**
  está archivado (`Client.archived`, `Company.archived`). Si se desarchiva, el
  lunes siguiente se evalúa con todo su historial.
- **Orden:** N/P descendente (el más pasado de su ritmo primero), después N
  descendente, después cliente y empresa alfabéticos.
- **Columnas:** Cliente, Empresa, Última compra, Días sin comprar, Ritmo
  habitual ("cada N días") y Deuda.
- **Deuda del par** (todas sus cobranzas abiertas, sin las pagadas sin
  registrar), con uno de estos 4 textos exactos:
  - `(tiene deudas pendientes de $1.234,56)`
  - `(tiene deudas pendientes de $1.000,00 + 2 sin monto)`
  - `(tiene deudas pendientes sin monto cargado)`
  - `(no tiene deudas actuales)`
- Vacía: "Sin registros".

> **UMBRALES PROVISIONALES: viven en `regularidad.py`; ajustar tras el primer
> lunes real.** `MIN_DIAS = 3`, `MAX_INTERVALOS = 12`, `FACTOR_TOPE = 3` y
> `TOPE_DIAS = 365` están definidos **una sola vez**, en el bloque comentado
> `UMBRALES PROVISIONALES` de `backend/services/reporte_diario/regularidad.py`
> (`datos.py` los importa). **No se calibraron con datos de producción.** Si
> el primer lunes la lista sale vacía o desbordada, se ajustan ahí (y en
> `tests/test_regularidad.py::test_umbrales_provisionales`, que los fija).
>
> **Formato pendiente de confirmar con el cliente:** el cliente lo pidió como
> **oraciones** (una frase por cliente). El PDF lo muestra como **tabla**, por legibilidad y paginado. Falta que el
> cliente confirme que la tabla le sirve. Ver [§12.3](#123-temas-abiertos).

### Diferencias esperadas con el dashboard y `/deudas`

Los conteos del reporte pueden no coincidir con los del dashboard o
`/deudas`. **No es un bug**, son reglas distintas a propósito:

1. **Pagadas sin registrar cobro.** Una cobranza vencida con pagos que cubren
   el monto, pero sin fecha de cobro, cuenta como ATRASADO en el dashboard.
   En el reporte va **solo** a "Pagadas sin registrar cobro" y no suma a
   ATRASADAS.
2. **Pedidos sin fila de cobranza.** El dashboard parte de la tabla de
   cobranzas (`Collection`), así que un pedido sin esa fila no aparece. El
   reporte parte de los pedidos y sí lo incluye.
3. **Pagos parciales.** En el dashboard, PARCIAL es "tiene algún pago no
   borrador **o** un borrador de cobranza (`CollectionDraft`)". En el reporte
   es "tiene pagos o notas de crédito guardados **y** le queda saldo > 0,01".
   Un borrador nunca cuenta como plata.
4. **Alcance.** Un usuario no admin ve en el dashboard solo sus pedidos. El
   reporte es global: todos los pedidos de todos los usuarios, incluidos los
   que tienen la alerta silenciada.
5. **Regularidad vs. alerta de inactividad del dashboard.** El dashboard
   avisa "empresa inactiva" con un umbral **fijo de 60 días** sin compras por
   cliente-empresa. La regularidad usa el ritmo **propio** de cada par: un
   cliente que compra cada 7 días aparece a los 7, y uno que compra cada 90
   recién a los 90 (y deja de aparecer pasado 3·P o 365 días).
6. **Regularidad vs. TRABAJA → TRABAJABA.** La web pasa el vínculo a
   TRABAJABA a los **90 días** sin pedidos. La regularidad no mira ese
   estado: un par TRABAJABA puede aparecer si está dentro de su ventana.
7. **UTC vs. ART.** `created_at` y el default de `fecha_compra` se guardan en
   UTC. Un pedido cargado después de las 21:00 ART cuenta como del día
   siguiente. Puede mover N en un día respecto de lo que ve un usuario.
8. **Retrofechados y fechas futuras.** Si alguien edita `fecha_compra` hacia
   atrás, la regularidad usa esa fecha. Si la pone en el futuro, ese día se
   ignora hasta que llegue.

Si aparece una diferencia que **no** entra en ninguno de estos ocho casos,
eso sí hay que revisarlo.

---

## 2. Cómo está armado

- **Job:** `python -m backend.jobs.reporte_diario`
  (`backend/jobs/reporte_diario.py`). Usa una app Flask liviana
  (`backend/jobs/app_job.py`) que **no** importa `backend.app`. Por eso no
  corre migraciones, `create_all` ni el seed.
- **Idempotencia:** la tabla `report_send_log` guarda una fila por fecha de
  negocio (migración `c3e5a9d1f7b2`). Correr el job dos veces el mismo día no
  manda dos mails.
- **Lectura:** se hace en una transacción `READ ONLY`. El job no escribe nada
  fuera de `report_send_log`.
- **PDF:** se genera con fpdf2 y la fuente Poppins
  (`backend/services/reporte_diario/pdf.py`).
- **Envío:** usa la Gmail API con OAuth (`backend/services/mailer/`), con
  reintentos acotados (hasta 3 intentos ante 429/503 o si no pudo conectar).
  Si se corta la conexión **después** de mandar, o Gmail responde 500/502/504
  al envío, el envío queda en "estado incierto" y no se reintenta solo, para
  no duplicar el mail.
- **Generación compartida:** `backend/services/reporte_diario/generar.py`
  (`generar_pdf`, `armar_mail`) la usan el job y el botón de la web, así que
  "Ver PDF" es exactamente lo mismo que un `--dry-run`.
- **Registro de envíos:** `backend/services/reporte_diario/envios.py`
  (reclamar, registrar SENT/FAILED, y `reclamar_prueba` para el botón). El job
  lo re-exporta.
- **Aviso en la web:** los admins ven un banner si el reporte del último día
  hábil no salió (`backend/services/reporte_estado.py`). Se prende con
  `REPORTE_BANNER=1`.
- **Botón "Generar reporte":** página `/reporte` para admins
  (`backend/views/reporte_estado.py`), con "Ver PDF" y "Enviar prueba". Se
  prende con `REPORTE_BOTON=1`. Ver [§8.5](#85-botón-generar-reporte).

---

## 3. Google Cloud y Gmail, paso a paso

Se hace **una sola vez** y con la cuenta Gmail **general del negocio**, que es
remitente y destinatario a la vez.

> **Ojo con los nombres de los menús.** Google cambia la consola seguido. Hoy
> la configuración de OAuth está en **Google Auth Platform**, con las secciones
> Branding / Audience / Data Access / Clients. Antes se llamaba "Pantalla de
> consentimiento de OAuth". Si algún nombre no coincide, buscá el equivalente.
> La documentación oficial está en
> <https://developers.google.com/workspace/guides/configure-oauth-consent>.

### 3.1 Proyecto y Gmail API

1. Entrá a <https://console.cloud.google.com/> con la cuenta del negocio.
2. Creá un proyecto nuevo, por ejemplo `reporte-diario`.
3. Habilitá la **Gmail API**: APIs y servicios → Biblioteca → "Gmail API" →
   Habilitar.

### 3.2 Pantalla de consentimiento (Google Auth Platform)

1. Google Auth Platform → **Get Started**. Completá el nombre de la app (por
   ejemplo "Reporte diario Arabia") y el mail de soporte (la cuenta del
   negocio).
2. En **Audience**, elegí **External**.
3. En **Data Access** → "Add or Remove Scopes", agregá **solo**
   `https://www.googleapis.com/auth/gmail.send`. No hace falta leer mails ni
   nada más.
4. **Publicá la app en "In production"** (en Audience: "Publish app"; el estado
   pasa de *Testing* a *In production*).

> **IMPORTANTE:** mientras la app esté en **Testing**, el refresh token
> **vence a los 7 días** y el reporte deja de salir sin aviso previo. Hay que
> publicarla "In production" **antes** de correr el script de autorización. Si
> ya lo corriste en Testing, publicala y volvé a correrlo.

- **No hace falta verificar la app.** Es de uso propio (1 usuario; el límite
  sin verificación es 100). `gmail.send` es un scope "sensible", así que al
  autorizar Google muestra la pantalla **"Google no verificó esta app"**. Es
  esperable: entrá en "Avanzado" → "Ir a … (no seguro)" y continuá.

### 3.3 Cliente OAuth tipo Desktop

1. Google Auth Platform → **Clients** → Create client → tipo de aplicación
   **Desktop app**.
2. Bajá el JSON del cliente (`client_secret_….json`) y guardalo **fuera del
   repo**, por ejemplo en `~/Downloads`.
3. Anotá el **Client ID** y el **Client secret**: van a Railway (ver
   [§4](#4-variables-de-entorno)).

> **Nunca commitees el JSON, el client secret ni el refresh token.** Si alguno
> se filtra, borrá el cliente en Google Cloud, creá otro y volvé a autorizar.

### 3.4 Autorizar y obtener el refresh token

Se corre **en tu máquina**, con navegador, no en Railway:

```bash
.venv/bin/pip install -r requirements-dev.txt   # trae google-auth-oauthlib (solo dev)
.venv/bin/python scripts/autorizar_gmail.py --client-secrets ~/Downloads/client_secret_XXXX.json
```

Alternativa sin el JSON: exportá `REPORTE_GMAIL_CLIENT_ID` (o pasá
`--client-id`). El secreto se toma de `REPORTE_GMAIL_CLIENT_SECRET` o el
script lo pide por consola sin mostrarlo. El secreto **no** se acepta como
argumento, porque quedaría en el historial del shell. Opciones completas:
`python scripts/autorizar_gmail.py --help`.

Qué pasa al correrlo:

1. Se abre el navegador. Entrá con la **cuenta general del negocio** y aceptá
   el permiso "Enviar correo en tu nombre".
2. El script usa redirect por **loopback** (un puerto libre de `localhost`).
   El viejo flujo "OOB" de copiar y pegar un código ya no existe en Google.
3. Por **stdout** sale **solo el refresh token**, y los avisos van por stderr.
   Copialo a `REPORTE_GMAIL_REFRESH_TOKEN` en Railway. Si lo redirigís a un
   archivo (`> token.txt`), borralo después.

### 3.5 Cosas que revocan o invalidan el token

- **Cambiar la contraseña** de la cuenta del negocio revoca el token. También
  lo revoca quitar la app en "Apps con acceso a tu cuenta"
  (<https://myaccount.google.com/permissions>). Después de cualquiera de las
  dos, hay que volver a correr el script y actualizar la variable en Railway.
- **Máximo 100 refresh tokens** por cuenta y cliente: el token 101 invalida el
  más viejo **sin avisar**. No corras el script "para probar": cada corrida
  genera un token nuevo (usa `prompt=consent`).
- Si el token se revoca, el job falla con `invalid_grant`, la fila queda
  FAILED y el banner dice "hay que re-autorizar Gmail".

---

## 4. Variables de entorno

El job usa **solo** variables `REPORTE_*`. También las lista `.env.example`.

| Variable | ¿Obligatoria? | Default | Para qué |
|---|---|---|---|
| `DATABASE_URL` | Sí | — | Postgres. En Railway, la del plugin. |
| `REPORTE_MAILER` | Local: no. **Railway: sí, `gmail_api`** | `archivo` (solo fuera de Railway) | `gmail_api` (manda de verdad) o `archivo` (escribe un `.eml` en disco). |
| `REPORTE_GMAIL_CLIENT_ID` | Sí con `gmail_api` | — | Client ID del cliente OAuth Desktop. |
| `REPORTE_GMAIL_CLIENT_SECRET` | Sí con `gmail_api` | — | Client secret. |
| `REPORTE_GMAIL_REFRESH_TOKEN` | Sí con `gmail_api` | — | Salida de `scripts/autorizar_gmail.py`. |
| `REPORTE_MAIL_FROM` | Sí con `gmail_api` | `reporte-diario@localhost` (solo modo archivo) | Remitente: la casilla del negocio. Tiene que ser exactamente una dirección. |
| `REPORTE_MAIL_TO` | Sí con `gmail_api` | `reporte-diario@localhost` (solo modo archivo) | Destinatario: la **misma** casilla, exactamente **una** dirección. |
| `REPORTE_REMITENTE_NOMBRE` | No | `Arabia Representaciones` | Nombre visible del From. |
| `REPORTE_ARCHIVO_DIR` | No | `/tmp/reportes` | Carpeta de los `.eml` en modo `archivo`. |
| `REPORTE_BANNER` | No | apagado | **En el servicio web**, no en el cron. `1` prende el aviso a admins. |
| `REPORTE_BOTON` | No | **apagado** | **En el servicio web**, no en el cron. `1` habilita `/reporte` (solo admin global). Apagado, las 3 rutas dan **404** y no hay links. Requiere las `REPORTE_GMAIL_*` en el web para "Enviar prueba". |

**Qué variable va en qué servicio**

| Variable | Web (`sirio-arabia-crm`) | Cron |
|---|---|---|
| `DATABASE_URL` | Sí (ya está) | Sí |
| `SECRET_KEY` | **Sí, real** (no `dev-secret-key`): firma la sesión donde vive el token CSRF del botón | No |
| `AUTO_MIGRATE` | Sí (`Railway.toml`) | No |
| `REPORTE_MAILER`, `REPORTE_GMAIL_CLIENT_ID`, `REPORTE_GMAIL_CLIENT_SECRET`, `REPORTE_GMAIL_REFRESH_TOKEN`, `REPORTE_MAIL_FROM`, `REPORTE_MAIL_TO`, `REPORTE_REMITENTE_NOMBRE` | Sí, **solo si** `REPORTE_BOTON=1` (como shared variables, [§5.4](#54-variables-compartidas-webcron)) | Sí |
| `REPORTE_BANNER` | Opcional | No |
| `REPORTE_BOTON` | Opcional (default apagado) | No |
| `GMAIL_USER` / `GMAIL_PASSWORD` / `GMAIL_FROM` | Las usa el mail viejo; el reporte **nunca** las lee | No |

El botón usa **exactamente las mismas** `REPORTE_*` que el cron. Si el web no
las tiene, "Enviar prueba" **no cae** a las `GMAIL_*`: muestra "El servicio
web no tiene configurado Gmail (REPORTE_MAILER y REPORTE_GMAIL_*)…" y no
escribe ninguna fila. En Railway el modo `archivo` también está prohibido para
el web (mismo chequeo que el cron, con el texto "…en el servicio web").

Si con `gmail_api` falta alguna variable obligatoria, el job sale con exit 1
**antes** de tocar `report_send_log`. El error nombra las variables que
faltan, nunca sus valores.

**En Railway el modo `archivo` está prohibido.** El job detecta que corre en
Railway por las variables que Railway inyecta en todo deploy
(`RAILWAY_ENVIRONMENT_NAME`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_PROJECT_ID`,
`RAILWAY_SERVICE_ID`, y la vieja `RAILWAY_ENVIRONMENT`). Si `REPORTE_MAILER`
falta o dice `archivo`, sale con exit 1 sin escribir fila y loguea
`REPORTE_MAILER no configurado en Railway: el reporte no se envió` (o
`REPORTE_MAILER=archivo no se permite en Railway`). A las 09:00 el banner
muestra "No se envió el reporte". Sin esta regla, el `.eml` quedaría en el
disco efímero del contenedor, la fila quedaría SENT y nadie se enteraría.
Fuera de Railway el default sigue siendo `archivo`. Ojo: `railway run` también
inyecta esas variables, así que bajo `railway run` el modo `archivo` falla
(igual no hay que usar `railway run`, ver [§5.3](#53-correr-el-cron-a-mano)).

Ejemplo para el servicio cron. Los valores son **placeholders**:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REPORTE_MAILER=gmail_api
REPORTE_GMAIL_CLIENT_ID=1234567890-xxxxxxxx.apps.googleusercontent.com
REPORTE_GMAIL_CLIENT_SECRET=<client-secret>
REPORTE_GMAIL_REFRESH_TOKEN=<refresh-token>
REPORTE_MAIL_FROM=casilla-del-negocio@gmail.com
REPORTE_MAIL_TO=casilla-del-negocio@gmail.com
REPORTE_REMITENTE_NOMBRE=Arabia Representaciones
```

> **Las `GMAIL_USER` / `GMAIL_PASSWORD` / `GMAIL_FROM` son otra cosa.** Las usa
> el mail viejo por SMTP del ex-dev (`backend/views/main.py`) y son sus
> credenciales. **No tienen nada que ver** con este reporte: el job nunca las
> lee y no hay que reutilizarlas ni copiarlas al cron.

---

## 5. Railway

El reporte corre en un **servicio cron aparte**, creado desde el mismo repo.
El servicio web no cambia, salvo por `REPORTE_BANNER`, `REPORTE_BOTON` y, si
el botón está prendido, las `REPORTE_*` de mail compartidas
([§5.4](#54-variables-compartidas-webcron)).

Documentación de Railway:
- Cron: <https://docs.railway.com/reference/cron-jobs>
- Restart policy: <https://docs.railway.com/deployments/restart-policy>
- CLI `railway cron`: <https://docs.railway.com/cli/cron>

### 5.1 Orden obligatorio: primero la web

La migración `c3e5a9d1f7b2`, que crea `report_send_log`, la aplica el
**servicio web** al arrancar con `AUTO_MIGRATE=1`. El job **no** migra.
Entonces:

1. Mergeá y deployá la web.
2. Verificá que `alembic_version` sea `c3e5a9d1f7b2`, o que el log del deploy
   no tenga errores de migración.
3. Recién después creá el cron.

Si el cron corre antes, falla con `falta la migración report_send_log:
deployar la web primero` y exit 1.

### 5.2 Crear el servicio cron

En el dashboard del proyecto: New → GitHub Repo → este repo, rama `main`. Los
nombres exactos de los menús pueden variar. Después, en **Settings** del
servicio nuevo:

| Campo | Valor |
|---|---|
| Start Command | `python -m backend.jobs.reporte_diario` |
| Cron Schedule | `0 11 * * 1-5` (UTC = L-V 08:00 ART) |
| Restart Policy | **Never** (el default es "On Failure", que lo reiniciaría hasta 10 veces tras un fallo) |
| Dominio público | ninguno |
| Variables | las de [§4](#4-variables-de-entorno), con `DATABASE_URL` referenciando al mismo Postgres que la web |

En el cron **no** van `AUTO_MIGRATE`, `SECRET_KEY`, `GMAIL_*` ni
`REPORTE_BANNER`. `REPORTE_MAILER=gmail_api` es **obligatoria**: sin ella (o
con `archivo`) el job falla con exit 1 y no manda nada (ver
[§4](#4-variables-de-entorno)).

> **Verificá el start command en el primer run.** En la raíz hay un
> `Railway.toml` con el start de gunicorn de la web. Según la documentación de
> Railway, la configuración en código **pisa** la del dashboard. Ese archivo no
> usa las claves estándar (`deploy.startCommand`), así que no está confirmado
> si el servicio cron lo toma o no. En el log del primer run tiene que aparecer
> `reporte diario: inicio, fecha de negocio …`, **no** gunicorn escuchando un
> puerto. Si aparece gunicorn, el cron está levantando la web: frenalo y
> revisá la config del servicio.

Datos útiles de Railway:
- El schedule va en **UTC**.
- Si una corrida sigue viva cuando toca la siguiente, Railway saltea la nueva.
- El proceso tiene que terminar: el job termina solo.

### 5.3 Correr el cron a mano

```bash
railway cron run -s <servicio-cron>     # dispara una corrida fuera de horario
railway cron list -s <servicio-cron>    # últimas corridas con estado y duración
```

`railway cron run` **no acepta argumentos**: corre el start command
configurado, o sea sin `--force`. Alcanza para un día hábil en el que el
reporte todavía no salió. Para reenviar un día que ya tiene fila (`--force`),
ver [§8.4](#84-reenviar-un-día).

> **Nunca** corras el job desde tu máquina contra la base de producción, por
> ejemplo con `railway run` o exportando la `DATABASE_URL` de Railway. Las
> vistas previas se hacen contra la base local.

> **`env -u DATABASE_URL` NO alcanza para protegerte.** El job (y la app)
> hacen `load_dotenv(override=False)` al arrancar: si la variable no está
> exportada, la **vuelven a cargar desde `.env`**. `env -u` solo sirve para que
> gane el `.env` sobre una variable exportada; si el `.env` apunta a otra base,
> el job usa esa. **Antes de cualquier corrida manual, verificá a qué host
> apunta el `.env`**, mostrando solo el host (nunca la URL completa):
>
> ```bash
> .venv/bin/python -c "from dotenv import dotenv_values; from urllib.parse import urlsplit; print(urlsplit(dotenv_values('.env').get('DATABASE_URL') or '').hostname)"
> ```
>
> Tiene que decir `localhost`. Si dice cualquier otra cosa, no corras nada.

### 5.4 Variables compartidas web+cron

El botón "Enviar prueba" corre en el **servicio web** y necesita las mismas
credenciales de Gmail que el cron. Para no tener dos copias que se
desincronicen (por ejemplo, al re-autorizar Gmail), van como **Shared
Variables** del proyecto:

1. Project Settings → **Shared Variables** → crear:
   `REPORTE_MAILER` (`gmail_api`), `REPORTE_GMAIL_CLIENT_ID`,
   `REPORTE_GMAIL_CLIENT_SECRET`, `REPORTE_GMAIL_REFRESH_TOKEN`,
   `REPORTE_MAIL_FROM`, `REPORTE_MAIL_TO` y `REPORTE_REMITENTE_NOMBRE`.
2. En cada una, **Share** a los servicios `cron` y `sirio-arabia-crm`.
3. En las Variables de cada servicio tienen que aparecer como referencia
   `${{shared.VAR}}`, **sin** copias sueltas con el mismo nombre (una copia
   suelta pisa la compartida y queda vieja al re-autorizar).

Alternativa: dejar las variables en el cron y referenciarlas desde el web con
`${{cron.VAR}}`. Funciona igual; lo importante es que exista **un solo valor**.

Al re-autorizar Gmail ([§3.4](#34-autorizar-y-obtener-el-refresh-token)) se
actualiza `REPORTE_GMAIL_REFRESH_TOKEN` **una sola vez** y toman el valor los
dos servicios (cada uno se redeploya).

Las `REPORTE_*` de mail en el web **solo hacen falta con `REPORTE_BOTON=1`**.
Con el botón apagado, el web no las lee.

---

## 6. Uso del comando

```
python -m backend.jobs.reporte_diario [--fecha AAAA-MM-DD] [--dry-run] [--out RUTA.pdf] [--force]
```

| Opción | Qué hace |
|---|---|
| (ninguna) | Uso normal (cron): fecha = hoy en ART. Sábado o domingo: no hace nada y sale 0. Si ya se envió (SENT) o hay otra corrida en curso (PENDING), no reenvía. |
| `--fecha AAAA-MM-DD` | Fecha de negocio a usar. **Sin `--dry-run` exige `--force`**, para no mandar por error el reporte de otro día. |
| `--dry-run` | Genera el PDF **sin mandar** y sin tocar `report_send_log`. No necesita la config de mail. Funciona también en fin de semana. **Exige `--out`.** |
| `--out RUTA.pdf` | Con `--dry-run` es la vista previa. Sin `--dry-run`, guarda una copia del PDF enviado. |
| `--force` | Manda aunque sea sábado o domingo, o aunque esa fecha ya esté SENT, PENDING o en estado incierto. Reutiliza la misma fila: suma 1 a `attempts` y limpia `error`, `message_id` y `sent_at`. |

**Exit codes**

| Código | Significado |
|---|---|
| `0` | Enviado, o salteado a propósito: fin de semana, ya SENT, PENDING en curso o último intento incierto (estos dos últimos dejan una advertencia en el log). |
| `1` | Falla: config de mail incompleta, DB, tabla inexistente, datos, PDF o envío. Si la fila ya estaba reclamada, queda FAILED. |
| `2` | Uso incorrecto: `--dry-run` sin `--out`, fecha mal formada, `--fecha` sin `--dry-run` ni `--force`. |

Ejemplos:

```bash
# Vista previa de un lunes (extendido: semana, vencen más adelante, total general y regularidad).
# Sale extendido aunque lo corras otro día: el modo depende de --fecha.
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --fecha 2026-11-09 --out /tmp/reporte-lun.pdf

# Vista previa de un martes (del día: sin semana, parciales, total general ni regularidad)
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --fecha 2026-11-10 --out /tmp/reporte-mar.pdf

# Vista previa del viernes 13/11 ("hoy" incluye vencimientos y cumples de sáb y dom)
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --fecha 2026-11-13 --out /tmp/reporte-vie.pdf

# Vista previa de hoy
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --out /tmp/reporte-hoy.pdf

# Reenviar el reporte del 29/10 (y guardar una copia)
.venv/bin/python -m backend.jobs.reporte_diario --fecha 2026-10-29 --force --out /tmp/reenviado.pdf
```

El log muestra el inicio, el host y la base (sin usuario ni contraseña), la
fecha, los conteos por sección (`conteos: modo=extendido …` o
`modo=del_dia …`, con `vencen_mas_adelante` y `regularidad`) y el message id o
el error. Nunca muestra tokens ni secretos.

Antes de correr cualquiera de estos comandos, verificá el host del `.env`
([§5.3](#53-correr-el-cron-a-mano)).

---

## 7. Probar en local

### 7.1 Base y migración

Levantá la web una vez con `AUTO_MIGRATE=1`, así aplica la migración de
`report_send_log`:

```bash
env -u DATABASE_URL AUTO_MIGRATE=1 .venv/bin/flask --app backend.app run --port 5050 --debug
```

`env -u DATABASE_URL` hace que la URL salga del `.env` local y no de una
variable exportada que podría apuntar a otra base. **Eso solo protege si el
`.env` apunta a `localhost`**: verificalo antes (comando en
[§5.3](#53-correr-el-cron-a-mano)). Sin `AUTO_MIGRATE=1`, el arranque puede
colgarse por el autodeadlock del issue #7.

> **Trampa de `create_all`:** importar `backend.app` corre `db.create_all()`.
> Eso pasa con `python -m backend.app` y con `flask db …` cuando
> `FLASK_APP=backend.app`. Si la web arrancó **sin** `AUTO_MIGRATE=1`,
> `create_all` crea `report_send_log` por fuera de Alembic y el siguiente
> `flask db upgrade` falla con "already exists". Para arreglarlo, si la tabla
> ya tiene la estructura correcta, marcá la revisión como aplicada:
>
> ```bash
> FLASK_APP=backend.app .venv/bin/flask db stamp c3e5a9d1f7b2
> ```

### 7.2 Correr el job sin mandar mails

- Por defecto `REPORTE_MAILER` es `archivo`: el "envío" escribe
  `reporte-diario-AAAA-MM-DD.eml` en `REPORTE_ARCHIVO_DIR` (default
  `/tmp/reportes`) y no contacta a Google. Así se prueba el flujo completo,
  incluida la fila en `report_send_log`.
- Para ver solo el PDF, usá `--dry-run --out` (ver [§6](#6-uso-del-comando)).
- Después de probar envíos locales podés limpiar las filas de prueba de
  `report_send_log` **en la base local**.

> **ATENCIÓN:** el job carga el `.env` al arrancar, sin pisar las variables ya
> exportadas. Si tu `.env` local tiene `REPORTE_MAILER=gmail_api` con
> credenciales reales, **una corrida local manda un mail de verdad** a la
> casilla del negocio. Hacelo solo a propósito, por ejemplo para probar el
> token una vez. Después volvé a `archivo`.

### 7.3 Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
env -u DATABASE_URL .venv/bin/python -m pytest
```

- Casi todos los tests son puros: no usan base ni `.env`.
- Los marcados `db` (`pytest.ini`) usan la base **local** del `.env`. Se
  **saltean solos** si no hay base o si la URL no apunta a `localhost`, así que
  nunca corren contra otra base. Para correr solo esos: `pytest -m db`; para
  excluirlos: `pytest -m "not db"`.
- Un test del PDF usa `pdftotext` (poppler) si está instalado y se saltea si
  no está. En macOS: `brew install poppler`.
- Producción corre Python 3.10. Conviene correr la suite también en 3.10, por
  ejemplo con `uv venv --python 3.10` en una carpeta fuera del repo.

### 7.4 Ver el banner en local

1. Arrancá la web con `REPORTE_BANNER=1` y entrá con un usuario **admin**.
2. Después de las 09:00 de un día hábil, si no hay fila para hoy, aparece "No
   se envió el reporte…".
3. La cache dura 5 minutos por proceso. Con `--debug`, tocar
   `backend/views/reporte_estado.py` reinicia el proceso y la limpia.

### 7.5 Probar el botón en local

```bash
env -u DATABASE_URL REPORTE_BOTON=1 .venv/bin/flask --app backend.app run --port 5050 --debug
```

- El `.env` local necesita un `SECRET_KEY` distinto de `dev-secret-key`: con
  la default el botón queda apagado (404) y se loguea un WARNING.
- Sin `REPORTE_MAILER` (modo `archivo`), "Enviar prueba" escribe el `.eml` en
  `REPORTE_ARCHIVO_DIR`, deja una fila `'prueba'` SENT con
  `message_id=archivo:<ruta>` y muestra "Guardado en <ruta>". No sale ningún
  mail.
- **No** pongas credenciales reales de Gmail en el `.env` para probar el
  botón: con `REPORTE_MAILER=gmail_api` manda de verdad.
- Al terminar, borrá las filas de prueba **de la base local**:
  `DELETE FROM report_send_log WHERE report_key = 'prueba';`

---

## 8. Operación

### 8.1 El aviso en la web (banner)

El aviso solo existe si el servicio web tiene `REPORTE_BANNER=1`, y solo lo
ven los **admins**.

Mira el **último día hábil esperado**: hoy, si es lunes a viernes y ya son
las **09:00 ART** o más; si no, el día hábil anterior. Lunes antes de las
09:00, sábado y domingo apuntan al viernes. Para ese día lee la fila de
`report_send_log`.

| Mensaje | Qué pasó | Qué hacer |
|---|---|---|
| **No se envió el reporte del DD/MM.** | No hay fila: el cron no corrió o falló antes de reclamar el envío (config, DB, tabla). | Mirá los logs del servicio cron (`railway cron list` / logs del run). Corregí la causa y corrélo a mano (`railway cron run`; si ya pasó ese día, ver [§8.4](#84-reenviar-un-día)). |
| **Estado incierto …** | El envío se cortó después de mandar (timeout de lectura o Gmail respondió 500/502/504), o quedó PENDING más de 30 minutos (el proceso murió, o Gmail aceptó el mail pero no se pudo grabar SENT). Puede haber salido. | Revisá **Enviados** de la cuenta del negocio. Si el mail está, no hagas nada. Si **no** está, reenviá con `--force` ([§8.4](#84-reenviar-un-día)). |
| **Falló el reporte del DD/MM: hay que re-autorizar Gmail …** | El token fue revocado (cambio de contraseña, acceso quitado, app en Testing a los 7 días) → `invalid_grant`. | Corré `scripts/autorizar_gmail.py` ([§3.4](#34-autorizar-y-obtener-el-refresh-token)), actualizá `REPORTE_GMAIL_REFRESH_TOKEN` en el cron y reenviá ese día. |
| **Falló el reporte del DD/MM.** + detalle | Cualquier otra falla: Google, PDF, datos, DB. El detalle es la primera línea del error, sin URLs, mails ni cadenas que parezcan secretos. | Mirá el log del run y la columna `error` de la fila. Corregí la causa y reenviá. |

Comportamiento a tener en cuenta:

- **Cache de 5 minutos:** después de arreglar algo, el aviso tarda hasta 5
  minutos en desaparecer. La cache es por proceso, así que cada worker de
  gunicorn tiene la suya. Un error al consultar (por ejemplo, tabla
  inexistente) no rompe la página: no muestra aviso.
- **Una falla del viernes se ve todo el fin de semana.** Sábado y domingo el
  día esperado sigue siendo el viernes, así que el aviso sigue hasta que se
  reenvíe, o hasta el lunes a las 09:00, cuando pasa a mirar el lunes.
- Un `--force --fecha` de **otra** fecha no tapa el aviso: se consulta la
  fecha exacta esperada.

### 8.2 Estados de `report_send_log`

Hay una fila por `(report_key, fecha_negocio)`, con dos valores de
`report_key`:

- `'diario'`: el envío del cron. Es la que mira el banner.
- `'prueba'`: los envíos de prueba del botón ([§8.5](#85-botón-generar-reporte)).
  Una fila por fecha: probar dos veces la misma fecha reutiliza la fila
  (attempts+1). **Nunca** toca ni reemplaza la fila `'diario'` de esa fecha, y
  el banner la ignora: una prueba SENT no apaga el aviso de un `'diario'`
  fallido, y una prueba FAILED no genera aviso.

Los estados son los mismos para las dos:

| `status` | Significado | ¿El cron la reintenta sola? |
|---|---|---|
| `PENDING` | Reclamado, enviándose. Si queda así, el proceso murió entre el envío y el registro, o Gmail aceptó el mail y falló el UPDATE a SENT (el log del run dice `enviado (id=…) pero no se pudo registrar SENT: NO reenviar`). | No: queda salteada para no duplicar. Después de 30 min el banner la marca como incierta. |
| `SENT` | Enviado. `message_id` y `sent_at` cargados. | No. |
| `FAILED` | Falló. El motivo está en `error`, truncado a 2000 caracteres y sin secretos. | **Sí** en la siguiente corrida, **salvo** que el error empiece con `estado incierto` (eso exige `--force`). |

`attempts` cuenta los intentos. Cada reclamo nuevo (reintento de FAILED o
`--force`) lo incrementa y limpia `error`, `message_id` y `sent_at`, así que la
fila refleja solo el último intento.

### 8.3 Ver las filas (solo lectura)

```sql
SELECT fecha_negocio, status, attempts, message_id, sent_at, left(error, 200) AS error
FROM report_send_log
WHERE report_key = 'diario'
ORDER BY fecha_negocio DESC
LIMIT 10;
```

Para las pruebas del botón, lo mismo con `WHERE report_key = 'prueba'`
(ordenado por `updated_at DESC`). Siempre filtrá por `report_key`: sin el
filtro se mezclan los envíos reales con las pruebas.

### 8.4 Reenviar un día

El comando es `python -m backend.jobs.reporte_diario --fecha AAAA-MM-DD --force`.
Como `railway cron run` no acepta argumentos, en Railway hay dos caminos:

- **Si es hoy, es hábil y la fila está FAILED** (no incierta) **o no
  existe:** alcanza con `railway cron run`.
- **Si no (SENT, PENDING, incierto u otra fecha):** cambiá temporalmente el
  Start Command del cron a
  `python -m backend.jobs.reporte_diario --fecha AAAA-MM-DD --force`, disparalo
  con `railway cron run` y **volvé a dejar el start original enseguida**. Si
  no, cada día hábil reenviaría esa fecha.

Antes de usar `--force` sobre un estado incierto o PENDING, **revisá Enviados**:
`--force` siempre manda.

### 8.5 Botón "Generar reporte"

Página `/reporte` (link "Generar reporte" en el menú, y "Generar / reenviar
prueba" en el banner). Sirve para **ver** el PDF de cualquier fecha y para
**probar** el envío por Gmail cuando se configura todo. **No reemplaza al
cron** ni cuenta como el envío del día.

**Quién y cuándo**

- Existe solo con `REPORTE_BOTON=1` en el web (default **apagado**). Apagado,
  `/reporte`, `/reporte/pdf` y `/reporte/enviar` dan **404** para todos,
  admins incluidos, y no hay links. (Un usuario sin login es redirigido al
  login antes, como con cualquier URL.)
- Solo **admin global**: `is_admin` y **fuera del modo soporte**
  (impersonando a otro usuario da 403). Un no admin recibe **403**.
- Fechas permitidas: de hoy − 365 días a hoy + 31 días (hora ART), con
  formato `AAAA-MM-DD`. Sábado y domingo se permiten. Fuera de rango: 400 en
  "Ver PDF", mensaje de error en "Enviar prueba".

**Ver PDF** (`GET /reporte/pdf?fecha=…`, se abre en otra pestaña)

- Es exactamente el `--dry-run` del job (`generar.generar_pdf`): lectura en
  transacción `READ ONLY`, sin construir mailer y **sin escribir nada** (ni
  siquiera en `report_send_log`). Funciona sin ninguna `REPORTE_GMAIL_*`.
- Respuesta inline con `Cache-Control: no-store`.

**Enviar prueba** (`POST /reporte/enviar`)

1. Chequea que `Origin` (o, si falta, `Referer`) sea del mismo host, y el
   token CSRF de la sesión (comparación en tiempo constante). Si falla
   cualquiera de los dos: **400**, sin generar, enviar ni escribir.
2. Valida la fecha.
3. Arma el mailer **antes** de reclamar: si al web le faltan las
   `REPORTE_*` de mail, muestra el error de configuración y **no escribe
   fila**. Nunca usa `GMAIL_*`.
4. `reclamar_prueba`: toma un **advisory lock** de Postgres
   (`pg_advisory_xact_lock`) que serializa a todos los threads y workers, y
   aplica dos límites **globales** (de cualquier fecha y cualquier admin):
   - hay una prueba PENDING de **menos de 5 minutos** → "en curso", no manda;
   - la última prueba SENT fue hace **menos de 60 segundos** → "reciente", no
     manda.

   Si pasa, reclama la fila `'prueba'` de esa fecha (**una por día**; si ya
   existe, attempts+1).
5. Genera el PDF y lo manda **siempre a `REPORTE_MAIL_TO`** (un `to` en el
   form se ignora), con asunto `[PRUEBA] Reporte diario – lun 09/11/2026` y
   cuerpo "Envío de prueba generado desde la web (no es el envío
   automático)…". El adjunto se llama igual que el del cron.
6. Registra SENT o FAILED en la fila `'prueba'` y vuelve a la página (PRG,
   303) con el mensaje.

Además, el botón se deshabilita con JS ("Enviando…") al primer click.

**Mailer del web: 1 intento, timeouts cortos.** El web usa
`OPCIONES_WEB = {"max_intentos": 1, "timeout": (5, 20)}`: 5 s para conectar,
20 s de lectura, sin reintentos (el cron tiene hasta 3). El peor caso es
~50 s (refresh del token + envío). **Durante ese tiempo el envío ocupa 1 de
los 4 threads** de gunicorn (`--workers 1 --threads 4`).

**Mensajes y qué hacer**

| Mensaje | Qué pasó | Qué hacer |
|---|---|---|
| **Enviado a j\*\*\*@… (id …)** | Salió. Fila `'prueba'` SENT. | Revisar que llegó con asunto `[PRUEBA]`. |
| **Guardado en <ruta> (modo archivo…)** | Solo local, sin `REPORTE_MAILER`. No sale mail. | Nada. |
| **El servicio web no tiene configurado Gmail (REPORTE_MAILER y REPORTE_GMAIL_*)…** | Faltan las variables en el web, o está en `archivo` en Railway. No hay fila. | Compartir las variables con el web ([§5.4](#54-variables-compartidas-webcron)). |
| **Ya hay un envío de prueba en curso o reciente…** | Límite de 5 min (PENDING) o de 60 s (último SENT). No se mandó nada. | Esperar y volver a probar. |
| **Falló el envío: hay que re-autorizar Gmail** | `invalid_grant`. Fila FAILED. | [§3.4](#34-autorizar-y-obtener-el-refresh-token) y actualizar la shared variable. **El cron también va a fallar.** |
| **Gmail no confirmó el envío: puede haber salido…** (estado incierto) | Timeout de lectura o 500/502/504 **después** de mandar. Fila FAILED con `estado incierto`. | **Revisá Enviados** antes de volver a probar. Si está, no hagas nada. Si no está, probá de nuevo (pasados 60 s). |
| **Se envió (id …) pero no se pudo registrar: NO reenviar** | Gmail aceptó el mail pero falló el UPDATE a SENT. La fila queda **PENDING** y bloquea nuevas pruebas por 5 min. | **No reenviar**: el mail salió. Verificalo en Enviados. Mirá la DB (logs del web). Pasados 5 min el botón vuelve a andar; la fila PENDING vieja se re-reclama en la próxima prueba de esa fecha. |
| **Falló el envío: …** / **Error inesperado (Tipo)** | Otra falla (Google, PDF, DB). Fila FAILED si llegó a reclamar. | Logs del web y columna `error` de la fila. |

**Auditoría.** Cada "Ver PDF" y cada "Enviar prueba", **incluidos los
rechazos** (y los 403 o sin login al entrar a cualquiera de las 3 rutas),
deja una línea en el logger `backend.reporte.auditoria`, que
escribe a stdout y se ve en los **logs del servicio web** en Railway:

```
... INFO backend.reporte.auditoria: reporte prueba: usuario=admin1 id=3 fecha=2026-11-09 accion=enviar resultado=ok intento=1 message_id=18c...
```

`resultado` puede ser `ok`, `config`, `en_curso`, `demasiado_pronto`,
`token_revocado`, `incierto`, `error`, `enviado_sin_registrar`,
`falla_sin_registrar`, `fecha_invalida`, `origen_invalido`, `csrf_invalido`,
`sin_login`, `prohibido_no_admin` o `prohibido_soporte`. En los errores se
agrega `detalle=<TipoDeExcepción>`. **Nunca** se loguean tokens, secretos,
el contenido del PDF ni el texto de las excepciones; los campos se limpian de
espacios y saltos de línea (no se puede inyectar una línea falsa).

**Limitaciones conocidas (aceptadas)**

- **"Ver PDF" es un GET sin token CSRF.** Es de solo lectura y solo para admin
  global; un sitio externo como mucho podría hacer que el navegador del admin
  lo descargue, sin poder leerlo. Aceptado en el design.
- **La cookie de sesión no tiene `SameSite`** (Flask no lo configura en este
  proyecto). Por eso el POST chequea `Origin`/`Referer` además del token. La
  falta de CSRF global en el resto de la web es deuda aparte (issue separado).
- **Un envío ocupa 1 de los 4 threads hasta ~50 s.** Con los límites globales
  de 60 s / 5 min no puede haber más de uno a la vez.

---

## 9. Rollback

- **Frenar los envíos:** vaciá el Cron Schedule o borrá el servicio cron.
- **Apagar el aviso:** `REPORTE_BANNER=0`, o quitá la variable del servicio
  web.
- **Apagar el botón:** `REPORTE_BOTON=0` (o quitá la variable) en el web: las
  rutas vuelven a dar 404 y desaparecen los links. Si además querés sacar las
  credenciales del web, quitá el **Share** de las `REPORTE_*` a
  `sirio-arabia-crm` (el cron las sigue teniendo). Las filas de prueba se
  pueden borrar sin afectar al cron ni al banner:
  `DELETE FROM report_send_log WHERE report_key='prueba';`
- **Credenciales:** revocá el acceso en <https://myaccount.google.com/permissions>
  y borrá las `REPORTE_GMAIL_*` del cron.
- **Código:** revert del merge. La tabla `report_send_log` es aditiva y puede
  quedar; no molesta a la web.

---

## 10. Dependencias

En `requirements.txt` (producción):

| Paquete | Versión | Por qué |
|---|---|---|
| `fpdf2` | 2.8.9 | Genera el PDF. Soporta Python ≥ 3.10. |
| `Pillow` | 12.3.0 | Dependencia de fpdf2. **Pinneada:** las versiones siguientes exigen Python ≥ 3.11 y producción corre **3.10** (`BP_PYTHON_VERSION` en `Railway.toml`). |
| `fonttools` | 4.65.0 | Dependencia de fpdf2 (subset de la fuente). **Pinneada** por el mismo motivo. |
| `defusedxml` | 0.7.1 | Dependencia de fpdf2. |
| `requests` | 2.32.3 | Ya estaba. Se usa para la Gmail API (no se usa `google-api-python-client`). |

En `requirements-dev.txt` (solo desarrollo; **no** van a producción):
`pytest==9.2.0` y `google-auth-oauthlib==1.5.0` (este último lo usa solo
`scripts/autorizar_gmail.py`).

Si actualizás Pillow o fonttools, comprobá antes que la versión nueva siga
soportando Python 3.10.

**Fuente:** Poppins Regular y SemiBold, licencia **SIL Open Font License
1.1**. Los TTF y la licencia están en `backend/services/reporte_diario/fuentes/`
(`OFL.txt`). Están fuera de `backend/static` a propósito: la web no los sirve.

---

## 11. Apéndice: VPS con systemd

Si algún día el sistema se muda de Railway a un VPS, este es el equivalente
del cron. Ajustá las rutas:

`/etc/systemd/system/reporte-diario.service`

```ini
[Unit]
Description=Reporte diario Sirio Arabia

[Service]
Type=oneshot
WorkingDirectory=/srv/sirio-arabia
EnvironmentFile=/srv/sirio-arabia/.env.reporte
ExecStart=/srv/sirio-arabia/.venv/bin/python -m backend.jobs.reporte_diario
```

`/etc/systemd/system/reporte-diario.timer`

```ini
[Unit]
Description=Reporte diario L-V 08:00 ART

[Timer]
OnCalendar=Mon..Fri 08:00 America/Argentina/Buenos_Aires
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reporte-diario.timer
journalctl -u reporte-diario
```

`Persistent=true` corre una ejecución perdida, por ejemplo si el servidor
estaba apagado a las 08:00. La idempotencia por fecha evita los duplicados.

---

## 12. Checklist de puesta en producción

### 12.1 Antes del merge (verificación manual)

Durante el desarrollo se verificaron en local la suite en 3.12 y 3.10, el
flujo end-to-end en modo `archivo`, los estados del banner y el botón (ver
abajo). Antes de mergear, conviene repasarlos con la rama ya rebaseada sobre
`main`. Entre corchetes va el criterio de aceptación de la spec que cubre cada
punto.

Antes de cualquier corrida local, verificá que el `.env` apunte a `localhost`
([§5.3](#53-correr-el-cron-a-mano)).

- [ ] `env -u DATABASE_URL .venv/bin/python -m pytest` verde en Python 3.12 y
      en 3.10 [J Compatibilidad].
- [ ] Dry-run de un **lunes**, un **martes** y un **viernes**
      (`--dry-run --fecha <día> --out …`) contra la base local [G Vista
      previa]:
  - martes: solo cumpleaños de hoy, vencen hoy, ATRASADAS, pagadas sin
    registrar (y sin vencimiento si hay); **sin** semana, parciales, "vencen
    más adelante", total general ni regularidad;
  - viernes: títulos "… y del fin de semana" / "… hoy y el fin de semana" y
    subtítulo "incluye sáb y dom hasta dom dd/mm/aaaa";
  - lunes: las secciones extendidas en orden, "vencen más adelante" con
    total, "Total general de deuda vigente" y la regularidad al final
    ("Sin registros" con la seed local, que no tiene pares con 3 días
    distintos).
- [ ] Revisar el PDF a ojo:
  - orden de las secciones y conteos;
  - "Sin registros" en las secciones vacías;
  - totales solo en las secciones con total y con filas;
  - "Sin monto" en los pedidos sin precio, fuera del total;
  - ATRASADAS con días de atraso y encabezado repetido en cada página;
  - ñ / “ ” / — bien renderizados;
  - pie con la hora real

  [D, C ATRASADAS / Sin monto].
- [ ] Ninguna "pagada sin registrar" aparece en ATRASADAS. Si no hay datos
      locales, lo cubre `tests/test_clasificar.py` [C Clasificación
      exclusiva].
- [ ] Sábado sin `--force`: exit 0 y sin fila [A Negativa en fin de semana].
- [ ] Idempotencia: la segunda corrida con SENT sale con exit 0 sin enviar;
      `--force` hace attempts+1; dos corridas concurrentes dejan un solo SENT
      [F].
- [ ] `REPORTE_MAILER=gmail_api` con config incompleta y `GMAIL_*` presentes:
      exit 1, sin fila y sin usar `GMAIL_*` [E Canal].
- [ ] El log muestra `localhost/sirioarabia`, la fecha, `modo=`, los conteos y
      el message id, sin tokens [E Secretos, G Log].
- [ ] La tabla `collection` no tiene filas nuevas después de correr el job
      [I].
- [ ] Banner:
  - apagado: no muestra aviso;
  - prendido: lo ve un admin y no lo ve un usuario no admin;
  - corte a las 08:59 vs 09:01;
  - con la tabla inexistente, la página no se rompe;
  - una fila `'prueba'` no prende ni apaga el aviso

  [H, F].
- [ ] Botón en local ([§7.5](#75-probar-el-botón-en-local)), sin credenciales
      reales de Gmail [L, J]:
  - `REPORTE_BOTON` sin definir: `/reporte`, `/reporte/pdf` y
    `/reporte/enviar` dan 404 y no hay link en el menú;
  - con `REPORTE_BOTON=1`: un admin ve el link y la página; un no admin
    recibe 403;
  - "Ver PDF" devuelve un PDF y no cambia `report_send_log`;
  - POST sin token, con token malo o con `Origin` ajeno: 400 sin fila;
  - "Enviar prueba" en modo archivo: "Guardado en …" y fila `'prueba'` SENT;
    un segundo envío antes de 60 s se rechaza; la fila `'diario'` no cambia;
  - al terminar, borrar las filas `'prueba'` de la base local.
- [ ] /deudas y el dashboard muestran lo mismo que antes del cambio [J Web
      intacta].

### 12.2 Puesta en producción (en este orden)

El PR #18 (`feat/pedidos-cliente-oct`) ya está mergeado en `main`.
`feat/reporte-regularidad` está apilada sobre `feat/reporte-diario`
(PR #19). Esta rama **no** agrega migraciones: la única es
`c3e5a9d1f7b2` (`report_send_log`), que viene con #19.

- [ ] 1. **PRIMERO: confirmar que el web tiene un `SECRET_KEY` real.** En las
      Variables del servicio `sirio-arabia-crm`, `SECRET_KEY` tiene que
      existir y **no** ser `dev-secret-key` (el default de
      `backend/config.py`). El token CSRF del botón vive en la sesión firmada
      con esa clave: con la default, cualquiera puede fabricar una sesión y el
      CSRF del botón no vale nada. Si falta o es la default, generá una
      (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) y
      cargala en el web, sin pegarla en ningún otro lado. Ojo: cambiarla
      **cierra la sesión de todos los usuarios**; hacelo en un horario
      tranquilo. **No prender `REPORTE_BOTON` hasta tener esto confirmado.**
      (Además, el código lo exige: con `SECRET_KEY` ausente o en
      `dev-secret-key`, el botón queda apagado aunque `REPORTE_BOTON=1`,
      con 404 y sin links, y el web loguea un WARNING una vez, sin la clave.)
- [ ] 2. **Variables.**
  - Google Cloud: proyecto, Gmail API, consentimiento External solo con
    `gmail.send`, **publicada "In production"** y cliente Desktop app. Ver
    [§3.1–3.3](#3-google-cloud-y-gmail-paso-a-paso).
  - Correr `scripts/autorizar_gmail.py` **una sola vez** con la cuenta del
    negocio y guardar el refresh token en un lugar seguro. Ver
    [§3.4](#34-autorizar-y-obtener-el-refresh-token).
  - Crear las **shared variables** `REPORTE_MAILER=gmail_api`,
    `REPORTE_GMAIL_CLIENT_ID`, `REPORTE_GMAIL_CLIENT_SECRET`,
    `REPORTE_GMAIL_REFRESH_TOKEN`, `REPORTE_MAIL_FROM`, `REPORTE_MAIL_TO` y
    `REPORTE_REMITENTE_NOMBRE`, y hacer Share al web (`sirio-arabia-crm`) y
    al cron (cuando exista, paso 9). Verificar que en el web aparecen como
    `${{shared.VAR}}` y que **no** hay copias sueltas. Ver
    [§5.4](#54-variables-compartidas-webcron).
  - Dejar `REPORTE_BOTON` y `REPORTE_BANNER` **sin definir** por ahora.
- [ ] 3. **Mergear** (y con eso deployar la web con la migración).
      Rebasear `feat/reporte-diario` sobre `main`, correr la suite, mergear
      #19; después el PR de `feat/reporte-regularidad`. El deploy del web
      aplica `c3e5a9d1f7b2` al arrancar (`AUTO_MIGRATE=1` en
      `Railway.toml`). Verificar que `alembic_version` sea `c3e5a9d1f7b2`, que
      el log no tenga errores de migración y que la web ande igual que antes
      (sin link "Generar reporte"). Ver
      [§5.1](#51-orden-obligatorio-primero-la-web).
- [ ] 4. **Prender `REPORTE_BOTON=1` en el web** (y solo en el web). Recién
      después del paso 1.
- [ ] 5. **Probar desde el botón**, como admin **fuera** del modo soporte, en
      `/reporte`:
  - "Ver PDF" de un lunes (extendido, con regularidad) y de un martes. No
    escribe nada; sirve para mirar la regularidad con datos reales antes de
    mandar nada;
  - "Enviar prueba" de una fecha. Tiene que decir "Enviado a j\*\*\*@… (id …)".
    Si dice "El servicio web no tiene configurado Gmail…", falta el Share
    del paso 2.
- [ ] 6. **Verificar que llegó** a `REPORTE_MAIL_TO`:
  - asunto `[PRUEBA] Reporte diario – …` y el adjunto;
  - que el From muestre "Arabia Representaciones" (Google no garantiza el
    display name);
  - que **el celular notifique** un mail enviado a uno mismo. Esto no está
    verificado. Si no notifica, las alternativas son un filtro de Gmail que
    lo marque importante o con una etiqueta que notifique, o usar otra
    casilla destino en `REPORTE_MAIL_TO`;
  - que la fila `'diario'` de esa fecha (si existe) **no cambió**
    (`SELECT … WHERE report_key='diario'`, [§8.3](#83-ver-las-filas-solo-lectura)).
- [ ] 7. **Verificar la línea de auditoría** en los logs del web:
      `backend.reporte.auditoria: reporte prueba: usuario=… accion=enviar
      resultado=ok … message_id=…`, sin tokens ni secretos. Ver
      [§8.5](#85-botón-generar-reporte).
- [ ] 8. **(Opcional) Apagar el botón** con `REPORTE_BOTON=0` si no se va a
      usar en el día a día. Se puede volver a prender cuando haga falta.
- [ ] 9. **Crear el servicio cron**: start
      `python -m backend.jobs.reporte_diario`, schedule `0 11 * * 1-5`,
      restart policy **Never**, sin dominio, con las shared variables del
      paso 2 compartidas y `DATABASE_URL` al mismo Postgres. Ver
      [§5.2](#52-crear-el-servicio-cron).
- [ ] 10. **Primera corrida del cron**, en un **día hábil**:
      `railway cron run` si ese día todavía no tiene fila `'diario'`, o
      `--force` si ya la tiene (ver [§8.4](#84-reenviar-un-día)). Confirmar
      en el log que corrió el job (`reporte diario: inicio…`, `conteos:
      modo=…`) y **no** gunicorn. El mail llega **sin** `[PRUEBA]`.
- [ ] 11. **Prender `REPORTE_BANNER=1` en el web.** Verificar que, con el
      reporte del día en SENT, un admin no ve ningún aviso.
- [ ] 12. **Vigilar la primera corrida programada:**
  - el siguiente día hábil, ~08:00 ART, revisar que llegó el mail y que
    `railway cron list` muestra el run OK;
  - **a los 8 días**, confirmar que sigue llegando. Eso prueba que el token
    no quedó en modo Testing.
- [ ] 13. **Primer lunes real:** revisar la regularidad con el cliente.
      Si la lista sale vacía o desbordada, recalibrar los **umbrales
      provisionales** en `regularidad.py` ([§1](#regularidad-de-compra-solo-el-lunes))
      y confirmar el formato tabla ([§12.3](#123-temas-abiertos)).

### 12.3 Temas abiertos

- **Formato de la regularidad:** el cliente la pidió como oraciones; el PDF
  la muestra como tabla. **Falta que el cliente confirme** que la tabla le
  sirve. Cambiarla a oraciones es tocar solo `pdf.py`.
- **Umbrales provisionales** (`MIN_DIAS=3`, `MAX_INTERVALOS=12`,
  `FACTOR_TOPE=3`, `TOPE_DIAS=365`): sin calibrar con datos de producción.
  Revisarlos después del primer lunes real.
- **CSRF global de la web:** el botón tiene su propio token + `Origin`, pero
  el resto de los POST de la web no tiene CSRF y la cookie de sesión no tiene
  `SameSite`. Es deuda previa, va en un issue aparte.
