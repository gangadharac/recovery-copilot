import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings

# SQLite Database Engine
SQLALCHEMY_DATABASE_URL = f"sqlite:///{settings.DB_PATH.as_posix()}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency / generator for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initializes the database tables and performs safe additive column migrations."""
    import app.db.models
    Base.metadata.create_all(bind=engine)
    
    # Safe SQLite additive column migrations for existing tables
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN agent_run_id VARCHAR(64)"))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN iterations_used INTEGER DEFAULT 1"))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN payment_id VARCHAR(64)"))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN order_id VARCHAR(64)"))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN lifecycle_status VARCHAR(32) DEFAULT 'initiated'"))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN agent_run_id VARCHAR(64)"))
            conn.commit()
        except Exception:
            pass

        # Phase 6.1 Additive columns for recovery_attempts
        for col_def in [
            "payment_id VARCHAR(64)",
            "order_id VARCHAR(64)",
            "reference_id VARCHAR(64)",
            "verification_source VARCHAR(32)",
            "verified_amount FLOAT",
            "verified_currency VARCHAR(8)",
        ]:
            try:
                conn.execute(text(f"ALTER TABLE recovery_attempts ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass
