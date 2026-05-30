"""SQLAlchemy models and session factory."""
import datetime

from sqlalchemy import (
    create_engine, event, Column, Integer, Float, Boolean, String, Text, JSON,
    DateTime, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

import config

Base = declarative_base()
# check_same_thread=False: the background scrape thread writes while Flask request
# threads read. WAL + busy_timeout (set below) make that concurrency safe.
engine = create_engine(
    f"sqlite:///{config.DB_PATH}", future=True,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_con, _record):
    """WAL + busy timeout so the scraper can write while the UI reads concurrently."""
    cur = dbapi_con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; avoids fsync on every commit
    cur.execute("PRAGMA cache_size=-8000")    # 8 MB page cache
    cur.close()


def utcnow():
    return datetime.datetime.utcnow()


class Product(Base):
    __tablename__ = "products"

    asin = Column(String, primary_key=True)
    title = Column(Text)
    brand = Column(String)
    url = Column(Text)
    image_url = Column(Text)
    category = Column(String, default="power_bank")  # power_bank | power_station
    first_seen = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow)
    listing_position = Column(Integer)

    # Listing data
    price = Column(Float)
    in_stock = Column(Boolean, default=True)
    rating = Column(Float)
    review_count = Column(Integer)

    # Parsed specs
    claimed_mah = Column(Integer)
    capacity_wh = Column(Float)            # energy capacity (banks: derived; stations: stated)
    chemistry = Column(String)             # lifepo4 | li-ion | li-po | nmc | lto
    weight_g = Column(Float)
    date_first_available = Column(String)  # ISO date from the listing, for age
    dims = Column(String)
    usb_a = Column(Integer)
    usb_c = Column(Integer)
    max_w = Column(Float)
    pd_w = Column(Float)
    wireless = Column(Boolean, default=False)
    display = Column(Boolean, default=False)
    passthrough = Column(Boolean, default=False)
    solar = Column(Boolean, default=False)
    # Power-station specs
    ac_output_w = Column(Float)
    ac_sockets = Column(Integer)
    solar_input_w = Column(Float)
    cycle_life = Column(Integer)
    expandable = Column(Boolean, default=False)
    ups = Column(Boolean, default=False)
    raw_specs = Column(JSON)
    review_snippets = Column(JSON)

    # Soft-delist: when a product hasn't been seen in any results for
    # REMOVE_AFTER_HOURS we mark it delisted (rather than deleting) so price
    # history survives. The hourly queue skips these. Set back to None if it
    # ever reappears in a future discovery sweep.
    delisted_at = Column(DateTime)
    cost_per_wh = Column(Float)
    # Derived (analysis pass)
    cost_per_mah = Column(Float)
    honesty = Column(JSON)            # {physics, price, brand, reviews} sub-scores 0..1
    honesty_flags = Column(JSON)      # list[str]
    fair_price = Column(Float)
    price_delta = Column(Float)
    feature_contrib = Column(JSON)    # {feature: £ contribution} for value-to-you

    history = relationship(
        "PriceHistory", back_populates="product",
        cascade="all, delete-orphan", order_by="PriceHistory.captured_at",
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True)
    asin = Column(String, ForeignKey("products.asin"))
    captured_at = Column(DateTime, default=utcnow)
    price = Column(Float)
    in_stock = Column(Boolean)

    product = relationship("Product", back_populates="history")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime)
    status = Column(String)           # ok | partial | failed
    trigger = Column(String)          # schedule | manual | startup
    n_found = Column(Integer, default=0)
    n_errors = Column(Integer, default=0)
    notes = Column(Text)


class Meta(Base):
    __tablename__ = "meta"

    key = Column(String, primary_key=True)
    value = Column(JSON)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


def init_db():
    Base.metadata.create_all(engine)
    # create_all won't add columns to an existing table; add new ones idempotently.
    new_columns = {
        "date_first_available": "VARCHAR",
        "category": "VARCHAR",
        "capacity_wh": "FLOAT",
        "chemistry": "VARCHAR",
        "cost_per_wh": "FLOAT",
        "ac_output_w": "FLOAT",
        "ac_sockets": "INTEGER",
        "solar_input_w": "FLOAT",
        "cycle_life": "INTEGER",
        "expandable": "BOOLEAN",
        "ups": "BOOLEAN",
        "delisted_at": "DATETIME",
        "listing_position": "INTEGER",
        "image_url": "TEXT",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(products)")}
        for col, sqltype in new_columns.items():
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {col} {sqltype}")
        # Existing rows predate categories — they were all power banks.
        conn.exec_driver_sql(
            "UPDATE products SET category='power_bank' WHERE category IS NULL")
