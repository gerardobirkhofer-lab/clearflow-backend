"""
ClearFlow matching engine — trust-first auto-confirm with 1:1 and 1:N
+ contractual audit gate (bank must match contract-recalculated net).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from app.services.contractual_audit import contractual_block_reason

ENGINE = "clearflow_v5_contract_audit"
AUTO_CONFIRM_MAX_DAY_DIFF = 3
SUGGEST_MAX_AMOUNT_DIFF = 1.0


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


def compute_score(
    *,
    amount_diff: float | None,
    day_diff_val: int | None,
    ref_ok: bool,
    settle_ok: bool,
    contract_pass: bool,
    suggested: bool = False,
) -> int:
    """0–100 confidence for evidence / trial-report (not a black-box ML score)."""
    score = 40
    if ref_ok:
        score += 20
    if settle_ok:
        score += 15
    if amount_diff is not None:
        if amount_diff < 0.01:
            score += 25
        elif amount_diff <= SUGGEST_MAX_AMOUNT_DIFF:
            score += 10
        else:
            score -= 20
    if day_diff_val is not None:
        if day_diff_val <= 1:
            score += 10
        elif day_diff_val <= AUTO_CONFIRM_MAX_DAY_DIFF:
            score += 5
        else:
            score -= 15
    if contract_pass:
        score += 10
    if suggested:
        score = min(score, 79)
    return max(0, min(100, score))


def amount_candidates(
    p_amount, p_gross, p_fee, p_net, contract_expected_net=None
) -> list[tuple[float, str]]:
    """Contract expected net is preferred when present (independent recalc)."""
    out: list[tuple[float, str]] = []
    if contract_expected_net is not None:
        out.append((float(contract_expected_net), "contract_expected_net"))
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


def leg_amount(p_amount, p_gross, p_fee, p_net, contract_expected_net=None) -> tuple[float, str]:
    if contract_expected_net is not None:
        return float(contract_expected_net), "contract_expected_net"
    cands = amount_candidates(p_amount, p_gross, p_fee, p_net, contract_expected_net)
    if cands:
        return cands[0]
    return (float(p_amount or 0), "provider_amount_fallback")


def _unpack_leg(p: tuple):
    """Support legacy 9-tuple and extended 12-tuple with contract fields."""
    if len(p) >= 12:
        return p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11]
    # pad
    base = list(p) + [None] * (12 - len(p))
    return tuple(base[:12])


def evaluate_auto_confirm_1to1(
    b_amount, b_date, b_ref, b_conc,
    p_amount, p_date, p_ref, p_conc,
    p_gross=None, p_fee=None, p_net=None,
    b_settlement_id=None, p_settlement_id=None,
    contract_expected_net=None,
    gateway_reported_net=None,
    contract_audit_status=None,
) -> tuple[str, dict]:
    """
    Returns (decision, evidence) where decision is confirmed | suggested | reject.
    """
    evidence: dict[str, Any] = {
        "engine": ENGINE,
        "match_mode": "1:1",
        "rules_fired": [],
        "bank_amount": b_amount,
        "provider_amount": p_amount,
        "provider_gross": p_gross,
        "provider_fee": p_fee,
        "provider_net": p_net,
        "contract_expected_net": contract_expected_net,
        "gateway_reported_net": gateway_reported_net,
        "contract_audit_status": contract_audit_status,
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
        evidence["decision"] = "reject"
        evidence["score"] = 0
        return "reject", evidence
    if ref_ok:
        evidence["rules_fired"].append("exact_reference")
    if settle_ok:
        evidence["rules_fired"].append("exact_settlement_id")

    if b_amount is None:
        evidence["reject_reason"] = "missing_bank_amount"
        evidence["decision"] = "reject"
        evidence["score"] = 0
        return "reject", evidence

    # Contractual gate: block if bank aligns with gateway but not with contract
    block = contractual_block_reason(
        b_amount, contract_expected_net, gateway_reported_net, contract_audit_status
    )
    if block == "bank_matches_gateway_but_not_contract":
        evidence["reject_reason"] = block
        evidence["decision"] = "blocked_contractual_discrepancy"
        evidence["rules_fired"].append("contractual_audit_block")
        evidence["score"] = 5
        return "reject", evidence

    amount_ok = False
    best_near: tuple[float, str, float] | None = None  # diff, label, cand
    ordered = amount_candidates(
        p_amount, p_gross, p_fee, p_net, contract_expected_net=contract_expected_net
    )
    if contract_expected_net is not None:
        ordered = [
            (float(contract_expected_net), "contract_expected_net"),
        ] + [x for x in ordered if x[1] != "contract_expected_net"]
        ordered = [x for x in ordered if x[1] == "contract_expected_net"]

    for cand, label in ordered:
        diff = abs(float(b_amount) - cand)
        if diff < 0.01:
            evidence["amount_diff"] = diff
            evidence["amount_rule"] = label
            evidence["rules_fired"].append(f"exact_amount:{label}")
            amount_ok = True
            break
        if best_near is None or diff < best_near[0]:
            best_near = (diff, label, cand)

    days = day_diff(b_date, p_date)
    evidence["day_diff"] = days

    if not amount_ok:
        # Near-miss suggest (only when NO contract gate — contract must be exact)
        if (
            contract_expected_net is None
            and best_near is not None
            and best_near[0] <= SUGGEST_MAX_AMOUNT_DIFF
        ):
            if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
                evidence["reject_reason"] = "dates_too_far"
                evidence["decision"] = "reject"
                evidence["amount_diff"] = best_near[0]
                evidence["score"] = compute_score(
                    amount_diff=best_near[0],
                    day_diff_val=days,
                    ref_ok=ref_ok,
                    settle_ok=settle_ok,
                    contract_pass=False,
                )
                return "reject", evidence
            evidence["amount_diff"] = best_near[0]
            evidence["amount_rule"] = best_near[1]
            evidence["rules_fired"].append("near_amount_suggested_not_confirmed")
            if days is not None:
                evidence["rules_fired"].append("date_within_window")
            evidence["decision"] = "suggested"
            evidence["suggested_by"] = "system_auto"
            evidence["suggested_at"] = datetime.utcnow().isoformat() + "Z"
            evidence["score"] = compute_score(
                amount_diff=best_near[0],
                day_diff_val=days,
                ref_ok=ref_ok,
                settle_ok=settle_ok,
                contract_pass=False,
                suggested=True,
            )
            return "suggested", evidence

        if contract_expected_net is not None:
            evidence["reject_reason"] = "bank_differs_from_contract_expected_net"
            evidence["decision"] = "blocked_contractual_discrepancy"
            evidence["amount_diff"] = abs(
                float(b_amount) - float(contract_expected_net)
            )
            evidence["rules_fired"].append("contractual_audit_block")
            evidence["score"] = 10
        else:
            evidence["reject_reason"] = "amount_not_exact_vs_amount_or_net_or_gross_minus_fee"
            evidence["decision"] = "reject"
            evidence["amount_diff"] = best_near[0] if best_near else None
            evidence["score"] = compute_score(
                amount_diff=best_near[0] if best_near else None,
                day_diff_val=days,
                ref_ok=ref_ok,
                settle_ok=settle_ok,
                contract_pass=False,
            )
        return "reject", evidence

    if contract_expected_net is not None:
        evidence["rules_fired"].append("contractual_audit_pass")

    if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
        evidence["reject_reason"] = "dates_too_far"
        evidence["decision"] = "reject"
        evidence["score"] = compute_score(
            amount_diff=evidence.get("amount_diff"),
            day_diff_val=days,
            ref_ok=ref_ok,
            settle_ok=settle_ok,
            contract_pass=contract_expected_net is not None,
        )
        return "reject", evidence
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
    evidence["score"] = compute_score(
        amount_diff=float(evidence.get("amount_diff") or 0),
        day_diff_val=days,
        ref_ok=ref_ok,
        settle_ok=settle_ok,
        contract_pass=contract_expected_net is not None,
    )
    return "confirmed", evidence


def evaluate_auto_confirm_1ton(
    b_amount, b_date, b_ref, b_settle,
    legs: list[tuple],
) -> tuple[str, dict]:
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
        evidence["score"] = 0
        return "reject", evidence
    if len(legs) < 2:
        evidence["reject_reason"] = "one_n_needs_at_least_two_legs"
        evidence["score"] = 0
        return "reject", evidence

    group_key = norm_ref(b_settle) or norm_ref(b_ref)
    if not group_key:
        evidence["reject_reason"] = "missing_settlement_or_ref_for_1n"
        evidence["score"] = 0
        return "reject", evidence

    coherent = []
    for p in legs:
        pid, p_amount, p_date, p_ref, p_conc, p_gross, p_fee, p_net, p_settle, c_net, g_net, c_status = _unpack_leg(p)
        if norm_ref(p_settle) == group_key or norm_ref(p_ref) == group_key:
            coherent.append(p)
    if len(coherent) < 2:
        evidence["reject_reason"] = "legs_not_coherent_on_settlement"
        evidence["score"] = 0
        return "reject", evidence

    far = 0
    for p in coherent:
        days = day_diff(b_date, p[2])
        if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
            far += 1
    if far:
        evidence["reject_reason"] = "dates_too_far_for_some_legs"
        evidence["legs_out_of_window"] = far
        evidence["score"] = 15
        return "reject", evidence
    evidence["rules_fired"].append("settlement_group")
    evidence["rules_fired"].append("date_within_window_all_legs")

    use_contract = all(
        _unpack_leg(p)[9] is not None for p in coherent
    )
    total = 0.0
    rule_labels: list[str] = []
    leg_details = []
    contractual_blocks = []
    for p in coherent:
        pid, p_amount, p_date, p_ref, p_conc, p_gross, p_fee, p_net, p_settle, c_net, g_net, c_status = _unpack_leg(p)
        if c_status == "gateway_deviation":
            contractual_blocks.append(pid)
        if use_contract:
            amt, label = float(c_net), "contract_expected_net"
        else:
            amt, label = leg_amount(p_amount, p_gross, p_fee, p_net, c_net)
        total += amt
        rule_labels.append(label)
        leg_details.append({
            "provider_tx_id": pid,
            "leg_amount": amt,
            "leg_rule": label,
            "contract_expected_net": c_net,
            "gateway_reported_net": g_net,
            "contract_audit_status": c_status,
            "reference": p_ref,
            "settlement_id": p_settle,
        })

    evidence["legs"] = leg_details
    evidence["legs_sum"] = round(total, 2)
    evidence["provider_tx_ids"] = [d["provider_tx_id"] for d in leg_details]
    evidence["amount_rule"] = "sum_contract_expected_net" if use_contract else (
        "sum_" + ("provider_net" if all(x == "provider_net" for x in rule_labels) else "mixed_legs")
    )
    if contractual_blocks:
        evidence["legs_with_gateway_deviation"] = contractual_blocks
        evidence["rules_fired"].append("gateway_deviation_on_legs_using_contract_recalc")

    diff = abs(float(b_amount) - total)
    evidence["amount_diff"] = diff
    days = day_diff(b_date, coherent[0][2]) if coherent else None

    if use_contract and diff >= 0.01:
        gateway_sum = 0.0
        has_gw = True
        for p in coherent:
            _pid, _a, _d, _r, _c2, _g, _f, p_net, _s, _cnet, g_net, _st = _unpack_leg(p)
            if g_net is None and p_net is None:
                has_gw = False
                break
            gateway_sum += float(g_net if g_net is not None else p_net)
        if has_gw and abs(float(b_amount) - gateway_sum) < 0.01:
            evidence["reject_reason"] = "bank_matches_gateway_sum_but_not_contract_sum"
            evidence["decision"] = "blocked_contractual_discrepancy"
            evidence["gateway_sum"] = round(gateway_sum, 2)
            evidence["contract_sum"] = round(total, 2)
            evidence["score"] = 8
            return "reject", evidence

    if diff < 0.01:
        evidence["rules_fired"].append("exact_sum_equals_bank")
        if use_contract:
            evidence["rules_fired"].append("contractual_audit_pass_1n")
        evidence["decision"] = "confirmed"
        evidence["confirmed_by"] = "system_auto"
        evidence["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
        evidence["score"] = compute_score(
            amount_diff=diff,
            day_diff_val=days,
            ref_ok=True,
            settle_ok=True,
            contract_pass=use_contract,
        )
        return "confirmed", evidence

    if diff <= SUGGEST_MAX_AMOUNT_DIFF and not use_contract:
        evidence["rules_fired"].append("near_sum_suggested_not_confirmed")
        evidence["decision"] = "suggested"
        evidence["suggested_by"] = "system_auto"
        evidence["suggested_at"] = datetime.utcnow().isoformat() + "Z"
        evidence["score"] = compute_score(
            amount_diff=diff,
            day_diff_val=days,
            ref_ok=True,
            settle_ok=True,
            contract_pass=False,
            suggested=True,
        )
        return "suggested", evidence

    if use_contract:
        evidence["reject_reason"] = "bank_differs_from_contract_expected_sum"
        evidence["decision"] = "blocked_contractual_discrepancy"
        evidence["contract_sum"] = round(total, 2)
        evidence["rules_fired"].append("contractual_audit_block_1n")
        evidence["score"] = 12
    else:
        evidence["reject_reason"] = "sum_not_close_to_bank"
        evidence["score"] = compute_score(
            amount_diff=diff,
            day_diff_val=days,
            ref_ok=True,
            settle_ok=True,
            contract_pass=False,
        )
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
