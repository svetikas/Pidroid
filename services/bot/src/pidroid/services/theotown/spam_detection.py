import asyncio
import datetime
import imagehash
import logging

from collections import defaultdict
from discord import AutoModAction, Guild, Member
from discord.ext import commands
from discord.message import Message
from io import BytesIO
from PIL import Image
from typing import TypedDict, override

from pidroid.constants import RESOURCE_FILE_PATH
from pidroid.client import Pidroid
from pidroid.models.logs import ScamImageHashData, ScamImageLog
from pidroid.modules.moderation.models.types import Ban2, Timeout2
from pidroid.utils import run_in_executor, truncate_string
from pidroid.utils.aliases import MessageableGuildChannelTuple
from pidroid.utils.checks import is_guild_moderator, is_guild_theotown
from pidroid.utils.time import delta_to_datetime, utcnow

# Threshold for the number of same messages in different channels from the same user
# before they are considered spam.
CHANNEL_THRESHOLD = 3

# The amount of time to keep a message from a member tracked before it is removed from the tracking list.
KEEP_MESSAGE_TRACKED_FOR = 30 # seconds

# Keep moderation dedupe markers short-lived. They are only for race-window protection.
TIMEOUT_DEDUPE_TTL = datetime.timedelta(minutes=10)
BAN_DEDUPE_TTL = datetime.timedelta(minutes=10)

# IDs of AutoMod rules that we want to track for banning on matched phrases
AUTOMOD_RULE_IDS_TO_TRACK = {
    1123571301693522020,
}

# A set of phrases that will trigger an automatic ban if matched.
# * may be used for partial matching
# (e.g., "*badword*" will match any content containing "badword",
# "*badword" will match any content ending with "badword",
# and "badword*" will match any content starting with "badword").
BANNABLE_PHRASES: set[str] = {
    "omg join girl in cam*",
    "OMGG look this girl in vc*",
    "shes on cam look*",
    "look at this wtf*",
    "*discord.gg/PJYyqGMZfe",
    "*discordapp.com/invite/PJrt6k5xns",
    "*discord.gg/cam-girl",
    "*discord.gg/sweetygirl",
    "*discord.com/invite/sugar-girls",
    "*https://discord.gg/dolls-girls",
    "*https://discord.gg/cherry-girls",
    "*https://discord.gg/honey-girls",
    "*discordapp.com/invite/KMc96Z9pnt",
}

def init_hashes() -> set[imagehash.ImageHash]:
    """
    Initialize a set of known scam image hashes.

    The initialization includes computing the average hash for each known scam image
    and storing it in a set for quick comparison.
    """
    dir_path = RESOURCE_FILE_PATH / "scam_images"
    hashes: set[imagehash.ImageHash] = set()
    for filename in dir_path.iterdir():
        try:
            with Image.open(filename) as img:
                hash = imagehash.average_hash(img, hash_size=32) # pyright: ignore[reportUnknownMemberType]
                hashes.add(hash)
        except Exception:
            logger.exception(f"Failed to compute hash for scam image {filename}")
    return hashes

# A set of known scam image hashes (computed using average_hash with hash size 32)
# TODO: storing increasingly more hashes in memory and comparing them will become inefficient
# consider a more scalable approach in the future
KNOWN_SCAM_IMAGE_HASHES: set[imagehash.ImageHash] = init_hashes()

class Infraction(TypedDict):
    channel_id: int
    message_id: int
    timestamp: datetime.datetime

class ScamImageQueueItem(TypedDict):
    message: Message
    image_data: bytes
    attachment_url: str
    result_future: asyncio.Future[tuple[str | None, int]]

# (user_id, message_content)
KEY = tuple[int, str]
# list of infractions
VALUE = list[Infraction] 

logger = logging.getLogger('pidroid.services.theotown.spam_detection')

