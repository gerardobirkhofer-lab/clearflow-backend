"""
Authentication endpoints — demo mode with no DB dependency.
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timedelta, timezone
import jwt
import os

router = APIRouter()
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
security = HTTPBearer()

# Demo token (always works)
DEMO_PAYLOAD = {
    "user_id": 1,
    "email": "demo@clearflow.local",
    "exp": datetime.now(timezone.utc) + timedelta(days=7)
}
DEMO_TOKEN = jwt.encode(DEMO_PAYLOAD, SECRET_KEY, algorithm=ALGORITHM)
DEMO_USER = {"id": 1, "email": "demo@clearflow.local", "name": "Demo User", "role": "self_owner"}

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
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
    return DEMO_USER

@router.post("/register", status_code=201)
async def register(data: dict):
    return {"token": DEMO_TOKEN, "user": DEMO_USER}

@router.post("/login")
async def login(data: dict):
    return {"token": DEMO_TOKEN, "user": DEMO_USER}
