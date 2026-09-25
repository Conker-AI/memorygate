from app.core.config import DATABASE_URL
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass


def database_health() -> dict:
    """Coarse reachability probe for the source of truth. Never raises.

    The reason stays generic on purpose: `/health` is unauthenticated, and a
    driver error string would leak the host, port and database name.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return {"status": "unavailable", "reason": "database unreachable"}
