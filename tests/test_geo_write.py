"""Tests for additive geo-target add and geo_target_type update (no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import geo_write
from adloop.ads.write import validate_plan_only
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import get_plan, remove_plan

CID = "3865591631"
CAMP = "24427159679"


@pytest.fixture
def config():
    return AdLoopConfig(
        ads=AdsConfig(customer_id=CID, developer_token="test"),
        safety=SafetyConfig(max_daily_budget=50.0, require_dry_run=True),
    )


@pytest.fixture
def real_client():
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient(
        credentials=MagicMock(), developer_token="x", version="v23", use_proto_plus=True
    )


def _wire(real_client):
    svc = MagicMock()
    real_client.get_service = MagicMock(
        side_effect=lambda n: svc if n == "GoogleAdsService" else MagicMock(
            campaign_path=lambda c, i: f"customers/{c}/campaigns/{i}"
        )
    )
    return svc


def _existing(*pairs):
    return [
        {
            "campaign_criterion.location.geo_target_constant": f"geoTargetConstants/{g}",
            "campaign_criterion.negative": neg,
        }
        for g, neg in pairs
    ]


class TestAddGeoTargets:
    def test_apply_builds_create_only_ops(self, real_client):
        svc = _wire(real_client)
        out = geo_write.apply_add_campaign_geo_targets(
            real_client, CID,
            {"campaign_id": CAMP, "geo_target_constant_ids": ["9057698", "9057703"]},
            validate_only=True,
        )
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is True
        assert len(req.mutate_operations) == 2
        for op in req.mutate_operations:
            co = op.campaign_criterion_operation
            assert co._pb.WhichOneof("operation") == "create"  # never remove/update
            assert co.create.negative is False
            assert co.create.campaign == f"customers/{CID}/campaigns/{CAMP}"
        got = [o.campaign_criterion_operation.create.location.geo_target_constant
               for o in req.mutate_operations]
        assert got == ["geoTargetConstants/9057698", "geoTargetConstants/9057703"]
        assert out["operations"] == 2

    def test_draft_skips_existing_and_validates(self, config, real_client):
        svc = _wire(real_client)
        rows = _existing(("1", False), ("9057698", False))
        with patch("adloop.ads.gaql.execute_query", return_value=rows), \
             patch("adloop.ads.geo_write._get_client", return_value=real_client):
            r = geo_write.draft_add_campaign_geo_targets(
                config, customer_id=CID, campaign_id=CAMP,
                geo_target_constant_ids=["9057698", "9057703"],
            )
        assert r["changes"]["geo_target_constant_ids"] == ["9057703"]
        assert r["changes"]["skipped_already_targeted"] == ["9057698"]
        assert r["validate_only_result"]["status"] == "VALIDATE_ONLY_OK"
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is True
        assert len(req.mutate_operations) == 1
        assert get_plan(r["plan_id"]) is not None
        remove_plan(r["plan_id"])

    def test_draft_all_existing_is_noop_without_mutate(self, config, real_client):
        svc = _wire(real_client)
        with patch("adloop.ads.gaql.execute_query", return_value=_existing(("5", False))), \
             patch("adloop.ads.geo_write._get_client", return_value=real_client):
            r = geo_write.draft_add_campaign_geo_targets(
                config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=["5"],
            )
        assert r["validate_only_result"]["status"] == "NOOP"
        svc.mutate.assert_not_called()

    def test_draft_rejects_excluded_conflict_and_bad_input(self, config):
        with patch("adloop.ads.gaql.execute_query", return_value=_existing(("7", True))):
            r = geo_write.draft_add_campaign_geo_targets(
                config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=["7"],
            )
        assert "error" in r
        assert "error" in geo_write.draft_add_campaign_geo_targets(
            config, customer_id=CID, campaign_id="x", geo_target_constant_ids=["1"])
        assert "error" in geo_write.draft_add_campaign_geo_targets(
            config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=["1; DROP"])
        assert "error" in geo_write.draft_add_campaign_geo_targets(
            config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=[])

    def test_validation_failure_is_reported_not_raised(self, config, real_client):
        svc = _wire(real_client)
        svc.mutate.side_effect = RuntimeError("boom")
        with patch("adloop.ads.gaql.execute_query", return_value=[]), \
             patch("adloop.ads.geo_write._get_client", return_value=real_client):
            r = geo_write.draft_add_campaign_geo_targets(
                config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=["9"],
            )
        assert r["validate_only_result"]["status"] == "VALIDATION_FAILED"
        remove_plan(r["plan_id"])

    def test_validate_plan_only_routes(self, config, real_client):
        svc = _wire(real_client)
        with patch("adloop.ads.gaql.execute_query", return_value=[]), \
             patch("adloop.ads.geo_write._get_client", return_value=real_client):
            r = geo_write.draft_add_campaign_geo_targets(
                config, customer_id=CID, campaign_id=CAMP, geo_target_constant_ids=["9"],
            )
        with patch("adloop.ads.client.get_ads_client", return_value=real_client):
            out = validate_plan_only(config, plan_id=r["plan_id"])
        assert out["status"] == "VALIDATE_ONLY_OK"
        assert svc.mutate.call_args.kwargs["request"].validate_only is True
        remove_plan(r["plan_id"])


class TestGeoTargetType:
    def test_apply_sets_only_positive_with_mask(self, real_client):
        svc = _wire(real_client)
        out = geo_write.apply_update_geo_target_type(
            real_client, CID, {"campaign_id": CAMP, "positive": "PRESENCE_OR_INTEREST"},
            validate_only=True,
        )
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is True
        u = req.mutate_operations[0].campaign_operation
        assert list(u.update_mask.paths) == ["geo_target_type_setting.positive_geo_target_type"]
        assert u.update.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE_OR_INTEREST"
        assert u.update.geo_target_type_setting.negative_geo_target_type.name == "UNSPECIFIED"
        assert out["positive"] == "PRESENCE_OR_INTEREST"

    def test_draft_validates_and_rejects_bad_values(self, config, real_client):
        svc = _wire(real_client)
        with patch("adloop.ads.geo_write._get_client", return_value=real_client):
            r = geo_write.draft_update_geo_target_type(
                config, customer_id=CID, campaign_id=CAMP, positive="presence")
        assert r["changes"]["positive"] == "PRESENCE"
        assert r["validate_only_result"]["status"] == "VALIDATE_ONLY_OK"
        assert svc.mutate.call_args.kwargs["request"].validate_only is True
        remove_plan(r["plan_id"])
        assert "error" in geo_write.draft_update_geo_target_type(
            config, customer_id=CID, campaign_id=CAMP, positive="LOCATION_OF_PRESENCE")
        assert "error" in geo_write.draft_update_geo_target_type(
            config, customer_id=CID, campaign_id="abc", positive="PRESENCE")


class TestServerGuards:
    def test_tools_are_customer_locked(self, monkeypatch):
        from adloop import server

        for name in ("draft_add_campaign_geo_targets", "draft_update_geo_target_type"):
            assert name in server._WRITE_TOOL_NAMES
            monkeypatch.setenv("ADLOOP_ALLOWED_CUSTOMER_ID", CID)
            with pytest.raises(RuntimeError, match="restricted to customer_id"):
                server._enforce_write_customer(name, {"customer_id": "999"})
            kw = {}
            server._enforce_write_customer(name, kw)
            assert kw["customer_id"] == CID

    def test_tools_registered(self):
        from adloop import server

        import asyncio

        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert {"draft_add_campaign_geo_targets", "draft_update_geo_target_type"} <= names
