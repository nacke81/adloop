"""Conversion action create/update and call-asset conversion setting.

Same flow as the other writes: draft -> preview (plan_id) -> confirm_and_apply,
with validate_only supported through validate_plan_only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

CREATE_TYPES = {"AD_CALL", "WEBSITE_CALL"}
CATEGORIES = {
    "DEFAULT", "PAGE_VIEW", "PURCHASE", "SIGNUP", "LEAD", "DOWNLOAD",
    "ADD_TO_CART", "BEGIN_CHECKOUT", "SUBSCRIBE_PAID", "PHONE_CALL_LEAD",
    "IMPORTED_LEAD", "SUBMIT_LEAD_FORM", "BOOK_APPOINTMENT", "REQUEST_QUOTE",
    "GET_DIRECTIONS", "OUTBOUND_CLICK", "CONTACT", "ENGAGEMENT", "STORE_VISIT",
    "STORE_SALE", "QUALIFIED_LEAD", "CONVERTED_LEAD",
}
STATUSES = {"ENABLED", "HIDDEN"}
COUNTING_TYPES = {"ONE_PER_CLICK", "MANY_PER_CLICK"}
UPDATE_CA_FIELDS = (
    "primary_for_goal", "status", "counting_type", "phone_call_duration_seconds",
)
REPORTING_STATES = {
    "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
    "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION",
    "DISABLED",
}
CALL_ASSET_FIELDS = ("call_conversion_reporting_state", "call_conversion_action")


def _id_from(value: object, collection: str) -> str:
    """Accept a bare numeric id or customers/X/<collection>/<id>."""
    s = str(value or "").strip()
    if s.isdigit():
        return s
    parts = s.split("/")
    if len(parts) == 4 and parts[0] == "customers" and parts[2] == collection and parts[3].isdigit():
        return parts[3]
    return ""


def _blocked(op: str, config: "AdLoopConfig") -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(op, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _store(op: str, entity_type: str, entity_id: str, customer_id: str, changes: dict) -> dict:
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(
        operation=op, entity_type=entity_type, entity_id=entity_id,
        customer_id=customer_id, changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# create_conversion_action
# ---------------------------------------------------------------------------

def draft_create_conversion_action(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    name: str = "",
    type: str = "AD_CALL",
    category: str = "PHONE_CALL_LEAD",
    phone_call_duration_seconds: int | None = None,
    counting_type: str = "ONE_PER_CLICK",
    status: str = "ENABLED",
    primary_for_goal: bool = True,
    value_settings: dict | None = None,
) -> dict:
    err = _blocked("create_conversion_action", config)
    if err:
        return err
    errors: list[str] = []
    name = str(name or "").strip()
    type_ = str(type or "").strip().upper()
    category = str(category or "").strip().upper()
    counting_type = str(counting_type or "").strip().upper()
    status = str(status or "").strip().upper()
    if not name:
        errors.append("name is required")
    if type_ not in CREATE_TYPES:
        errors.append(f"type must be one of {sorted(CREATE_TYPES)}")
    if category not in CATEGORIES:
        errors.append(f"category must be one of {sorted(CATEGORIES)}")
    if counting_type not in COUNTING_TYPES:
        errors.append(f"counting_type must be one of {sorted(COUNTING_TYPES)}")
    if status not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")
    if not isinstance(primary_for_goal, bool):
        errors.append("primary_for_goal must be true or false")
    if phone_call_duration_seconds is not None and (
        isinstance(phone_call_duration_seconds, bool)
        or not isinstance(phone_call_duration_seconds, int)
        or phone_call_duration_seconds < 0
    ):
        errors.append("phone_call_duration_seconds must be a non-negative integer")
    vs: dict = {}
    if value_settings:
        allowed = {"default_value", "default_currency_code", "always_use_default_value"}
        extra = set(value_settings) - allowed
        if extra:
            errors.append(f"value_settings has unsupported keys {sorted(extra)}; allowed {sorted(allowed)}")
        if "default_value" in value_settings:
            try:
                vs["default_value"] = float(value_settings["default_value"])
            except (TypeError, ValueError):
                errors.append("value_settings.default_value must be a number")
        if "default_currency_code" in value_settings:
            cur = str(value_settings["default_currency_code"]).strip().upper()
            if len(cur) != 3 or not cur.isalpha():
                errors.append("value_settings.default_currency_code must be a 3-letter code")
            vs["default_currency_code"] = cur
        if "always_use_default_value" in value_settings:
            vs["always_use_default_value"] = bool(value_settings["always_use_default_value"])
    if errors:
        return {"error": "Validation failed", "details": errors}
    changes: dict = {
        "name": name, "type": type_, "category": category,
        "counting_type": counting_type, "status": status,
        "primary_for_goal": primary_for_goal,
    }
    if phone_call_duration_seconds is not None:
        changes["phone_call_duration_seconds"] = phone_call_duration_seconds
    if vs:
        changes["value_settings"] = vs
    return _store("create_conversion_action", "conversion_action", "", customer_id, changes)


def build_create_conversion_action_op(client, cid: str, changes: dict):
    op = client.get_type("ConversionActionOperation")
    ca = op.create
    ca.name = changes["name"]
    ca.type_ = getattr(client.enums.ConversionActionTypeEnum, changes["type"])
    ca.category = getattr(client.enums.ConversionActionCategoryEnum, changes["category"])
    ca.status = getattr(client.enums.ConversionActionStatusEnum, changes["status"])
    ca.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, changes["counting_type"])
    ca.primary_for_goal = bool(changes["primary_for_goal"])
    if "phone_call_duration_seconds" in changes:
        ca.phone_call_duration_seconds = int(changes["phone_call_duration_seconds"])
    vs = changes.get("value_settings") or {}
    if "default_value" in vs:
        ca.value_settings.default_value = vs["default_value"]
    if "default_currency_code" in vs:
        ca.value_settings.default_currency_code = vs["default_currency_code"]
    if "always_use_default_value" in vs:
        ca.value_settings.always_use_default_value = vs["always_use_default_value"]
    return op


def apply_create_conversion_action(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    svc = client.get_service("ConversionActionService")
    op = build_create_conversion_action_op(client, cid, changes)
    resp = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op], validate_only=bool(validate_only)
    )
    result: dict = {"validate_only": bool(validate_only), "operations": 1}
    if not validate_only:
        rn = resp.results[0].resource_name
        result["resource_name"] = rn
        result["conversion_action_id"] = rn.rsplit("/", 1)[-1]
    return result


# ---------------------------------------------------------------------------
# update_conversion_action
# ---------------------------------------------------------------------------

def draft_update_conversion_action(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    conversion_action_id: str = "",
    **fields,
) -> dict:
    """Only primary_for_goal, status, counting_type, phone_call_duration_seconds."""
    err = _blocked("update_conversion_action", config)
    if err:
        return err
    errors: list[str] = []
    extra = sorted(set(fields) - set(UPDATE_CA_FIELDS))
    if extra:
        errors.append(
            f"Field(s) {extra} cannot be updated. Allowed: {list(UPDATE_CA_FIELDS)}"
        )
    caid = _id_from(conversion_action_id, "conversionActions")
    if not caid:
        errors.append("conversion_action_id must be numeric or customers/X/conversionActions/N")
    changes: dict = {}
    for k in UPDATE_CA_FIELDS:
        v = fields.get(k)
        if v is None or v == "":
            continue
        if k == "primary_for_goal":
            if not isinstance(v, bool):
                errors.append("primary_for_goal must be true or false")
        elif k == "status":
            v = str(v).strip().upper()
            if v not in STATUSES:
                errors.append(f"status must be one of {sorted(STATUSES)}")
        elif k == "counting_type":
            v = str(v).strip().upper()
            if v not in COUNTING_TYPES:
                errors.append(f"counting_type must be one of {sorted(COUNTING_TYPES)}")
        elif k == "phone_call_duration_seconds":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                errors.append("phone_call_duration_seconds must be a non-negative integer")
        changes[k] = v
    if not changes and not extra:
        errors.append(f"No changes specified. Provide one of {list(UPDATE_CA_FIELDS)}")
    if errors:
        return {"error": "Validation failed", "details": errors}
    changes = {"conversion_action_id": caid, **changes}
    return _store("update_conversion_action", "conversion_action", caid, customer_id, changes)


def build_update_conversion_action_op(client, cid: str, changes: dict):
    from google.protobuf import field_mask_pb2

    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.update
    ca.resource_name = svc.conversion_action_path(cid, changes["conversion_action_id"])
    paths: list[str] = []
    if "primary_for_goal" in changes:
        ca.primary_for_goal = bool(changes["primary_for_goal"])
        paths.append("primary_for_goal")
    if "status" in changes:
        ca.status = getattr(client.enums.ConversionActionStatusEnum, changes["status"])
        paths.append("status")
    if "counting_type" in changes:
        ca.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, changes["counting_type"])
        paths.append("counting_type")
    if "phone_call_duration_seconds" in changes:
        ca.phone_call_duration_seconds = int(changes["phone_call_duration_seconds"])
        paths.append("phone_call_duration_seconds")
    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    return op


def apply_update_conversion_action(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    svc = client.get_service("ConversionActionService")
    op = build_update_conversion_action_op(client, cid, changes)
    resp = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op], validate_only=bool(validate_only)
    )
    result: dict = {"validate_only": bool(validate_only), "operations": 1}
    if not validate_only:
        result["resource_name"] = resp.results[0].resource_name
    return result


# ---------------------------------------------------------------------------
# update_call_asset
# ---------------------------------------------------------------------------

def draft_update_call_asset(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    asset_id: str = "",
    **fields,
) -> dict:
    """Only call_conversion_reporting_state and call_conversion_action."""
    err = _blocked("update_call_asset", config)
    if err:
        return err
    errors: list[str] = []
    extra = sorted(set(fields) - set(CALL_ASSET_FIELDS))
    if extra:
        errors.append(
            f"Field(s) {extra} cannot be updated. Allowed: {list(CALL_ASSET_FIELDS)}"
        )
    aid = _id_from(asset_id, "assets")
    if not aid:
        errors.append("asset_id must be numeric or customers/X/assets/N")
    changes: dict = {}
    state = fields.get("call_conversion_reporting_state")
    if state not in (None, ""):
        state = str(state).strip().upper()
        if state not in REPORTING_STATES:
            errors.append(f"call_conversion_reporting_state must be one of {sorted(REPORTING_STATES)}")
        changes["call_conversion_reporting_state"] = state
    action = fields.get("call_conversion_action")
    if action not in (None, ""):
        caid = _id_from(action, "conversionActions")
        if not caid:
            errors.append("call_conversion_action must be numeric or customers/X/conversionActions/N")
        changes["call_conversion_action_id"] = caid
    if not changes and not extra:
        errors.append(f"No changes specified. Provide one of {list(CALL_ASSET_FIELDS)}")
    if (
        "call_conversion_action_id" in changes
        and changes.get("call_conversion_reporting_state")
        and changes["call_conversion_reporting_state"] != "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
    ):
        errors.append(
            "call_conversion_action only takes effect when call_conversion_reporting_state "
            "is USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}
    changes = {"asset_id": aid, **changes}
    preview = _store("update_call_asset", "asset", aid, customer_id, changes)
    preview["note"] = (
        "Asset docs say assets are immutable; the CallAsset conversion fields are not "
        "marked immutable in the v23 proto. Run validate_plan_only first: if Google "
        "refuses, the supported fallback is a new call asset (draft_call_asset) with the "
        "conversion fields set, then relink and remove the old link."
    )
    return preview


def build_update_call_asset_op(client, cid: str, changes: dict):
    from google.protobuf import field_mask_pb2

    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    a = op.update
    a.resource_name = svc.asset_path(cid, changes["asset_id"])
    paths: list[str] = []
    if "call_conversion_reporting_state" in changes:
        a.call_asset.call_conversion_reporting_state = getattr(
            client.enums.CallConversionReportingStateEnum,
            changes["call_conversion_reporting_state"],
        )
        paths.append("call_asset.call_conversion_reporting_state")
    if "call_conversion_action_id" in changes:
        a.call_asset.call_conversion_action = svc.conversion_action_path(
            cid, changes["call_conversion_action_id"]
        )
        paths.append("call_asset.call_conversion_action")
    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    return op


def apply_update_call_asset(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    svc = client.get_service("AssetService")
    op = build_update_call_asset_op(client, cid, changes)
    resp = svc.mutate_assets(
        customer_id=cid, operations=[op], validate_only=bool(validate_only)
    )
    result: dict = {"validate_only": bool(validate_only), "operations": 1}
    if not validate_only:
        result["resource_name"] = resp.results[0].resource_name
    return result
