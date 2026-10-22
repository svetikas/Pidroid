from typing import NamedTuple, TypedDict

from discord import ClientUser, Member, User

DiscordUser = ClientUser | Member | User

class ConfigDict(TypedDict):
    debugging: bool
    token: str
    prefixes: list[str]
    postgres_dsn: str
    tt_api_key: str | None
    deepl_api_key: str | None
    tenor_api_key: str | None
    unbelievaboat_api_key: str | None
    rabbitmq_url: str | None

class VersionInfo(NamedTuple):
    major: str
    minor: str
    micro: str
    commit_id: str
