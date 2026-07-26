"""
database.py
-----------
Storage layer for the bot, backed by MongoDB (works great with MongoDB
Atlas's free tier — data lives off Render entirely, so it survives Render
free-plan restarts/redeploys, unlike the SQLite version this replaced).

The public function signatures are unchanged from the SQLite version, so
bot.py needs no changes. Documents are returned as plain dicts with the
same field names bot.py already expects (e.g. order["order_id"],
product["name"]), so dict-style access ("[...]") keeps working exactly
like it did with sqlite3.Row.

Collections
-----------
products      : the items you sell (id, name, price, description, active)
voucher_codes : the actual secret codes/coupons for each product ("stock").
                Each document is ONE unit of stock. When a code is
                delivered to a buyer it's marked used and linked to the order.
counters      : internal — powers auto-incrementing integer product ids,
                since /addproduct, /addcodes <id> etc. use short int ids.
orders        : every purchase attempt, from "pending payment" to "paid" /
                "rejected" / "cancelled" / "expired".

Setup
-----
1. Create a free cluster at https://www.mongodb.com/cloud/atlas/register
   (the M0 free tier is enough for this).
2. Database Access -> add a database user + password.
3. Network Access -> allow access from anywhere (0.0.0.0/0) if you don't
   know your deployment's static IP (Render free tier IPs aren't fixed).
4. Get your connection string from "Connect -> Drivers" — looks like
   mongodb+srv://<user>:<password>@cluster0.xxxxx.mongodb.net/
5. Set it as the MONGO_URI environment variable (see README.md).
"""

import os
import random
import string
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "coupon_shop")

_client: MongoClient | None = None
_db = None


def _get_db():
    """Lazily create the Mongo connection (so importing this module never
    requires a live DB, only calling into it does)."""
    global _client, _db
    if _db is None:
        _client = MongoClient(MONGO_URI)
        _db = _client[MONGO_DB_NAME]
    return _db


def _now():
    return datetime.now(timezone.utc)


def init_db():
    """Create indexes. Safe to call every startup — create_index is a no-op
    if the index already exists with the same spec."""
    db = _get_db()
    db.products.create_index([("id", ASCENDING)], unique=True)
    db.voucher_codes.create_index([("product_id", ASCENDING), ("used", ASCENDING)])
    db.orders.create_index([("order_id", ASCENDING)], unique=True)
    db.orders.create_index([("user_id", ASCENDING)])
    # No index needed on counters._id — MongoDB's default _id index is
    # already unique, and it rejects a custom "unique" option on it.


