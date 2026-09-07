#!/usr/bin/env python3
"""
One-time migration: add password_hash column to users table.
Run this manually against the production database before deploying auth-v2.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.core.database import shared_engine


async def migrate():
    async with shared_engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)"
        ))
        print("✅ password_hash column added (or already exists)")


if __name__ == "__main__":
    asyncio.run(migrate())
