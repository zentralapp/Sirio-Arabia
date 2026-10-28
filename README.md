# Sirio Arabia CRM (Flask)

## Requisitos
- Python 3.10+
- pip
- **PostgreSQL 13+** (único motor soportado, ver más abajo)
- (Opcional) virtualenv
- Node.js + npm (para instalar Railway CLI)

## Base de datos: sólo PostgreSQL

El proyecto requiere PostgreSQL. **No hay base por defecto ni fallback**: si
`DATABASE_URL` no está definida, la app falla al arrancar con un mensaje
explicando qué configurar.

El motivo es que el modelo usa aritmética de fechas de Postgres (`date + entero`
para calcular el vencimiento de cobranzas, ver `vencimiento_efectivo_expr()` en
`backend/models.py`). Otros motores no fallan con esa expresión, devuelven
valores silenciosamente incorrectos.

## Configuración local
1. Crear entorno virtual e instalar dependencias:
```
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
2. Crear la base en PostgreSQL:
```
createdb sirioarabia
```
3. Crear archivo `.env` basado en `.env.example` y definir `DATABASE_URL`:
```
DATABASE_URL=postgresql://usuario:password@localhost:5432/sirioarabia
```
4. Aplicar las migraciones:
```
export FLASK_APP=backend.app    # Windows: set FLASK_APP=backend.app
flask db upgrade
```
5. Ejecutar:
```
python -m backend.app
```

## Railway
1. Instalar Railway CLI:
```
npm i -g @railway/cli
```
2. Iniciar sesión y crear proyecto:
```
railway login
railway init
```
3. Agregar Postgres como plugin en Railway y obtener `DATABASE_URL` (se inyecta como variable en el servicio).
4. Desplegar:
```
railway up
```

Se recomienda tener dos entornos (development y production) en Railway y usar variables por entorno.
