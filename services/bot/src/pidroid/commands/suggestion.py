import os
import random
import logging

from contextlib import suppress
from datetime import timedelta
from discord import app_commands
from discord.channel import TextChannel
from discord.errors import HTTPException
from discord.ext import commands
from discord.ext.commands import BadArgument, MissingRequiredArgument
from discord.ext.commands.context import Context
from discord.member import Member

from pidroid.client import Pidroid
from pidroid.models.categories import UtilityCategory 
from pidroid.models.persistent_views import PersistentSuggestionManagementView
from pidroid.services.error_handler import notify
from pidroid.utils import truncate_string
from pidroid.utils.checks import assert_bot_channel_permissions, is_guild_theotown
from pidroid.utils.embeds import PidroidEmbed
from pidroid.utils.time import timedelta_to_datetime

ALLOWED_SUGGESTION_ATTACHMENT_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif']

SUGGESTION_REACTIONS: dict[str, str] = {
    "✅": "I like this idea",
    "❌": "I hate this idea",
}

THEOTOWN_SUGGESTION_REACTIONS = {
    "✅": "I like this idea",
    "❌": "I hate this idea",
    "❗": "Already possible",
    "❕": "Already possible with plugin(s)"
}

BOT_COMMANDS_CHANNEL_ID = 367299681635794954
REFUSE_COMMAND_RESPONSES = [
    'I do not recommend using my commands here. Perhaps there\'s a better channel for that?',
    'It wouldn\'t be wise to run your command here, therefore, I won\'t.',
    'You may not run your command here.',
    'Sorry, I cannot do that here.'
]

logger = logging.getLogger('pidroid.suggestion')


class SuggestionCommandCog(commands.Cog):
    """This class implements a cog for the suggestion command."""

    def __init__(self, client: Pidroid) -> None:
        super().__init__()
        self.client: Pidroid = client

    @commands.hybrid_command(
        name="suggest",
        brief='Send a suggestion to the server suggestions channel.',
        usage='<suggestion>',
        extras={
            'examples': [("Suggest a luxurious building", 'suggest A luxury pickle-themed building')],
            'category': UtilityCategory
        }
    )
    @commands.guild_only()
    @commands.cooldown(rate=1, per=60 * 5, type=commands.BucketType.user)
    @app_commands.describe(suggestion="Your suggestion.")
    async def suggest_command(self, ctx: Context[Pidroid], *, suggestion: str):
        assert isinstance(ctx.me, Member)
        assert ctx.guild

        config = await self.client.fetch_guild_configuration(ctx.guild.id)
        if not config.suggestion_system_active:
            raise BadArgument("Suggestion system is not active in this server!")

        # Check if we're in TheoTown guild
        is_theotown_guild = is_guild_theotown(ctx.guild)
        reactions = SUGGESTION_REACTIONS
        if is_theotown_guild:
            # Check if they are sending it outside the bot commands channel
            if ctx.channel.id != BOT_COMMANDS_CHANNEL_ID:
                raise BadArgument(random.choice(REFUSE_COMMAND_RESPONSES)) # nosec

            reactions = THEOTOWN_SUGGESTION_REACTIONS

        # Construct the reaction legend
        reaction_legend = ""
        keys = list(reactions.keys())
        for key in keys[:-1]:
            reaction_legend += f"{key} {reactions[key]}; "
        reaction_legend += f"{keys[-1]} {reactions[keys[-1]]}."

        if config.suggestions_channel_id is None:
            raise BadArgument("This server does not have a suggestion channel set.")

        # Get the channel and ensure bot has the permission to send messages there
        channel = self.client.get_channel(config.suggestions_channel_id)
        if channel is None:
            raise BadArgument("Could not find the suggestion channel for this server.")

        assert isinstance(channel, TextChannel)
        assert_bot_channel_permissions(
            ctx.me, channel,
            send_messages=True,
            attach_files=True,
            add_reactions=True,
            create_public_threads=True
        )

        async with ctx.typing():
            if len(suggestion) < 10:
                raise BadArgument("Your suggestion is too short! Include at least 10 characters.")

            # If suggestion text is above discord embed description limit
            if len(suggestion) > 2048:
                raise BadArgument("The suggestion is too long! Keep it within 2048 character limit.")

            embed = (
                PidroidEmbed(description=suggestion.replace("# ", "\\# "))
                .set_author(name=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

            # Deal with attachments
            file = None
            attachments = ctx.message.attachments
            if attachments:
                if len(attachments) > 1:
                    raise BadArgument("Only one image can be submitted for a suggestion!")

                attachment = attachments[0]
                if attachment.size >= ctx.filesize_limit:
                    raise BadArgument("Your image is too big to be uploaded!")

                filename = attachment.filename
                extension = os.path.splitext(filename)[1]
                if extension.lower() not in ALLOWED_SUGGESTION_ATTACHMENT_EXTENSIONS:
                    raise BadArgument("Could not submit a suggestion: unsupported file extension. Only image files are supported!")

                file = await attachment.to_file()
                _ = embed.set_image(url=f"attachment://{filename}")

            # Add the reaction legend to the footer
            _ = embed.set_footer(text=reaction_legend)

            view = PersistentSuggestionManagementView()

            # Send the suggestion message
            if file:
                message = await channel.send(embed=embed, files=[file], view=view)
            else:
                message = await channel.send(embed=embed, view=view)

            # Add reactions to the sent message
            for key in reactions:
                await message.add_reaction(key)

            if config.suggestion_threads_enabled:
                _ = await self.client.create_expiring_thread(
                    message, f"{truncate_string(str(ctx.author), 40)}'s suggestion discussion",
                    timedelta_to_datetime(timedelta(days=30)),
                    channel.default_auto_archive_duration
                )

            # Let the suggestion author know that the suggestion was sent
            with suppress(HTTPException):
                return await ctx.reply(f'Your suggestion has been submitted to {message.jump_url} successfully!')

    @suggest_command.error
    async def on_suggest_command_error(self, ctx: Context[Pidroid], error: Exception):
        if isinstance(error, MissingRequiredArgument):
            if error.param.name == "suggestion":
                return await notify(ctx, "Please specify your suggestion.")
        setattr(error, 'unhandled', True)

async def setup(client: Pidroid) -> None:
    await client.add_cog(SuggestionCommandCog(client))
