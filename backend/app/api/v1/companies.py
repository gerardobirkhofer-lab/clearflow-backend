"""Companies an owner can open, the sites inside each one, and who may manage them."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import _hash_password
from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_shared_db
from app.core.iban import compact_iban, iban_ok
from app.core.secrets import decrypt_secret, encrypt_secret, mask_secret
from app.core.tenant_access import bind_tenant
from app.models.account_profile import AccountProfile
from app.models.bank_account import BankAccount
from app.models.bank_account_site import BankAccountSite
from app.models.bank_transaction import BankTransaction
from app.models.company_membership import CompanyMembership
from app.models.local_auth_user import LocalAuthUser
from app.models.security_event import SecurityEvent
from app.models.site import Site
from app.models_orm import Tenant, TenantTier

router = APIRouter()

SITE_KINDS = {"public", "online", "lodging", "restaurant", "bar", "chiringuito", "apartments"}
MONEY_SOURCES = {"cards", "cash", "booking", "stripe"}
MIN_PASSWORD_LENGTH = 10


def require_owner(current_user: CurrentUser, tenant_id: uuid.UUID) -> None:
    if current_user.company_roles.get(tenant_id) != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the company owner can do this")


def _as_uuid(value) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _site_out(site: Site) -> dict:
    return {"id": _as_uuid(site.id), "name": site.name, "kind": site.kind}


def _company_out(tenant: Tenant, role: str, sites: list[Site]) -> dict:
    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "timezone": tenant.timezone,
        "currency": tenant.currency,
        "is_active": tenant.is_active,
        "subscription_plan": tenant.subscription_plan,
        "subscription_expires_at": tenant.subscription_expires_at,
        "tier": tenant.tier,
        "created_at": tenant.created_at,
        "updated_at": tenant.updated_at,
        "role": role,
        "sites": [_site_out(site) for site in sites],
    }


async def companies_for_user(db: AsyncSession, current_user: CurrentUser) -> dict:
    roles = {_as_uuid(key): value for key, value in current_user.company_roles.items()}
    if not roles:
        return {"items": []}
    tenant_ids = list(roles)
    tenant_rows = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
    tenants = list(tenant_rows.scalars().all())
    site_rows = await db.execute(select(Site).where(Site.tenant_id.in_(tenant_ids)))
    grouped: dict[uuid.UUID, list[Site]] = {}
    for site in site_rows.scalars().all():
        grouped.setdefault(_as_uuid(site.tenant_id), []).append(site)
    items = [
        _company_out(tenant, roles.get(_as_uuid(tenant.id), "manager"), grouped.get(_as_uuid(tenant.id), []))
        for tenant in tenants
    ]
    items.sort(key=lambda item: (item["name"] or "").lower())
    return {"items": items}


async def create_company(db: AsyncSession, current_user: CurrentUser, name: str, timezone: str, currency: str) -> dict:
    if "owner" not in current_user.company_roles.values():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an owner can add a company")
    label = (name or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="Company name is required")
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        id=tenant_id,
        name=label[:255],
        slug=f"co-{tenant_id.hex[:16]}",
        timezone=timezone or "Europe/Madrid",
        currency=(currency or "EUR")[:3],
        is_active=True,
        subscription_plan="free",
        tier=TenantTier.STARTER,
    )
    db.add(tenant)
    db.add(CompanyMembership(user_id=current_user.id, tenant_id=tenant_id, role="owner"))
    await db.commit()
    await db.refresh(tenant)
    return _company_out(tenant, "owner", [])


@router.get("")
async def list_companies(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await companies_for_user(db, current_user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_company(
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await create_company(
        db,
        current_user,
        data.get("name") or "",
        data.get("timezone") or "Europe/Madrid",
        data.get("currency") or "EUR",
    )


def _sources(raw) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(raw or "[]")
        except json.JSONDecodeError:
            values = []
    cleaned = []
    for value in values:
        key = str(value).strip().lower()
        if key in MONEY_SOURCES and key not in cleaned:
            cleaned.append(key)
    return cleaned


def _validate_guided_setup(data: dict) -> list[dict]:
    companies = data.get("companies")
    if not isinstance(companies, list) or not companies:
        raise HTTPException(status_code=422, detail="Add at least one company")
    seen_places: set[str] = set()
    cleaned = []
    for company in companies:
        name = (company.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Every company needs a name")
        places = []
        for place in company.get("places") or []:
            place_name = (place.get("name") or "").strip()
            kind = (place.get("kind") or "").strip().lower()
            if not place_name:
                raise HTTPException(status_code=422, detail="Every place needs a name")
            if kind not in SITE_KINDS:
                raise HTTPException(status_code=422, detail="Each place needs a general kind of business")
            key = place_name.lower()
            if key in seen_places:
                raise HTTPException(status_code=422, detail=f"Place names must be unique: {place_name}")
            seen_places.add(key)
            places.append({"name": place_name[:255], "kind": kind})
        if not places:
            raise HTTPException(status_code=422, detail=f"{name} needs at least one place")
        place_names = {place["name"] for place in places}
        accounts = []
        covered: set[str] = set()
        for account in company.get("accounts") or []:
            linked = []
            for place_name in account.get("place_names") or []:
                label = str(place_name).strip()
                if label not in place_names:
                    raise HTTPException(status_code=422, detail=f"{label} is not a place of {name}")
                if label in covered:
                    raise HTTPException(status_code=422, detail=f"{label} is already on another account")
                covered.add(label)
                linked.append(label)
            if not linked:
                raise HTTPException(status_code=422, detail="Every account needs at least one place")
            pending = bool(account.get("pending"))
            bank_name = (account.get("bank_name") or "").strip()
            iban = compact_iban(account.get("iban") or "")
            if len(iban) > 42:
                raise HTTPException(status_code=422, detail="IBAN is too long")
            sources = _sources(account.get("sources"))
            if not pending and (not bank_name or not iban):
                raise HTTPException(status_code=422, detail="Each account needs a bank and an IBAN, or mark it as not yet")
            if not pending and not iban_ok(iban):
                raise HTTPException(status_code=422, detail="IBAN does not check out")
            if not pending and not sources:
                raise HTTPException(status_code=422, detail="Say what money arrives in each account")
            accounts.append({
                "bank_name": bank_name[:100],
                "iban": iban,
                "currency": ((account.get("currency") or "EUR").strip() or "EUR")[:3],
                "sources": sources,
                "pending": pending,
                "place_names": linked,
            })
        missing = place_names - covered
        if missing:
            raise HTTPException(status_code=422, detail=f"These places still need an account: {', '.join(sorted(missing))}")
        cleaned.append({"name": name[:255], "places": places, "accounts": accounts})
    return cleaned


async def _save_holding_name(db: AsyncSession, tenant_id: uuid.UUID, holding_name: str) -> None:
    found = await db.execute(select(AccountProfile).where(AccountProfile.tenant_id == tenant_id))
    profile = found.scalar_one_or_none()
    payload = {}
    if profile is not None and profile.payload:
        try:
            payload = json.loads(profile.payload)
        except json.JSONDecodeError:
            payload = {}
    payload["holding_name"] = holding_name
    payload["guided_setup"] = True
    encoded = json.dumps(payload)
    if profile is None:
        db.add(AccountProfile(tenant_id=tenant_id, payload=encoded, onboarding_complete=1))
    else:
        profile.payload = encoded
        profile.onboarding_complete = 1


@router.post("/guided-setup", status_code=status.HTTP_201_CREATED)
async def apply_guided_setup(
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Save the companies, places, and bank accounts from the short setup."""
    if "owner" not in current_user.company_roles.values():
        raise HTTPException(status_code=403, detail="Only an owner can set up the group")
    companies = _validate_guided_setup(data)
    holding_name = (data.get("holding_name") or "").strip()[:255]
    home = await db.get(Tenant, current_user.tenant_id)
    if home is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    require_owner(current_user, current_user.tenant_id)

    created_ids: list[uuid.UUID] = []
    for index, company in enumerate(companies):
        if index == 0:
            home.name = company["name"]
            created_ids.append(home.id)
            continue
        tenant_id = uuid.uuid4()
        db.add(Tenant(
            id=tenant_id,
            name=company["name"],
            slug=f"co-{tenant_id.hex[:16]}",
            timezone="Europe/Madrid",
            currency="EUR",
            is_active=True,
            subscription_plan="free",
            tier=TenantTier.STARTER,
        ))
        db.add(CompanyMembership(user_id=current_user.id, tenant_id=tenant_id, role="owner"))
        created_ids.append(tenant_id)
    await db.flush()

    picture = []
    for tenant_id, company in zip(created_ids, companies):
        sites_by_name = {}
        site_rows = []
        for place in company["places"]:
            site = Site(id=uuid.uuid4(), tenant_id=tenant_id, name=place["name"], kind=place["kind"])
            db.add(site)
            sites_by_name[place["name"]] = site
            site_rows.append(site)
        await db.flush()
        account_rows = []
        for account in company["accounts"]:
            label = account["bank_name"] or "Pendiente"
            plain_iban = account["iban"]
            row = BankAccount(
                tenant_id=tenant_id,
                name=label[:100],
                bank_name=account["bank_name"] or None,
                iban=encrypt_secret(plain_iban) if plain_iban else None,
                currency=account["currency"],
                sources=json.dumps(account["sources"]),
                pending=1 if account["pending"] else 0,
                is_active=1,
            )
            db.add(row)
            await db.flush()
            for place_name in account["place_names"]:
                db.add(BankAccountSite(bank_account_id=row.id, site_id=sites_by_name[place_name].id))
            _remember(db, current_user, tenant_id, "bank_account.saved", row.id, {
                "bank_name": row.bank_name or "",
                "iban": mask_secret(plain_iban),
            })
            account_rows.append({
                "id": row.id,
                "bank_name": row.bank_name or "",
                "iban": mask_secret(plain_iban),
                "currency": row.currency,
                "pending": bool(row.pending),
                "sources": account["sources"],
                "place_names": account["place_names"],
            })
        picture.append({
            "id": str(tenant_id),
            "name": company["name"],
            "sites": [_site_out(site) for site in site_rows],
            "accounts": account_rows,
        })

    await _save_holding_name(db, home.id, holding_name)
    await db.commit()
    return {"holding_name": holding_name, "companies": picture}


