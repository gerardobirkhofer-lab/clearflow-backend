"""Which places use a bank account. An account stays inside one company."""
from sqlalchemy import Column, Integer, UniqueConstraint

from app.core.database import Base
from app.core.uuid_type import UUID


class BankAccountSite(Base):
    __tablename__ = "bank_account_sites"
    __table_args__ = (
        UniqueConstraint("bank_account_id", "site_id", name="uq_bank_account_site"),
    )

    id = Column(Integer, primary_key=True, index=True)
    bank_account_id = Column(Integer, nullable=False, index=True)
    site_id = Column(UUID, nullable=False, index=True)
