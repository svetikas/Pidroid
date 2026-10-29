import base64
import logging
import random
from io import BytesIO
from typing import TypedDict, final

from discord import File
from discord.ext import commands
from discord.ext.commands import BadArgument
from discord.ext.commands.context import Context

from pidroid.client import Pidroid
from pidroid.models.categories import TheoTownCategory
from pidroid.models.exceptions import APIException
from pidroid.utils import format_version_code, http
from pidroid.utils.embeds import PidroidEmbed
from pidroid.utils.http import Route

logger = logging.getLogger("pidroid.commands.theotown")

SUPPORTED_GALLERY_MODES = ["recent", "trends", "rating"]

def resolve_gallery_mode(query: str | None) -> str | None:
    if query is None:
        return None
    query = query.lower()
    if query in ["trending", "hot", "trends"]:
        return "trends"
    if query in ["new", "recent", "latest"]:
        return "recent"
    if query in ["rating", "top", "best"]:
        return "rating"
    return None

class ScreenshotDict(TypedDict):
    name: str
    image_url: str

@final
class TheoTownCommandCog(commands.Cog):
    """Class responsible for implementing commands related to TheoTown API."""

    def __init__(self, client: Pidroid) -> None:
        super().__init__()
        self.client = client
        self.api = self.client.api

    @commands.command(
        brief="Returns the latest game version of TheoTown for all platforms.",
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.bot_has_permissions(send_messages=True)
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def version(self, ctx: Context[Pidroid]):
        async with ctx.typing():
            cache = self.client.version_cache
            embed = PidroidEmbed(title="Most recent versions of TheoTown")
            async with await http.get(self.client, "https://bd.theotown.com/get_version") as response:
                version_data = await response.json()
            for version_name in version_data:
                # Ignore Amazon since it's not updated
                if version_name == "Amazon":
                    continue

                version = format_version_code(version_data[version_name]["version"])
                url: str | None = None
                if version not in cache or cache[version] is None:
                    logger.info("URL for version %s not found in internal cache, querying the API", version)
                    try:
                        data = await self.api.legacy_get(Route(
                            "/forum/post/lookup_version",
                            {"query": version},
                        ))
                        url = data["url"]
                        logger.info("Version URL found, internal cache updated with %s", url)
                    except APIException:
                        pass

                    cache[version] = url
                url = cache[version]
                value = f"[{version}]({url})"
                if url is None:
                    value = version
                _ = embed.add_field(name=version_name, value=value)
            self.client.version_cache = cache
            _ = embed.set_footer(text="Note: this will also include versions which are not yet available to regular users.")
            return await ctx.reply(embed=embed)

    @commands.command(
        brief="Returns TheoTown's online mode statistics.",
        aliases=["multiplayer"],
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.bot_has_permissions(send_messages=True)
    @commands.cooldown(rate=1, per=15, type=commands.BucketType.channel)
    @commands.max_concurrency(number=3, per=commands.BucketType.guild)
    async def online(self, ctx: Context[Pidroid]):
        async with ctx.typing():
            data = await self.api.legacy_get(Route("/game/region/statistics"))

            total_plots: int = data["plots"]["total"]
            free_plots: int = data["plots"]["free"]
            region_count: int = data["region_count"]
            population: int = data["population"]

            embed = (
                PidroidEmbed(title="Online mode statistics")
                .add_field(name="Active regions", value=f"{region_count:,}")
                .add_field(name="Total plots", value=f"{total_plots:,}")
                .add_field(name="Free plots", value=f"{free_plots:,}")
                .add_field(name="Total population", value=f"{population:,}")
            )
            return await ctx.reply(embed=embed)

    @commands.command(
        brief="Returns an image from TheoTown's in-game gallery.",
        usage="[recent/trends/rating] [random number]",
        aliases=["screenshot"],
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.bot_has_permissions(send_messages=True)
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def gallery(self, ctx: Context[Pidroid], mode: str | None = "recent", number: int | None = None):
        selected_mode = resolve_gallery_mode(mode)

        if selected_mode not in SUPPORTED_GALLERY_MODES:
            raise BadArgument(
                "Wrong mode specified. Allowed modes are `"
                + "`, `".join(SUPPORTED_GALLERY_MODES)
                + "`.",
            )

        if number is None:
            number = random.randint(1, 200) # nosec

        try:
            number = int(number)
        except ValueError:
            raise BadArgument("Your specified position is incorrect.")

        if number not in range(1, 201):
            raise BadArgument("Number must be between 1 and 200!")

        async with ctx.typing():
            data = await self.client.api.legacy_get(Route(
                "/game/gallery/list", {"mode": selected_mode, "limit": number},
            ))
            screenshot: ScreenshotDict = data[number - 1]

            embed = (
                PidroidEmbed(title=screenshot['name'])
                .set_image(url=screenshot['image_url'])
                .set_footer(text=f'#{screenshot["id"]}')
            )
            return await ctx.reply(embed=embed)

    @commands.command(
        name="link-account",
        brief="Link your Discord account to a TheoTown account.",
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.bot_has_permissions(send_messages=True)
    async def link_account(self, ctx: Context[Pidroid]):
        return await ctx.send((
            "You can link your Discord account to TheoTown account "
            "[here](https://forum.theotown.com/account_link/discord)."
        ))

    @commands.command(
        name="encrypt-plugin",
        brief="Encrypts and signs the plugin in a provided zip archive to a .ttplugin file.",
        extras={
            "category": TheoTownCategory,
        },
    )
    @commands.max_concurrency(number=1, per=commands.BucketType.user)
    @commands.bot_has_permissions(send_messages=True)
    async def encrypt_plugin_command(self, ctx: Context[Pidroid]):
        if not ctx.message.attachments:
            raise BadArgument("Please provide the plugin zip file as an attachment.")
        
        attachment = ctx.message.attachments[0]
        if attachment.size > 25*1000*1000:
            raise BadArgument("Your plugin file size must be at most 25 MiB.")

        account = await self.api.fetch_theotown_account_by_discord_id(ctx.author.id)
        if account is None:
            message = ((
                "Your Discord account is not linked to a TheoTown account. "
                "This command requires you to link your account using `Plink-account` to sign and encrypt plugins. "
                "You can read more about it "
                "[here](https://pca.svetikas.lt/docs/guides/plugin-encryption/#ttplugin_file_creation)."
            ))
            raise BadArgument(message)

        async with ctx.typing():
            parts = attachment.filename.split(".")
            if len(parts) == 1:
                filename = parts[0] + ".ttplugin"
            else:
                filename = '.'.join(parts[:-1]) + ".ttplugin"

            file = await attachment.read()
            res = await self.api.post(Route("/game/plugin/encrypt"), {
                "sign_as": account.forum_account.id,
                "file": base64.b64encode(file).decode("utf-8")
            })
            if res.code == 200:
                data: dict[str, str] = res.data
                decoded = base64.b64decode(data["file"])
                io = BytesIO(decoded)
                _ = await ctx.reply('Your encrypted plugin file', file=File(io, filename))
                return io.close()
            res.raise_on_error()

async def setup(client: Pidroid) -> None:
    await client.add_cog(TheoTownCommandCog(client))
