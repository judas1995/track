import os

import cachetools
import cachetools.keys
from sqlalchemy import Boolean, Column, Float, Integer, JSON, String
from sqlalchemy import select, Index
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# _DB_PATH = ":memory:"
_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../assets/private/bot.db"
)
engine = create_async_engine(f"sqlite+aiosqlite:///{_DB_PATH}", future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class CachedMixin:
    CACHE_SIZE = 1000
    cache = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.cache = cachetools.LRUCache(maxsize=cls.CACHE_SIZE)

    @classmethod
    def invalidate(cls, **kwargs):
        try:
            del cls.cache[cachetools.keys.hashkey(**kwargs)]
        except KeyError:
            pass

    @classmethod
    async def get(cls, **kwargs):
        key = cachetools.keys.hashkey(**kwargs)
        if (cached := cls.cache.get(key)) is not None:
            return cached

        # noinspection PyUnresolvedReferences
        clauses = [cls.__table__.columns[col] == value for col, value in kwargs.items()]

        async with async_session() as session:
            statement = select(cls).where(*clauses)
            result = (await session.execute(statement)).all()
            cls.cache[key] = result
            return result

    @classmethod
    async def get_or_create(cls, **kwargs):
        if result := await cls.get(**kwargs):
            return result[0][0]
        else:
            async with async_session() as session:
                # noinspection PyArgumentList
                obj = cls(**kwargs)
                session.add(obj)
                await session.commit()
            cls.invalidate(**kwargs)
            return obj


class User(Base, CachedMixin):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    guess_count = Column(Integer, default=0)
    guess_record = Column(Float, default=None)
    wg_region = Column(Integer, default=None)
    wg_id = Column(Integer, default=None)
    wg_ac = Column(String, default=None)
    background = Column(String, default=None)
    locale = Column(String, default=None)
    is_blacklisted = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)


class Guild(Base, CachedMixin):
    __tablename__ = "guilds"

    id = Column(Integer, primary_key=True)
    disabled = Column(JSON, default="{}")
    wg_region = Column(Integer, default=None)
    locale = Column(String, default=None)
    is_blacklisted = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)


class ClanWatcher(Base):
    __tablename__ = "clan_watchers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(Integer, nullable=False)
    channel_id = Column(Integer, nullable=False)
    region = Column(String, nullable=False)
    clan_id = Column(Integer, nullable=False)
    clan_tag = Column(String, nullable=False)
    clan_name = Column(String, nullable=False)
    season = Column(Integer, nullable=False)
    last_battles_1 = Column(Integer, default=0)   # Alpha
    last_wins_1 = Column(Integer, default=0)
    last_league_1 = Column(Integer, default=0)
    last_division_1 = Column(Integer, default=1)
    last_dr_1 = Column(Integer, default=0)
    last_battles_2 = Column(Integer, default=0)   # Bravo
    last_wins_2 = Column(Integer, default=0)
    last_league_2 = Column(Integer, default=0)
    last_division_2 = Column(Integer, default=1)
    last_dr_2 = Column(Integer, default=0)
    created_at = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


class ClanBattleRecord(Base):
    __tablename__ = "clan_battle_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    watcher_id = Column(Integer, nullable=False)
    team = Column(Integer, nullable=False)  # 1=Alpha, 2=Bravo
    timestamp = Column(Integer, nullable=False)
    battles_delta = Column(Integer, nullable=False)
    wins_delta = Column(Integer, nullable=False)
    total_battles = Column(Integer, nullable=False)
    total_wins = Column(Integer, nullable=False)
    result = Column(String, nullable=True)  # 'W', 'L', or None when multiple battles
    division_rating = Column(Integer, default=0)  # ==100 means entered BO5 promotion
    league = Column(Integer, default=0)           # 0=Squall, 1=Gale, 2=Storm, 3=Typhoon, 4=Hurricane
    division = Column(Integer, default=1)         # 1/2/3 within the league


if __name__ == "__main__":

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.run(create_tables())
