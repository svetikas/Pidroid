from __future__ import annotations

import datetime

from discord import Message, Member, Guild
from typing import TYPE_CHECKING, Any

from pidroid.models.tags import Tag
from pidroid.models.guild_configuration import GuildConfiguration
from pidroid.models.accounts import TheoTownAccount
from pidroid.models.translation import TranslationEntryDict
from pidroid.modules.moderation.models.case import Case
from pidroid.modules.moderation.models.types import PunishmentType
from pidroid.utils.db.expiring_thread import ExpiringThread
from pidroid.utils.db.guild_configuration import GuildConfigurationTable
from pidroid.utils.db.levels import LevelRewards, UserLevels
from pidroid.utils.db.linked_account import LinkedAccount
from pidroid.utils.db.moderation import GuildCaseCounter, ModerationCase, ActivePunishment, RevocationData
from pidroid.utils.db.punishment import PunishmentTable
from pidroid.utils.db.reminder import Reminder
from pidroid.utils.db.tag import TagTable
from pidroid.utils.db.translation import Translation
from pidroid.utils.http import HTTP, APIResponse, Route
from pidroid.utils.time import utcnow


from sqlalchemy import func, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker

if TYPE_CHECKING:
    from pidroid.client import Pidroid

class API:
    """This class handles operations related to Pidroid's Postgres database and remote TheoTown API."""

    def __init__(self, client: Pidroid, dsn: str, echo: bool = False) -> None:
        super().__init__()
        self.client = client
        self.__http = HTTP(client)
        self.__engine = create_async_engine(dsn, echo=echo)
        self.session = async_sessionmaker(self.__engine, expire_on_commit=False, class_=AsyncSession)

    async def test_connection(self) -> None:
        """Test the connection to the database by opening a temporary connection."""
        temp_conn = await self.__engine.connect()
        await temp_conn.close()

    async def get(self, route: Route) -> APIResponse:
        """Sends a GET request to the TheoTown API."""
        return await self.__http.request("GET", route)

    async def post(self, route: Route, data: dict[Any, Any]) -> APIResponse:
        """Sends a POST request to the TheoTown API."""
        return await self.__http.request("POST", route, data=data)

    async def legacy_get(self, route: Route) -> dict[Any, Any]:
        """Deprecated. Sends a GET request to the TheoTown API."""
        return await self.__http.legacy_request("GET", route)

    """Tag related"""

    async def insert_tag(self, guild_id: int, name: str, content: str, authors: list[int]) -> int:
        """Creates a tag entry in the database."""
        async with self.session() as session:
            async with session.begin():
                entry = TagTable(
                    guild_id=guild_id,
                    name=name,
                    content=content,
                    authors=authors
                )
                session.add(entry)
            await session.commit()
        return entry.id 

    async def fetch_tag(self, id: int) -> Tag | None:
        """Returns a tag with at the specified row."""
        async with self.session() as session:
            result = await session.execute(
                select(TagTable).
                filter(TagTable.id == id),
            )
        row = result.scalar()
        if row:
            return Tag.from_table(self.client, row)
        return None

    async def fetch_guild_tag(self, guild_id: int, tag_name: str) -> Tag | None:
        """Returns a guild tag for the appropriate name."""
        async with self.session() as session:
            result = await session.execute(
                select(TagTable).
                filter(TagTable.guild_id == guild_id, TagTable.name.ilike(tag_name)).
                order_by(func.lower(TagTable.name).asc()),
            )
        row = result.scalar()
        if row:
            return Tag.from_table(self.client, row)
        return None

    async def search_guild_tags(self, guild_id: int, tag_name: str) -> list[Tag]:
        """Returns all guild tags matching the appropriate name."""
        async with self.session() as session:
            result = await session.execute(
                select(TagTable).
                filter(TagTable.guild_id == guild_id, TagTable.name.ilike(f'%{tag_name}%')).
                order_by(func.lower(TagTable.name).asc()),
            )
        return [Tag.from_table(self.client, r) for r in result.scalars()]

    async def fetch_guild_tags(self, guild_id: int) -> list[Tag]:
        """Returns a list of all tags defined in the guild."""
        async with self.session() as session: 
            result = await session.execute(
                select(TagTable).
                filter(TagTable.guild_id == guild_id).
                order_by(func.lower(TagTable.name).asc())
            )
        return [Tag.from_table(self.client, r) for r in result.scalars()]

    async def update_tag(self, row_id: int, content: str, authors: list[int], aliases: list[str], locked: bool) -> None:
        """Updates a tag entry by specified row ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(
                    update(TagTable).
                    filter(TagTable.id == row_id).
                    values(
                        content=content,
                        authors=authors,
                        aliases=aliases,
                        locked=locked
                    )
                )
            await session.commit()

    async def delete_tag(self, row_id: int) -> None:
        """Removes a tag by specified row ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(delete(TagTable).filter(TagTable.id == row_id))
            await session.commit()

    """Guild configuration related"""

    async def insert_guild_configuration(
        self,
        guild_id: int,
        jail_channel: int | None = None, jail_role: int | None = None,
        log_channel: int | None = None
    ) -> GuildConfiguration:
        """Inserts a minimal guild configuration entry to the database."""
        async with self.session() as session: 
            async with session.begin():
                entry = GuildConfigurationTable(
                    guild_id=guild_id,
                    jail_channel=jail_channel,
                    jail_role=jail_role,
                    log_channel=log_channel
                )
                session.add(entry)
            await session.commit()
        config = await self.__fetch_guild_configuration_by_id(entry.id) 
        assert config is not None
        return config

    async def __fetch_guild_configuration_by_id(self, id: int) -> GuildConfiguration | None:
        """Fetches and returns a deserialized guild configuration if available."""
        async with self.session() as session: 
            result = await session.execute(
                select(GuildConfigurationTable).
                filter(GuildConfigurationTable.id == id)
            )
        r = result.scalar()
        if r:
            return GuildConfiguration.from_table(self, r)
        return None

    async def fetch_guild_configuration(self, guild_id: int) -> GuildConfiguration | None:
        """Fetches and returns a deserialized guild configuration if available for the specified guild."""
        async with self.session() as session: 
            result = await session.execute(
                select(GuildConfigurationTable).
                filter(GuildConfigurationTable.guild_id == guild_id)
            )
        r = result.scalar()
        if r:
            return GuildConfiguration.from_table(self, r)
        return None

    async def fetch_guild_configurations(self) -> list[GuildConfiguration]:
        """Returns a list of all guild configuration entries in the database."""
        async with self.session() as session: 
            result = await session.execute(
                select(GuildConfigurationTable)
            )
        return [GuildConfiguration.from_table(self, r) for r in result.scalars()]

    async def _update_guild_configuration(
        self,
        row_id: int,
        *,
        jail_channel: int | None,
        jail_role: int | None,
        log_channel: int | None,
        prefixes: list[str],
        public_tags: bool,
        punishing_moderators: bool,
        appeal_url: str | None,

        xp_system_active: bool,
        xp_multiplier: float,
        xp_exempt_roles: list[int],
        xp_exempt_channels: list[int],
        stack_level_rewards: bool,

        suggestion_system_active: bool,
        suggestion_channel: int | None,
        suggestion_threads_enabled: bool
    ) -> None:
        """Updates a guild configuration entry by specified row ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(
                    update(GuildConfigurationTable).
                    filter(GuildConfigurationTable.id == row_id).
                    values(
                        jail_channel=jail_channel,
                        jail_role=jail_role,
                        log_channel=log_channel,
                        prefixes=prefixes,
                        public_tags=public_tags,
                        punishing_moderators=punishing_moderators,
                        appeal_url=appeal_url,

                        xp_system_active=xp_system_active,
                        xp_multiplier=xp_multiplier,
                        xp_exempt_roles=xp_exempt_roles,
                        xp_exempt_channels=xp_exempt_channels,
                        stack_level_rewards=stack_level_rewards,

                        suggestion_system_active=suggestion_system_active,
                        suggestion_channel=suggestion_channel,
                        suggestion_threads_enabled=suggestion_threads_enabled
                    )
                )
            await session.commit()

    async def delete_guild_configuration(self, row_id: int) -> None:
        """Removes a guild configuration entry by specified row ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(delete(GuildConfigurationTable).filter(GuildConfigurationTable.id == row_id))
            await session.commit()

    """Expiring thread related"""

    async def insert_expiring_thread(self, thread_id: int, expiration_date: datetime.datetime) -> None:
        async with self.session() as session: 
            async with session.begin():
                session.add(ExpiringThread(thread_id=thread_id, expiration_date=expiration_date))
            await session.commit()

    async def fetch_expired_threads(self, expiration_date: datetime.datetime) -> list[ExpiringThread]:
        """Returns a list of active threads which require archiving."""
        async with self.session() as session: 
            result = await session.execute(
                select(ExpiringThread).
                filter(ExpiringThread.expiration_date <= expiration_date)
            )
        return list(result.scalars())

    async def delete_expiring_thread(self, row_id: int) -> None:
        """Removes an expiring thread entry from the database."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(delete(ExpiringThread).filter(ExpiringThread.id == row_id))
            await session.commit()

    """Punishment related"""

    async def insert_punishment_entry(
        self,
        type: str,
        guild_id: int,
        user_id: int, user_name: str,
        moderator_id: int, moderator_name: str,
        reason: str | None,
        expire_date: datetime.datetime | None
    ) -> Case:
        async with self.session() as session: 
            async with session.begin():
                insert_stmt = pg_insert(GuildCaseCounter).values(guild_id=guild_id).on_conflict_do_update(
                    index_elements=[GuildCaseCounter.guild_id],
                    set_=dict(counter=GuildCaseCounter.counter + 1)
                ).returning(GuildCaseCounter.counter)

                res = await session.execute(insert_stmt)
                counter = res.scalar()
                if counter is None:
                    raise RuntimeError((
                        f"After trying to insert a punishment entry, row was None\n\n"
                        "Information about punishment:\n"
                        f"type: {type}\n"
                        f"guild_id: {guild_id}\n"
                        f"user_id: {user_id}\n"
                        f"moderator_id: {moderator_id}\n"
                        f"reason: {reason}\n"
                        f"expire_date: {expire_date}"
                    ))

                entry = PunishmentTable(
                    case_id=counter,
                    type=type,
                    guild_id=guild_id,
                    user_id=user_id,
                    user_name=user_name,
                    moderator_id=moderator_id,
                    moderator_name=moderator_name,
                    reason=reason,
                    expire_date=expire_date
                )
                session.add(entry)
            await session.commit()

        case = await self.__fetch_case_by_internal_id(entry.id) 
        assert case is not None
        return case

    async def __fetch_case_by_internal_id(self, id: int) -> Case | None:
        """Fetches and returns a deserialized case, if available."""
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable).
                filter(PunishmentTable.id == id)
            )
        r = result.scalar()
        if r:
            return Case(self, r)
        return None

    async def _fetch_case(self, guild_id: int, case_id: int) -> Case | None:
        """Fetches and returns a deserialized case if available."""
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable).
                filter(
                    PunishmentTable.case_id == case_id,
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.visible == True
                )
            )
        r = result.scalar()
        if r:
            return Case(self, r)
        return None

    async def fetch_guilds_user_was_punished_in(self, user_id: int) -> list[Guild]:
        """Fetches and returns a list of guilds where user was punished."""
        async with self.session() as session:
            result = await session.execute(
                select(PunishmentTable.guild_id).
                filter(
                    PunishmentTable.user_id == user_id,
                    PunishmentTable.visible == True
                )
                .distinct(PunishmentTable.guild_id)
            )

        guilds: list[Guild] = []
        for row in result.scalars():
            guild = self.client.get_guild(row)
            if guild:
                guilds.append(guild)
        return guilds

    async def _fetch_cases(self, guild_id: int, user_id: int) -> list[Case]:
        """Fetches and returns a list of deserialized cases."""
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable).
                filter(
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.user_id == user_id,
                    PunishmentTable.visible == True
                ).
                order_by(PunishmentTable.issue_date.desc())
            )
        case_list: list[Case] = []
        for r in result.scalars():
            case_list.append(Case(self, r))
        return case_list
    
    async def fetch_cases_by_username(self, guild_id: int, username: str) -> list[Case]:
        """Returns all cases in the guild where original username at punishment time matches the provided username."""
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable).
                filter(
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.user_name.ilike(f'%{username}%'),
                    PunishmentTable.visible == True
                ).
                order_by(PunishmentTable.user_name.asc())
            )
        case_list: list[Case] = []
        for r in result.scalars():
            case_list.append(Case(self, r))
        return case_list

    async def update_case_by_internal_id(
        self,
        id: int,
        reason: str | None,
        expire_date: datetime.datetime | None,
        visible: bool,
        handled: bool
    ) -> None:
        """Updates a case entry by specified row ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(
                    update(PunishmentTable).
                    filter(PunishmentTable.id == id).
                    values(
                        reason=reason,
                        expire_date=expire_date,
                        visible=visible,
                        handled=handled
                    )
                )
            await session.commit()

    async def expire_cases_by_type(
        self,
        type: PunishmentType,
        guild_id: int, user_id: int
    ) -> None:
        """Expires a case entry by making it expired using the specified type, user ID and guild ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(
                    update(PunishmentTable).
                    filter(PunishmentTable.type == type.value, PunishmentTable.guild_id == guild_id, PunishmentTable.user_id == user_id).
                    values(
                        # Note that we do not hide it, only set it as handled so Pidroid doesn't touch it
                        expire_date=utcnow(),
                        handled=True
                    )
                )
            await session.commit()

    async def fetch_moderation_statistics(self, guild_id: int, moderator_id: int) -> dict[str, int]:
        """Fetches and returns a dictionary containing the general moderation statistics."""
        bans = kicks = jails = warnings = user_total = guild_total = 0
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable.type).
                filter(
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.moderator_id == moderator_id,
                    PunishmentTable.visible == True
                )
            )

            # Tally moderator specific data
            for punishment_type in result.scalars():
                user_total += 1
                if punishment_type == 'ban': bans += 1         # noqa: E701
                if punishment_type == 'kick': kicks += 1       # noqa: E701
                if punishment_type == 'jail': jails += 1       # noqa: E701
                if punishment_type == 'warning': warnings += 1 # noqa: E701

            # Get total cases of the guild
            result = await session.execute(
                select(func.count(PunishmentTable.type)).
                filter(
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.visible == True
                )
            )
            row = result.scalar()
            assert row
            guild_total = row

        return {
            "bans": bans, "kicks": kicks, "jails": jails, "warnings": warnings,
            "user_total": user_total,
            "guild_total": guild_total
        }

    async def __is_currently_punished(self, punishment_type: str, guild_id: int, user_id: int) -> bool:
        """Returns true if user is currently punished in a guild for the specified punishment type."""
        async with self.session() as session:
            result = await session.execute(
                select(func.count(PunishmentTable.type)).
                filter(
                    PunishmentTable.type == punishment_type,
                    PunishmentTable.guild_id == guild_id,
                    PunishmentTable.user_id == user_id,
                    PunishmentTable.visible == True
                ).
                filter(
                    (PunishmentTable.expire_date.is_(None))
                    | (PunishmentTable.expire_date > utcnow())
                )
            )
            row = result.scalar()
            assert row is not None
            return row > 0

    async def is_currently_jailed(self, guild_id: int, user_id: int) -> bool:
        """Returns true if user is currently jailed in the guild."""
        return await self.__is_currently_punished(PunishmentType.JAIL.value, guild_id, user_id)

    async def fetch_active_guild_bans(self, guild_id: int) -> list[Case]:
        """
        Returns a list of active guild bans that can expire. Bans
        without an expiration date will not be included.
        """
        async with self.session() as session: 
            result = await session.execute(
                select(PunishmentTable).
                filter(
                    PunishmentTable.guild_id == guild_id,

                    # Expires if expiration date is past current date
                    # and if expiration date is actually set
                    PunishmentTable.expire_date <= utcnow(),
                    PunishmentTable.expire_date.is_not(None),

                    # Explicit statement to know if case was already handled
                    PunishmentTable.handled == False,

                    PunishmentTable.type == PunishmentType.BAN.value,
                    PunishmentTable.visible == True
                )
            )
        case_list: list[Case] = []
        for r in result.scalars():
            case_list.append(Case(self, r))
        return case_list

    """Translation related"""

    async def insert_translation_entry(self, original_str: str, detected_lang: str, translated_str: str) -> None:
        """Inserts a new translation entry to the database."""
        async with self.session() as session: 
            async with session.begin():
                entry = Translation(
                    original_content=original_str,
                    detected_language=detected_lang,
                    translated_string=translated_str
                )
                session.add(entry)
            await session.commit()

    async def fetch_translations(self, original_str: str) -> list[TranslationEntryDict]:
        """Returns a list of translations for specified string."""
        async with self.session() as session: 
            result = await session.execute(
                select(Translation).
                filter(Translation.original_content == original_str)
            )
        return [
            {"detected_source_language": r.detected_language, "text": r.translated_string} for r in result.scalars()
        ]

    """Linked account related"""

    async def insert_linked_account(self, user_id: int, forum_id: int) -> int:
        """Creates a linked account entry in the database."""
        async with self.session() as session: 
            async with session.begin():
                entry = LinkedAccount(
                    user_id=user_id,
                    forum_id=forum_id
                )
                session.add(entry)
            await session.commit()
        return entry.id

    async def fetch_linked_account_by_user_id(self, user_id: int) -> LinkedAccount | None:
        """Fetches and returns a linked account if available."""
        async with self.session() as session: 
            result = await session.execute(
                select(LinkedAccount).
                filter(LinkedAccount.user_id == user_id)
            )
        return result.scalar()

    async def fetch_linked_account_by_forum_id(self, forum_id: int) -> LinkedAccount | None:
        """Fetches and returns a linked account if available."""
        async with self.session() as session: 
            result = await session.execute(
                select(LinkedAccount).
                filter(LinkedAccount.forum_id == forum_id)
            )
        return result.scalar()
    
    """Leveling system related"""

    async def insert_level_reward(self, guild_id: int, role_id: int, level: int) -> int:
        """Creates a level reward entry in the database."""
        async with self.session() as session: 
            async with session.begin():
                entry = LevelRewards(
                    guild_id=guild_id,
                    role_id=role_id,
                    level=level
                )
                session.add(entry)
            await session.commit()
        self.client.dispatch(
            'pidroid_level_reward_add',
            await self.fetch_level_reward_by_id(entry.id)
        )
        return entry.id

    async def update_level_reward_by_id(self, id: int, role_id: int, level: int) -> None:
        """Updates a level reward entry by specified ID."""
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(
                    update(LevelRewards).
                    filter(LevelRewards.id == id).
                    values(
                       role_id=role_id,
                       level=level
                    )
                )
            await session.commit()

    async def delete_level_reward(self, id: int) -> None:
        """Removes a level reward by specified row ID."""
        obj = await self.fetch_level_reward_by_id(id)
        async with self.session() as session: 
            async with session.begin():
                _ = await session.execute(delete(LevelRewards).filter(LevelRewards.id == id))
            await session.commit()
        self.client.dispatch("pidroid_level_reward_remove", obj)

    async def fetch_all_guild_level_rewards(self, guild_id: int) -> list[LevelRewards]:
        """Returns a list of all LevelReward entries available for the specified guild.
        
        Role IDs are sorted by their appropriate level requirement descending."""
        async with self.session() as session: 
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id
                ).
                order_by(LevelRewards.level.desc())
            )
        return list(result.scalars())

    async def fetch_level_reward_by_id(self, id: int) -> LevelRewards | None:
        """Returns a LevelReward entry for the specified ID."""
        async with self.session() as session: 
            result = await session.execute(
                select(LevelRewards).
                filter(LevelRewards.id == id)
            )
        return result.scalar()

    async def fetch_level_reward_by_role(self, guild_id: int, role_id: int) -> LevelRewards | None:
        """Returns a LevelReward entry for the specified guild and role."""
        async with self.session() as session: 
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id,
                    LevelRewards.role_id == role_id
                )
            )
        return result.scalar()

    async def fetch_guild_level_reward_by_level(self, guild_id: int, level: int) -> LevelRewards | None:
        """Returns a LevelReward entry for the specified guild and level."""
        async with self.session() as session: 
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id,
                    LevelRewards.level == level
                )
            )
        return result.scalar()

    async def fetch_eligible_level_rewards_for_level(self, guild_id: int, level: int) -> list[LevelRewards]:
        """Returns a list of LevelReward entries available for the specified guild and level.
        
        Entries are sorted by required level descending."""
        async with self.session() as session: 
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id,
                    LevelRewards.level <= level
                ).
                order_by(LevelRewards.level.desc())
            )
        return list(result.scalars())

    async def fetch_eligible_level_reward_for_level(self, guild_id: int, level: int) -> LevelRewards | None:
        """Returns a LevelReward entry for the specified guild and level.
        
        Entry will be sorted by required level descending."""
        rewards = await self.fetch_eligible_level_rewards_for_level(guild_id, level)
        if len(rewards) == 0:
            return None
        return rewards[0]

    async def fetch_previous_level_reward(self, guild_id: int, level: int) -> LevelRewards | None:
        async with self.session() as session:
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id,
                    LevelRewards.level < level,
                ).order_by(LevelRewards.level.desc()),
            )
        return result.scalar()

    async def fetch_next_level_reward(self, guild_id: int, level: int) -> LevelRewards | None:
        async with self.session() as session:
            result = await session.execute(
                select(LevelRewards).
                filter(
                    LevelRewards.guild_id == guild_id,
                    LevelRewards.level > level,
                ).order_by(LevelRewards.level.desc()),
            )
        return result.scalar()



    """ User levels related """

    async def insert_member_level_info(
        self,
        guild_id: int,
        user_id: int
    ) -> UserLevels:
        """Inserts an empty member level information entry to the database."""
        async with self.session() as session:
            async with session.begin():
                entry = UserLevels(
                    guild_id=guild_id,
                    user_id=user_id,
                )
                session.add(entry)
            await session.commit()
        info = await self.fetch_user_level_info(guild_id, user_id)
        assert info is not None
        return info

    async def fetch_guild_level_rankings(self, guild_id: int, start: int = 0, limit: int = 10) -> list[UserLevels]:
        """Returns a list of guild levels."""
        async with self.session() as session:
            result = await session.execute(
                select(
                    UserLevels,
                ).
                filter(UserLevels.guild_id == guild_id).
                order_by(UserLevels.total_xp.desc()).
                limit(limit).
                offset(start),
            )
        return list(result.scalars())

    async def fetch_guild_level_infos(self, guild_id: int) -> list[UserLevels]:
        """Returns the bare member level information excluding ranking information."""
        async with self.session() as session:
            result = await session.execute(
                select(
                    UserLevels,
                ).
                filter(
                    UserLevels.guild_id == guild_id
                ),
            )
        return list(result.scalars())

    async def fetch_ranked_user_level_info(self, guild_id: int, user_id: int) -> UserLevels | None:
        """Returns ranked level information for the specified user."""
        async with self.session() as session:
            query = (
                select(
                    UserLevels,
                )
                .where(
                    UserLevels.guild_id == guild_id,
                    UserLevels.user_id == user_id
                )
            )
            result = await session.execute(query)
        return result.scalar()

    async def fetch_user_level_info(self, guild_id: int, user_id: int) -> UserLevels | None:
        """Returns the level information for the specified user."""
        async with self.session() as session:
            result = await session.execute(
                select(
                    UserLevels,
                ).
                filter(
                    UserLevels.guild_id == guild_id,
                    UserLevels.user_id == user_id,
                ),
            )
        return result.scalar()

    async def fetch_user_level_info_between(self, guild_id: int, min_level: int, max_level: int | None) -> list[UserLevels]:
        """
        Returns a list of user level information for specified levels.

        Returned user data is ``min_level <= USERS < max_level``
        """
        async with self.session() as session:
            stmt = (
                select(UserLevels).
                filter(
                    UserLevels.guild_id == guild_id,
                    UserLevels.level >= min_level
                )
            )
            if max_level is not None:
                stmt = stmt.filter(UserLevels.level < max_level)
            result = await session.execute(stmt)
        return list(result.scalars())

    async def update_user_level_theme(self, row_id: int, theme_name: str):
        """Updates the user level theme."""
        async with self.session() as session:
            async with session.begin():
                _ = await session.execute(
                    update(UserLevels).
                    filter(
                        UserLevels.id == row_id,
                    ).values(
                        theme_name=theme_name,
                    ),
                )
            await session.commit()

    async def award_xp(self, message: Message, amount: int):
        """Awards the specified amount of XP to the specified message."""

        # We only award XP to messages in guilds from members
        assert message.guild is not None
        assert isinstance(message.author, Member)
        guild_id = message.guild.id
        member_id = message.author.id

        # Acquire the level information before leveling up
        info = await self.fetch_user_level_info(guild_id, member_id)
        new_insert = False
        if info is None:
            new_insert = True
            info = await self.insert_member_level_info(guild_id, member_id)

        # Get the current information
        original_xp = info.current_xp
        original_level = info.level
        original_xp_to_next_lvl = info.xp_to_next_level

        # If we get no level up
        new_xp_to_next_level = original_xp_to_next_lvl
        new_level = original_level
        new_xp = original_xp + amount

        # Calculate the new levels
        while True:
            if new_xp >= new_xp_to_next_level:
                new_level = new_level + 1
                # https://github.com/Mee6/Mee6-documentation/blob/master/docs/levels_xp.md
                new_xp = new_xp - new_xp_to_next_level
                new_xp_to_next_level = 5 * (new_level ** 2) + (50 * new_level) + 100
            else:
                break

        async with self.session() as session:
            async with session.begin():
                _ = await session.execute(
                    update(UserLevels).
                    filter(
                        UserLevels.guild_id == guild_id,
                        UserLevels.user_id == member_id,
                    ).values(
                        current_xp=new_xp,
                        xp_to_next_level=new_xp_to_next_level,
                        total_xp=UserLevels.total_xp + amount,
                        level=new_level
                    )
                )
            await session.commit()

        if new_insert or new_level != original_level:
            self.client.dispatch(
                'pidroid_level_up',
                message.author,
                message,
                info,
                await self.fetch_user_level_info(guild_id, member_id)
            )

    """Reminder system related"""

    async def insert_reminder(
        self,
        *,
        user_id: int,
        channel_id: int | None,
        message_id: int,
        message_url: str,
        content: str,
        date_remind: datetime.datetime,
    ):
        """Inserts a reminder to the database."""
        async with self.session() as session:
            async with session.begin():
                entry = Reminder(
                    user_id=user_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    message_url=message_url,
                    content=content,
                    date_remind=date_remind,
                )
                session.add(entry)
            await session.commit()
        return entry.id

    async def fetch_reminder(self, *, row: int) -> Reminder | None:
        """Fetches reminder entry at the specified row."""
        async with self.session() as session:
            result = await session.execute(
                select(
                    Reminder,
                ).
                filter(
                    Reminder.id == row,
                ),
            )
        return result.scalar()

    async def fetch_reminders(self, *, user_id: int) -> list[Reminder]:
        """Fetches reminders for the specified user."""
        async with self.session() as session:
            result = await session.execute(
                select(
                    Reminder
                ).
                filter(
                    Reminder.user_id == user_id,
                ).
                order_by(Reminder.date_remind.asc()),
            )
        return list(result.scalars())

    async def delete_reminder(self, *, row: int):
        """Deletes reminder at the specified row."""
        async with self.session() as session:
            async with session.begin():
                _ = await session.execute(delete(Reminder).filter(Reminder.id==row))
            await session.commit()

    """TheoTown backend related"""

    async def fetch_theotown_account_by_discord_id(self, account_id: int) -> TheoTownAccount | None:
        """Fetches TheoTown account by discord ID."""
        res = await self.get(Route(
            "/game/account/find",
            {"discord_id": account_id},
        ))
        if res.code == 200:
            return TheoTownAccount(res.data)
        return None
