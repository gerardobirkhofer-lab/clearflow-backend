from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone
import jwt
import hashlib
import secrets
import os

from app.core.database import get_shared_db
from app.models.user import User

router = APIRouter()
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
security = HTTPBearer()

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pwdhash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt + pwdhash.hex()

def verify_password(plain: str, hashed: str) -> bool:
    salt = hashed[:32]
    stored_hash = hashed[32:]
    pwdhash = hashlib.pbkdf2_hmac('sha256', plain.encode(), salt.encode(), 100000)
    return pwdhash.hex() == stored_hash

def create_token(user_id: int, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_shared_db)
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

@router.post("/register", status_code=201)
async def register(data: dict, db: AsyncSession = Depends(get_shared_db)):
    result = await db.execute(select(User).where(User.email == data.get("email")))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user = User(
        email=data.get("email"),
        password_hash=hash_password(data.get("password")),
        name=data.get("name"),
        role=data.get("role", "self_owner"),
        is_active=1,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    token = create_token(user.id, user.email)
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "name": user.name, "role": user.role}
    }

@router.post("/login")
async def login(data: dict, db: AsyncSession = Depends(get_shared_db)):
    result = await db.execute(select(User).where(User.email == data.get("email")))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.get("password"), user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = create_token(user.id, user.email)
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "name": user.name, "role": user.role}
    }
