from app.core.db import Base
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column


class AuthSetting(Base):
    __tablename__ = "auth_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    admin_key_hash: Mapped[str] = mapped_column(Text, default="")
