import caldav
from redbot.core import commands, Config
from redbot.core.bot import Red
import discord
from discord.ext import tasks
from datetime import datetime, date, timedelta, timezone


class CalDAVSync(commands.Cog):
    """Syncs CalDAV calendar events to a Discord channel (next 7 days)."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8723918273)  # Unique identifier

        default_guild = {
            "server": None,          # CalDAV server URL (e.g. https://cal.example.com/)
            "username": None,
            "password": None,
            "channel": None,         # Channel ID
            "messageID": None,       # Auto-managed
            "period": "1h",          # 1h / 30m / 3600 etc.
        }
        self.config.register_guild(**default_guild)

        self.last_update: dict[int, datetime] = {}
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
    def _fetch_events_sync(self, server: str, username: str, password: str):
        """Synchronous CalDAV fetch (run in executor)."""
        if not server or not password:
            return []

        try:
            client = caldav.DAVClient(url=server, username=username or "", password=password)
            principal = client.get_principal()
            calendars = client.get_calendars(principal=principal)

            today = datetime.now(timezone.utc).date()
            end_date = today + timedelta(days=7)

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

                        dtstart_obj = comp.get("dtstart")
                        if dtstart_obj:
                            dt = dtstart_obj.dt
                            if isinstance(dt, date) and not isinstance(dt, datetime):
                                dt = datetime.combine(dt, datetime.min.time(), tzinfo=timezone.utc)
                            elif isinstance(dt, datetime):
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                else:
                                    dt = dt.astimezone(timezone.utc)
                            unix_ts = int(dt.timestamp())
                        else:
                            unix_ts = int(datetime.now(timezone.utc).timestamp())

                        events_list.append((unix_ts, summary))
                except Exception:
                    continue

            events_list.sort(key=lambda x: x[0])
            return events_list
        except Exception:
            return []

    async def sync_and_update_message(self, guild: discord.Guild):
        server = await self.config.guild(guild).server()
        username = await self.config.guild(guild).username() or ""
        password = await self.config.guild(guild).password()

        events_list = await self.bot.loop.run_in_executor(
            None, self._fetch_events_sync, server, username, password
        )

        if not events_list:
            content = "No events found in the next 7 days."
        else:
            formatted = [f"<t:{unix_ts}:F> `{summary}`" for unix_ts, summary in events_list]
            content = "\n\n".join(formatted)

        channel_id = await self.config.guild(guild).channel()
        if not channel_id:
            return

        try:
            channel = guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except Exception:
            return

        message_id = await self.config.guild(guild).messageID()

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(content=content[:2000])
                return
            except (discord.NotFound, discord.Forbidden):
                pass
            except Exception:
                pass

        # Create new message
        try:
            new_msg = await channel.send(content[:2000])
            await self.config.guild(guild).messageID.set(new_msg.id)
        except Exception:
            pass

    # ==================== COMMANDS ====================
    @commands.group(name="caldavset", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def caldavset(self, ctx):
        """Configure CalDAV sync settings."""
        await ctx.send_help(ctx.command)

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
        """Force an immediate CalDAV sync and message update."""
        data = await self.config.guild(ctx.guild).all()
        if not (data.get("server") and data.get("password") and data.get("channel")):
            await ctx.send("Please configure `server`, `password`, and `channel` first using `caldavset`.")
            return

        await ctx.send("Syncing CalDAV events...")
        await self.sync_and_update_message(ctx.guild)
        await ctx.send("✅ Sync complete.")


def setup(bot: Red):
    bot.add_cog(CalDAVSync(bot))
