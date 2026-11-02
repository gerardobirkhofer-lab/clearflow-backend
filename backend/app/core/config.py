from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Clearflow"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://user:password@db:5432/dbname"
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_NAME: str = "clearflow"
    # When set, ClearFlow-hosted vault uses durable MinIO/S3 instead of ephemeral local disk
    CLEARFLOW_VAULT_S3_ENDPOINT: str = ""
    CLEARFLOW_VAULT_S3_BUCKET: str = ""
    CLEARFLOW_VAULT_S3_ACCESS_KEY: str = ""
    CLEARFLOW_VAULT_S3_SECRET_KEY: str = ""
    CLEARFLOW_VAULT_S3_REGION: str = "eu-west-1"
    MAILPIT_HOST: str = "mailpit"
    MAILPIT_SMTP_PORT: int = 1025
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_SENDER: str = "noreply@clearview.app"
    # Comma-separated; never default to "*" in production loaders
    CORS_ORIGINS: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
