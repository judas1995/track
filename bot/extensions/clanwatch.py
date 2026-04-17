import asyncio
import io
import json
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select, update

import api
from bot.track import Track
from bot.utils import db, wows
from bot.utils.logs import logger
from config import cfg


TEAM_NAMES = {1: "Alpha", 2: "Bravo"}
DIVISION_ROMAN = {1: "I", 2: "II", 3: "III"}
PER_PAGE = 15


def _league_name(region: str, season: int, league: int) -> str:
    try:
        leagues = api.wg.seasons[region].data[season].leagues
        # league value: 0=Hurricane (highest) ... N=Squall (lowest)
        # leagues list: index 0=Squall (lowest) ... N=Hurricane (highest)
        return leagues[len(leagues) - 1 - league].name
    except (KeyError, IndexError):
        return f"League {league}"


def _tier_str(region: str, season: int, league: int, division: int) -> str:
    name = _league_name(region, season, league)
    div = DIVISION_ROMAN.get(division, str(division))
    return f"{name} {div}"


def is_clanwatch_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in cfg.discord.owner_ids:
            return True
        perms = interaction.user.guild_permissions
        if perms.manage_guild:
            return True
        raise app_commands.MissingPermissions(["manage_guild"])
    return app_commands.check(predicate)


def _team_stats(clan: api.FullClan, season: int, team: int) -> tuple[int, int, int, int, int]:
    """Return (battles, wins, division_rating, league, division) for a specific team in a season."""
    if not clan.wows_ladder:
        return 0, 0, 0, 0, 1
    for r in clan.wows_ladder.ratings:
        if r.season_number == season and r.team_number == team:
            return r.battles_count, r.wins_count, r.division_rating, r.league, r.division
    return 0, 0, 0, 0, 1


def _format_record(rec: db.ClanBattleRecord, region: str, season: int) -> str:
    team_name = TEAM_NAMES.get(rec.team, f"Team {rec.team}")
    ts = f"<t:{rec.timestamp}:f>"
    if rec.result == "W":
        icon, desc = "🏆", "Win"
    elif rec.result == "L":
        icon, desc = "💀", "Loss"
    else:
        losses = rec.battles_delta - rec.wins_delta
        icon = "⚔️"
        desc = f"{rec.battles_delta} battles ({rec.wins_delta}W/{losses}L)"
    win_rate = 100 * rec.total_wins / rec.total_battles if rec.total_battles else 0
    tier = _tier_str(region, season, rec.league, rec.division)
    bo5 = " 🎯`BO5`" if rec.division_rating >= 100 else ""
    return f"{icon} `{team_name}` {ts} — {desc} | `{tier}` DR:`{rec.division_rating}` | Total:{rec.total_battles} ({win_rate:.1f}%){bo5}"


