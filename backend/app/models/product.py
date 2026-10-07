"""What a place sells, with the price the customer pays and the cost to make it."""
from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    site_id = Column(UUID, nullable=True, index=True)
    name = Column(String(80), nullable=False)
    sale_price = Column(Float, nullable=False)
    cost = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
