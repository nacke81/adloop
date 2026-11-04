"""Call asset (campaign level) + guard against missing `import re`."""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import pmax_write
from adloop.ads.write import validate_plan_only
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import get_plan, remove_plan

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "adloop"


@pytest.fixture
def config():
    return AdLoopConfig(
        ads=AdsConfig(customer_id="3865591631", developer_token="test"),
        safety=SafetyConfig(max_daily_budget=50.0, require_dry_run=True),
    )


@pytest.fixture
def real_client():
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient(
        credentials=MagicMock(), developer_token="x", version="v23", use_proto_plus=True
    )


class TestDraftCallAsset:
    def test_formats_us_number(self, config):
        r = pmax_write.draft_call_asset(
            config, customer_id="3865591631", campaign_id="24427159679",
            phone_number="(270) 383-7510",
        )
        assert r["operation"] == "create_call_asset"
        assert r["changes"] == {
            "campaign_id": "24427159679", "phone_number": "2703837510", "country_code": "US",
        }
        remove_plan(r["plan_id"])

    def test_accepts_leading_one(self, config):
        r = pmax_write.draft_call_asset(
            config, customer_id="1", campaign_id="5", phone_number="+1 270-383-7510",
        )
        assert r["changes"]["phone_number"] == "2703837510"
        remove_plan(r["plan_id"])

    @pytest.mark.parametrize(
        "kw",
        [
            {"campaign_id": "abc", "phone_number": "2703837510"},
            {"campaign_id": "5", "phone_number": "12345"},
            {"campaign_id": "5", "phone_number": ""},
            {"campaign_id": "5", "phone_number": "2703837510", "country_code": "USA"},
        ],
    )
    def test_rejects_bad_input(self, config, kw):
        r = pmax_write.draft_call_asset(config, customer_id="1", **kw)
        assert r["error"] == "Validation failed"

    def test_blocked_operation(self):
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="1", developer_token="t"),
            safety=SafetyConfig(blocked_operations=["create_call_asset"]),
        )
        r = pmax_write.draft_call_asset(
            cfg, customer_id="1", campaign_id="5", phone_number="2703837510"
        )
        assert "error" in r and "details" not in r

    def test_ops_content(self, real_client):
        ops, labels = pmax_write.build_call_asset_ops(
            real_client, "3865591631",
            {"campaign_id": "24427159679", "phone_number": "2703837510", "country_code": "US"},
        )
        assert labels == ["asset", "campaign_asset"]
        asset = ops[0].asset_operation.create
        assert asset.call_asset.phone_number == "2703837510"
        assert asset.call_asset.country_code == "US"
        ca = ops[1].campaign_asset_operation.create
        assert ca.campaign == "customers/3865591631/campaigns/24427159679"
        assert ca.asset == asset.resource_name == "customers/3865591631/assets/-3001"
        assert ca.field_type == real_client.enums.AssetFieldTypeEnum.CALL

    def test_validate_only_flag_and_pending(self, config, real_client):
        r = pmax_write.draft_call_asset(
            config, customer_id="3865591631", campaign_id="24427159679",
            phone_number="2703837510",
        )
        with patch("adloop.ads.client.get_ads_client", return_value=real_client), patch(
            "adloop.ads.pmax_write._send"
        ) as send:
            out = validate_plan_only(config, plan_id=r["plan_id"])
        assert out["status"] == "VALIDATE_ONLY_OK"
        assert send.call_args.args[3] is True
        assert get_plan(r["plan_id"]) is not None
        remove_plan(r["plan_id"])

    def test_execute_plan_dispatches(self, config, real_client):
        from adloop.ads.write import _execute_plan

        r = pmax_write.draft_call_asset(
            config, customer_id="3865591631", campaign_id="24427159679",
            phone_number="2703837510",
        )
        plan = get_plan(r["plan_id"])
        with patch("adloop.ads.client.get_ads_client", return_value=real_client), patch(
            "adloop.ads.pmax_write._send"
        ) as send, patch("adloop.ads.pmax_write._collect", return_value={"asset": "a"}):
            out = _execute_plan(config, plan)
        assert send.call_args.args[3] is False
        assert out["asset"] == "a"
        remove_plan(r["plan_id"])


def _re_findings(path: pathlib.Path) -> list[str]:
    """Return `re.<attr>` uses in a module with no module-scope `import re`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level = any(
        isinstance(n, ast.Import) and any(a.name == "re" and not a.asname for a in n.names)
        for n in tree.body
    )
    if module_level:
        return []
    uses = [
        f"{path.name}:{n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "re"
    ]
    return uses


class TestReImportGuard:
    def test_every_re_user_imports_re_at_module_scope(self):
        bad = [f for p in SRC.rglob("*.py") for f in _re_findings(p)]
        assert bad == [], f"`re` used without module-scope import: {bad}"

    def test_detector_flags_a_missing_import(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text("def f(x):\n    return re.sub('a', 'b', x)\n", encoding="utf-8")
        assert _re_findings(p) == ["m.py:2"]

    def test_three_headline_asset_group_draft_path(self, config):
        """The exact call that once returned "name 're' is not defined"."""
        r = pmax_write.draft_pmax_asset_group_assets(
            config, customer_id="1", asset_group_id="5",
            add=[{"field_type": "HEADLINE", "text": t} for t in ("One", "Two", "Three")],
        )
        assert r.get("operation") == "add_pmax_asset_group_assets", r
        remove_plan(r["plan_id"])
