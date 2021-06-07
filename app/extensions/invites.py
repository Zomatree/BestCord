
from app.utils import spec, RequestHandler, JsonErrors

class Invites(RequestHandler):
    async def get(self, invite_code: str):
        with_counts = bool(self.get_query_argument("with_counts", False))
        with_expiration = bool(self.get_query_argument("with_expiration", False))

        invite = await self.database.get_invite(invite_code, with_counts=with_counts, with_expiration=with_expiration)

        self.finish(invite)

    async def delete(self, invite_code: str):
        async with self.database.accqire() as conn:
            invite = await conn.fetchrow(f"delete from guild_invites where code=$1 returning channel_id, guild_id, inviter_id", invite_code)

            if not invite:
                return self.error(JsonErrors.unknown_invite, 404)

            guild = await self.database.get_guild(invite["guild_id"], partial=True)
            channel = await self.database.get_channel(invite["channel_id"], partial=True)
            user = await self.database.get_user(invite["inviter_id"])

        payload = {
            "code": invite_code,
            "guild": guild,
            "channel": channel,
            "inviter": user,
        }

        self.finish(payload)

def setup(app):
    return [(f"/api/v{app.version}/invites/(.+)", Invites, app.args)]
