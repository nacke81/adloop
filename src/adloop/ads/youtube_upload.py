"""YouTube video upload via Google Ads YouTubeVideoUploadService.

Flow: draft_youtube_video_upload -> (validate) -> confirm_and_apply.
The service has no validate_only, so the draft preview and request build are
the only pre-flight. Privacy is restricted to UNLISTED or PRIVATE, never PUBLIC.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

MAX_VIDEO_BYTES = 512 * 1024 * 1024  # conservative cap; YouTube itself allows far more
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
ALLOWED_PRIVACY = ("UNLISTED", "PRIVATE")
POLL_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _privacy_enum_values() -> set[str]:
    from google.ads.googleads.v23.enums.types.youtube_video_privacy import (
        YouTubeVideoPrivacyEnum,
    )

    return set(YouTubeVideoPrivacyEnum.YouTubeVideoPrivacy.__members__)


def validate_youtube_upload(
    video_path: str,
    title: str,
    description: str,
    privacy: str,
    channel_id: str | None,
) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    path = os.path.abspath(os.path.expanduser(str(video_path or "")))
    size = 0
    if not video_path or not os.path.isfile(path):
        errors.append(f"video_path does not exist or is not a file: {video_path!r}")
    else:
        if not path.lower().endswith(".mp4"):
            errors.append("video_path must be an .mp4 file")
        size = os.path.getsize(path)
        if size == 0:
            errors.append("video file is empty")
        elif size > MAX_VIDEO_BYTES:
            errors.append(
                f"video is {size} bytes, over the {MAX_VIDEO_BYTES} byte cap"
            )
    title = (title or "").strip()
    description = description or ""
    if not title:
        errors.append("title is required")
    if len(title) > MAX_TITLE_CHARS:
        errors.append(f"title exceeds {MAX_TITLE_CHARS} characters")
    if len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(f"description exceeds {MAX_DESCRIPTION_CHARS} characters")
    if any(c in title + description for c in "<>"):
        errors.append("title and description cannot contain < or >")
    priv = str(privacy or "").strip().upper()
    if priv not in ALLOWED_PRIVACY:
        errors.append(
            f"privacy must be one of {list(ALLOWED_PRIVACY)}; PUBLIC is never allowed"
        )
    elif priv not in _privacy_enum_values():
        errors.append(
            f"privacy {priv} is not supported by the pinned Google Ads API version; use UNLISTED"
        )
    channel = (channel_id or "").strip() or None
    if channel and not _CHANNEL_ID_RE.match(channel):
        errors.append("channel_id must be a 24-character id starting with UC")
    if errors:
        return None, errors
    return {
        "video_path": path,
        "file_size_bytes": size,
        "title": title,
        "description": description,
        "privacy": priv,
        "channel_id": channel,
    }, []


def draft_youtube_video_upload(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    video_path: str = "",
    title: str = "",
    description: str = "",
    privacy: str = "UNLISTED",
    channel_id: str | None = None,
) -> dict:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_youtube_video_upload", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    changes, errors = validate_youtube_upload(
        video_path, title, description, privacy, channel_id
    )
    if errors:
        return {"error": "Validation failed", "details": errors}
    plan = ChangePlan(
        operation="create_youtube_video_upload",
        entity_type="youtube_video_upload",
        entity_id=os.path.basename(changes["video_path"]),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["note"] = (
        "YouTubeVideoUploadService has no validate_only. Applying uploads the file "
        "to YouTube (channel: "
        + (changes["channel_id"] or "Google-managed channel for the Ads account")
        + ") and polls until PROCESSED."
    )
    return preview


def build_upload_request(client, cid: str, changes: dict):
    """Build the CreateYouTubeVideoUploadRequest (no network)."""
    req = client.get_type("CreateYouTubeVideoUploadRequest")
    req.customer_id = cid
    up = req.you_tube_video_upload
    up.video_title = changes["title"]
    if changes.get("description"):
        up.video_description = changes["description"]
    up.video_privacy = getattr(client.enums.YouTubeVideoPrivacyEnum, changes["privacy"])
    if changes.get("channel_id"):
        up.channel_id = changes["channel_id"]
    return req


def _query_upload(client, cid: str, resource_name: str) -> dict | None:
    svc = client.get_service("GoogleAdsService")
    query = (
        "SELECT you_tube_video_upload.resource_name, you_tube_video_upload.state, "
        "you_tube_video_upload.video_id, you_tube_video_upload.channel_id "
        f"FROM you_tube_video_upload WHERE you_tube_video_upload.resource_name = '{resource_name}'"
    )
    for row in svc.search(customer_id=cid, query=query):
        u = row.you_tube_video_upload
        return {
            "state": getattr(u.state, "name", str(u.state)),
            "video_id": u.video_id,
            "channel_id": u.channel_id,
        }
    return None


def apply_create_youtube_video_upload(
    client,
    cid: str,
    changes: dict,
    *,
    poll_timeout: float = POLL_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> dict:
    # Re-check at apply time: file may have changed since the draft.
    _, errors = validate_youtube_upload(
        changes["video_path"],
        changes["title"],
        changes.get("description", ""),
        changes["privacy"],
        changes.get("channel_id"),
    )
    if errors:
        return {"error": "Validation failed", "details": errors}
    req = build_upload_request(client, cid, changes)
    svc = client.get_service("YouTubeVideoUploadService")
    with open(changes["video_path"], "rb") as fh:
        resp = svc.create_you_tube_video_upload(request=req, stream=fh)
    rn = resp.resource_name
    deadline = time.monotonic() + poll_timeout
    last: dict | None = None
    while True:
        last = _query_upload(client, cid, rn)
        state = (last or {}).get("state")
        if state == "PROCESSED":
            vid = last.get("video_id", "")
            if not _VIDEO_ID_RE.match(vid or ""):
                return {
                    "error": f"PROCESSED but video_id is not 11 chars: {vid!r}",
                    "resource_name": rn,
                    "state": state,
                }
            return {
                "resource_name": rn,
                "state": state,
                "video_id": vid,
                "channel_id": last.get("channel_id", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        if state in ("FAILED", "REJECTED", "UNAVAILABLE"):
            return {
                "error": f"YouTube upload ended in state {state}",
                "resource_name": rn,
                "state": state,
            }
        if time.monotonic() >= deadline:
            return {
                "error": f"Timed out after {poll_timeout}s waiting for PROCESSED "
                f"(last state {state}). Re-query the resource to continue.",
                "resource_name": rn,
                "state": state,
            }
        sleep(poll_interval)
