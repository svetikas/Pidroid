import logging
from typing import TYPE_CHECKING, override

import aiocron
from discord.channel import TextChannel
from discord.ext import commands

from pidroid.client import Pidroid
from pidroid.modules.plugin_store.types import PluginStoreStatisticsDict
from pidroid.modules.plugin_store.ui import MonthlyPluginReportLayout
from pidroid.utils import http
from pidroid.utils.cronjobs import start_cronjob
from pidroid.utils.data import PersistentDataStore

if TYPE_CHECKING:
    from asyncio import Task

PLUGIN_SHOWCASE_CHANNEL_ID = 640522649033769000
PLUGIN_INFORMATION_CHANNEL_ID = 640521916943171594

logger = logging.getLogger("pidroid.plugin_store.service")

class PluginStoreService(commands.Cog):
    """This class implements a cog for handling of automatic tasks related to TheoTown's plugin store."""

    def __init__(self, client: Pidroid) -> None:
        super().__init__()
        self.client: Pidroid = client
        self.monthly_plugin_cronjob: Task[None] = self.client.loop.create_task(
            start_cronjob(self.client, monthly_plugin_cronjob, "Monthly plugin statistics"),
        )

    @override
    async def cog_unload(self) -> None:
        """Ensure that tasks are cancelled on cog unload."""
        _ = self.monthly_plugin_cronjob.cancel()

@aiocron.crontab("0 9 1 * *", start=False) # At 8 in the morning of the first day every month
async def monthly_plugin_cronjob(client: Pidroid) -> None:
    """Retrieve monthly plugin information and post it to TheoTown guild channel."""
    try:
        channel = await client.get_or_fetch_channel(PLUGIN_INFORMATION_CHANNEL_ID)
        assert isinstance(channel, TextChannel)
        async with await http.get(client, "https://api.theotown.com/store/get_stats") as response:
            data: PluginStoreStatisticsDict = await response.json()

        month_of_data = int(data["month"])

        async with PersistentDataStore() as store:
            previous_month = await store.get("last_plugin_statistic_month")

        if previous_month is None:
            return
        previous_month = int(previous_month)

        if month_of_data != previous_month:
            async with PersistentDataStore() as store:
                await store.set("last_plugin_statistic_month", str(month_of_data))

            layout = MonthlyPluginReportLayout(data)
            _ = await channel.send(view=layout)
    except Exception:
        logger.exception("An exception was encountered while trying announce monthly plugin information")


async def setup(client: Pidroid) -> None:
    await client.add_cog(PluginStoreService(client))
