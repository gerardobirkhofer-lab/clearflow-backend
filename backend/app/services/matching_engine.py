"""
ClearFlow matching engine — trust-first auto-confirm with 1:1 and 1:N.

1:1  bank credit ↔ one provider row (exact ref/settlement + amount/net/gross−fee)
1:N  bank credit ↔ many provider rows sharing settlement_id (or bank ref = settlement),
     when sum(legs) equals bank amount within €0.01

Suggested (not confirmed): same 1:N group when |sum − bank| < €1 but not exact.
Never auto-confirm on concept/fuzzy alone.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

ENGINE = "clearflow_v4_1n"
AUTO_CONFIRM_MAX_DAY_DIFF = 3
SUGGEST_MAX_AMOUNT_DIFF = 1.0  # euros — suggested only, never confirmed


def norm_ref(ref: str | None) -> str:
    if not ref:
        return ""
    return str(ref).strip().upper()


def day_diff(a, b) -> int | None:
    if not a or not b:
        return None
    try:
        da = a.date() if hasattr(a, "date") else a
        db_ = b.date() if hasattr(b, "date") else b
        return abs((da - db_).days)
    except Exception:
        return None


def amount_candidates(p_amount, p_gross, p_fee, p_net) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    if p_net is not None:
        out.append((float(p_net), "provider_net"))
    if p_amount is not None:
        out.append((float(p_amount), "provider_amount"))
    if p_gross is not None and p_fee is not None:
        out.append((float(p_gross) - float(p_fee), "provider_gross_minus_fee"))
    seen: set[float] = set()
    uniq: list[tuple[float, str]] = []
    for val, label in out:
        key = round(val, 2)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((val, label))
    return uniq


def leg_amount(p_amount, p_gross, p_fee, p_net) -> tuple[float, str]:
    """Preferred single-leg value for summing in 1:N."""
    cands = amount_candidates(p_amount, p_gross, p_fee, p_net)
    if cands:
        return cands[0]
    return (float(p_amount or 0), "provider_amount_fallback")


def evaluate_auto_confirm_1to1(
    b_amount, b_date, b_ref, b_conc,
    p_amount, p_date, p_ref, p_conc,
    p_gross=None, p_fee=None, p_net=None,
    b_settlement_id=None, p_settlement_id=None,
) -> tuple[bool, dict]:
    evidence: dict[str, Any] = {
        "engine": ENGINE,
        "match_mode": "1:1",
        "rules_fired": [],
        "bank_amount": b_amount,
        "provider_amount": p_amount,
        "provider_gross": p_gross,
        "provider_fee": p_fee,
        "provider_net": p_net,
        "bank_ref": b_ref,
        "provider_ref": p_ref,
        "bank_date": b_date.isoformat() if b_date else None,
        "provider_date": p_date.isoformat() if p_date else None,
        "settlement_id": p_settlement_id or b_settlement_id,
    }

    n_bank = norm_ref(b_ref)
    n_prov = norm_ref(p_ref)
    n_settle_b = norm_ref(b_settlement_id)
    n_settle_p = norm_ref(p_settlement_id)

    ref_ok = bool(n_bank and n_prov and n_bank == n_prov)
    settle_ok = bool(n_settle_b and n_settle_p and n_settle_b == n_settle_p)
    if not ref_ok and not settle_ok:
        evidence["reject_reason"] = "missing_or_mismatched_reference"
        return False, evidence
    if ref_ok:
        evidence["rules_fired"].append("exact_reference")
    if settle_ok:
        evidence["rules_fired"].append("exact_settlement_id")

    if b_amount is None:
        evidence["reject_reason"] = "missing_bank_amount"
        return False, evidence

    amount_ok = False
    for cand, label in amount_candidates(p_amount, p_gross, p_fee, p_net):
        diff = abs(float(b_amount) - cand)
        if diff < 0.01:
            evidence["amount_diff"] = diff
            evidence["amount_rule"] = label
            evidence["rules_fired"].append(f"exact_amount:{label}")
            amount_ok = True
            break
    if not amount_ok:
        evidence["reject_reason"] = "amount_not_exact_vs_amount_or_net_or_gross_minus_fee"
        return False, evidence

    days = day_diff(b_date, p_date)
    evidence["day_diff"] = days
    if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
        evidence["reject_reason"] = "dates_too_far"
        return False, evidence
    if days is not None:
        evidence["rules_fired"].append("date_within_window")
    else:
        evidence["rules_fired"].append("date_unavailable_ok_with_ref_amount")

    if b_conc and p_conc:
        bc, pc = str(b_conc).lower(), str(p_conc).lower()
        if bc and pc and (bc in pc or pc in bc):
            evidence["rules_fired"].append("concept_overlap_info")

    evidence["decision"] = "confirmed"
    evidence["confirmed_by"] = "system_auto"
    evidence["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
    return True, evidence


def evaluate_auto_confirm_1ton(
    b_amount, b_date, b_ref, b_settle,
    legs: list[tuple],  # provider row tuples as in execute_reconciliation
) -> tuple[str, dict]:
    """
    Returns (decision, evidence) where decision is:
      confirmed | suggested | reject
    legs: (pid, amount, date, ref, conc, gross, fee, net, settle_id)
    """
    evidence: dict[str, Any] = {
        "engine": ENGINE,
        "match_mode": "1:N",
        "rules_fired": [],
        "bank_amount": b_amount,
        "bank_ref": b_ref,
        "bank_date": b_date.isoformat() if b_date else None,
        "settlement_id": b_settle,
        "provider_count": len(legs),
    }
    if b_amount is None:
        evidence["reject_reason"] = "missing_bank_amount"
        return "reject", evidence
    if len(legs) < 2:
        evidence["reject_reason"] = "one_n_needs_at_least_two_legs"
        return "reject", evidence

    group_key = norm_ref(b_settle) or norm_ref(b_ref)
    if not group_key:
        evidence["reject_reason"] = "missing_settlement_or_ref_for_1n"
        return "reject", evidence

    # All legs must share the group key via settlement_id or reference
    coherent = []
    for p in legs:
        pid, p_amount, p_date, p_ref, p_conc, p_gross, p_fee, p_net, p_settle = p
        if norm_ref(p_settle) == group_key or norm_ref(p_ref) == group_key:
            coherent.append(p)
    if len(coherent) < 2:
        evidence["reject_reason"] = "legs_not_coherent_on_settlement"
        return "reject", evidence

    # Date window: every leg with a date must be within window of bank
    far = 0
    for p in coherent:
        days = day_diff(b_date, p[2])
        if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
            far += 1
    if far:
        evidence["reject_reason"] = "dates_too_far_for_some_legs"
        evidence["legs_out_of_window"] = far
        return "reject", evidence
    evidence["rules_fired"].append("settlement_group")
    evidence["rules_fired"].append("date_within_window_all_legs")

    total = 0.0
    rule_labels: list[str] = []
    leg_details = []
    for p in coherent:
        pid, p_amount, p_date, p_ref, p_conc, p_gross, p_fee, p_net, p_settle = p
        amt, label = leg_amount(p_amount, p_gross, p_fee, p_net)
        total += amt
        rule_labels.append(label)
        leg_details.append({
            "provider_tx_id": pid,
            "leg_amount": amt,
            "leg_rule": label,
            "reference": p_ref,
            "settlement_id": p_settle,
        })

    evidence["legs"] = leg_details
    evidence["legs_sum"] = round(total, 2)
    evidence["provider_tx_ids"] = [d["provider_tx_id"] for d in leg_details]
    evidence["amount_rule"] = "sum_" + (
        "provider_net" if all(x == "provider_net" for x in rule_labels) else "mixed_legs"
    )
    diff = abs(float(b_amount) - total)
    evidence["amount_diff"] = diff

    if diff < 0.01:
        evidence["rules_fired"].append("exact_sum_equals_bank")
        evidence["decision"] = "confirmed"
        evidence["confirmed_by"] = "system_auto"
        evidence["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
        return "confirmed", evidence

    if diff <= SUGGEST_MAX_AMOUNT_DIFF:
        evidence["rules_fired"].append("near_sum_suggested_not_confirmed")
        evidence["decision"] = "suggested"
        evidence["suggested_by"] = "system_auto"
        evidence["suggested_at"] = datetime.utcnow().isoformat() + "Z"
        return "suggested", evidence

    evidence["reject_reason"] = "sum_not_close_to_bank"
    return "reject", evidence


def index_providers(prov_rows: list) -> tuple[dict[str, list], dict[str, list]]:
    prov_by_ref: dict[str, list] = defaultdict(list)
    prov_by_settle: dict[str, list] = defaultdict(list)
    for p in prov_rows:
        nref = norm_ref(p[3])
        if nref:
            prov_by_ref[nref].append(p)
        nsettle = norm_ref(p[8])
        if nsettle:
            prov_by_settle[nsettle].append(p)
    return prov_by_ref, prov_by_settle
