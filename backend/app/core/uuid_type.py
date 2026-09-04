"""Cross-database UUID type that works with PostgreSQL and SQLite."""
from __future__ import annotations

import uuid
from sqlalchemy import TypeDecorator, String


class UUID(TypeDecorator):
    """Platform-independent UUID type.
    Uses PostgreSQL UUID when available, falls back to CHAR(36) for SQLite."""
    impl = String(36)

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import UUID as PGUUID
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        else:
            return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)
