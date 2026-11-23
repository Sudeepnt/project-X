"""X (Twitter) API v2 with user-context OAuth 2.0 tokens. Pay-per-use: links cost more (see platforms)."""

from __future__ import annotations

from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    PublishRequest,
    PublishResult,
    raise_for_status,
)

API = "https://api.x.com/2"


class XAdapter:
    platform = "x"

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.credentials['access_token']}", "Content-Type": "application/json"}

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        parts = [req.text, *req.thread] if req.thread else [req.text]
        first_id: str | None = None
        previous: str | None = None
        for part in parts:
            body: dict[str, object] = {"text": part}
            if previous:
                body["reply"] = {"in_reply_to_tweet_id": previous}
            response = await ctx.http.post(f"{API}/tweets", json=body, headers=self._headers(ctx))
            raise_for_status(response, "X")
            previous = str(response.json()["data"]["id"])
            first_id = first_id or previous
        assert first_id is not None
        handle = ctx.settings.get("handle") or "i"
        return PublishResult(external_id=first_id, url=f"https://x.com/{handle}/status/{first_id}")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(f"{API}/tweets/{external_id}", headers=self._headers(ctx))
        return response.status_code == 200 and "data" in response.json()

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(f"{API}/tweets/{external_id}", headers=self._headers(ctx))
        raise_for_status(response, "X")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for i in range(0, len(external_ids), 100):
            chunk = external_ids[i : i + 100]
            response = await ctx.http.get(
                f"{API}/tweets",
                headers=self._headers(ctx),
                params={"ids": ",".join(chunk), "tweet.fields": "public_metrics"},
            )
            raise_for_status(response, "X")
            for tweet in response.json().get("data", []):
                m = tweet.get("public_metrics", {})
                engagements = sum(
                    int(m.get(k, 0))
                    for k in ("like_count", "reply_count", "retweet_count", "quote_count", "bookmark_count")
                )
                out[str(tweet["id"])] = {
                    "impressions": m.get("impression_count", 0),
                    "reach": m.get("impression_count", 0),
                    "engagements": engagements,
                    "likes": m.get("like_count", 0),
                    "comments": m.get("reply_count", 0),
                    "shares": m.get("retweet_count", 0) + m.get("quote_count", 0),
                    "saves": m.get("bookmark_count", 0),
                }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(
            f"{API}/users/me", headers=self._headers(ctx), params={"user.fields": "public_metrics,profile_image_url"}
        )
        raise_for_status(response, "X")
        user = response.json()["data"]
        return AccountInfo(
            external_id=str(user["id"]),
            name=user.get("name", ""),
            handle=user.get("username"),
            avatar_url=user.get("profile_image_url"),
            followers=(user.get("public_metrics") or {}).get("followers_count"),
        )
