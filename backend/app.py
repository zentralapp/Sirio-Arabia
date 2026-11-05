from flask import Flask
from dotenv import load_dotenv
import os
import time
import traceback
import sys

from .extensions import db, migrate, login_manager
from .config import Config
from . import models
from flask_migrate import upgrade
from sqlalchemy import text
from werkzeug.security import generate_password_hash


def create_app():
    load_dotenv()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config())

    is_debug = (os.getenv("FLASK_DEBUG", "0") == "1")
    try:
        db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
        is_local_sqlite = db_uri.startswith("sqlite")
    except Exception:
        is_local_sqlite = False

    if is_debug or is_local_sqlite:
        app.config["TEMPLATES_AUTO_RELOAD"] = True
        app.jinja_env.auto_reload = True
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    db.init_app(app)
    migrate.init_app(app, db)
    # Auth
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Ensure DB schema exists in production (Railway)
    with app.app_context():
        did_upgrade = False

        def _sqlite_ensure_user_schema():
            try:
                if db.engine.dialect.name != "sqlite":
                    return
            except Exception:
                return

            def _ensure_table(sql: str):
                try:
                    db.session.execute(text(sql))
                    db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            def _ensure_col(table: str, col: str, ddl: str):
                try:
                    cols = db.session.execute(text(f"PRAGMA table_info('{table}')"))
                    names = {row[1] for row in cols.fetchall()}
                    if col not in names:
                        db.session.execute(text(ddl))
                        db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            # Tabla de usuarios (para login)
            _ensure_table(
                """
                CREATE TABLE IF NOT EXISTS app_user (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    is_admin BOOLEAN NOT NULL DEFAULT 0,
                    permissions_json TEXT,
                    created_at DATETIME
                )
                """
            )

            # Nuevas columnas en app_user
            _ensure_col("app_user", "permissions_json", "ALTER TABLE app_user ADD COLUMN permissions_json TEXT")

            # Ownership columns (multi-tenancy)
            _ensure_col("client", "owner_user_id", "ALTER TABLE client ADD COLUMN owner_user_id INTEGER")
            _ensure_col("order", "owner_user_id", "ALTER TABLE 'order' ADD COLUMN owner_user_id INTEGER")
            _ensure_col("order", "deleted_at", "ALTER TABLE 'order' ADD COLUMN deleted_at DATETIME")
            _ensure_col("order_draft", "owner_user_id", "ALTER TABLE order_draft ADD COLUMN owner_user_id INTEGER")
            _ensure_col("collection", "owner_user_id", "ALTER TABLE collection ADD COLUMN owner_user_id INTEGER")
            _ensure_col("collection_payment", "owner_user_id", "ALTER TABLE collection_payment ADD COLUMN owner_user_id INTEGER")
            _ensure_col("client_document", "owner_user_id", "ALTER TABLE client_document ADD COLUMN owner_user_id INTEGER")
            _ensure_col("client_alert_state", "owner_user_id", "ALTER TABLE client_alert_state ADD COLUMN owner_user_id INTEGER")

        def _seed_default_users_if_missing():
            try:
                from .models import AppUser
            except Exception:
                return

            try:
                # Si la tabla no existe todavía, el count fallará.
                existing = AppUser.query.count()
            except Exception:
                return

            if existing and int(existing) > 0:
                return

            users = [
                ("SIRIO", "Sirio76", True),
                ("RICARDO", "Santiago76", False),
                ("EDGARDO", "Edgar76", False),
                ("PEPE", "Centro76", False),
            ]
            try:
                for username, pwd, is_admin in users:
                    u = AppUser.query.filter_by(username=username).first()
                    if u:
                        continue
                    u = AppUser(username=username, password_hash=generate_password_hash(pwd), is_admin=bool(is_admin))
                    db.session.add(u)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass

        if os.getenv("AUTO_MIGRATE") == "1":
            try:
                advisory_lock_taken = False
                lock_conn = None
                try:
                    if db.engine.dialect.name == "postgresql":
                        lock_conn = db.engine.connect()
                        got_lock = lock_conn.execute(text("SELECT pg_try_advisory_lock(2147483647)"))
                        advisory_lock_taken = bool(got_lock.scalar())

                    if advisory_lock_taken:
                        try:
                            # Por defecto, migrar hasta HEAD (última revisión) para incluir nuevas migraciones.
                            upgrade(revision=os.getenv("ALEMBIC_TARGET_REVISION", "head"))
                        except Exception:
                            traceback.print_exc(file=sys.stderr)
                            raise
                    else:
                        if db.engine.dialect.name == "postgresql":
                            deadline = time.time() + float(os.getenv("AUTO_MIGRATE_WAIT_SECONDS", "60"))
                            while time.time() < deadline:
                                try:
                                    ok = db.session.execute(
                                        text(
                                            "SELECT 1 "
                                            "FROM information_schema.columns "
                                            "WHERE table_schema = 'public' "
                                            "AND table_name = 'client_company_link' "
                                            "AND column_name = 'plazo_pago_dias'"
                                        )
                                    ).first()
                                    if ok is not None:
                                        break
                                except Exception:
                                    try:
                                        db.session.rollback()
                                    except Exception:
                                        pass
                                time.sleep(1)
                            else:
                                raise RuntimeError("Timed out waiting for migrations to complete")
                finally:
                    if advisory_lock_taken:
                        try:
                            if lock_conn is not None:
                                lock_conn.execute(text("SELECT pg_advisory_unlock(2147483647)"))
                        except Exception:
                            pass
                    if lock_conn is not None:
                        try:
                            lock_conn.close()
                        except Exception:
                            pass
                did_upgrade = True
            except Exception:
                # Rely on logs in platform to inspect failure
                did_upgrade = False
                # On Postgres, don't continue booting with a potentially incompatible schema.
                try:
                    if db.engine.dialect.name != "sqlite":
                        raise
                except Exception:
                    raise
        # If migrations are not enabled or failed, fall back to create_all
        if not did_upgrade:
            try:
                db.create_all()
                _sqlite_ensure_user_schema()
                _seed_default_users_if_missing()
            except Exception:
                # As último recurso, dejar que la app arranque y ver logs
                pass

        # Safety net: en Postgres, si falta alguna tabla por fuera de Alembic (p.ej. commission_state),
        # create_all la crea sin tocar tablas existentes. No silenciar fallos en Postgres.
        try:
            db.create_all()
        except Exception:
            try:
                if db.engine.dialect.name != "sqlite":
                    raise
            except Exception:
                raise

        # Postgres: asegurar columnas nuevas críticas antes de atender requests (Railway)
        try:
            if db.engine.dialect.name == "postgresql":
                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            DO $$
                            BEGIN
                                ALTER TABLE app_user ADD COLUMN permissions_json TEXT;
                            EXCEPTION
                                WHEN duplicate_column THEN NULL;
                            END $$;
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            DO $$
                            BEGIN
                                ALTER TABLE collection_payment ADD COLUMN voided_at TIMESTAMP;
                            EXCEPTION
                                WHEN duplicate_column THEN NULL;
                            END $$;
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            DO $$
                            BEGIN
                                ALTER TABLE collection_payment ADD COLUMN voided_by_user_id INTEGER;
                            EXCEPTION
                                WHEN duplicate_column THEN NULL;
                            END $$;
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            DO $$
                            BEGIN
                                ALTER TABLE collection_payment ADD COLUMN voided_reason TEXT;
                            EXCEPTION
                                WHEN duplicate_column THEN NULL;
                            END $$;
                            """
                        )
                    )
        except Exception:
            # No bloquear arranque por esto; si falla, el before_app_request puede reintentar.
            try:
                db.session.rollback()
            except Exception:
                pass

        # SQLite: aunque haya arrancado con esquema previo, asegurar columnas mínimas para multi-user
        try:
            _sqlite_ensure_user_schema()
            _seed_default_users_if_missing()
        except Exception:
            pass

        # Asegurar admin para Sirio (Railway/SQLite): si el usuario existe, forzar is_admin=true
        try:
            try:
                db.session.execute(
                    text(
                        "UPDATE app_user SET is_admin = true WHERE UPPER(username) = 'SIRIO'"
                    )
                )
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        except Exception:
            pass

        # SQLite: create_all no agrega columnas nuevas; asegurar compatibilidad mínima
        try:
            if db.engine.dialect.name == "sqlite":
                cols = db.session.execute(text("PRAGMA table_info(client_company_link)"))
                col_names = {row[1] for row in cols.fetchall()}  # row[1] = name
                if "plazo_pago_dias" not in col_names:
                    db.session.execute(text("ALTER TABLE client_company_link ADD COLUMN plazo_pago_dias INTEGER"))
                    db.session.commit()
        except Exception:
            # No impedir el arranque si el check/alter falla
            try:
                db.session.rollback()
            except Exception:
                pass

        # SQLite: agregar columnas nuevas de forma de pago detalle si faltan
        try:
            if db.engine.dialect.name == "sqlite":
                def _ensure_col(table: str, col: str, ddl: str):
                    try:
                        # SQLite: algunos nombres (p.ej. 'order') requieren quoting.
                        cols = db.session.execute(text(f"PRAGMA table_info('{table}')"))
                        names = {row[1] for row in cols.fetchall()}
                        if col not in names:
                            db.session.execute(text(ddl))
                            db.session.commit()
                    except Exception:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass

                _ensure_col("order", "forma_pago_detalle", "ALTER TABLE 'order' ADD COLUMN forma_pago_detalle VARCHAR(32)")
                _ensure_col("logistics_status", "forma_pago_detalle", "ALTER TABLE logistics_status ADD COLUMN forma_pago_detalle VARCHAR(32)")
                _ensure_col("collection", "forma_pago_detalle", "ALTER TABLE collection ADD COLUMN forma_pago_detalle VARCHAR(32)")
                _ensure_col("order_draft", "forma_pago_detalle", "ALTER TABLE order_draft ADD COLUMN forma_pago_detalle VARCHAR(32)")
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass

    # Register blueprints
    from .views.auth import bp as auth_bp
    app.register_blueprint(auth_bp)
    from .views.main import bp as main_bp
    app.register_blueprint(main_bp)
    from .views.reporte_estado import bp as reporte_estado_bp
    app.register_blueprint(reporte_estado_bp)

    return app


# WSGI entrypoint for Railway
app = create_app()

if __name__ == "__main__":
    debug = (os.getenv("FLASK_DEBUG", "0") == "1")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=debug)
