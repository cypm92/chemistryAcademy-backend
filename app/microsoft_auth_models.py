"""Storage models for Microsoft OAuth state and credentials."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class MicrosoftOAuthState(Base):
    __tablename__ = 'microsoft_oauth_states'

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_id: Mapped[int] = mapped_column(Integer, nullable=False)
    verifier_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)


class MicrosoftOAuthGeneration(Base):
    __tablename__ = 'microsoft_oauth_generation'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    account_object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MicrosoftCredential(Base):
    __tablename__ = 'microsoft_credentials'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refresh_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    needs_reconnect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refresh_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    refresh_lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    refresh_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
