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

- **Formato:** el mail trae solo el PDF adjunto
  (`reporte-diario-AAAA-MM-DD.pdf`). El asunto es
  `Reporte diario – jue 29/10/2026` y el cuerpo es
  `Adjunto el reporte del día.`
- **Casilla:** la misma cuenta Gmail **general del negocio** manda el mail y
  lo recibe. No se usa la casilla personal de nadie.
- **Alcance:** global. Entran todos los pedidos de todos los usuarios, sin
  filtrar por vendedor.

### Secciones del PDF (en este orden)

| Sección | Qué entra |
|---|---|
| Cumpleaños de hoy | Cumpleaños cuya fecha cae hoy. |
| Cumpleaños de la semana | Desde mañana hasta el domingo inclusive. |
| Cobranzas que vencen hoy | Vencimiento = hoy. |
| Cobranzas que vencen en la semana | Desde mañana hasta el domingo inclusive. |
| ATRASADAS | Vencimiento < hoy, con días de atraso. Las más atrasadas van primero. |
| Pagos parciales | Tienen pagos o notas de crédito y les queda saldo > 0,01. Es una sección transversal: un parcial también aparece en su sección por vencimiento. |
| Pagadas sin registrar cobro | Ver la regla abajo. Aparecen SOLO acá. |
| Sin fecha de vencimiento | Aparece solo si tiene filas. |

Una sección vacía dice "Sin registros". Si están todas vacías, el PDF dice
"Sin novedades".

### Reglas (resumen; la fuente de verdad es el código)

- **"La semana"** va desde hoy hasta el domingo siguiente inclusive. Un
  viernes incluye sábado y domingo. Un domingo es el mismo día.
  Código: `fin_de_semana` en `backend/services/reporte_diario/fechas.py`.
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
- **Totales:** solo hay total de saldo en "vencen hoy", "vencen en la semana",
  ATRASADAS y "Pagos parciales".
- **Cumpleaños:** se proyectan al año en curso. El 29/2 se muestra el 28/2 en
  los años no bisiestos. Funciona también cuando la semana cruza de año.
- **Clientes archivados:** sus **cobranzas sí aparecen**, porque la deuda es
  deuda. Sus **cumpleaños no**.

Código de la clasificación: `backend/services/reporte_diario/clasificar.py`.
Las queries están en `backend/services/reporte_diario/datos.py`.

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
  reintentos acotados. Si se corta la conexión **después** de mandar, el envío
  queda en "estado incierto" y no se reintenta solo, para no duplicar el mail.
- **Aviso en la web:** los admins ven un banner si el reporte del último día
  hábil no salió (`backend/services/reporte_estado.py`). Se prende con
  `REPORTE_BANNER=1`.

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
| `REPORTE_MAILER` | No | `archivo` | `gmail_api` (manda de verdad) o `archivo` (escribe un `.eml` en disco). |
| `REPORTE_GMAIL_CLIENT_ID` | Sí con `gmail_api` | — | Client ID del cliente OAuth Desktop. |
| `REPORTE_GMAIL_CLIENT_SECRET` | Sí con `gmail_api` | — | Client secret. |
| `REPORTE_GMAIL_REFRESH_TOKEN` | Sí con `gmail_api` | — | Salida de `scripts/autorizar_gmail.py`. |
| `REPORTE_MAIL_FROM` | Sí con `gmail_api` | `reporte-diario@localhost` (solo modo archivo) | Remitente: la casilla del negocio. Tiene que ser exactamente una dirección. |
| `REPORTE_MAIL_TO` | Sí con `gmail_api` | `reporte-diario@localhost` (solo modo archivo) | Destinatario: la **misma** casilla, exactamente **una** dirección. |
| `REPORTE_REMITENTE_NOMBRE` | No | `Arabia Representaciones` | Nombre visible del From. |
| `REPORTE_ARCHIVO_DIR` | No | `/tmp/reportes` | Carpeta de los `.eml` en modo `archivo`. |
| `REPORTE_BANNER` | No | apagado | **En el servicio web**, no en el cron. `1` prende el aviso a admins. |

Si con `gmail_api` falta alguna variable obligatoria, el job sale con exit 1
**antes** de tocar `report_send_log`. El error nombra las variables que
faltan, nunca sus valores.

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
El servicio web no cambia, salvo por `REPORTE_BANNER`.

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
`REPORTE_BANNER`.

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
# Vista previa del viernes 30/10 (incluye vencimientos y cumples de sáb y dom)
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --fecha 2026-10-30 --out /tmp/reporte-vie.pdf

# Vista previa de hoy
.venv/bin/python -m backend.jobs.reporte_diario --dry-run --out /tmp/reporte-hoy.pdf

