import logging
import caldav
from redbot.core import commands, Config
from redbot.core.bot import Red
import discord
from discord.ext import tasks
from datetime import datetime, date, timedelta, timezone


class CalDAVSync(commands.Cog):
    """Syncs CalDAV calendar events to Discord channels with per-channel configuration."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8723918273)

        default_guild = {
            "server": None,
            "username": None,
            "password": None,
        }
        self.config.register_guild(**default_guild)

        default_channel = {
            "calendars": [],
            "days": 7,
            "period": "1h",
            "messageID": None,
        }
        self.config.register_channel(**default_channel)

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
                guild_data = await self.config.guild(guild).all()
                if not (guild_data.get("server") and guild_data.get("password")):
                    continue

                for channel in guild.text_channels:
                    ch_data = await self.config.channel(channel).all()
                    if not ch_data.get("messageID") and not ch_data.get("calendars"):
                        continue

                    period_sec = self.parse_period(ch_data.get("period", "1h"))
                    last = self.last_update.get(channel.id, datetime.min.replace(tzinfo=timezone.utc))
                    now = datetime.now(timezone.utc)

                    if (now - last).total_seconds() >= period_sec:
                        await self.sync_and_update_message(channel)
                        self.last_update[channel.id] = now
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
    def _fetch_events_sync(self, server: str, username: str, password: str, days: int, allowed_calendars: list):
        if not server or not password:
            return []

        try:
            client = caldav.DAVClient(url=server, username=username or "", password=password)
            principal = client.get_principal()
            all_calendars = client.get_calendars(principal=principal)

            today = datetime.now(timezone.utc).date()
            end_date = today + timedelta(days=days)
            now = datetime.now(timezone.utc)

            events_list = []

            for cal in all_calendars:
                cal_name = cal.get_display_name() or getattr(cal, "name", str(cal.url).split("/")[-1])

                if allowed_calendars and cal_name not in allowed_calendars:
                    continue

                try:
                    evs = cal.search(event=True, start=today, end=end_date, expand=True)

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
                            start_dt = start_dt.replace(tzinfo=timezone.utc) if start_dt.tzinfo is None else start_dt.astimezone(timezone.utc)

                        dtend_obj = comp.get("dtend")
                        end_dt = dtend_obj.dt if dtend_obj else start_dt + timedelta(hours=1)
                        if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                            end_dt = datetime.combine(end_dt, datetime.max.time(), tzinfo=timezone.utc)
                        elif isinstance(end_dt, datetime):
                            end_dt = end_dt.replace(tzinfo=timezone.utc) if end_dt.tzinfo is None else end_dt.astimezone(timezone.utc)

                        if end_dt > now:
                            events_list.append((int(start_dt.timestamp()), summary, description))

                except Exception:
                    continue

            events_list.sort(key=lambda x: x[0])
            return events_list

        except Exception as e:
            self.logger.error(f"CalDAV error: {e}")
            return []

    async def sync_and_update_message(self, channel: discord.TextChannel):
        guild = channel.guild
        guild_data = await self.config.guild(guild).all()
        ch_data = await self.config.channel(channel).all()

        server = guild_data.get("server")
        username = guild_data.get("username") or ""
        password = guild_data.get("password")
        days = ch_data.get("days", 7)
        allowed_calendars = ch_data.get("calendars", [])

        if not server or not password:
            return

        events_list = await self.bot.loop.run_in_executor(
            None, self._fetch_events_sync, server, username, password, days, allowed_calendars
        )

        if not events_list:
            content = f"No upcoming events in the next {days} days."
        else:
            formatted = []
            for unix_ts, summary, description in events_list:
                block = f"## <t:{unix_ts}:F>\n`{summary}`"
                if description:
                    block += f"\n{description}"
                formatted.append(block)
            content = "\n\n".join(formatted)

        if not channel.permissions_for(guild.me).send_messages:
            return

        message_id = ch_data.get("messageID")
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(content=content[:2000])
                return
            except discord.NotFound:
                pass

        try:
            new_msg = await channel.send(content[:2000])
            await self.config.channel(channel).messageID.set(new_msg.id)
        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")

    # ==================== COMMANDS ====================
    @commands.group(name="caldavset", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def caldavset(self, ctx, channel: discord.TextChannel = None, *, setting: str = None):
        """
        Configure CalDAV per channel.
        Usage:
        [p]caldavset #channel calendars Work,Personal
        [p]caldavset #channel days 14
        [p]caldavset days 7
        """

        if channel is None:
            channel = ctx.channel

        if setting is None:
            # Show current channel settings
            data = await self.config.channel(channel).all()
            cal_list = ", ".join(data.get("calendars", [])) or "All calendars"
            embed = discord.Embed(title=f"Settings for {channel.mention}", color=discord.Color.blue())
            embed.add_field(name="Calendars", value=cal_list, inline=False)
            embed.add_field(name="Days", value=data.get("days", 7), inline=True)
            embed.add_field(name="Period", value=data.get("period", "1h"), inline=True)
            await ctx.send(embed=embed)
            return

        # Parse setting
        parts = setting.split(maxsplit=1)
        key = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""

        if key == "calendars":
            calendars = [c.strip() for c in value.split(",")] if value else []
            await self.config.channel(channel).calendars.set(calendars)
            msg = f"Set to: {', '.join(calendars)}" if calendars else "Now showing **all** calendars"
            await ctx.send(f"✅ Calendars for {channel.mention}: {msg}")

        elif key == "days":
            try:
                days = int(value)
                await self.config.channel(channel).days.set(days)
                await ctx.send(f"✅ Days for {channel.mention} set to **{days}**.")
            except ValueError:
                await ctx.send("Please provide a valid number for days.")

        elif key == "period":
            await self.config.channel(channel).period.set(value.strip())
            await ctx.send(f"✅ Period for {channel.mention} set to `{value.strip()}`.")

        else:
            await ctx.send("Valid options: `calendars`, `days`, or `period`.")

    @caldavset.command(name="show")
    async def show_all(self, ctx):
        """Show guild + all channel settings."""
        guild_data = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(title="CalDAV Configuration", color=discord.Color.blue())
        embed.add_field(name="Server", value=guild_data.get("server") or "Not set", inline=False)
        embed.add_field(name="Username", value=guild_data.get("username") or "Not set", inline=True)
        embed.add_field(name="Password", value="Set" if guild_data.get("password") else "Not set", inline=True)

        await ctx.send(embed=embed)

    @caldavset.command(name="server")
    async def set_server(self, ctx, *, url: str):
        await self.config.guild(ctx.guild).server.set(url.strip())
        await ctx.send("✅ Server URL set.")

    @caldavset.command(name="username")
    async def set_username(self, ctx, *, username: str):
        await self.config.guild(ctx.guild).username.set(username.strip())
        await ctx.send("✅ Username set.")

    @caldavset.command(name="password")
    async def set_password(self, ctx, *, password: str):
        await self.config.guild(ctx.guild).password.set(password)
        await ctx.send("✅ Password set.")

    @commands.command(name="caldav")
    @commands.admin_or_permissions(manage_guild=True)
    async def force_sync(self, ctx, channel: discord.TextChannel = None):
        if channel is None:
            channel = ctx.channel

        guild_data = await self.config.guild(ctx.guild).all()
        if not (guild_data.get("server") and guild_data.get("password")):
            await ctx.send("Please configure server and password first.")
            return

        await ctx.send(f"Syncing {channel.mention}...")
        await self.sync_and_update_message(channel)
        await ctx.send("✅ Done.")


def setup(bot: Red):
    bot.add_cog(CalDAVSync(bot))