class HistoryView(discord.ui.View):
    def __init__(self, user_id: int, watcher: db.ClanWatcher, records: list):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.watcher = watcher
        self.records = records
        self.page = 0
        self.max_page = (len(records) - 1) // PER_PAGE
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page == self.max_page

    def build_embed(self) -> discord.Embed:
        start = self.page * PER_PAGE
        page_records = self.records[start: start + PER_PAGE]
        lines = [_format_record(r, self.watcher.region, self.watcher.season) for r in page_records]
        total_pages = self.max_page + 1
        return discord.Embed(
            title=f"[{self.watcher.clan_tag}] Battle History — Season {self.watcher.season} ({self.page + 1}/{total_pages})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "You must be the command invoker to do that.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class ClanWatchCog(commands.Cog):
    def __init__(self, bot: Track):
        self.bot = bot
        self.poll_cb.start()

    def cog_unload(self):
        self.poll_cb.cancel()

    @tasks.loop(minutes=15)
    async def poll_cb(self):
        async with db.async_session() as session:
            result = await session.execute(
                select(db.ClanWatcher).where(db.ClanWatcher.is_active == True)
            )
            watchers = result.scalars().all()

        for watcher in watchers:
            try:
                clan = await api.get_clan(watcher.region, watcher.clan_id)
                if clan is None:
                    continue

                # Auto-switch season if a new one has started
                if watcher.region in api.wg.seasons:
                    current_season = api.wg.seasons[watcher.region].last_clan_season
                    if current_season > watcher.season:
                        b1, w1, *_ = _team_stats(clan, current_season, 1)
                        b2, w2, *_ = _team_stats(clan, current_season, 2)
                        async with db.async_session() as session:
                            await session.execute(
                                update(db.ClanWatcher)
                                .where(db.ClanWatcher.id == watcher.id)
                                .values(
                                    season=current_season,
                                    last_battles_1=b1, last_wins_1=w1,
                                    last_battles_2=b2, last_wins_2=w2,
                                )
                            )
                            await session.commit()
                        channel = self.bot.get_channel(watcher.channel_id)
                        if channel:
                            await channel.send(
                                f"🔄 **[{watcher.clan_tag}]** New clan battle season detected! "
                                f"Now tracking Season {current_season}."
                            )
                        continue  # next poll will use the new baseline

                for team in (1, 2):
                    await self._check_team(watcher, clan, team)

            except Exception as e:
                logger.warning(f"poll_cb error for watcher {watcher.id}", exc_info=e)

            # Small delay between watchers to avoid hammering the API
            await asyncio.sleep(2)

    async def _check_team(self, watcher: db.ClanWatcher, clan: api.FullClan, team: int):
        total_b, total_w, div_rating, league, division = _team_stats(clan, watcher.season, team)
        last_b = getattr(watcher, f"last_battles_{team}")
        last_w = getattr(watcher, f"last_wins_{team}")

        if total_b == last_b:
            return

        delta_b = total_b - last_b
        delta_w = total_w - last_w

        if delta_b < 0:
            logger.warning(
                f"Watcher {watcher.id} [{watcher.clan_tag}] team {team}: "
                f"negative delta ({last_b} -> {total_b}), re-anchoring baseline."
            )
            async with db.async_session() as session:
                await session.execute(
                    update(db.ClanWatcher)
                    .where(db.ClanWatcher.id == watcher.id)
                    .values(**{f"last_battles_{team}": total_b, f"last_wins_{team}": total_w})
                )
                await session.commit()
            return

        result_str = None
        if delta_b == 1:
            result_str = "W" if delta_w == 1 else "L"

        record = db.ClanBattleRecord(
            watcher_id=watcher.id,
            team=team,
            timestamp=int(time.time()),
            battles_delta=delta_b,
            wins_delta=delta_w,
            total_battles=total_b,
            total_wins=total_w,
            result=result_str,
            division_rating=div_rating,
            league=league,
            division=division,
        )
        async with db.async_session() as session:
            session.add(record)
            await session.execute(
                update(db.ClanWatcher)
                .where(db.ClanWatcher.id == watcher.id)
                .values(**{f"last_battles_{team}": total_b, f"last_wins_{team}": total_w})
            )
            await session.commit()

        channel = self.bot.get_channel(watcher.channel_id)
        if channel is None:
            return

        tag = f"[{watcher.clan_tag}]"
        team_name = TEAM_NAMES[team]
        win_rate = 100 * total_w / total_b if total_b else 0
        tier = _tier_str(watcher.region, watcher.season, league, division)
        bo5_tag = " 🎯 **BO5 Promotion Match!**" if div_rating >= 100 else ""
        dr_info = f"`{tier}` | DR: `{div_rating}` | "

        if result_str == "W":
            msg = f"🏆 **{tag} {team_name}** won a clan battle!{bo5_tag}\n{dr_info}Total: {total_b} | Win rate: {win_rate:.1f}%"
        elif result_str == "L":
            msg = f"💀 **{tag} {team_name}** lost a clan battle!{bo5_tag}\n{dr_info}Total: {total_b} | Win rate: {win_rate:.1f}%"
        else:
            losses = delta_b - delta_w
            msg = (
                f"⚔️ **{tag} {team_name}** played {delta_b} clan battles: "
                f"{delta_w}W / {losses}L{bo5_tag}\n"
                f"{dr_info}Total: {total_b} | Win rate: {win_rate:.1f}%"
            )

        await channel.send(msg)

    @poll_cb.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _watcher_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        async with db.async_session() as session:
            result = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.guild_id == interaction.guild_id,
                    db.ClanWatcher.is_active == True,
                )
            )
            watchers = result.scalars().all()
        return [
            app_commands.Choice(
                name=f"[{w.clan_tag}] {w.clan_name} (S{w.season})",
                value=w.id,
            )
            for w in watchers
            if not current or current.lower() in w.clan_tag.lower() or current.lower() in w.clan_name.lower()
        ][:25]

    group = app_commands.Group(
        name="clanwatch",
        description="Track clan battle win rates.",
        extras={"category": "wows"},
    )

    @group.command(name="add", description="[Admin] Start tracking a clan's clan battles.")
    @app_commands.describe(
        region="The WoWS region.",
        clan="The clan tag or name.",
        channel="Channel to post battle results.",
    )
    @is_clanwatch_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        region: wows.Regions,
        clan: app_commands.Transform[api.FullClan, api.ClanTransformer],
        channel: discord.TextChannel,
    ):
        # ClanTransformer already defers, but guard in case it doesn't
        if not interaction.response.is_done():
            await interaction.response.defer()

        if clan is None:
            await interaction.followup.send("Clan not found.")
            return

        region_str = region.value
        current_season = api.wg.seasons[region_str].last_clan_season
        b1, w1, dr1, lg1, dv1 = _team_stats(clan, current_season, 1)
        b2, w2, dr2, lg2, dv2 = _team_stats(clan, current_season, 2)

        async with db.async_session() as session:
            existing = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.guild_id == interaction.guild_id,
                    db.ClanWatcher.clan_id == clan.clan.id,
                    db.ClanWatcher.is_active == True,
                )
            )
            if existing.scalars().first():
                await interaction.followup.send(
                    f"Already tracking `[{clan.clan.tag}]` in this server."
                )
                return

            watcher = db.ClanWatcher(
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                region=region_str,
                clan_id=clan.clan.id,
                clan_tag=clan.clan.tag,
                clan_name=clan.clan.name,
                season=current_season,
                last_battles_1=b1,
                last_wins_1=w1,
                last_battles_2=b2,
                last_wins_2=w2,
                created_at=int(time.time()),
                is_active=True,
            )
            session.add(watcher)
            await session.commit()

        wr1 = f"{100 * w1 / b1:.1f}%" if b1 else "N/A"
        wr2 = f"{100 * w2 / b2:.1f}%" if b2 else "N/A"
        tier1 = _tier_str(region_str, current_season, lg1, dv1)
        tier2 = _tier_str(region_str, current_season, lg2, dv2)
        bo5_1 = " 🎯BO5" if dr1 >= 100 else ""
        bo5_2 = " 🎯BO5" if dr2 >= 100 else ""
        await interaction.followup.send(
            f"Now tracking `[{clan.clan.tag}] {clan.clan.name}` for Season {current_season}. "
            f"Results will be posted in {channel.mention}.\n"
            f"Alpha: {b1} battles ({wr1}) | `{tier1}` DR: `{dr1}`{bo5_1}\n"
            f"Bravo: {b2} battles ({wr2}) | `{tier2}` DR: `{dr2}`{bo5_2}"
        )

    @group.command(name="remove", description="[Admin] Stop tracking a clan.")
    @app_commands.describe(watcher_id="Select the clan to stop tracking.")
    @is_clanwatch_admin()
    async def remove(self, interaction: discord.Interaction, watcher_id: int):
        await interaction.response.defer(ephemeral=True)
        async with db.async_session() as session:
            result = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.id == watcher_id,
                    db.ClanWatcher.guild_id == interaction.guild_id,
                    db.ClanWatcher.is_active == True,
                )
            )
            watcher = result.scalars().first()
            if not watcher:
                await interaction.followup.send("Watcher not found.", ephemeral=True)
                return

            tag = watcher.clan_tag
            await session.execute(
                update(db.ClanWatcher)
                .where(db.ClanWatcher.id == watcher_id)
                .values(is_active=False)
            )
            await session.commit()

        await interaction.followup.send(f"Stopped tracking `[{tag}]`.", ephemeral=True)

    @remove.autocomplete("watcher_id")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._watcher_autocomplete(interaction, current)

    @group.command(name="list", description="List tracked clans in this server.")
    async def list_watchers(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with db.async_session() as session:
            result = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.guild_id == interaction.guild_id,
                    db.ClanWatcher.is_active == True,
                )
            )
            watchers = result.scalars().all()

        if not watchers:
            await interaction.followup.send("No clans being tracked in this server.")
            return

        embed = discord.Embed(title="Tracked Clans", color=discord.Color.blue())
        for w in watchers:
            wr1 = f"{100 * w.last_wins_1 / w.last_battles_1:.1f}%" if w.last_battles_1 else "N/A"
            wr2 = f"{100 * w.last_wins_2 / w.last_battles_2:.1f}%" if w.last_battles_2 else "N/A"
            channel = self.bot.get_channel(w.channel_id)
            chan_str = channel.mention if channel else f"<#{w.channel_id}>"
            tracking_since = f"<t:{w.created_at}:D>" if w.created_at else "Unknown"
            embed.add_field(
                name=f"ID {w.id} — [{w.clan_tag}] {w.clan_name}",
                value=(
                    f"Region: `{w.region.upper()}` | Season: `{w.season}` | Tracking since: {tracking_since}\n"
                    f"Alpha: `{w.last_battles_1}` battles ({wr1})\n"
                    f"Bravo: `{w.last_battles_2}` battles ({wr2})\n"
                    f"Channel: {chan_str}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @group.command(name="history", description="Show battle history for a tracked clan.")
    @app_commands.describe(watcher_id="Select the clan to view history.")
    async def history(self, interaction: discord.Interaction, watcher_id: int):
        await interaction.response.defer()
        async with db.async_session() as session:
            w_result = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.id == watcher_id,
                    db.ClanWatcher.guild_id == interaction.guild_id,
                )
            )
            watcher = w_result.scalars().first()
            if not watcher:
                await interaction.followup.send("Watcher not found.")
                return

            r_result = await session.execute(
                select(db.ClanBattleRecord)
                .where(db.ClanBattleRecord.watcher_id == watcher_id)
                .order_by(db.ClanBattleRecord.timestamp.desc())
            )
            records = r_result.scalars().all()

        if not records:
            await interaction.followup.send("No battle records yet.")
            return

        view = HistoryView(interaction.user.id, watcher, records)
        view.message = await interaction.followup.send(
            embed=view.build_embed(), view=view
        )

    @history.autocomplete("watcher_id")
    async def history_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._watcher_autocomplete(interaction, current)

    @group.command(name="export", description="[Admin] Export battle records as JSON.")
    @app_commands.describe(watcher_id="Select the clan to export.")
    @is_clanwatch_admin()
    async def export(self, interaction: discord.Interaction, watcher_id: int):
        await interaction.response.defer(ephemeral=True)
        async with db.async_session() as session:
            w_result = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.id == watcher_id,
                    db.ClanWatcher.guild_id == interaction.guild_id,
                )
            )
            watcher = w_result.scalars().first()
            if not watcher:
                await interaction.followup.send("Watcher not found.", ephemeral=True)
                return

            r_result = await session.execute(
                select(db.ClanBattleRecord)
                .where(db.ClanBattleRecord.watcher_id == watcher_id)
                .order_by(db.ClanBattleRecord.timestamp.asc())
            )
            records = r_result.scalars().all()

        data = {
            "watcher": {
                "clan_id": watcher.clan_id,
                "clan_tag": watcher.clan_tag,
                "clan_name": watcher.clan_name,
                "region": watcher.region,
                "season": watcher.season,
            },
            "records": [
                {
                    "team": r.team,
                    "timestamp": r.timestamp,
                    "battles_delta": r.battles_delta,
                    "wins_delta": r.wins_delta,
                    "total_battles": r.total_battles,
                    "total_wins": r.total_wins,
                    "result": r.result,
                }
                for r in records
            ],
        }

        content = json.dumps(data, ensure_ascii=False, indent=2)
        file = discord.File(
            io.BytesIO(content.encode()),
            filename=f"clanwatch_{watcher.clan_tag}_{watcher.season}.json",
        )
        await interaction.followup.send(file=file, ephemeral=True)

    @export.autocomplete("watcher_id")
    async def export_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._watcher_autocomplete(interaction, current)

    @group.command(name="import", description="[Admin] Import battle records from a JSON file.")
    @app_commands.describe(file="The JSON file previously exported via /clanwatch export.")
    @is_clanwatch_admin()
    async def import_data(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        try:
            content = await file.read()
            data = json.loads(content)
            watcher_data = data["watcher"]
            records_data = data["records"]
        except Exception:
            await interaction.followup.send("Invalid file format.", ephemeral=True)
            return

        async with db.async_session() as session:
            existing = await session.execute(
                select(db.ClanWatcher).where(
                    db.ClanWatcher.guild_id == interaction.guild_id,
                    db.ClanWatcher.clan_id == watcher_data["clan_id"],
                    db.ClanWatcher.season == watcher_data["season"],
                )
            )
            watcher = existing.scalars().first()
            if not watcher:
                await interaction.followup.send(
                    f"No watcher found for `[{watcher_data['clan_tag']}]` Season {watcher_data['season']}. "
                    "Add it first with `/clanwatch add`.",
                    ephemeral=True,
                )
                return

            ts_result = await session.execute(
                select(db.ClanBattleRecord.timestamp, db.ClanBattleRecord.team).where(
                    db.ClanBattleRecord.watcher_id == watcher.id
                )
            )
            existing_keys = set(ts_result.all())

            imported = skipped = 0
            for rec in records_data:
                try:
                    key = (int(rec["timestamp"]), int(rec.get("team", 1)))
                    if key in existing_keys:
                        continue
                    session.add(db.ClanBattleRecord(
                        watcher_id=watcher.id,
                        team=key[1],
                        timestamp=key[0],
                        battles_delta=int(rec["battles_delta"]),
                        wins_delta=int(rec["wins_delta"]),
                        total_battles=int(rec["total_battles"]),
                        total_wins=int(rec["total_wins"]),
                        result=rec.get("result"),
                    ))
                    imported += 1
                except (KeyError, TypeError, ValueError):
                    skipped += 1

            await session.commit()

        msg = f"Imported {imported} new record(s) for `[{watcher_data['clan_tag']}]`."
        if skipped:
            msg += f" ({skipped} malformed record(s) skipped)"
        await interaction.followup.send(msg, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need **Manage Server** permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: Track):
    await bot.add_cog(ClanWatchCog(bot))
