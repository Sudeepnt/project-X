"""Meta: Facebook Pages, Instagram (professional accounts) and Threads, through the Graph APIs.
All three need Meta app review before posting for customers (accounts added as app roles can
post during development)."""

from __future__ import annotations

import asyncio
from typing import Any

from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    AdapterError,
    PlatformRejected,
    PublishRequest,
    PublishResult,
    raise_for_status,
)

GRAPH_VERSION = "v24.0"


def _graph(ctx: AdapterContext) -> str:
    return f"https://graph.facebook.com/{ctx.settings.get('graph_version', GRAPH_VERSION)}"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class FacebookPageAdapter:
    platform = "facebook"

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        token = ctx.credentials["page_access_token"]
        page = ctx.account_external_id
        image = next((m["url"] for m in req.media if m.get("kind") == "image"), None)
        if image:
            response = await ctx.http.post(
                f"{_graph(ctx)}/{page}/photos", data={"url": image, "caption": req.text, "access_token": token}
            )
        else:
            response = await ctx.http.post(
                f"{_graph(ctx)}/{page}/feed", data={"message": req.text, "access_token": token}
            )
        raise_for_status(response, "Facebook")
        data = response.json()
        post_id = str(data.get("post_id") or data["id"])
        return PublishResult(external_id=post_id, url=f"https://www.facebook.com/{post_id}")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(
            f"{_graph(ctx)}/{external_id}",
            params={"fields": "id", "access_token": ctx.credentials["page_access_token"]},
        )
        return response.status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(
            f"{_graph(ctx)}/{external_id}", params={"access_token": ctx.credentials["page_access_token"]}
        )
        raise_for_status(response, "Facebook")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        fields = "shares,comments.summary(true).limit(0),reactions.summary(true).limit(0)"
        for external_id in external_ids:
            response = await ctx.http.get(
                f"{_graph(ctx)}/{external_id}",
                params={"fields": fields, "access_token": ctx.credentials["page_access_token"]},
            )
            if response.status_code != 200:
                continue
            d = response.json()
            likes = _num((d.get("reactions") or {}).get("summary", {}).get("total_count"))
            comments = _num((d.get("comments") or {}).get("summary", {}).get("total_count"))
            shares = _num((d.get("shares") or {}).get("count"))
            out[external_id] = {
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "engagements": likes + comments + shares,
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            f"{_graph(ctx)}/{ctx.account_external_id}",
            params={"fields": "id,name,followers_count,picture", "access_token": ctx.credentials["page_access_token"]},
        )
        raise_for_status(response, "Facebook")
        d = response.json()
        return AccountInfo(
            external_id=str(d["id"]),
            name=d.get("name", "Page"),
            followers=d.get("followers_count"),
            avatar_url=((d.get("picture") or {}).get("data") or {}).get("url"),
        )


