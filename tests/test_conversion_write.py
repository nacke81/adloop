"""Conversion action create/update + call asset conversion setting. Fixtures only."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec, patch

import pytest

from adloop.ads import conversion_write as cw
from adloop.ads.write import _execute_plan, validate_plan_only
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import get_plan, remove_plan

CID = "3865591631"


@pytest.fixture
def config():
    return AdLoopConfig(
        ads=AdsConfig(customer_id=CID, developer_token="test"),
        safety=SafetyConfig(max_daily_budget=50.0, require_dry_run=True),
    )


@pytest.fixture
def client():
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient(
        credentials=MagicMock(), developer_token="x", version="v23", use_proto_plus=True
    )


def _strict_mutate(real_client, svc_name, method, resource_name):
    """Mock whose signature matches the real client method, so unknown kwargs
    (e.g. validate_only=) raise TypeError exactly like the live client."""
    real_method = getattr(real_client.get_service(svc_name), method)
    resp = MagicMock()
    resp.results = [MagicMock(resource_name=resource_name)]
    return create_autospec(real_method, return_value=resp)


def _mock_svc_client(real_client, svc_name, resource_name):
    """Real client types/enums, but the named service is a strict mock."""
    svc = MagicMock()
    svc.mutate_conversion_actions = _strict_mutate(
        real_client, "ConversionActionService", "mutate_conversion_actions", resource_name
    )
    svc.mutate_assets = _strict_mutate(
        real_client, "AssetService", "mutate_assets", resource_name
    )
    real_get = real_client.get_service
    real_svc = real_get(svc_name)
    for attr in ("conversion_action_path", "asset_path"):
        if hasattr(real_svc, attr):
            setattr(svc, attr, getattr(real_svc, attr))

    def get_service(name, *a, **k):
        return svc if name == svc_name else real_get(name, *a, **k)

    wrapped = MagicMock(wraps=real_client)
    wrapped.get_service = get_service
    wrapped.get_type = real_client.get_type
    wrapped.enums = real_client.enums
    return wrapped, svc


class TestCreateConversionAction:
    def test_preview(self, config):
        r = cw.draft_create_conversion_action(
            config, customer_id=CID, name="SCC Calls", phone_call_duration_seconds=60,
            value_settings={"default_value": 25, "default_currency_code": "usd"},
        )
        assert r["operation"] == "create_conversion_action"
        assert r["changes"]["type"] == "AD_CALL"
        assert r["changes"]["phone_call_duration_seconds"] == 60
        assert r["changes"]["value_settings"] == {"default_value": 25.0, "default_currency_code": "USD"}
        remove_plan(r["plan_id"])

    def test_website_call_allowed(self, config):
        r = cw.draft_create_conversion_action(config, customer_id=CID, name="x", type="website_call")
        assert r["changes"]["type"] == "WEBSITE_CALL"
        remove_plan(r["plan_id"])

    @pytest.mark.parametrize(
        "kw",
        [
            {"name": ""},
            {"name": "x", "type": "WEBPAGE"},
            {"name": "x", "category": "NOPE"},
            {"name": "x", "counting_type": "TWICE"},
            {"name": "x", "status": "REMOVED"},
            {"name": "x", "phone_call_duration_seconds": -1},
            {"name": "x", "value_settings": {"bogus": 1}},
        ],
    )
    def test_rejects_bad_input(self, config, kw):
        r = cw.draft_create_conversion_action(config, customer_id=CID, **kw)
        assert r["error"] == "Validation failed"

    def test_blocked_operation(self):
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id=CID, developer_token="t"),
            safety=SafetyConfig(blocked_operations=["create_conversion_action"]),
        )
        assert "error" in cw.draft_create_conversion_action(cfg, customer_id=CID, name="x")

    def test_op_content(self, client):
        op = cw.build_create_conversion_action_op(client, CID, {
            "name": "SCC Calls", "type": "AD_CALL", "category": "PHONE_CALL_LEAD",
            "counting_type": "ONE_PER_CLICK", "status": "ENABLED", "primary_for_goal": True,
            "phone_call_duration_seconds": 60,
        })
        ca = op.create
        assert ca.name == "SCC Calls"
        assert ca.type_ == client.enums.ConversionActionTypeEnum.AD_CALL
        assert ca.category == client.enums.ConversionActionCategoryEnum.PHONE_CALL_LEAD
        assert ca.phone_call_duration_seconds == 60
        assert ca.primary_for_goal is True

    def test_apply_returns_resource_and_id(self, client):
        c, svc = _mock_svc_client(client, "ConversionActionService", f"customers/{CID}/conversionActions/777")
        out = cw.apply_create_conversion_action(c, CID, {
            "name": "n", "type": "AD_CALL", "category": "PHONE_CALL_LEAD",
            "counting_type": "ONE_PER_CLICK", "status": "ENABLED", "primary_for_goal": True,
        })
        assert out["resource_name"] == f"customers/{CID}/conversionActions/777"
        assert out["conversion_action_id"] == "777"
        assert svc.mutate_conversion_actions.call_args.kwargs["request"].validate_only is False


class TestUpdateConversionAction:
    def test_preview_and_mask(self, config, client):
        r = cw.draft_update_conversion_action(
            config, customer_id=CID, conversion_action_id="555",
            primary_for_goal=False, status="hidden",
        )
        assert r["operation"] == "update_conversion_action"
        assert r["entity_id"] == "555"
        op = cw.build_update_conversion_action_op(client, CID, get_plan(r["plan_id"]).changes)
        assert op.update.resource_name == f"customers/{CID}/conversionActions/555"
        assert list(op.update_mask.paths) == ["primary_for_goal", "status"]
        assert op.update.status == client.enums.ConversionActionStatusEnum.HIDDEN
        remove_plan(r["plan_id"])

    def test_all_four_fields_mask(self, config, client):
        r = cw.draft_update_conversion_action(
            config, customer_id=CID, conversion_action_id=f"customers/{CID}/conversionActions/9",
            primary_for_goal=True, status="ENABLED", counting_type="MANY_PER_CLICK",
            phone_call_duration_seconds=30,
        )
        op = cw.build_update_conversion_action_op(client, CID, get_plan(r["plan_id"]).changes)
        assert list(op.update_mask.paths) == [
            "primary_for_goal", "status", "counting_type", "phone_call_duration_seconds",
        ]
        remove_plan(r["plan_id"])

    @pytest.mark.parametrize("field", ["name", "type", "category", "value_settings", "resource_name"])
    def test_disallowed_field_rejected(self, config, field):
        r = cw.draft_update_conversion_action(
            config, customer_id=CID, conversion_action_id="1", status="HIDDEN", **{field: "x"}
        )
        assert r["error"] == "Validation failed"
        assert field in r["details"][0]

    def test_requires_change_and_id(self, config):
        assert cw.draft_update_conversion_action(config, customer_id=CID, conversion_action_id="1")["error"]
        assert cw.draft_update_conversion_action(config, customer_id=CID, conversion_action_id="x", status="HIDDEN")["error"]

    def test_validate_only_and_execute(self, config, client):
        r = cw.draft_update_conversion_action(config, customer_id=CID, conversion_action_id="5", status="HIDDEN")
        c, svc = _mock_svc_client(client, "ConversionActionService", f"customers/{CID}/conversionActions/5")
        with patch("adloop.ads.client.get_ads_client", return_value=c):
            out = validate_plan_only(config, plan_id=r["plan_id"])
            assert out["status"] == "VALIDATE_ONLY_OK"
            assert svc.mutate_conversion_actions.call_args.kwargs["request"].validate_only is True
            assert get_plan(r["plan_id"]) is not None
            res = _execute_plan(config, get_plan(r["plan_id"]))
        assert svc.mutate_conversion_actions.call_args.kwargs["request"].validate_only is False
        assert res["resource_name"].endswith("/5")
        remove_plan(r["plan_id"])


class TestUpdateCallAsset:
    def test_preview_and_mask(self, config, client):
        r = cw.draft_update_call_asset(
            config, customer_id=CID, asset_id="12345",
            call_conversion_reporting_state="use_resource_level_call_conversion_action",
            call_conversion_action="888",
        )
        assert r["operation"] == "update_call_asset"
        assert r["entity_id"] == "12345"
        assert "validate_plan_only" in r["note"]
        op = cw.build_update_call_asset_op(client, CID, get_plan(r["plan_id"]).changes)
        assert op.update.resource_name == f"customers/{CID}/assets/12345"
        assert list(op.update_mask.paths) == [
            "call_asset.call_conversion_reporting_state",
            "call_asset.call_conversion_action",
        ]
        assert op.update.call_asset.call_conversion_action == f"customers/{CID}/conversionActions/888"
        assert (
            op.update.call_asset.call_conversion_reporting_state
            == client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
        remove_plan(r["plan_id"])

    @pytest.mark.parametrize("field", ["phone_number", "country_code", "ad_schedule_targets"])
    def test_disallowed_field_rejected(self, config, field):
        r = cw.draft_update_call_asset(
            config, customer_id=CID, asset_id="1", call_conversion_action="2", **{field: "x"}
        )
        assert r["error"] == "Validation failed"

    def test_bad_inputs(self, config):
        assert cw.draft_update_call_asset(config, customer_id=CID, asset_id="1")["error"]
        assert cw.draft_update_call_asset(config, customer_id=CID, asset_id="a", call_conversion_action="1")["error"]
        assert cw.draft_update_call_asset(
            config, customer_id=CID, asset_id="1", call_conversion_reporting_state="NOPE"
        )["error"]
        assert cw.draft_update_call_asset(
            config, customer_id=CID, asset_id="1", call_conversion_action="2",
            call_conversion_reporting_state="DISABLED",
        )["error"]

    def test_validate_only_flag(self, config, client):
        r = cw.draft_update_call_asset(config, customer_id=CID, asset_id="1", call_conversion_action="2")
        c, svc = _mock_svc_client(client, "AssetService", f"customers/{CID}/assets/1")
        with patch("adloop.ads.client.get_ads_client", return_value=c):
            out = validate_plan_only(config, plan_id=r["plan_id"])
        assert out["status"] == "VALIDATE_ONLY_OK"
        assert svc.mutate_assets.call_args.kwargs["request"].validate_only is True
        remove_plan(r["plan_id"])


class TestExistingUnchanged:
    def test_existing_dispatch_keys_still_present(self, config):
        import inspect

        from adloop.ads import write

        src = inspect.getsource(write._execute_plan)
        for op in ("create_campaign", "update_campaign", "create_call_asset", "remove_entity", "pause_entity"):
            assert f'"{op}"' in src

    def test_unknown_operation_still_raises(self, config):
        from adloop.safety.preview import ChangePlan

        with pytest.raises(ValueError):
            with patch("adloop.ads.client.get_ads_client", return_value=MagicMock()):
                _execute_plan(config, ChangePlan(operation="nope", customer_id=CID))

    def test_validate_plan_only_still_rejects_unsupported(self, config):
        from adloop.safety.preview import ChangePlan, store_plan

        p = ChangePlan(operation="update_campaign", customer_id=CID)
        store_plan(p)
        with patch("adloop.ads.client.get_ads_client", return_value=MagicMock()):
            out = validate_plan_only(config, plan_id=p.plan_id)
        assert "not 'update_campaign'" in out["error"]
        remove_plan(p.plan_id)

    def test_server_registers_new_tools(self):
        from adloop import server

        for n in ("create_conversion_action", "update_conversion_action", "update_call_asset"):
            assert n in server._WRITE_TOOL_NAMES
            assert callable(getattr(server, n))


class TestRealSignature:
    """Regression: validate_only belongs on the request, never a method kwarg."""

    def test_strict_mock_rejects_validate_only_kwarg(self, client):
        m = _strict_mutate(client, "AssetService", "mutate_assets", "x")
        with pytest.raises(TypeError):
            m(customer_id=CID, operations=[], validate_only=True)

    def test_real_method_has_no_validate_only_param(self, client):
        import inspect

        for svc, meth in (("ConversionActionService", "mutate_conversion_actions"),
                          ("AssetService", "mutate_assets")):
            params = inspect.signature(getattr(client.get_service(svc), meth)).parameters
            assert "validate_only" not in params and "request" in params

    @pytest.mark.parametrize("flag", [True, False])
    def test_all_apply_fns_put_validate_only_on_request(self, client, flag):
        cases = [
            (cw.apply_create_conversion_action, "ConversionActionService", "mutate_conversion_actions",
             {"name": "n", "type": "AD_CALL", "category": "PHONE_CALL_LEAD",
              "counting_type": "ONE_PER_CLICK", "status": "ENABLED", "primary_for_goal": True}),
            (cw.apply_update_conversion_action, "ConversionActionService", "mutate_conversion_actions",
             {"conversion_action_id": "5", "status": "HIDDEN"}),
            (cw.apply_update_call_asset, "AssetService", "mutate_assets",
             {"asset_id": "1", "call_conversion_action": "2"}),
        ]
        for fn, svc_name, meth, changes in cases:
            c, svc = _mock_svc_client(client, svc_name, f"customers/{CID}/x/1")
            fn(c, CID, changes, validate_only=flag)
            call = getattr(svc, meth).call_args
            assert call.args == ()
            req = call.kwargs["request"]
            assert req.validate_only is flag
            assert req.customer_id == CID
            assert len(req.operations) == 1
