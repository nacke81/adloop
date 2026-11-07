"""Additive campaign geo-target add and positive geo_target_type update.

Both operations are draft/apply pairs. The draft runs a real validate_only
mutate against Google and stores a plan; nothing is changed until the plan is
applied with confirm_and_apply. The add path only ever builds CREATE
operations: no campaign criterion is removed or updated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

GEO_TYPES = ("PRESENCE", "PRESENCE_OR_INTEREST")


def _send(client, cid: str, ops: list, validate_only: bool):
    svc = client.get_service("GoogleAdsService")
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(ops)
    request.validate_only = bool(validate_only)
    return svc.mutate(request=request)


def _tail_id(value) -> str:
    return str(value).strip().rsplit("/", 1)[-1]


def _existing_location_targets(config, customer_id: str, campaign_id: str) -> dict:
    """Return {geo_constant_id: negative_bool} for LOCATION criteria on a campaign."""
    from adloop.ads.gaql import execute_query

    query = (
        "SELECT campaign_criterion.criterion_id, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.negative "
        "FROM campaign_criterion "
        f"WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_criterion.type = 'LOCATION' "
        "AND campaign_criterion.status != 'REMOVED'"
    )
    out: dict[str, bool] = {}
    for row in execute_query(config, customer_id, query):
        geo = row.get("campaign_criterion.location.geo_target_constant", "")
        if not geo:
            continue
        neg = row.get("campaign_criterion.negative", False)
        if isinstance(neg, str):
            neg = neg.strip().lower() == "true"
        out[_tail_id(geo)] = bool(neg)
    return out


def _validate_ids(ids) -> tuple[list[str], list[str]]:
    clean: list[str] = []
    errors: list[str] = []
    for raw in ids or []:
        s = str(raw).strip()
        if not s.isdigit():
            errors.append(f"geo_target_constant_id '{raw}' must be numeric")
        elif s not in clean:
            clean.append(s)
    if not clean and not errors:
        errors.append("geo_target_constant_ids must not be empty")
    return clean, errors


def build_add_geo_ops(client, cid: str, campaign_id: str, geo_ids: list[str]) -> list:
    """Build CREATE-only location criterion operations (positive targets)."""
    camp_rn = client.get_service("CampaignService").campaign_path(cid, campaign_id)
    ops = []
    for gid in geo_ids:
        op = client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = camp_rn
        crit.negative = False
        crit.location.geo_target_constant = f"geoTargetConstants/{gid}"
        ops.append(op)
    return ops


def _get_client(config):
    from adloop.ads.client import get_ads_client

    return get_ads_client(config)


def draft_add_campaign_geo_targets(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    campaign_id: str = "",
    geo_target_constant_ids: list | None = None,
) -> dict:
    """Draft adding positive location targets to a campaign (additive only)."""
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_campaign_geo_targets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    if not str(campaign_id or "").strip().isdigit():
        return {"error": "Validation failed", "details": ["campaign_id must be numeric"]}
    campaign_id = str(campaign_id).strip()
    ids, errs = _validate_ids(geo_target_constant_ids)
    if errs:
        return {"error": "Validation failed", "details": errs}

    cid = normalize_customer_id(customer_id)
    existing = _existing_location_targets(config, cid, campaign_id)
    conflicts = [g for g in ids if existing.get(g) is True]
    if conflicts:
        return {
            "error": "Validation failed",
            "details": [
                f"geo_target_constant_id {g} is already an EXCLUDED location on this "
                "campaign; remove the exclusion first" for g in conflicts
            ],
        }
    skipped = [g for g in ids if g in existing]
    to_add = [g for g in ids if g not in existing]

    plan = ChangePlan(
        operation="add_campaign_geo_targets",
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "geo_target_constant_ids": to_add,
            "skipped_already_targeted": skipped,
        },
    )
    preview = plan.to_preview()
    if not to_add:
        preview["validate_only_result"] = {
            "status": "NOOP",
            "detail": "all requested locations are already targeted; nothing to add",
        }
        return preview
    store_plan(plan)

    try:
        result = apply_add_campaign_geo_targets(
            _get_client(config), cid, plan.changes, validate_only=True
        )
        preview["validate_only_result"] = {"status": "VALIDATE_ONLY_OK", **result}
    except Exception as e:  # surface Google's validation error, keep the plan
        from adloop.ads.write import _extract_error_message

        preview["validate_only_result"] = {
            "status": "VALIDATION_FAILED",
            "error": _extract_error_message(e),
        }
    return preview


def apply_add_campaign_geo_targets(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    """CREATE location criteria. Never removes or edits existing criteria."""
    ids = [str(g) for g in changes["geo_target_constant_ids"]]
    ops = build_add_geo_ops(client, cid, changes["campaign_id"], ids)
    resp = _send(client, cid, ops, validate_only)
    result = {
        "validate_only": validate_only,
        "campaign_id": changes["campaign_id"],
        "added_geo_target_constant_ids": ids,
        "skipped_already_targeted": list(changes.get("skipped_already_targeted", [])),
        "operations": len(ops),
    }
    if not validate_only:
        result["resource_names"] = [
            r.campaign_criterion_result.resource_name for r in resp.mutate_operation_responses
        ]
    return result


def draft_update_geo_target_type(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    campaign_id: str = "",
    positive: str = "",
) -> dict:
    """Draft setting campaign.geo_target_type_setting.positive_geo_target_type."""
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_geo_target_type", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    if not str(campaign_id or "").strip().isdigit():
        return {"error": "Validation failed", "details": ["campaign_id must be numeric"]}
    positive = str(positive or "").strip().upper()
    if positive not in GEO_TYPES:
        return {
            "error": "Validation failed",
            "details": [f"positive must be one of {', '.join(GEO_TYPES)}"],
        }
    campaign_id = str(campaign_id).strip()
    plan = ChangePlan(
        operation="update_geo_target_type",
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={"campaign_id": campaign_id, "positive": positive},
    )
    store_plan(plan)
    preview = plan.to_preview()
    try:
        result = apply_update_geo_target_type(
            _get_client(config), normalize_customer_id(customer_id), plan.changes,
            validate_only=True,
        )
        preview["validate_only_result"] = {"status": "VALIDATE_ONLY_OK", **result}
    except Exception as e:
        from adloop.ads.write import _extract_error_message

        preview["validate_only_result"] = {
            "status": "VALIDATION_FAILED",
            "error": _extract_error_message(e),
        }
    return preview


def apply_update_geo_target_type(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    """Update only positive_geo_target_type (mask is exactly that one path)."""
    rn = client.get_service("CampaignService").campaign_path(cid, changes["campaign_id"])
    op = client.get_type("MutateOperation")
    c = op.campaign_operation.update
    c.resource_name = rn
    c.geo_target_type_setting.positive_geo_target_type = getattr(
        client.enums.PositiveGeoTargetTypeEnum, changes["positive"]
    )
    op.campaign_operation.update_mask.paths.append(
        "geo_target_type_setting.positive_geo_target_type"
    )
    _send(client, cid, [op], validate_only)
    return {"validate_only": validate_only, "campaign": rn, "positive": changes["positive"]}