# Reenviar el reporte del 29/10 (y guardar una copia)
.venv/bin/python -m backend.jobs.reporte_diario --fecha 2026-10-29 --force --out /tmp/reenviado.pdf
```

El log muestra el inicio, el host y la base (sin usuario ni contraseña), la
fecha, los conteos por sección y el message id o el error. Nunca muestra
tokens ni secretos.

---

## 7. Probar en local

### 7.1 Base y migración

Levantá la web una vez con `AUTO_MIGRATE=1`, así aplica la migración de
`report_send_log`:

```bash
env -u DATABASE_URL AUTO_MIGRATE=1 .venv/bin/flask --app backend.app run --port 5050 --debug
```

`env -u DATABASE_URL` hace que la URL salga del `.env` local y no de una
variable exportada que podría apuntar a otra base. Sin `AUTO_MIGRATE=1`, el
arranque puede colgarse por el autodeadlock del issue #7.

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
| **Estado incierto …** | El envío se cortó después de mandar (timeout de lectura), o quedó PENDING más de 30 minutos (el proceso murió). Puede haber salido. | Revisá **Enviados** de la cuenta del negocio. Si el mail está, no hagas nada. Si **no** está, reenviá con `--force` ([§8.4](#84-reenviar-un-día)). |
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

Hay una fila por `(report_key='diario', fecha_negocio)`.

| `status` | Significado | ¿El cron la reintenta sola? |
|---|---|---|
| `PENDING` | Reclamado, enviándose. Si queda así, el proceso murió entre el envío y el registro. | No: queda salteada para no duplicar. Después de 30 min el banner la marca como incierta. |
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

---

## 9. Rollback

- **Frenar los envíos:** vaciá el Cron Schedule o borrá el servicio cron.
- **Apagar el aviso:** `REPORTE_BANNER=0`, o quitá la variable del servicio
  web.
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
flujo end-to-end en modo `archivo` y los estados del banner. Antes de mergear,
conviene repasarlos con la rama ya rebaseada sobre `main`. Entre corchetes va
el criterio de aceptación de la spec que cubre cada punto.

- [ ] `env -u DATABASE_URL .venv/bin/python -m pytest` verde en Python 3.12 y
      en 3.10 [J Compatibilidad].
- [ ] Dry-run del viernes (`--dry-run --fecha <viernes> --out …`) contra la
      base local. Tiene que incluir los vencimientos y cumpleaños del sábado y
      el domingo [A Ventana, G Vista previa].
- [ ] Revisar el PDF a ojo:
  - orden de las secciones y conteos;
  - "Sin registros" en las secciones vacías;
  - totales solo en 4 secciones;
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
- [ ] El log muestra `localhost/sirioarabia`, la fecha, los conteos y el
      message id, sin tokens [E Secretos, G Log].
- [ ] La tabla `collection` no tiene filas nuevas después de correr el job
      [I].
- [ ] Banner:
  - apagado: no muestra aviso;
  - prendido: lo ve un admin y no lo ve un usuario no admin;
  - corte a las 08:59 vs 09:01;
  - con la tabla inexistente, la página no se rompe

  [H].
- [ ] /deudas y el dashboard muestran lo mismo que antes del cambio [J Web
      intacta].

### 12.2 Puesta en producción (en este orden)

- [ ] 1. **Mergear el PR #18** (`feat/pedidos-cliente-oct`) después de
      probarlo en un iPhone. Esta rama está apilada sobre esa.
- [ ] 2. **Rebasear** `feat/reporte-diario` sobre `main` y volver a correr la
      suite.
- [ ] 3. **Abrir el PR** de `feat/reporte-diario` y mergearlo.
- [ ] 4. **Deploy de la web** (migración). Verificar que `alembic_version` sea
      `c3e5a9d1f7b2` y que la web ande igual que antes. Dejar
      `REPORTE_BANNER` sin definir por ahora. Ver [§5.1](#51-orden-obligatorio-primero-la-web).
- [ ] 5. **Google Cloud**: proyecto, Gmail API, consentimiento External solo
      con `gmail.send`, **publicada "In production"** y cliente Desktop app.
      Ver [§3.1–3.3](#3-google-cloud-y-gmail-paso-a-paso).
- [ ] 6. **Correr `scripts/autorizar_gmail.py` una sola vez** con la cuenta
      del negocio y guardar el refresh token en un lugar seguro. Ver
      [§3.4](#34-autorizar-y-obtener-el-refresh-token).
- [ ] 7. **Variables de Railway** del servicio cron. Ver
      [§4](#4-variables-de-entorno).
- [ ] 8. **Crear el servicio cron**: start
      `python -m backend.jobs.reporte_diario`, schedule `0 11 * * 1-5`,
      restart policy **Never**, sin dominio. Ver
      [§5.2](#52-crear-el-servicio-cron).
- [ ] 9. **Primera corrida manual:**
  - antes, una vista previa con `--dry-run --out` en local, contra la base
    local;
  - después, en un **día hábil**, un envío real en Railway: `railway cron run`
    si ese día todavía no tiene fila, o `--force` si ya la tiene (ver
    [§8.4](#84-reenviar-un-día));
  - confirmar en el log que corrió el job y **no** gunicorn.
- [ ] 10. **Revisar el mail recibido:**
  - asunto y adjunto;
  - que el From muestre "Arabia Representaciones" (Google no garantiza el
    display name);
  - que **el celular notifique** un mail enviado a uno mismo. Esto no está
    verificado. Si no notifica, las alternativas son un filtro de Gmail que lo
    marque importante o con una etiqueta que notifique, o usar otra casilla
    destino en `REPORTE_MAIL_TO`.
- [ ] 11. **Prender `REPORTE_BANNER=1` en el servicio web.** Verificar que,
      con el reporte del día en SENT, un admin no ve ningún aviso.
- [ ] 12. **Vigilar la primera corrida programada:**
  - el siguiente día hábil, ~08:00 ART, revisar que llegó el mail y que
    `railway cron list` muestra el run OK;
  - **a los 8 días**, confirmar que sigue llegando. Eso prueba que el token no
    quedó en modo Testing.
