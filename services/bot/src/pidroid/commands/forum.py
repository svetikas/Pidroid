from typing import final

from discord.ext import commands
from discord.ext.commands import MissingRequiredArgument
from discord.ext.commands.context import Context

from pidroid.client import Pidroid
from pidroid.models.accounts import ForumAccount
from pidroid.models.categories import TheoTownCategory
from pidroid.services.error_handler import notify
from pidroid.utils.embeds import PidroidEmbed
from pidroid.utils.http import Route


@final
class ForumCommandCog(commands.Cog):
    """Class responsible for implementing commands primarily used to interact with TheoTown forums."""

    def __init__(self, client: Pidroid) -> None:
        super().__init__()
        self.client = client

    @commands.command(
        name="forum-account",
        brief="Returns forum account information for the specified user.",
        usage="<forum username/ID>",
        aliases=["forum-ui", "forum-user"],
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.bot_has_permissions(send_messages=True)
    async def forum_account_command(self, ctx: Context[Pidroid], username: str):
        async with ctx.typing():
            data = await self.client.api.legacy_get(Route(
                "/forum/account/find",
                {"query": username},
            ))
            account = ForumAccount(data[0])
            embed = (
                PidroidEmbed(title=f"{account.name}'s information")
                .add_field(name="Username", value=account.name, inline=True)
                .add_field(name="ID", value=f"[{account.id}]({account.profile_url})", inline=True)
                .add_field(name="Group", value=account.group_name, inline=True)
                .add_field(name="Rank", value=account.rank, inline=True)
                .add_field(name="Post count", value=account.post_count, inline=True)
                .add_field(name="Plugins", value=f"[Forum plugins]({account.forum_plugin_url})\n[Plugin store plugins]({account.plugin_store_url})", inline=False)
                .set_thumbnail(url=account.avatar_url)
            )
            return await ctx.reply(embed=embed)

    @forum_account_command.error
    async def on_forum_account_command_error(self, ctx: Context[Pidroid], error: Exception):
        if isinstance(error, MissingRequiredArgument):
            if error.param.name == "username":
                return await notify(ctx, "Please specify the username to view the information for.")
        setattr(error, 'unhandled', True)


async def setup(client: Pidroid) -> None:
    await client.add_cog(ForumCommandCog(client))
