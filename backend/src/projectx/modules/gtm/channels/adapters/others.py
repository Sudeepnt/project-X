"""LinkedIn (Posts API), Pinterest (v5), TikTok (Content Posting API), YouTube (Data API v3) and
Ayrshare (a unified social API used to launch to many customers before our own app approvals)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    AdapterError,
    PlatformRejected,
    PublishRequest,
    PublishResult,
    raise_for_status,
)


class LinkedInAdapter:
    platform = "linkedin"
    API = "https://api.linkedin.com/rest"

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ctx.credentials['access_token']}",
            "LinkedIn-Version": str(ctx.settings.get("api_version", "202510")),
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _author(self, ctx: AdapterContext) -> str:
        return str(ctx.settings.get("organization_urn") or f"urn:li:person:{ctx.account_external_id}")

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        body = {
            "author": self._author(ctx),
            "commentary": req.text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        response = await ctx.http.post(f"{self.API}/posts", json=body, headers=self._headers(ctx))
        raise_for_status(response, "LinkedIn")
        urn = response.headers.get("x-restli-id") or response.headers.get("x-linkedin-id") or ""
        if not urn:
            raise AdapterError("LinkedIn didn't return a post id", retryable=False)
        return PublishResult(external_id=urn, url=f"https://www.linkedin.com/feed/update/{urn}")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(f"{self.API}/posts/{quote(external_id, safe='')}", headers=self._headers(ctx))
        return response.status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(f"{self.API}/posts/{quote(external_id, safe='')}", headers=self._headers(ctx))
        if response.status_code != 404:
            raise_for_status(response, "LinkedIn")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for urn in external_ids:
            response = await ctx.http.get(f"{self.API}/socialActions/{quote(urn, safe='')}", headers=self._headers(ctx))
            if response.status_code != 200:
                continue
            d = response.json()
            likes = float((d.get("likesSummary") or {}).get("totalLikes", 0))
            comments = float((d.get("commentsSummary") or {}).get("aggregatedTotalComments", 0))
            out[urn] = {"likes": likes, "comments": comments, "engagements": likes + comments}
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {ctx.credentials['access_token']}"},
        )
        raise_for_status(response, "LinkedIn")
        d = response.json()
        return AccountInfo(external_id=str(d["sub"]), name=d.get("name", "LinkedIn"), avatar_url=d.get("picture"))


class PinterestAdapter:
    platform = "pinterest"
    API = "https://api.pinterest.com/v5"

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.credentials['access_token']}", "Content-Type": "application/json"}

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        image = next((m["url"] for m in req.media if m.get("kind") == "image"), None)
        if image is None:
            raise PlatformRejected("Pins need an image")
        board = ctx.settings.get("board_id")
        if not board:
            raise PlatformRejected("Choose a Pinterest board in the account settings")
        body = {
            "board_id": board,
            "title": (req.title or "")[:100],
            "description": req.text[:500],
            "media_source": {"source_type": "image_url", "url": image},
        }
        if ctx.settings.get("link"):
            body["link"] = ctx.settings["link"]
        response = await ctx.http.post(f"{self.API}/pins", json=body, headers=self._headers(ctx))
        raise_for_status(response, "Pinterest")
        pin_id = str(response.json()["id"])
        return PublishResult(external_id=pin_id, url=f"https://www.pinterest.com/pin/{pin_id}/")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        return (await ctx.http.get(f"{self.API}/pins/{external_id}", headers=self._headers(ctx))).status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(f"{self.API}/pins/{external_id}", headers=self._headers(ctx))
        if response.status_code != 404:
            raise_for_status(response, "Pinterest")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        end = datetime.now(UTC).date()
        for pin in external_ids:
            response = await ctx.http.get(
                f"{self.API}/pins/{pin}/analytics",
                headers=self._headers(ctx),
                params={
                    "start_date": (end - timedelta(days=30)).isoformat(),
                    "end_date": end.isoformat(),
                    "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK",
                },
            )
            if response.status_code != 200:
                continue
            summary = (response.json().get("all") or {}).get("summary_metrics", {})
            out[pin] = {
                "impressions": summary.get("IMPRESSION", 0),
                "reach": summary.get("IMPRESSION", 0),
                "saves": summary.get("SAVE", 0),
                "clicks": summary.get("OUTBOUND_CLICK", 0),
                "engagements": summary.get("SAVE", 0) + summary.get("PIN_CLICK", 0),
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(f"{self.API}/user_account", headers=self._headers(ctx))
        raise_for_status(response, "Pinterest")
        d = response.json()
        return AccountInfo(
            external_id=str(d.get("id") or d.get("username")),
            name=d.get("username", "Pinterest"),
            handle=d.get("username"),
            followers=d.get("follower_count"),
            avatar_url=d.get("profile_image"),
        )


class TikTokAdapter:
    platform = "tiktok"
    API = "https://open.tiktokapis.com/v2"

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ctx.credentials['access_token']}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        video = next((m["url"] for m in req.media if m.get("kind") == "video"), None)
        if video is None:
            raise PlatformRejected("TikTok posts need a video")
        # Unaudited apps may only post privately (SELF_ONLY).
        privacy = ctx.settings.get("privacy_level") or (
            "PUBLIC_TO_EVERYONE" if ctx.settings.get("audited") else "SELF_ONLY"
        )
        body = {
            "post_info": {
                "title": req.text[:2200],
                "privacy_level": privacy,
                "disable_comment": False,
                "is_aigc": req.ai_label == "api_flag",
            },
            "source_info": {"source": "PULL_FROM_URL", "video_url": video},
        }
        response = await ctx.http.post(f"{self.API}/post/publish/video/init/", json=body, headers=self._headers(ctx))
        raise_for_status(response, "TikTok")
        publish_id = response.json()["data"]["publish_id"]
        for _ in range(30):
            await asyncio.sleep(3)
            status = await ctx.http.post(
                f"{self.API}/post/publish/status/fetch/", json={"publish_id": publish_id}, headers=self._headers(ctx)
            )
            raise_for_status(status, "TikTok")
            data = status.json().get("data", {})
            if data.get("status") == "PUBLISH_COMPLETE":
                ids = data.get("publicaly_available_post_id") or []
                post_id = str(ids[0]) if ids else publish_id
                return PublishResult(external_id=post_id, extra={"publish_id": publish_id})
            if data.get("status") == "FAILED":
                raise PlatformRejected(f"TikTok failed the upload: {data.get('fail_reason')}")
        raise AdapterError("TikTok is still processing", retry_after=300)

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.post(
            f"{self.API}/video/query/",
            params={"fields": "id"},
            headers=self._headers(ctx),
            json={"filters": {"video_ids": [external_id]}},
        )
        return response.status_code == 200 and bool(response.json().get("data", {}).get("videos"))

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        raise PlatformRejected("TikTok doesn't allow deleting videos through the API; delete it in the app")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        response = await ctx.http.post(
            f"{self.API}/video/query/",
            headers=self._headers(ctx),
            params={"fields": "id,view_count,like_count,comment_count,share_count"},
            json={"filters": {"video_ids": external_ids[:20]}},
        )
        if response.status_code != 200:
            return {}
        out: dict[str, dict[str, float]] = {}
        for v in response.json().get("data", {}).get("videos", []):
            engagements = v.get("like_count", 0) + v.get("comment_count", 0) + v.get("share_count", 0)
            out[str(v["id"])] = {
                "impressions": v.get("view_count", 0),
                "reach": v.get("view_count", 0),
                "likes": v.get("like_count", 0),
                "comments": v.get("comment_count", 0),
                "shares": v.get("share_count", 0),
                "engagements": engagements,
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            f"{self.API}/user/info/",
            headers=self._headers(ctx),
            params={"fields": "open_id,display_name,avatar_url,follower_count"},
        )
        raise_for_status(response, "TikTok")
        u = response.json().get("data", {}).get("user", {})
        return AccountInfo(
            external_id=str(u.get("open_id")),
            name=u.get("display_name", "TikTok"),
            avatar_url=u.get("avatar_url"),
            followers=u.get("follower_count"),
        )


class YouTubeAdapter:
    platform = "youtube"
    MAX_BYTES = 512 * 1024 * 1024

    def _auth(self, ctx: AdapterContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.credentials['access_token']}"}

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        video = next((m["url"] for m in req.media if m.get("kind") == "video"), None)
        if video is None:
            raise PlatformRejected("YouTube uploads need a video")
        title = (req.title or req.text.split("\n")[0])[:100]
        if req.format == "vertical_video" and "#shorts" not in title.lower():
            title = f"{title[:91]} #Shorts"
        metadata = {
            "snippet": {"title": title, "description": req.text[:5000]},
            "status": {
                "privacyStatus": ctx.settings.get("privacy", "public"),
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": req.ai_label == "api_flag",
            },
        }
        source = await ctx.http.get(video)
        raise_for_status(source, "Media host")
        if len(source.content) > self.MAX_BYTES:
            raise PlatformRejected("The video is larger than 512 MB")
        start = await ctx.http.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers={
                **self._auth(ctx),
                "X-Upload-Content-Type": source.headers.get("content-type", "video/mp4"),
                "X-Upload-Content-Length": str(len(source.content)),
            },
        )
        raise_for_status(start, "YouTube")
        upload = await ctx.http.put(
            start.headers["location"],
            content=source.content,
            headers={"Content-Type": source.headers.get("content-type", "video/mp4")},
            timeout=600,
        )
        raise_for_status(upload, "YouTube")
        video_id = str(upload.json()["id"])
        return PublishResult(external_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers=self._auth(ctx),
            params={"part": "id", "id": external_id},
        )
        return response.status_code == 200 and bool(response.json().get("items"))

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(
            "https://www.googleapis.com/youtube/v3/videos", headers=self._auth(ctx), params={"id": external_id}
        )
        if response.status_code != 404:
            raise_for_status(response, "YouTube")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        response = await ctx.http.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers=self._auth(ctx),
            params={"part": "statistics", "id": ",".join(external_ids[:50])},
        )
        if response.status_code != 200:
            return {}
        out: dict[str, dict[str, float]] = {}
        for item in response.json().get("items", []):
            s = {k: float(v) for k, v in item.get("statistics", {}).items()}
            out[str(item["id"])] = {
                "impressions": s.get("viewCount", 0),
                "reach": s.get("viewCount", 0),
                "likes": s.get("likeCount", 0),
                "comments": s.get("commentCount", 0),
                "engagements": s.get("likeCount", 0) + s.get("commentCount", 0),
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            "https://www.googleapis.com/youtube/v3/channels",
            headers=self._auth(ctx),
            params={"part": "snippet,statistics", "mine": "true"},
        )
        raise_for_status(response, "YouTube")
        items = response.json().get("items") or []
        if not items:
            raise PlatformRejected("This Google account has no YouTube channel")
        c = items[0]
        return AccountInfo(
            external_id=str(c["id"]),
            name=c["snippet"]["title"],
            avatar_url=((c["snippet"].get("thumbnails") or {}).get("default") or {}).get("url"),
            followers=int(c.get("statistics", {}).get("subscriberCount", 0) or 0),
        )


class AyrshareAdapter:
    """One adapter for many platforms through Ayrshare profiles (multi-customer launch mode)."""

    API = "https://api.ayrshare.com/api"

    def __init__(self, platform: str, api_key: str) -> None:
        self.platform = platform
        self._key = api_key

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Profile-Key": str(ctx.credentials["profile_key"]),
            "Content-Type": "application/json",
        }

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        body: dict[str, Any] = {"post": req.text, "platforms": [self.platform if self.platform != "x" else "twitter"]}
        if req.media:
            body["mediaUrls"] = [m["url"] for m in req.media]
        response = await ctx.http.post(f"{self.API}/post", json=body, headers=self._headers(ctx))
        raise_for_status(response, "Ayrshare")
        data = response.json()
        ids = data.get("postIds") or [{}]
        return PublishResult(external_id=str(data.get("id")), url=ids[0].get("postUrl"), extra={"post_ids": ids})

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(f"{self.API}/post/{external_id}", headers=self._headers(ctx))
        return response.status_code == 200 and response.json().get("status") in ("success", "posted", None)

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.request(
            "DELETE", f"{self.API}/post", json={"id": external_id}, headers=self._headers(ctx)
        )
        raise_for_status(response, "Ayrshare")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for external_id in external_ids:
            response = await ctx.http.post(
                f"{self.API}/analytics/post", json={"id": external_id}, headers=self._headers(ctx)
            )
            if response.status_code != 200:
                continue
            platform_data = response.json().get(self.platform if self.platform != "x" else "twitter", {})
            analytics = platform_data.get("analytics", {}) if isinstance(platform_data, dict) else {}
            likes = float(analytics.get("likeCount", analytics.get("likes", 0)) or 0)
            comments = float(analytics.get("commentCount", analytics.get("comments", 0)) or 0)
            out[external_id] = {
                "likes": likes,
                "comments": comments,
                "engagements": likes + comments,
                "impressions": float(analytics.get("impressionCount", 0) or 0),
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(f"{self.API}/user", headers=self._headers(ctx))
        raise_for_status(response, "Ayrshare")
        d = response.json()
        return AccountInfo(
            external_id=str(ctx.credentials["profile_key"])[:40],
            name=d.get("title") or "Ayrshare profile",
            capabilities={"active_platforms": d.get("activeSocialAccounts", [])},
        )
