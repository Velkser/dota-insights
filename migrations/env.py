import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, create_engine, make_url, pool

from dota_insights.db.models import PARTITIONED_TABLES, Base
from dota_insights.db.session import database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Monthly partitions are created at runtime by ensure_month_partitions(), not by the models.
_PARTITION_NAME = re.compile(rf"^({'|'.join(PARTITIONED_TABLES)})_\d{{4}}_\d{{2}}$")


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    if type_ == "table" and name is not None:
        return _PARTITION_NAME.match(name) is None
    return True


def include_object(obj, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
    # PostgreSQL clones an FK to a partitioned table once per referenced partition.
    if type_ == "foreign_key_constraint" and reflected:
        return _PARTITION_NAME.match(obj.referred_table.name) is None
    return True


def _url() -> URL:
    """``-x url=...`` wins, then DOTA_INSIGHTS_DATABASE_URL.

    Offline mode (``--sql``) only needs a dialect, so it works without any URL configured.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override is not None:
        return database_url(override)
    try:
        return database_url()
    except RuntimeError:
        if context.is_offline_mode():
            return make_url("postgresql+psycopg://")
        raise


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
