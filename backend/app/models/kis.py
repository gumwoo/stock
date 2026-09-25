"""Credentials KIS issues to us, kept so they are reused rather than re-issued.

KIS allows one access token a minute and asks that a day's token be reused;
a process that asked for a fresh one on every start would soon be refused. So
the issued token and the WebSocket approval key are stored with their expiry
and handed out again until they lapse.

What is stored is only what KIS issued. The app key and secret stay in the
environment and are never copied here, and no value in this table is ever
logged or put in an error message: `__repr__` leaves it out on purpose.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class KisCredential(Base):
    """One issued access token or WebSocket approval key."""

    __tablename__ = "kis_credential"

    id: Mapped[BigIntPk]
    env: Mapped[str] = mapped_column(String(8), nullable=False, doc="real or mock.")
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="ACCESS_TOKEN or APPROVAL_KEY."
    )
    value: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_kis_credential_lookup", "env", "kind", "issued_at"),)

    def __repr__(self) -> str:
        return f"<KisCredential {self.env} {self.kind} expires={self.expires_at}>"