class SpamDetectionService(commands.Cog):
    """
    This class implements a cog for handling of events related to spam detection.
    
    This is relatively simple spam detection that tracks messages sent by users
    across different channels in the Theotown server. If a user sends the same message
    in more than `CHANNEL_THRESHOLD` different channels, they are considered to be spamming and bot will issue a timeout.
    """
    def __init__(self, client: Pidroid):
        super().__init__()
        self.client: Pidroid = client
        # Dictionary to store message tracking
        self.message_tracker: dict[KEY, VALUE] = defaultdict(list)
        # Prevent duplicate moderation actions when multiple events race for the same user.
        self.moderation_action_lock: asyncio.Lock = asyncio.Lock()
        self.timed_out_users: dict[int, datetime.datetime] = {}
        self.banned_users: dict[int, datetime.datetime] = {}
        # Queue and worker for scam image hash checks.
        self.scam_image_queue: asyncio.Queue[ScamImageQueueItem] = asyncio.Queue()
        # Task to periodically clean up old messages from the tracker
        self.cleanup_task: asyncio.Task[None] = self.client.loop.create_task(self._cleanup_messages())
        self.scam_image_worker_task: asyncio.Task[None] = self.client.loop.create_task(self._process_scam_image_queue())
        self.bannable_phrases: set[str] = BANNABLE_PHRASES.copy()

    @override
    async def cog_unload(self):
        _ = self.cleanup_task.cancel()
        _ = self.scam_image_worker_task.cancel()

    async def _cleanup_messages(self):
        """
        Periodically cleans up old messages from the tracker.
        """
        while True:
            await asyncio.sleep(10) # Clean up every 10 seconds
            now = utcnow()

            keys_to_remove: list[KEY] = []
            for (user_id, message_content), entries in self.message_tracker.items():
                new_entries = [
                    entry.copy() for entry in entries
                    if (now - entry['timestamp']).total_seconds() < KEEP_MESSAGE_TRACKED_FOR
                ]
                if not new_entries:
                    keys_to_remove.append((user_id, message_content))
                else:
                    self.message_tracker[(user_id, message_content)] = new_entries

            for key in keys_to_remove:
                del self.message_tracker[key]

            # Prune stale moderation dedupe entries so this state stays bounded.
            self.timed_out_users = {
                user_id: expires_at
                for user_id, expires_at in self.timed_out_users.items()
                if expires_at > now
            }
            self.banned_users = {
                user_id: expires_at
                for user_id, expires_at in self.banned_users.items()
                if expires_at > now
            }

    @staticmethod
    def _compare_image_hash(image_data: bytes, attachment_url: str, message_id: int) -> tuple[str | None, int]:
        """
        Computes the average hash of the given image data and compares it to the known scam image hashes.
        """
        try:
            image = Image.open(BytesIO(image_data))
            computed_hash = imagehash.average_hash(image, hash_size=32) # pyright: ignore[reportUnknownMemberType]
            for known_hash in KNOWN_SCAM_IMAGE_HASHES:
                # The threshold of 30 for the hash difference is chosen based on experimentation
                hamming_distance = computed_hash - known_hash
                if hamming_distance <= 30:
                    return str(known_hash), hamming_distance
            return None, 0
        except Exception:
            logger.exception(f"Failed to compute image hash for attachment {attachment_url} in message {message_id}")
            return None, 0

    async def _process_scam_image_queue(self):
        """
        Processes queued image hash checks one-by-one to avoid bursty parallel CPU work.
        """
        while True:
            queue_item = await self.scam_image_queue.get()
            try:
                computed_hash, distance = await run_in_executor(
                    self._compare_image_hash,
                    image_data=queue_item['image_data'],
                    attachment_url=queue_item['attachment_url'],
                    message_id=queue_item['message'].id
                )
                if not queue_item['result_future'].done():
                    queue_item['result_future'].set_result((computed_hash, distance))
            except Exception as ex:
                if not queue_item['result_future'].done():
                    queue_item['result_future'].set_exception(ex)
            finally:
                self.scam_image_queue.task_done()

    async def _timeout_member(self, member: Member, until: datetime.datetime, reason: str) -> bool:
        async with self.moderation_action_lock:
            now = utcnow()

            known_ban_until = self.banned_users.get(member.id)
            if known_ban_until and known_ban_until > now:
                return False
            if known_ban_until:
                del self.banned_users[member.id]

            known_timeout_until = self.timed_out_users.get(member.id)
            if known_timeout_until and known_timeout_until > now:
                return False
            if known_timeout_until:
                del self.timed_out_users[member.id]

            current_timeout = getattr(member, 'timed_out_until', None)
            if current_timeout and current_timeout > now:
                self.timed_out_users[member.id] = now + TIMEOUT_DEDUPE_TTL
                return False

            assert self.client.user, "Client user is not available for timing out. This should never happen."
            timeout = Timeout2(
                self.client.api,
                guild=member.guild,
                target=member,
                moderator=self.client.user,
                reason=reason,
                date_expire=until
            )
            _ = await timeout.issue()
            self.timed_out_users[member.id] = now + TIMEOUT_DEDUPE_TTL
            return True

    async def _ban_member(self, guild: Guild, target: Member, reason: str, text: str) -> bool:
        async with self.moderation_action_lock:
            now = utcnow()

            known_ban_until = self.banned_users.get(target.id)
            if known_ban_until and known_ban_until > now:
                return False
            if known_ban_until:
                del self.banned_users[target.id]

            if guild.get_member(target.id) is None:
                return False

            assert self.client.user, "Client user is not available for banning. This should never happen."
            logger.info(f"Banning user {str(target)} ({target.id}) in guild {guild.name} ({guild.id}) for {reason}.")
            ban = Ban2(
                self.client.api,
                guild=guild,
                target=target,
                moderator=self.client.user,
                reason=reason,
                date_expire=delta_to_datetime(datetime.timedelta(days=7)),
                delete_message_days=0
            )

            try:
                _ = await ban.issue()
            except Exception:
                logger.exception(f"Failed to ban user {target.id} in guild {guild.id} for {truncate_string(text, 64)}")
                raise

            self.banned_users[target.id] = now + BAN_DEDUPE_TTL
            return True

    async def check_message_for_scam_image_links(self, message: Message):
        if not message.attachments:
            return
        
        attachments_to_check = [
            att
            for att in message.attachments
            if att.content_type and att.content_type in ["image/png", "image/jpeg"] and att.size <= 5 * 1024 * 1024 # Only check images up to 5 MB
        ] 

        for attachment in attachments_to_check:
            attachment_data = await attachment.read()
            result_future: asyncio.Future[tuple[str | None, int]] = self.client.loop.create_future()
            await self.scam_image_queue.put({
                'message': message,
                'image_data': attachment_data,
                'attachment_url': attachment.url,
                'result_future': result_future
            })

            computed_hash, distance = await result_future
            if computed_hash:
                self.client.dispatch('pidroid_log', ScamImageLog(ScamImageHashData(
                    message=message,
                    hash=computed_hash,
                    distance=distance
                )))
                await message.delete(delay=0)
                assert message.guild, "Message guild is not available for timing out. This should never happen."
                assert self.client.user, "Client user is not available for timing out. This should never happen."

                if isinstance(message.author, Member):
                    _ = await self._timeout_member(
                        message.author,
                        utcnow() + datetime.timedelta(days=1),
                        "Posting known scam image"
                    )
                return

    @commands.Cog.listener()
    async def on_message(self, message: Message):
        if message.author.bot:
            return

        if not message.guild or not is_guild_theotown(message.guild):
            return
        
        if isinstance(message.author, Member) and is_guild_moderator(message.author):
            return

        # If the message content is empty after normalization (e.g., just an attachment), skip
        normalized_content = message.content.strip().lower()
        if not normalized_content:
            await self.check_message_for_scam_image_links(message)
            return
        
        assert isinstance(message.author, Member)

        user_id = message.author.id
        channel_id = message.channel.id
        message_id = message.id
        current_time = utcnow()

        # Add the current message to the tracker
        self.message_tracker[(user_id, normalized_content)].append({
            'channel_id': channel_id,
            'message_id': message_id,
            'timestamp': current_time
        })

        # Filter out old messages and count unique channels for the current message content
        unique_channels_for_message: set[int] = set()
        for infraction in self.message_tracker[(user_id, normalized_content)]:
            if (current_time - infraction['timestamp']).total_seconds() < KEEP_MESSAGE_TRACKED_FOR:
                unique_channels_for_message.add(infraction['channel_id'])

        # If the number of unique channels exceeds the threshold, it's spam
        if len(unique_channels_for_message) >= CHANNEL_THRESHOLD:
            member = message.author
            reason = f"Spamming the same message in {len(unique_channels_for_message)} channels."
            try:
                _ = await self._timeout_member(member, utcnow() + datetime.timedelta(hours=24), reason)

                # Delete the messages from the channels where the user spammed
                for infraction in self.message_tracker[(user_id, normalized_content)]:
                    channel = self.client.get_channel(infraction['channel_id'])
                    if not channel:
                        logger.warning(f"Channel {infraction['channel_id']} not found for user {user_id} in spam detection.")
                        continue
                    try:
                        assert isinstance(channel, MessageableGuildChannelTuple)
                        msg = channel.get_partial_message(infraction['message_id'])
                        await msg.delete(delay=0)
                    except Exception:
                        logger.exception(f"Failed to delete message {infraction['message_id']} in channel {channel.id}")

                # Clear the messages from the tracker for this user and content to prevent repeated timeouts
                del self.message_tracker[(user_id, normalized_content)]
            except Exception:
                logger.exception(f"An error occurred while timing out {member.display_name}")

        await self.check_message_for_scam_image_links(message)

    async def _ban_user(self, guild: Guild, target: Member, text: str):
        reason = f"AutoMod rule triggered for matched disallowed content: {truncate_string(text, 64)}"
        _ = await self._ban_member(guild, target, reason, text)

    @commands.Cog.listener()
    async def on_automod_action(self, execution: AutoModAction):
        """
        This event listener handles taken automod actions and checks if they were triggered by the rules
        we want to track for banning on matched phrases.
        """
        if not is_guild_theotown(execution.guild):
            return
        
        """logger.debug(
            "AutoModAction executed for user %s in channel %s with action %s for rule %s (trigger type: %s) with content: %s",
            execution.user_id, execution.channel_id,
            execution.action, execution.rule_id,
            execution.rule_trigger_type, 
            execution.content
        )"""

        # Ignore AutoMod actions that are not related to the rules we want to track
        if execution.rule_id not in AUTOMOD_RULE_IDS_TO_TRACK:
            return
        
        # Try to get the member object for the user who triggered the AutoMod action
        maybe_member = execution.guild.get_member(execution.user_id)
        if maybe_member is None:
            logger.warning(f"Could not get member with ID {execution.user_id} in guild {execution.guild.id} for AutoModAction. Skipping ban check.")
            return

        # Make sure we're not banning a moderator
        if is_guild_moderator(maybe_member):
            logger.warning(f"User {execution.user_id} is a moderator but triggered AutoModAction for rule {execution.rule_id}. Skipping ban check.")
            return

        # Check if the content of the message matches any of the bannable phrases
        normalized_content = execution.content.strip().lower()
        for phrase in self.bannable_phrases:
            if phrase.startswith('*') and phrase.endswith('*'):
                # Phrase can be matched anywhere in the content
                if phrase[1:-1] in normalized_content:
                    await self._ban_user(execution.guild, maybe_member, normalized_content)
                    return
            elif phrase.startswith('*'):
                # Phrase can be matched at the end of the content
                if normalized_content.endswith(phrase[1:]):
                    await self._ban_user(execution.guild, maybe_member, normalized_content)
                    return
            elif phrase.endswith('*'):
                # Phrase can be matched at the start of the content
                if normalized_content.startswith(phrase[:-1]):
                    await self._ban_user(execution.guild, maybe_member, normalized_content)
                    return
            else:
                # Phrase must match exactly
                if normalized_content == phrase:
                    await self._ban_user(execution.guild, maybe_member, normalized_content)
                    return

async def setup(client: Pidroid) -> None:
    await client.add_cog(SpamDetectionService(client))
