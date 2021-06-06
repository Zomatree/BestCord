from __future__ import annotations

from tornado.web import Application, StaticFileHandler, RedirectHandler, RequestHandler
import importlib
import asyncio
import logging
from typing import Any, Protocol, runtime_checkable, Literal, get_args
import contextlib

from .utils import DB, TornadoUvloop, Tokens
from .extensions.gateway import Gateway

@runtime_checkable
class ExtensionProtocol(Protocol):
    @staticmethod
    def setup(app: App) -> list[Any]:
        raise NotImplementedError

destination_keys = Literal["guild", "channel"]
logger: logging.Logger = logging.getLogger()

class NotFound(RequestHandler):
    def get(self, *_):
        self.set_status(404)
        self.write('{"message": "404: not found", "code": 0}')
    
    post = get
    delete = get
    patch = get
    put = get
    options = get
    head = get

class App(Application):
    def __init__(self, database: DB, config: dict[str, Any]):
        self.database = database
        self.config = config
        self.version = config["version"]

        token_config: dict[str, Any] = config["tokens"]
        self.tokens = Tokens(token_config["epoch"], token_config["worker_id"], token_config["process_id"], token_config["secret_key"], token_config["invite_length"])

        self.args = {"database": self.database, "tokens": self.tokens}
        
        self.gateway_connections: dict[str, Gateway] = {}  # userid -> gateway
        self.destinations: dict[destination_keys, dict[str, list[str]]] = {}  # type -> id -> userid[]
        self.member_cache: dict[str, dict[str, dict]] = {}  # guildid -> userid -> user
        self.user_cache: dict[str, dict] = {}  # userid -> user

        for key in get_args(destination_keys):
            self.destinations[key] = {}

        files: list[str] = config["extensions"]
        routes = []

        for file in files:
            module_path = f"app.extensions.{file}"
            module = importlib.import_module(module_path)
            if not isinstance(module, ExtensionProtocol):
                raise Exception(f"'{module_path}' is missing a setup function")
            
            module_routes = module.setup(self)
            
            routes.extend(module_routes)

        routes.append(("/", RedirectHandler, {"url": "/index.html"}))
        routes.append((f"/api/v{self.version}/(.*)", NotFound))
        routes.append(("/(.+)", StaticFileHandler, {"path": "./app/static"}))
        for route in routes:
            logging.info(f"Adding {route[0]}")

        super().__init__(routes)

    @classmethod
    def run(cls, config: dict[str, Any], log_level: str):
        logger.setLevel(getattr(logging, log_level.upper()))

        TornadoUvloop.current().make_current()
        loop = asyncio.get_event_loop()
        
        db = loop.run_until_complete(DB.from_args(config["database"]))
        server = cls(db, config)
        
        loop.run_until_complete(server.startup())
       
        server.listen(config["app"]["port"], config["app"]["address"])
        
        print(f"running at http://{config['app']['address']}:{config['app']['port']}")
        TornadoUvloop.current().start()

    def dispatch_event(self, event_name: str, payload: dict[str, Any], *, index: str, index_type: destination_keys):
        users = self.destinations[index_type].get(index)

        if users is None:
            logging.debug(f"Ignoring event %s with index %s:%s", event_name, index, index_type)
            return

        for id in users:
            with contextlib.suppress(KeyError):  # they are not online - ignore them
                self.gateway_connections[id].push_event(event_name, payload)

    async def fill_destinations(self):
        async with self.database.accqire() as conn:
            members = await conn.fetch("select user_id, guild_id from guild_members")
            channels = await conn.fetch("select id, guild_id from guild_channels")

        for member in members:
            self.destinations["guild"].setdefault(member["guild_id"], []).append(member["user_id"])

        for channel in channels:
            self.destinations["channel"][channel["id"]] = self.destinations["guild"][channel["guild_id"]]  # ill switch this to permissions when i implement them, until then its the same as guilds

    async def fill_member_cache(self):
        async with self.database.accqire() as conn:
            rows = await conn.fetch("select user_id, guild_members.guild_id, joined_at, deaf, mute, nick, username, discriminator, avatar from guild_members inner join users on guild_members.user_id = users.id")

        for row in rows:
            user = {
                "username": row["username"],
                "discriminator": row["discriminator"],
                "id": row["user_id"],
                "avatar": row["avatar"]
            }

            member = {
                "id": row["user_id"],
                "nick": row["nick"],
                "mute": row["mute"],
                "deaf": row["deaf"],
                "joined_at": row["joined_at"],
                "roles": []
            }

            self.user_cache[row["user_id"]] = user
            self.member_cache.setdefault(row["guild_id"], {})[user["id"]] = member

    async def startup(self):
        await self.fill_destinations()
        await self.fill_member_cache()