def _remember(db: AsyncSession, current_user: CurrentUser, tenant_id: uuid.UUID, action: str, target_id, detail: dict) -> None:
    db.add(SecurityEvent(
        tenant_id=tenant_id,
        user_id=current_user.id,
        action=action,
        target_type="bank_account",
        target_id=str(target_id) if target_id is not None else None,
        detail=json.dumps(detail),
    ))


def _public_iban(stored: str | None) -> str:
    if not stored:
        return ""
    try:
        return mask_secret(decrypt_secret(stored))
    except ValueError:
        return "••••"


async def _bank_accounts_for(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    rows = await db.execute(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.is_active == 1).order_by(BankAccount.id)
    )
    accounts = list(rows.scalars().all())
    if not accounts:
        return []
    ids = [account.id for account in accounts]
    link_rows = await db.execute(select(BankAccountSite).where(BankAccountSite.bank_account_id.in_(ids)))
    links = list(link_rows.scalars().all())
    site_ids = [link.site_id for link in links]
    names: dict = {}
    if site_ids:
        site_rows = await db.execute(select(Site).where(Site.id.in_(site_ids)))
        names = {site.id: site.name for site in site_rows.scalars().all()}
    grouped: dict[int, list[str]] = {}
    for link in links:
        grouped.setdefault(link.bank_account_id, []).append(names.get(link.site_id, ""))
    return [
        {
            "id": account.id,
            "bank_name": account.bank_name or account.name,
            "iban": _public_iban(account.iban or account.account_number),
            "currency": account.currency,
            "pending": bool(account.pending),
            "place_names": [name for name in grouped.get(account.id, []) if name],
        }
        for account in accounts
    ]