class InstagramAdapter:
    platform = "instagram"

    async def _container(self, ctx: AdapterContext, params: dict[str, Any]) -> str:
        response = await ctx.http.post(
            f"{_graph(ctx)}/{ctx.account_external_id}/media",
            data={**params, "access_token": ctx.credentials["access_token"]},
        )
        raise_for_status(response, "Instagram")
        return str(response.json()["id"])

    async def _wait_ready(self, ctx: AdapterContext, container_id: str, *, attempts: int = 30) -> None:
        for _ in range(attempts):
            response = await ctx.http.get(
                f"{_graph(ctx)}/{container_id}",
                params={"fields": "status_code", "access_token": ctx.credentials["access_token"]},
            )
            raise_for_status(response, "Instagram")
            status = response.json().get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PlatformRejected(f"Instagram could not process the media ({status})")
            await asyncio.sleep(2)
        raise AdapterError("Instagram is still processing the video", retry_after=120)

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        if not req.media:
            raise PlatformRejected("Instagram posts need an image or a video")
        caption = req.text
        if req.format == "carousel" and len(req.media) > 1:
            children = []
            for m in req.media[:10]:
                key = "video_url" if m.get("kind") == "video" else "image_url"
                params = {key: m["url"], "is_carousel_item": "true"}
                if key == "video_url":
                    params["media_type"] = "VIDEO"
                children.append(await self._container(ctx, params))
            for child in children:
                await self._wait_ready(ctx, child)
            container = await self._container(
                ctx, {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
            )
        elif req.media[0].get("kind") == "video":
            media_type = "STORIES" if req.format == "story" else "REELS"
            container = await self._container(
                ctx, {"media_type": media_type, "video_url": req.media[0]["url"], "caption": caption}
            )
        else:
            params = {"image_url": req.media[0]["url"], "caption": caption}
            if req.format == "story":
                params["media_type"] = "STORIES"
            container = await self._container(ctx, params)
        await self._wait_ready(ctx, container)
        response = await ctx.http.post(
            f"{_graph(ctx)}/{ctx.account_external_id}/media_publish",
            data={"creation_id": container, "access_token": ctx.credentials["access_token"]},
        )
        raise_for_status(response, "Instagram")
        media_id = str(response.json()["id"])
        link = await ctx.http.get(
            f"{_graph(ctx)}/{media_id}", params={"fields": "permalink", "access_token": ctx.credentials["access_token"]}
        )
        url = link.json().get("permalink") if link.status_code == 200 else None
        return PublishResult(external_id=media_id, url=url)

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(
            f"{_graph(ctx)}/{external_id}",
            params={"fields": "id,permalink", "access_token": ctx.credentials["access_token"]},
        )
        return response.status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(
            f"{_graph(ctx)}/{external_id}", params={"access_token": ctx.credentials["access_token"]}
        )
        if response.status_code >= 400:
            raise PlatformRejected("Instagram didn't allow deleting this post through the API; delete it in the app")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for external_id in external_ids:
            response = await ctx.http.get(
                f"{_graph(ctx)}/{external_id}/insights",
                params={
                    "metric": "reach,likes,comments,shares,saved,total_interactions,views",
                    "access_token": ctx.credentials["access_token"],
                },
            )
            if response.status_code != 200:
                continue
            values = {
                row["name"]: _num((row.get("values") or [{}])[0].get("value"))
                for row in response.json().get("data", [])
            }
            out[external_id] = {
                "reach": values.get("reach", 0),
                "impressions": values.get("views", 0),
                "likes": values.get("likes", 0),
                "comments": values.get("comments", 0),
                "shares": values.get("shares", 0),
                "saves": values.get("saved", 0),
                "engagements": values.get("total_interactions", 0),
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            f"{_graph(ctx)}/{ctx.account_external_id}",
            params={
                "fields": "id,username,name,followers_count,profile_picture_url",
                "access_token": ctx.credentials["access_token"],
            },
        )
        raise_for_status(response, "Instagram")
        d = response.json()
        return AccountInfo(
            external_id=str(d["id"]),
            name=d.get("name") or d.get("username", "Instagram"),
            handle=d.get("username"),
            followers=d.get("followers_count"),
            avatar_url=d.get("profile_picture_url"),
        )


class ThreadsAdapter:
    platform = "threads"
    API = "https://graph.threads.net/v1.0"

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        token = ctx.credentials["access_token"]
        user = ctx.account_external_id
        image = next((m["url"] for m in req.media if m.get("kind") == "image"), None)
        params: dict[str, Any] = {"media_type": "IMAGE" if image else "TEXT", "text": req.text, "access_token": token}
        if image:
            params["image_url"] = image
        response = await ctx.http.post(f"{self.API}/{user}/threads", data=params)
        raise_for_status(response, "Threads")
        container = response.json()["id"]
        await asyncio.sleep(1)
        published = await ctx.http.post(
            f"{self.API}/{user}/threads_publish", data={"creation_id": container, "access_token": token}
        )
        raise_for_status(published, "Threads")
        thread_id = str(published.json()["id"])
        previous = thread_id
        for part in req.thread:
            reply = await ctx.http.post(
                f"{self.API}/{user}/threads",
                data={"media_type": "TEXT", "text": part, "reply_to_id": previous, "access_token": token},
            )
            raise_for_status(reply, "Threads")
            done = await ctx.http.post(
                f"{self.API}/{user}/threads_publish", data={"creation_id": reply.json()["id"], "access_token": token}
            )
            raise_for_status(done, "Threads")
            previous = str(done.json()["id"])
        link = await ctx.http.get(f"{self.API}/{thread_id}", params={"fields": "permalink", "access_token": token})
        return PublishResult(
            external_id=thread_id, url=link.json().get("permalink") if link.status_code == 200 else None
        )

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(
            f"{self.API}/{external_id}", params={"fields": "id", "access_token": ctx.credentials["access_token"]}
        )
        return response.status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(
            f"{self.API}/{external_id}", params={"access_token": ctx.credentials["access_token"]}
        )
        raise_for_status(response, "Threads")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for external_id in external_ids:
            response = await ctx.http.get(
                f"{self.API}/{external_id}/insights",
                params={
                    "metric": "views,likes,replies,reposts,quotes",
                    "access_token": ctx.credentials["access_token"],
                },
            )
            if response.status_code != 200:
                continue
            v = {
                row["name"]: _num((row.get("values") or [{}])[0].get("value"))
                for row in response.json().get("data", [])
            }
            engagements = v.get("likes", 0) + v.get("replies", 0) + v.get("reposts", 0) + v.get("quotes", 0)
            out[external_id] = {
                "impressions": v.get("views", 0),
                "reach": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("replies", 0),
                "shares": v.get("reposts", 0) + v.get("quotes", 0),
                "engagements": engagements,
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            f"{self.API}/me",
            params={
                "fields": "id,username,name,threads_profile_picture_url",
                "access_token": ctx.credentials["access_token"],
            },
        )
        raise_for_status(response, "Threads")
        d = response.json()
        return AccountInfo(
            external_id=str(d["id"]),
            name=d.get("name") or d.get("username", "Threads"),
            handle=d.get("username"),
            avatar_url=d.get("threads_profile_picture_url"),
        )
