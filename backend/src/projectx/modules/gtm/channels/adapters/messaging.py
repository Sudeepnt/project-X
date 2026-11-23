"""Telegram channels (Bot API) and Discord channels (webhooks). Both work instantly, no review."""

from __future__ import annotations

from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    AdapterError,
    PublishRequest,
    PublishResult,
    raise_for_status,
)


class TelegramAdapter:
    platform = "telegram"

    def _url(self, ctx: AdapterContext, method: str) -> str:
        return f"https://api.telegram.org/bot{ctx.credentials['bot_token']}/{method}"

    async def _call(self, ctx: AdapterContext, method: str, payload: dict[str, object]) -> dict[str, object]:
        response = await ctx.http.post(self._url(ctx, method), json=payload)
        data = response.json() if response.content else {}
        if response.status_code == 429:
            retry = (data.get("parameters") or {}).get("retry_after", 30) if isinstance(data, dict) else 30
            raise AdapterError("Telegram rate limit", retry_after=float(retry))
        raise_for_status(response, "Telegram")
        if not data.get("ok"):
            raise AdapterError(f"Telegram: {data.get('description')}", retryable=False)
        result = data.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        chat_id = ctx.settings["chat_id"]
        text = "\n\n".join([req.text, *req.thread]) if req.thread else req.text
        image = next((m["url"] for m in req.media if m.get("kind") == "image"), None)
        if image:
            result = await self._call(ctx, "sendPhoto", {"chat_id": chat_id, "photo": image, "caption": text[:1024]})
        else:
            result = await self._call(ctx, "sendMessage", {"chat_id": chat_id, "text": text[:4096]})
        message_id = result["message_id"]
        username = ctx.settings.get("username")
        return PublishResult(
            external_id=f"{chat_id}:{message_id}", url=f"https://t.me/{username}/{message_id}" if username else None
        )

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        # The Bot API cannot read a channel message back; the send response carries the message id
        # assigned by Telegram, which is the platform's own record of the post.
        return ":" in external_id

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        chat_id, message_id = external_id.rsplit(":", 1)
        await self._call(ctx, "deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        return {}  # view counts are not exposed to bots

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        chat = await self._call(ctx, "getChat", {"chat_id": ctx.settings["chat_id"]})
        count = await self._call(ctx, "getChatMemberCount", {"chat_id": ctx.settings["chat_id"]})
        return AccountInfo(
            external_id=str(chat["id"]),
            name=str(chat.get("title") or chat.get("username") or "Channel"),
            handle=str(chat["username"]) if chat.get("username") else None,
            followers=int(str(count.get("value", 0) or 0)),
        )


class DiscordAdapter:
    platform = "discord"

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        content = "\n\n".join([req.text, *req.thread]) if req.thread else req.text
        response = await ctx.http.post(
            ctx.credentials["webhook_url"],
            params={"wait": "true"},
            json={"content": content[:2000], "allowed_mentions": {"parse": []}},
        )
        raise_for_status(response, "Discord")
        message = response.json()
        guild = ctx.settings.get("guild_id")
        url = f"https://discord.com/channels/{guild}/{message['channel_id']}/{message['id']}" if guild else None
        return PublishResult(external_id=str(message["id"]), url=url)

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        response = await ctx.http.get(f"{ctx.credentials['webhook_url']}/messages/{external_id}")
        return response.status_code == 200

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        response = await ctx.http.delete(f"{ctx.credentials['webhook_url']}/messages/{external_id}")
        if response.status_code != 404:
            raise_for_status(response, "Discord")

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        return {}

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        response = await ctx.http.get(ctx.credentials["webhook_url"])
        raise_for_status(response, "Discord")
        hook = response.json()
        return AccountInfo(
            external_id=str(hook["channel_id"]),
            name=hook.get("name") or "Discord channel",
            capabilities={"guild_id": hook.get("guild_id")},
        )
