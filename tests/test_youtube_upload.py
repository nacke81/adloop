"""YouTube video upload via YouTubeVideoUploadService (mocked client)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from adloop.ads import youtube_upload as yu
from adloop.ads.write import _execute_plan
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import get_plan, remove_plan


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


@pytest.fixture
def mp4(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 2048)
    return str(p)


def _draft(config, mp4, **kw):
    args = dict(
        customer_id="3865591631", video_path=mp4, title="SCC intro", description="d"
    )
    args.update(kw)
    return yu.draft_youtube_video_upload(config, **args)


class TestDraft:
    def test_preview_defaults_unlisted_google_managed(self, config, mp4):
        r = _draft(config, mp4)
        try:
            assert r["operation"] == "create_youtube_video_upload"
            assert r["changes"]["privacy"] == "UNLISTED"
            assert r["changes"]["channel_id"] is None
            assert r["changes"]["file_size_bytes"] == 2048
            assert "Google-managed" in r["note"]
        finally:
            remove_plan(r["plan_id"])

    def test_private_accepted_only_if_api_enum_has_it(self, config, mp4, monkeypatch):
        # v23 YouTubeVideoPrivacy has only PUBLIC and UNLISTED: PRIVATE is refused.
        r = _draft(config, mp4, privacy="private")
        assert "not supported by the pinned Google Ads API" in r["details"][0]
        monkeypatch.setattr(yu, "_privacy_enum_values", lambda: {"UNLISTED", "PRIVATE"})
        r = _draft(config, mp4, privacy="private")
        assert r["changes"]["privacy"] == "PRIVATE"
        remove_plan(r["plan_id"])

    @pytest.mark.parametrize("bad", ["PUBLIC", "public", "", "FOO"])
    def test_public_and_unknown_rejected(self, config, mp4, bad):
        r = _draft(config, mp4, privacy=bad)
        assert r["error"] == "Validation failed"
        assert "privacy" in r["details"][0]

    def test_missing_file(self, config, tmp_path):
        r = _draft(config, str(tmp_path / "nope.mp4"))
        assert "does not exist" in r["details"][0]

    def test_not_mp4(self, config, tmp_path):
        p = tmp_path / "a.mov"
        p.write_bytes(b"x")
        r = _draft(config, str(p))
        assert any(".mp4" in d for d in r["details"])

    def test_empty_file(self, config, tmp_path):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"")
        assert any("empty" in d for d in _draft(config, str(p))["details"])

    def test_size_cap(self, config, mp4, monkeypatch):
        monkeypatch.setattr(yu, "MAX_VIDEO_BYTES", 100)
        assert any("cap" in d for d in _draft(config, mp4)["details"])

    def test_title_rules(self, config, mp4):
        assert "title is required" in _draft(config, mp4, title=" ")["details"]
        assert any("100" in d for d in _draft(config, mp4, title="x" * 101)["details"])
        assert any("<" in d for d in _draft(config, mp4, title="a<b")["details"])

    def test_channel_id_format(self, config, mp4):
        assert any("channel_id" in d for d in _draft(config, mp4, channel_id="abc")["details"])
        r = _draft(config, mp4, channel_id="UC" + "a" * 22)
        assert r["changes"]["channel_id"] == "UC" + "a" * 22
        remove_plan(r["plan_id"])

    def test_blocked_operation(self, mp4):
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="1", developer_token="t"),
            safety=SafetyConfig(blocked_operations=["create_youtube_video_upload"]),
        )
        assert "error" in _draft(cfg, mp4)


class TestRequestBuild:
    def test_request_fields(self, real_client, mp4):
        changes, _ = yu.validate_youtube_upload(mp4, "T", "D", "UNLISTED", None)
        req = yu.build_upload_request(real_client, "3865591631", changes)
        assert req.customer_id == "3865591631"
        assert req.you_tube_video_upload.video_title == "T"
        assert req.you_tube_video_upload.video_description == "D"
        assert req.you_tube_video_upload.video_privacy.name == "UNLISTED"
        assert req.you_tube_video_upload.channel_id == ""

    def test_channel_set(self, real_client, mp4):
        ch = "UC" + "b" * 22
        changes, _ = yu.validate_youtube_upload(mp4, "T", "", "UNLISTED", ch)
        req = yu.build_upload_request(real_client, "1", changes)
        assert req.you_tube_video_upload.channel_id == ch


def _mock_client(states, video_id="abcDEF12345"):
    """Client whose GoogleAdsService.search yields one row per state in order."""
    client = MagicMock()
    upload_svc = MagicMock()
    upload_svc.create_you_tube_video_upload.return_value.resource_name = (
        "customers/1/youTubeVideoUploads/9"
    )
    ads_svc = MagicMock()
    seq = iter(states)

    def search(customer_id, query):
        st = next(seq)
        row = MagicMock()
        row.you_tube_video_upload.state.name = st
        row.you_tube_video_upload.video_id = video_id if st == "PROCESSED" else ""
        row.you_tube_video_upload.channel_id = "UCx"
        return [row]

    ads_svc.search.side_effect = search
    client.get_service.side_effect = lambda n: {
        "YouTubeVideoUploadService": upload_svc,
        "GoogleAdsService": ads_svc,
    }[n]
    client.get_type.side_effect = lambda n: MagicMock()
    client.enums.YouTubeVideoPrivacyEnum.UNLISTED = 3
    client.enums.YouTubeVideoPrivacyEnum.PRIVATE = 4
    return client, upload_svc


def _changes(mp4, **kw):
    c, errs = yu.validate_youtube_upload(mp4, "T", "D", kw.get("privacy", "UNLISTED"), None)
    assert not errs
    return c


class TestApply:
    def test_polls_until_processed(self, mp4):
        client, up = _mock_client(["PENDING", "UPLOADED", "PROCESSED"])
        out = yu.apply_create_youtube_video_upload(
            client, "1", _changes(mp4), poll_interval=0, sleep=lambda s: None
        )
        assert out["video_id"] == "abcDEF12345"
        assert out["state"] == "PROCESSED"
        assert out["url"].endswith("abcDEF12345")
        kwargs = up.create_you_tube_video_upload.call_args.kwargs
        assert kwargs["stream"].name == mp4
        assert kwargs["request"] is not None

    @pytest.mark.parametrize("st", ["FAILED", "REJECTED"])
    def test_failure_states_return_error(self, mp4, st):
        client, _ = _mock_client(["PENDING", st])
        out = yu.apply_create_youtube_video_upload(
            client, "1", _changes(mp4), poll_interval=0, sleep=lambda s: None
        )
        assert st in out["error"]
        assert "video_id" not in out

    def test_timeout(self, mp4):
        client, _ = _mock_client(["PENDING"] * 50)
        out = yu.apply_create_youtube_video_upload(
            client, "1", _changes(mp4), poll_timeout=0, poll_interval=0,
            sleep=lambda s: None,
        )
        assert "Timed out" in out["error"]
        assert out["resource_name"].endswith("/9")

    def test_bad_video_id_rejected(self, mp4):
        client, _ = _mock_client(["PROCESSED"], video_id="short")
        out = yu.apply_create_youtube_video_upload(
            client, "1", _changes(mp4), sleep=lambda s: None
        )
        assert "11 chars" in out["error"]

    def test_public_blocked_at_apply(self, mp4):
        client, up = _mock_client(["PROCESSED"])
        ch = _changes(mp4)
        ch["privacy"] = "PUBLIC"
        out = yu.apply_create_youtube_video_upload(client, "1", ch)
        assert out["error"] == "Validation failed"
        up.create_you_tube_video_upload.assert_not_called()


class TestDispatch:
    def test_execute_plan_routes(self, config, mp4, monkeypatch):
        r = _draft(config, mp4)
        plan = get_plan(r["plan_id"])
        called = {}
        monkeypatch.setattr(
            yu,
            "apply_create_youtube_video_upload",
            lambda client, cid, changes: called.update(cid=cid) or {"video_id": "x"},
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda c: object())
        assert _execute_plan(config, plan) == {"video_id": "x"}
        assert called["cid"] == "3865591631"
        remove_plan(r["plan_id"])