@router.get("/{tenant_id}/bank-accounts")
async def list_bank_accounts(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    return {"items": await _bank_accounts_for(db, tenant_id)}


@router.post("/{tenant_id}/bank-accounts", status_code=status.HTTP_201_CREATED)
async def add_bank_account(
    tenant_id: uuid.UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Store one account number for this company. The full number is encrypted."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    bank_name = (data.get("bank_name") or "").strip()
    iban = compact_iban(data.get("iban") or data.get("account_number") or "")
    if not bank_name or not iban:
        raise HTTPException(status_code=422, detail="Bank name and IBAN are required")
    if len(iban) > 42:
        raise HTTPException(status_code=422, detail="IBAN is too long")
    if not iban_ok(iban):
        raise HTTPException(status_code=422, detail="IBAN does not check out")
    currency = ((data.get("currency") or "EUR").strip() or "EUR")[:3]
    row = BankAccount(
        tenant_id=tenant_id,
        name=bank_name[:100],
        bank_name=bank_name[:100],
        iban=encrypt_secret(iban),
        currency=currency,
        pending=0,
        is_active=1,
    )
    db.add(row)
    await db.flush()
    _remember(db, current_user, tenant_id, "bank_account.saved", row.id, {
        "bank_name": bank_name[:100],
        "iban": mask_secret(iban),
    })
    await db.commit()
    return {
        "id": row.id,
        "bank_name": row.bank_name,
        "iban": mask_secret(iban),
        "currency": row.currency,
        "pending": False,
        "place_names": [],
    }


async def _delete_account(db: AsyncSession, tenant_id: uuid.UUID, account: BankAccount) -> int:
    await db.execute(delete(BankAccountSite).where(BankAccountSite.bank_account_id == account.id))
    movements = 0
    if account.bank_name:
        movement_filter = (
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.bank_name == account.bank_name,
        )
        movements = await db.scalar(select(func.count()).select_from(BankTransaction).where(*movement_filter))
        await db.execute(delete(BankTransaction).where(*movement_filter))
    await db.delete(account)
    return int(movements or 0)


@router.delete("/{tenant_id}/bank-accounts/{account_id}")
async def remove_bank_account(
    tenant_id: uuid.UUID,
    account_id: int,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Remove one account number and the imported movements of that bank."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    found = await db.execute(
        select(BankAccount).where(BankAccount.id == account_id, BankAccount.tenant_id == tenant_id)
    )
    account = found.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    masked = _public_iban(account.iban or account.account_number)
    bank_name = account.bank_name or ""
    movements = await _delete_account(db, tenant_id, account)
    _remember(db, current_user, tenant_id, "bank_account.deleted", account_id, {
        "bank_name": bank_name,
        "iban": masked,
        "movements_deleted": movements,
    })
    await db.commit()
    return {"deleted": True, "movements_deleted": movements}


@router.delete("/{tenant_id}/bank-data")
async def remove_company_bank_data(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Remove every stored account number and imported bank movement for this company."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    found = await db.execute(select(BankAccount.id).where(BankAccount.tenant_id == tenant_id))
    account_ids = [row[0] for row in found.all()]
    if account_ids:
        await db.execute(delete(BankAccountSite).where(BankAccountSite.bank_account_id.in_(account_ids)))
        await db.execute(delete(BankAccount).where(BankAccount.tenant_id == tenant_id))
    movements = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(BankTransaction.tenant_id == tenant_id)
    )
    await db.execute(delete(BankTransaction).where(BankTransaction.tenant_id == tenant_id))
    _remember(db, current_user, tenant_id, "bank_data.deleted", None, {
        "accounts_deleted": len(account_ids),
        "movements_deleted": int(movements or 0),
    })
    await db.commit()
    return {"deleted": True, "accounts_deleted": len(account_ids), "movements_deleted": int(movements or 0)}


@router.get("/{tenant_id}/sites")
async def list_sites(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = await db.execute(select(Site).where(Site.tenant_id == tenant_id).order_by(Site.name))
    return {"items": [_site_out(site) for site in rows.scalars().all()]}


@router.post("/{tenant_id}/sites", status_code=status.HTTP_201_CREATED)
async def add_site(
    tenant_id: uuid.UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    name = (data.get("name") or "").strip()
    kind = (data.get("kind") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="Site name is required")
    if kind not in SITE_KINDS:
        raise HTTPException(status_code=422, detail="Site kind must be a general kind of business")
    site = Site(id=uuid.uuid4(), tenant_id=tenant_id, name=name[:255], kind=kind)
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return _site_out(site)


@router.post("/{tenant_id}/members", status_code=status.HTTP_201_CREATED)
async def add_manager(
    tenant_id: uuid.UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Add a manager who can open this company and no other."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")

    found = await db.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
    user = found.scalar_one_or_none()
    if user is None:
        password = data.get("password") or ""
        full_name = (data.get("name") or data.get("full_name") or "").strip()
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=422, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if not full_name:
            raise HTTPException(status_code=422, detail="Name is required for a new manager")
        user = LocalAuthUser(
            email=email,
            password_hash=_hash_password(password),
            name=full_name[:100],
            role="manager",
            is_active=1,
            tenant_id=tenant_id,
        )
        db.add(user)
        await db.flush()

    existing = await db.execute(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.tenant_id == tenant_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(CompanyMembership(user_id=user.id, tenant_id=tenant_id, role="manager"))
    await db.commit()
    return {"email": user.email, "name": user.name, "role": "manager", "tenant_id": str(tenant_id)}
