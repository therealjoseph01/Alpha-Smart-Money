"""Portable column types, so the same schema runs on SQLite and PostgreSQL.

SQLite is the default: one file, no server, and a deployment that is just "copy the
directory". PostgreSQL stays supported for when write volume outgrows a single writer.

Three things need care, and getting any of them wrong is a real bug:

**Money.** SQLite has no DECIMAL type, and SQLAlchemy's Numeric round-trips through
*float* there. 0.1 + 0.2 != 0.3 is not acceptable for balances, so Decimal is stored as
TEXT and parsed back exactly. On PostgreSQL the native NUMERIC is used.

**UUIDs.** Stored as 36-char text on SQLite, native UUID on PostgreSQL, and returned as
`uuid.UUID` either way so application code never has to care.

**Lists and objects.** JSONB on PostgreSQL, JSON text on SQLite.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CHAR, JSON, Numeric, String, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


class Money(TypeDecorator):
    """Exact decimal on any backend.

    On SQLite the value is TEXT, which sorts lexicographically rather than numerically -
    fine, because nothing orders by a money column; ordering is by timestamps and scores.
    """

    impl = Text
    cache_ok = True

    def __init__(self, precision: int = 38, scale: int = 12):
        self.precision = precision
        self.scale = scale
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(self.precision, self.scale))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return Decimal(str(value))
        return format(Decimal(str(value)), "f")     # never scientific notation

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class GUID(TypeDecorator):
    """uuid.UUID on both backends."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class JSONDict(TypeDecorator):
    """JSONB on PostgreSQL, JSON on SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class StringList(TypeDecorator):
    """A list of strings: native ARRAY on PostgreSQL, JSON list on SQLite.

    Reason codes are stored here. On PostgreSQL they can be queried with unnest(); on
    SQLite the aggregation happens in Python, which is fine at these row counts.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Text()))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        return list(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return list(value) if value else []


class IntList(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy import Integer
            return dialect.type_descriptor(ARRAY(Integer()))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        return list(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return list(value) if value else []


class RawAmount(Money):
    """Token amounts in raw integer units. Exact on both backends."""

    def __init__(self):
        super().__init__(precision=78, scale=0)


__all__ = ["GUID", "IntList", "JSONDict", "Money", "RawAmount", "String", "StringList"]
