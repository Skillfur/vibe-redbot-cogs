import logging
import caldav
from redbot.core import commands, Config
from redbot.core.bot import Red
import discord
from discord.ext import tasks
from datetime import datetime, date, timedelta, timezone


class CalDAVSync(commands.Cog):
    """Syncs CalDAV calendar events to a Discord channel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8723918273)

        default_guild = {
            "server": None,
            "username": None,
            "password": None,
            "channel": None,
            "messageID": None,
            "period": "1h",
            "days": 7,
        }
        self.config.register_guild(**default_guild)

        self.last_update: dict[int, datetime] = {}
        self.logger = logging.getLogger("red.caldavsync")

        self.update_calendars.start()

    def cog_unload(self):
        self.update_calendars.cancel()

    # ==================== BACKGROUND TASK ====================
    @tasks.loop(minutes=1)
    async def update_calendars(self):
        for guild in self.bot.guilds:
            try:
                data = await self.config.guild(guild).all()
                if not (data.get("server") and data.get("password") and data.get("channel")):
                    continue

                period_sec = self.parse_period(data.get("period", "1h"))
                last = self.last_update.get(guild.id, datetime.min.replace(tzinfo=timezone.utc))
                now = datetime.now(timezone.utc)

                if (now - last).total_seconds() >= period_sec:
                    await self.sync_and_update_message(guild)
                    self.last_update[guild.id] = now
            except Exception:
                continue

    @update_calendars.before_loop
    async def before_update_calendars(self):
        await self.bot.wait_until_ready()

    def parse_period(self, period: str) -> int:
        period = str(period).strip().lower()
        if period.endswith("h"):
            return int(period[:-1]) * 3600
        elif period.endswith("m"):
            return int(period[:-1]) * 60
        try:
            return int(period)
        except ValueError:
            return 3600

    # ==================== CORE LOGIC ====================
    def _fetch_events_sync(self, server: str, username: str, password: str, days: int):
        if not server or not password:
            return []

        try:
            client = caldav.DAVClient(url=server, username=username or "", password=password)
            principal = client.get_principal()
            calendars = client.get_calendars(principal=principal)

            today = datetime.now(timezone.utc).date()
            end_date = today + timedelta(days=days)
            now = datetime.now(timezone.utc)

            events_list = []

            for cal in calendars:
                try:
                    evs = cal.search(
                        event=True,
                        start=today,
                        end=end_date,
                        expand=True
                    )

                    for ev in evs:
                        comp = ev.get_icalendar_component()
                        summary = str(comp.get("summary", "Untitled Event"))
                        description = str(comp.get("description", "")).strip()

                        dtstart_obj = comp.get("dtstart")
                        if not dtstart_obj:
                            continue

                        start_dt = dtstart_obj.dt
                        if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
                            start_dt = datetime.combine(start_dt, datetime.min.time(), tzinfo=timezone.utc)
                        elif isinstance(start_dt, datetime):
                            if start_dt.tzinfo is None:
                                start_dt = start_dt.replace(tzinfo=timezone.utc)
                            else:
                                start_dt = start_dt.astimezone(timezone.utc)

                        dtend_obj = comp.get("dtend")
                        if dtend_obj:
                            end_dt = dtend_obj.dt
                            if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                                end_dt = datetime.combine(end_dt, datetime.max.time(), tzinfo=timezone.utc)
                            elif isinstance(end_dt, datetime):
                                if end_dt.tzinfo is None:
                                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                                else:
                                    end_dt = end_dt.astimezone(timezone.utc)
                        else:
                            end_dt = start_dt + timedelta(hours=1)

                        if end_dt > now:
                            unix_ts = int(start_dt.timestamp())
                            events_list.append((unix_ts, summary, description))

                except Exception as e:
                    self.logger.debug(f"Error processing calendar: {e}")
                    continue

            events_list.sort(key=lambda x: x[0])
            return events_list

        except Exception as e:
            self.logger.error(f"CalDAV fetch error: {e}")
            return []

    async def sync_and_update_message(self, guild: discord.Guild):
        self.logger.info(f"Starting CalDAV sync for guild {guild.id}")

        try:
            server = await self.config.guild(guild).server()
            username = await self.config.guild(guild).username() or ""
            password = await self.config.guild(guild).password()
            channel_id = await self.config.guild(guild).channel()
            days = await self.config.guild(guild).days()

            if not server or not password:
                self.logger.warning("CalDAV server or password not configured.")
                return

            if not channel_id:
                self.logger.warning("No channel configured for this guild.")
                return

            events_list = await self.bot.loop.run_in_executor(
                None, self._fetch_events_sync, server, username, password, days
            )

            if not events_list:
                content = f"No upcoming events in the next {days} days."
            else:
                formatted = []
                for unix_ts, summary, description in events_list:
                    # New format with ## heading
                    block = f"## <t:{unix_ts}:F>\n`{summary}`"
                    if description:
                        block += f"\n{description}"
                    formatted.append(block)
                content = "\n\n".join(formatted)

            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    self.logger.error(f"Could not fetch channel {channel_id}: {e}")
                    return

            if not channel.permissions_for(guild.me).send_messages:
                self.logger.error(f"Missing 'Send Messages' permission in channel {channel.id}")
                return

            message_id = await self.config.guild(guild).messageID()

            if message_id:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(content=content[:2000])
                    self.logger.info("Existing message updated successfully.")
                    return
                except discord.NotFound:
                    self.logger.info("Saved message ID not found, creating new message.")
                except Exception as e:
                    self.logger.error(f"Failed to edit message: {e}")

            try:
                new_msg = await channel.send(content[:2000])
                await self.config.guild(guild).messageID.set(new_msg.id)
                self.logger.info(f"New message created with ID {new_msg.id}")
            except discord.Forbidden:
                self.logger.error("Bot is missing permissions to send messages in this channel.")
            except Exception as e:
                self.logger.error(f"Failed to send message: {e}")

        except Exception as e:
            self.logger.exception(f"Unexpected error during CalDAV sync: {e}")

    # ==================== COMMANDS ====================
    @commands.group(name="caldavset", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def caldavset(self, ctx):
        """Configure CalDAV sync settings."""
        await ctx.send_help(ctx.command)

    @caldavset.command(name="show")
    async def show_settings(self, ctx):
        """Show current CalDAV settings."""
        data = await self.config.guild(ctx.guild).all()

        channel = ctx.guild.get_channel(data.get("channel")) if data.get("channel") else None
        channel_mention = channel.mention if channel else "Not set"

        password_status = "Set" if data.get("password") else "Not set"

        embed = discord.Embed(title="CalDAV Sync Settings", color=discord.Color.blue())
        embed.add_field(name="Server", value=data.get("server") or "Not set", inline=False)
        embed.add_field(name="Username", value=data.get("username") or "Not set", inline=True)
        embed.add_field(name="Password", value=password_status, inline=True)
        embed.add_field(name="Channel", value=channel_mention, inline=False)
        embed.add_field(name="Period", value=data.get("period", "1h"), inline=True)
        embed.add_field(name="Days Ahead", value=str(data.get("days", 7)), inline=True)

        await ctx.send(embed=embed)

    @caldavset.command(name="server")
    async def set_server(self, ctx, *, url: str):
        await self.config.guild(ctx.guild).server.set(url.strip())
        await ctx.send("✅ CalDAV server URL set.")

    @caldavset.command(name="username")
    async def set_username(self, ctx, *, username: str):
        await self.config.guild(ctx.guild).username.set(username.strip())
        await ctx.send("✅ Username set.")

    @caldavset.command(name="password")
    async def set_password(self, ctx, *, password: str):
        await self.config.guild(ctx.guild).password.set(password)
        await ctx.send("✅ Password set. (hidden from now on)")

    @caldavset.command(name="channel")
    async def set_channel(self, ctx, channel: discord.TextChannel = None):
        if channel is None:
            channel = ctx.channel
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"✅ Channel set to {channel.mention}")

    @caldavset.command(name="period")
    async def set_period(self, ctx, period: str):
        try:
            self.parse_period(period)
            await self.config.guild(ctx.guild).period.set(period.strip())
            await ctx.send(f"✅ Sync period set to `{period}`.")
        except Exception:
            await ctx.send("Invalid format. Use `1h`, `30m`, or seconds (e.g. `3600`).")

    @caldavset.command(name="days")
    async def set_days(self, ctx, days: int):
        if days < 1:
            await ctx.send("The number of days must be at least 1.")
            return
        await self.config.guild(ctx.guild).days.set(days)
        await ctx.send(f"✅ Number of days set to **{days}**.")

    @caldavset.command(name="messageid")
    async def set_messageid(self, ctx, message_id: int = None):
        if message_id is None:
            await self.config.guild(ctx.guild).messageID.set(None)
            await ctx.send("Message ID cleared. A new message will be created on next sync.")
        else:
            await self.config.guild(ctx.guild).messageID.set(message_id)
            await ctx.send(f"Message ID manually set to `{message_id}`.")

    @commands.command(name="caldav")
    @commands.admin_or_permissions(manage_guild=True)
    async def force_sync(self, ctx):
        data = await self.config.guild(ctx.guild).all()
        if not (data.get("server") and data.get("password") and data.get("channel")):
            await ctx.send("Please configure `server`, `password`, and `channel` first using `caldavset` commands.")
            return

        await ctx.send("Syncing CalDAV events...")
        await self.sync_and_update_message(ctx.guild)
        await ctx.send("✅ Sync complete.")


def setup(bot: Red):
    bot.add_cog(CalDAVSync(bot))