def _next_id(db, counter_name: str) -> int:
    """Emulates SQLite's AUTOINCREMENT for product ids using a counters doc."""
    doc = db.counters.find_one_and_update(
        {"_id": counter_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


# ---------------------------------------------------------------- products
def add_product(name: str, price: float, description: str = "") -> int:
    db = _get_db()
    pid = _next_id(db, "product_id")
    db.products.insert_one(
        {"id": pid, "name": name, "price": price, "description": description, "active": True}
    )
    return pid


def list_products(active_only: bool = True):
    db = _get_db()
    query = {"active": True} if active_only else {}
    return list(db.products.find(query, {"_id": 0}).sort("id", ASCENDING))


def get_product(product_id: int):
    db = _get_db()
    return db.products.find_one({"id": product_id}, {"_id": 0})


def set_product_active(product_id: int, active: bool):
    db = _get_db()
    db.products.update_one({"id": product_id}, {"$set": {"active": active}})


def stock_count(product_id: int) -> int:
    db = _get_db()
    return db.voucher_codes.count_documents({"product_id": product_id, "used": False})


# ------------------------------------------------------------ voucher pool
def add_codes(product_id: int, codes: list[str]) -> int:
    """Bulk-add fresh stock. Returns number added."""
    db = _get_db()
    docs = [
        {"product_id": product_id, "code": c.strip(), "used": False, "order_id": None}
        for c in codes
        if c.strip()
    ]
    if not docs:
        return 0
    db.voucher_codes.insert_many(docs)
    return len(docs)


def _claim_codes(db, product_id: int, order_id: str, quantity: int) -> list[str] | None:
    """
    Claim `quantity` unused codes for this order.

    Each code is claimed with an atomic find_one_and_update (used: False ->
    used: True), so two buyers can never walk away with the same code even
    if they buy at the same instant. If a race causes a mid-batch shortfall,
    already-claimed codes in this batch are rolled back so the order fails
    cleanly instead of partially fulfilling.

    (For very high concurrency you could wrap this in a Mongo multi-document
    transaction via client.start_session() — Atlas supports this out of the
    box since it's a replica set. Not needed at the scale this bot runs at.)
    """
    candidates = list(
        db.voucher_codes.find({"product_id": product_id, "used": False}).limit(quantity)
    )
    if len(candidates) < quantity:
        return None

    claimed = []
    for doc in candidates:
        result = db.voucher_codes.find_one_and_update(
            {"_id": doc["_id"], "used": False},
            {"$set": {"used": True, "order_id": order_id}},
        )
        if result is None:
            # Someone else claimed this one first — roll back what we've taken.
            for c in claimed:
                db.voucher_codes.update_one(
                    {"_id": c["_id"]}, {"$set": {"used": False, "order_id": None}}
                )
            return None
        claimed.append(doc)
    return [c["code"] for c in claimed]


# ------------------------------------------------------------------ orders
def _gen_order_id(prefix: str = "ORD") -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand_part = "".join(random.choices(string.hexdigits.upper()[:16], k=6))
    return f"{prefix}-{date_part}-{rand_part}"


def create_order(
    user_id: int,
    username: str,
    product_id: int,
    product_name: str,
    unit_price: float,
    quantity: int = 1,
    order_prefix: str = "ORD",
) -> str:
    db = _get_db()
    total = round(unit_price * quantity, 2)
    # order_id collisions are astronomically unlikely (date + 6 random hex
    # chars) but retry a couple of times just in case, since it's a unique key.
    for _ in range(5):
        order_id = _gen_order_id(order_prefix)
        doc = {
            "order_id": order_id,
            "user_id": user_id,
            "username": username,
            "product_id": product_id,
            "product_name": product_name,
            "unit_price": unit_price,
            "quantity": quantity,
            "price": total,
            "status": "pending",
            "voucher_code": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        try:
            db.orders.insert_one(doc)
            return order_id
        except DuplicateKeyError:
            continue
    raise RuntimeError("Could not generate a unique order id — try again.")


def get_order(order_id: str):
    db = _get_db()
    return db.orders.find_one({"order_id": order_id}, {"_id": 0})


def user_orders(user_id: int, limit: int = 10):
    db = _get_db()
    return list(
        db.orders.find({"user_id": user_id}, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )


def mark_paid(order_id: str) -> list[str] | None:
    """
    Approve an order: claim `quantity` voucher codes from stock and mark the
    order paid. Returns the list of delivered codes, or None if out of stock
    or the order is not in a claimable state.
    """
    db = _get_db()
    order = db.orders.find_one({"order_id": order_id})
    if order is None or order["status"] != "pending":
        return None
    codes = _claim_codes(db, order["product_id"], order_id, order["quantity"])
    if codes is None:
        return None
    db.orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "paid", "voucher_code": "\n".join(codes), "updated_at": _now()}},
    )
    return codes


def utr_already_used(utr: str) -> bool:
    """
    True if this UTR is already attached to a paid order. Used to block UTR
    replay — someone reusing the same real transaction id to try to claim a
    second order for free.
    """
    db = _get_db()
    return db.orders.find_one({"utr": utr, "status": "paid"}) is not None


def record_utr_attempt(order_id: str, utr: str):
    """Stash the UTR a buyer just tried against this order (even if it didn't
    verify), so an admin doing manual fallback review can see it."""
    db = _get_db()
    db.orders.update_one({"order_id": order_id}, {"$set": {"utr": utr, "updated_at": _now()}})


def mark_paid_auto(order_id: str, utr: str, verification: dict) -> list[str] | None:
    """
    Approve an order that was auto-verified against the BharatPe API (instead
    of an admin tapping Approve): claim `quantity` voucher codes from stock,
    mark the order paid, and record the UTR + a snapshot of BharatPe's
    response for an audit trail. Returns the list of delivered codes, or
    None if out of stock or the order isn't in a claimable state.
    """
    db = _get_db()
    order = db.orders.find_one({"order_id": order_id})
    if order is None or order["status"] != "pending":
        return None
    codes = _claim_codes(db, order["product_id"], order_id, order["quantity"])
    if codes is None:
        return None
    db.orders.update_one(
        {"order_id": order_id},
        {
            "$set": {
                "status": "paid",
                "voucher_code": "\n".join(codes),
                "utr": utr,
                "verified_by": "bharatpe_auto",
                "verification_snapshot": verification,
                "updated_at": _now(),
            }
        },
    )
    return codes


def mark_rejected(order_id: str):
    db = _get_db()
    db.orders.update_one(
        {"order_id": order_id}, {"$set": {"status": "rejected", "updated_at": _now()}}
    )


def mark_cancelled(order_id: str):
    db = _get_db()
    db.orders.update_one(
        {"order_id": order_id}, {"$set": {"status": "cancelled", "updated_at": _now()}}
    )


def expire_if_still_pending(order_id: str) -> bool:
    """Called by the QR-expiry timer. Returns True if the order was expired just now."""
    db = _get_db()
    result = db.orders.find_one_and_update(
        {"order_id": order_id, "status": "pending"},
        {"$set": {"status": "expired", "updated_at": _now()}},
    )
    return result is not None
