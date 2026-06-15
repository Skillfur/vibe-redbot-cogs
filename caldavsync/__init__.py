from .caldavsync import CalDAVSync

async def setup(bot):
    await bot.add_cog(CalDAVSync(bot))
