
import asyncio, uuid, random
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from app.core.database import engine
from app.models_orm import Institution, CardCollection, ReconciliationResult, Tenant, TransactionType, ReconciliationStatus

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def seed():
    async with SessionLocal() as session:
        t = (await session.execute(select(Tenant))).scalars().first()
        if not t:
            print("No tenant found. Run app setup first."); return
        tid = t.id
        insts = []
        for d in [{"name":"BBVA","code":"BBVA","country":"ES"},{"name":"Santander","code":"SANTANDER","country":"ES"},{"name":"Deutsche Bank","code":"DB","country":"DE"},{"name":"ING","code":"ING","country":"NL"},{"name":"CaixaBank","code":"CAIXA","country":"ES"}]:
            r = await session.execute(select(Institution).where(Institution.code==d["code"], Institution.tenant_id==tid))
            i = r.scalar_one_or_none()
            if not i:
                i = Institution(tenant_id=tid, id=uuid.uuid4(), name=d["name"], code=d["code"], country=d["country"], institution_type="bank", is_active=True, bank_connection_status="connected")
                session.add(i); await session.commit(); await session.refresh(i)
            insts.append(i)
        base = date(2026,8,4)
        statuses = list(ReconciliationStatus)
        weights = [50,20,10,12,8]
        for idx in range(25):
            days = random.randint(0,30)
            d = base - timedelta(days=days)
            inst = random.choice(insts)
            amt = Decimal(str(round(random.uniform(50,2000),2)))
            st = random.choices(statuses, weights=weights)[0]
            ct = random.choice([TransactionType.DEBIT, TransactionType.CREDIT])
            coll = CardCollection(
                id=uuid.uuid4(), tenant_id=tid, collection_date=d, institution_id=inst.id,
                reference=f"BATCH-{idx+1:03d}-{inst.code}-{d.year}{d.month:02d}{d.day:02d}",
                amount_gross=amt, amount_net=amt*Decimal("0.99"), card_type=ct,
                terminal_id=f"TERM-{random.randint(100,999)}", batch_number=f"BATCH-{idx+1:03d}",
                transaction_count=random.randint(1,15), status=st,
                description=f"{ct.value} from {inst.name}"
            )
            session.add(coll); await session.commit(); await session.refresh(coll)
            hd = st in (ReconciliationStatus.DISCREPANCY, ReconciliationStatus.PARTIAL)
            ba = amt - Decimal(str(round(random.uniform(0,50),2))) if hd else amt
            ta = amt if not hd else amt - Decimal(str(round(random.uniform(0,20),2)))
            recon = ReconciliationResult(
                id=uuid.uuid4(), tenant_id=tid, collection_id=coll.id, collection_date=d,
                status=st, gross_amount=amt, bank_amount=ba, tpv_amount=ta,
                match_confidence="high" if st==ReconciliationStatus.MATCHED else "medium" if st==ReconciliationStatus.PARTIAL else "low",
                resolved=st==ReconciliationStatus.MATCHED
            )
            session.add(recon)
        await session.commit()
        print(f"Seeded {len(insts)} institutions, 25 collections, 25 reconciliation results")

asyncio.run(seed())
