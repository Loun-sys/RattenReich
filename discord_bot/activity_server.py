from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web


class ActivityServer:
    def __init__(self, database, static_root: Path):
        self.database = database
        self.static_root = static_root
        self.client_id = os.getenv("DISCORD_CLIENT_ID", "") or self._client_id_from_bot_token()
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
        self.tokens: dict[str, tuple[int, float]] = {}
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 128)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/config", self.config)
        app.router.add_post("/api/token", self.token)
        app.router.add_get("/api/character", self.character)
        app.router.add_get("/", self.index)
        if self.static_root.exists():
            app.router.add_static("/assets/", self.static_root / "assets", show_index=False)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = int(os.getenv("PORT", "8080"))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        logging.info("Discord Activity server listening on port %s", port)

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def health(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def config(self, _: web.Request) -> web.Response:
        return web.json_response({"clientId": self.client_id, "configured": bool(self.client_id and self.client_secret)})

    async def index(self, _: web.Request) -> web.StreamResponse:
        index = self.static_root / "index.html"
        if not index.exists():
            raise web.HTTPServiceUnavailable(text="Activity frontend is not built")
        return web.FileResponse(index)

    async def token(self, request: web.Request) -> web.Response:
        if not self.client_id or not self.client_secret:
            raise web.HTTPServiceUnavailable(text="Discord Activity OAuth is not configured")
        payload = await request.json()
        code = str(payload.get("code") or "")
        if not code:
            raise web.HTTPBadRequest(text="Missing authorization code")
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://discord.com/api/oauth2/token", data=form) as response:
                token_data = await response.json()
                if response.status != 200 or not token_data.get("access_token"):
                    logging.warning("Activity OAuth exchange failed with status %s", response.status)
                    raise web.HTTPUnauthorized(text="Discord authorization failed")
            access_token = token_data["access_token"]
            async with session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as response:
                user_data = await response.json()
                if response.status != 200 or not user_data.get("id"):
                    raise web.HTTPUnauthorized(text="Discord user lookup failed")
        expires = time.monotonic() + min(int(token_data.get("expires_in", 3600)), 3600)
        self.tokens[access_token] = (int(user_data["id"]), expires)
        return web.json_response({"access_token": access_token})

    def authorized_user(self, request: web.Request) -> int:
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        record = self.tokens.get(token)
        if not record or record[1] <= time.monotonic():
            self.tokens.pop(token, None)
            raise web.HTTPUnauthorized(text="Activity session expired")
        return record[0]

    async def character(self, request: web.Request) -> web.Response:
        user_id = self.authorized_user(request)
        try:
            guild_id = int(request.query.get("guild_id", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid guild id")
        if not guild_id:
            raise web.HTTPBadRequest(text="Activity must be opened in a server")
        character = await self.database.character(guild_id, user_id)
        if not character:
            raise web.HTTPNotFound(text="Register a character with /регистрация first")
        inventory = await self.database.inventory(character["id"])
        effects = await self.database.active_effects(character["id"])
        equipped = [item for item in inventory if item.get("equipped")]
        return web.json_response({
            "character": {
                "id": character["id"],
                "name": character["name"],
                "surname": character["surname"],
                "race": character["race"],
                "className": character["class_name"],
                "rankIndex": character["rank_index"],
                "attributes": character["attributes"],
                "skills": character["skills"],
                "will": {"current": character["will_current"], "max": character["will_max"]},
                "infection": character["infection"],
                "supplyForms": character["supply_forms"],
                "talents": character.get("talents", {}),
                "injuries": character.get("injuries", []),
            },
            "inventory": inventory,
            "equipped": equipped,
            "effects": effects,
        })