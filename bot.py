"""
bot.py
------
A coupon/voucher-selling Telegram bot, modeled on the "Buy Vouchers / My
Orders / Recover Vouchers / Support" flow, including the quantity picker,
Terms & Conditions step, and UPI QR code payment page.

All admin actions — payment approvals, product/stock management, and
support replies — happen in a private DM with the bot. No admin group is
needed.

Full buy flow
1. Buyer taps "Buy Vouchers" -> sees your in-stock products.
2. Buyer picks one -> bot shows stock/price and a quantity picker
   (1 code / 5 codes / 10 codes / Other amount).
3. Buyer picks a quantity -> bot shows Terms & Conditions with I Agree / Cancel.
4. I Agree -> bot creates a `pending` order, generates a UPI QR code for the
   exact amount, and shows the payment page (Order ID, Service, Qty, Amount,
   QR, "valid for 10 minutes", I've Paid button). The order auto-expires if
   unpaid after 10 minutes.
5. Buyer taps "I've Paid" -> every admin in ADMIN_USER_IDS gets a DM with
   Approve/Reject buttons (prevents fake-payment fraud — no auto-trust of a
   button tap).
6. An admin taps Approve in their DM -> the bot instantly pulls the right
   number of codes from stock and DMs them to the buyer. Tap Reject ->
   buyer is notified and no stock is touched.
7. Buyer can review "My Orders" any time, or use "Recover Vouchers" with an
   Order ID to fetch codes they lost.
8. "Support" lets a buyer pick one of their orders and message you directly;
   every admin gets it as a DM, and any admin can reply with
   /reply <user_id> <message> from their own DM with the bot.

Setup
-----
1. pip install -r requirements.txt
2. Create a bot with @BotFather, copy the token.
3. Every admin must open a DM with the bot and send /start at least once —
   Telegram only allows a bot to message a user after that user has messaged
   it first. Skipping this means that admin silently won't get notified.
4. Get each admin's numeric Telegram user id (e.g. via @userinfobot in a DM)
   and list them all in ADMIN_USER_IDS, comma-separated.
5. Set the environment variables below (or edit the constants directly).
6. python bot.py

Admin commands (send these directly to the bot in your own DM)
  /addproduct <price> <name>              e.g. /addproduct 54.99 Blinkit icecream 100 per 100 off
  /addcodes <product_id>                  then send codes, one per line, in your next message
  /products                               list products + ids + live stock
  /deactivate <product_id>                hide a sold-out / retired product
  /reply <user_id> <message>              reply to a buyer's support message
"""

import os
import io
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import qrcode

import database as db

# --------------------------------------------------------------- settings
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()
}
UPI_ID = os.environ.get("UPI_ID", "yourupi@bank")
SHOP_NAME = os.environ.get("SHOP_NAME", "My Coupon Shop")
QR_VALID_MINUTES = int(os.environ.get("QR_VALID_MINUTES", "10"))
TERMS_TEXT = os.environ.get(
    "TERMS_TEXT",
    "No returns after delivery. Coupons are fresh and verified — please know the usage before buying.",
)

# --------------------------------------------------------- BharatPe auto-verify
# When set, "I've Paid" asks the buyer for their UTR and checks it against
# this API instead of always waiting on an admin. If the token is missing,
# or the API call fails/doesn't match, we fall back to the original manual
# admin Approve/Reject flow so nothing is ever silently stuck.
BHARATPE_TOKEN = os.environ.get("BHARATPE_TOKEN", "")
BHARATPE_API_URL = "https://bharatpe-payment-checker.vercel.app/check"
# Rupees of slack allowed between the order total and what BharatPe reports,
# to absorb harmless UPI rounding — never used to approve a materially
# different amount.
BHARATPE_AMOUNT_TOLERANCE = float(os.environ.get("BHARATPE_AMOUNT_TOLERANCE", "1.0"))
# How many UTR guesses a buyer gets before we stop hitting the API and just
# hand it to an admin.
MAX_UTR_ATTEMPTS = int(os.environ.get("MAX_UTR_ATTEMPTS", "3"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


# --------------------------------------------------------- health-check server
# Render's free tier only offers "Web Service" instances for free, and those
# require something answering HTTP requests on $PORT — plus they sleep after
# 15 minutes with no traffic. This tiny stdlib server exists purely so:
#   1) Render sees a live HTTP port and treats the service as healthy, and
#   2) an external uptime pinger (e.g. UptimeRobot) can hit it every few
#      minutes to stop the service from sleeping and killing the bot's
#      Telegram connection.
# It has nothing to do with the bot's actual logic — Telegram updates still
# arrive via polling, not through this port.
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - bot is running")

    def log_message(self, format, *args):
        pass  # keep Render's logs focused on the bot, not health pings


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"Health-check server listening on port {port}")
    server.serve_forever()

MAIN_MENU = ReplyKeyboardMarkup(
    [["🛍 Buy Vouchers", "📦 My Orders"], ["🔑 Recover Vouchers", "🆘 Support"]],
    resize_keyboard=True,
)

# Conversation states
(
    RECOVER_WAIT_ID,
    SUPPORT_PICK_ORDER,
    SUPPORT_WAIT_MSG,
    ADMIN_WAIT_CODES,
    BUY_WAIT_CUSTOM_QTY,
    PAID_WAIT_UTR,
) = range(6)

QTY_PRESETS = [1, 5, 10]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """
    DM every admin in ADMIN_USER_IDS directly — no group chat needed.
    Note: Telegram only lets a bot message a user after that user has sent
    it at least one message (e.g. /start). If an admin hasn't messaged the
    bot yet, the send fails for them specifically and we just log it and
    keep trying the rest.
    """
    if not ADMIN_USER_IDS:
        log.error("No ADMIN_USER_IDS configured — nobody can be notified of orders/support.")
        return
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
        except Exception as e:
            log.warning(
                f"Could not DM admin {admin_id} — have they sent /start to the bot yet? ({e})"
            )


def order_prefix_for(user) -> str:
    """Order IDs are prefixed with the buyer's name, e.g. SUMIT-20260725-0E629B."""
    raw = (user.first_name or user.username or "ORD").upper()
    cleaned = "".join(ch for ch in raw if ch.isalnum())
    return cleaned[:10] or "ORD"


def md(text) -> str:
    """
    Escape any text that came from a user (usernames, free-form messages,
    product names an admin typed, etc.) before it's embedded in a
    Markdown-formatted message. Without this, a lone underscore or asterisk
    in someone's Telegram username or a support message breaks Telegram's
    Markdown parser and the whole message silently fails to send.
    """
    return escape_markdown(str(text), version=1)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """
    Send a message to every admin in ADMIN_USER_IDS individually (DM, not a
    group). Each admin gets their own copy with their own Approve/Reject
    buttons — whoever acts on it first wins, the others' copies will just
    say "already handled" if tapped afterward.

    Note: Telegram only lets a bot DM someone who has already started a
    conversation with it (sent /start at least once). If an admin hasn't
    done that yet, sending to them fails — logged clearly below rather
    than silently, so it's visible in Render's Logs tab.
    """
    if not ADMIN_USER_IDS:
        log.warning("No ADMIN_USER_IDS configured — nobody will be notified.")
        return
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
        except Exception as e:
            log.error(f"Could not DM admin {admin_id} — have they sent /start to the bot yet? ({e})")


# ------------------------------------------------------------------ /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 *Welcome to {SHOP_NAME}!*\n\n"
        "Buy discount vouchers & coupons instantly — payment is verified "
        "automatically and codes are delivered the moment it's confirmed.\n\n"
        "Choose an option below to get started."
    )
    if is_admin(update.effective_user.id):
        text += (
            "\n\n🛠 *You're an admin.* This DM is now unlocked to receive "
            "order/support notifications. Commands: /addproduct, /addcodes, "
            "/products, /deactivate, /reply."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)


# ------------------------------------------------------------- Buy Vouchers
async def buy_vouchers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = db.list_products()
    if not products:
        await update.message.reply_text("No products available right now — check back soon!")
        return
    buttons = []
    for p in products:
        stock = db.stock_count(p["id"])
        label = f"{p['name']} — ₹{p['price']:.2f} (Stock: {stock})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"prod:{p['id']}")])
    await update.message.reply_text(
        "🛍 *Choose a product:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show stock/price info and the quantity picker for one product."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":")[1])
    product = db.get_product(product_id)
    if product is None or not product["active"]:
        await query.edit_message_text("That product is no longer available.")
        return
    stock = db.stock_count(product_id)
    if stock <= 0:
        await query.edit_message_text(f"😔 *{md(product['name'])}* is out of stock right now.", parse_mode=ParseMode.MARKDOWN)
        return

    text = (
        f"*{md(product['name'])}*\n\n"
        f"Available stock: *{stock}* codes\n"
        f"Price: ₹{product['price']:.2f} per code\n\n"
        "Select option:"
    )
    buttons = []
    row = []
    for qty in QTY_PRESETS:
        if qty <= stock:
            total = product["price"] * qty
            row.append(InlineKeyboardButton(f"{qty} code{'s' if qty > 1 else ''} — ₹{total:.2f}", callback_data=f"qty:{product_id}:{qty}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Other amount", callback_data=f"qtyother:{product_id}")])
    buttons.append([InlineKeyboardButton("⭐ Back", callback_data="backtoproducts")])

    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def back_to_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.list_products()
    buttons = []
    for p in products:
        stock = db.stock_count(p["id"])
        label = f"{p['name']} — ₹{p['price']:.2f} (Stock: {stock})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"prod:{p['id']}")])
    await query.edit_message_text("🛍 *Choose a product:*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def qty_custom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":")[1])
    product = db.get_product(product_id)
    stock = db.stock_count(product_id)
    context.user_data["buy_product_id"] = product_id
    await query.edit_message_text(
        f"How many codes of *{md(product['name'])}* would you like? (max {stock})\nSend a number, or /cancel.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BUY_WAIT_CUSTOM_QTY


async def qty_custom_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("buy_product_id")
    product = db.get_product(product_id)
    stock = db.stock_count(product_id)
    try:
        qty = int(update.message.text.strip())
        assert 1 <= qty <= stock
    except Exception:
        await update.message.reply_text(f"Please send a whole number between 1 and {stock}, or /cancel.")
        return BUY_WAIT_CUSTOM_QTY
    await show_terms(update, context, product_id, qty, via_message=True)
    return ConversationHandler.END


async def qty_preset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, product_id, qty = query.data.split(":")
    await show_terms(update, context, int(product_id), int(qty), via_message=False)


async def show_terms(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, qty: int, via_message: bool):
    product = db.get_product(product_id)
    total = product["price"] * qty
    text = (
        f"*{md(product['name'])}*\n\n"
        f"🍹 *Terms & Conditions*\n"
        "――――――――――――――――――\n"
        f"{TERMS_TEXT}\n"
        "――――――――――――――――――\n\n"
        f"Service: *{md(product['name'])}*\n"
        f"Qty: *{qty}*  |  Amount: *₹{total:.2f}*\n\n"
        "Tap *I Agree* to confirm you have read and accepted the above terms."
    )
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔥 I Agree — Proceed to Payment", callback_data=f"agree:{product_id}:{qty}")],
            [InlineKeyboardButton("🔮 Cancel", callback_data="backtoproducts")],
        ]
    )
    if via_message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons)
    else:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons)


def verify_bharatpe_payment(utr: str) -> dict:
    """
    Calls the BharatPe transaction-checker API for a given UTR/transaction id.
    Always returns a dict shaped like the API's own response
    ({"success": bool, "message": str, "data": {...}}) — network errors,
    timeouts, or bad JSON are turned into a synthetic success:false result
    rather than raising, so callers can treat every outcome the same way.
    """
    if not BHARATPE_TOKEN:
        return {"success": False, "message": "Auto-verification is not configured."}
    try:
        resp = requests.get(
            BHARATPE_API_URL,
            params={"token": BHARATPE_TOKEN, "utr": utr},
            timeout=15,
        )
        return resp.json()
    except Exception as e:
        log.warning(f"BharatPe verification request failed for UTR {utr}: {e}")
        return {"success": False, "message": "Verification service is unreachable right now."}


async def send_to_admin_for_manual_review(context: ContextTypes.DEFAULT_TYPE, order: dict, utr: str, note: str = ""):
    """Fallback path: forward a payment claim to admins for the original
    manual Approve/Reject flow, used whenever auto-verification can't
    confidently confirm a payment itself."""
    admin_text = (
        "💰 *Payment claim (needs manual review)*\n"
        f"Order: `{order['order_id']}`\n"
        f"Item: {md(order['product_name'])}\n"
        f"Qty: {order['quantity']}\n"
        f"Amount: ₹{order['price']:.2f}\n"
        f"Buyer UTR: `{md(utr)}`\n"
        f"Buyer: {md(order['username'])} (id `{order['user_id']}`)\n"
    )
    if note:
        admin_text += f"\n⚠️ {md(note)}\n"
    admin_text += "\nVerify manually in your BharatPe/bank app, then tap Approve or Reject."
    admin_buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{order['order_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order['order_id']}"),
            ]
        ]
    )
    await notify_admins(context, admin_text, admin_buttons)


def make_qr_bytes(upi_link: str) -> io.BytesIO:
    img = qrcode.make(upi_link)
    buf = io.BytesIO()
    buf.name = "payment_qr.png"
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


async def agree_and_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, product_id, qty = query.data.split(":")
    product_id, qty = int(product_id), int(qty)
    product = db.get_product(product_id)
    stock = db.stock_count(product_id)
    if product is None or not product["active"] or stock < qty:
        await query.edit_message_text("Sorry, that's no longer available in that quantity.")
        return

    user = query.from_user
    order_id = db.create_order(
        user.id, user.username or user.full_name, product_id, product["name"],
        product["price"], quantity=qty, order_prefix=order_prefix_for(user),
    )
    order = db.get_order(order_id)
    total = order["price"]

    upi_link = f"upi://pay?pa={UPI_ID}&pn={SHOP_NAME.replace(' ', '%20')}&am={total:.2f}&cu=INR&tn={order_id}"
    qr_buf = make_qr_bytes(upi_link)

    caption = (
        "💳 *Payment Details*\n\n"
        f"💎 Order ID: `{order_id}`\n"
        f"👑 Service: *{md(product['name'])}*\n"
        f"🗿 Qty: *{qty}*\n"
        f"👀 Amount: *₹{total:.2f}*\n\n"
        "💳 Scan the QR with any UPI app (GPay / PhonePe / BharatPe)\n"
        f"🎗 QR valid for *{QR_VALID_MINUTES} minutes*\n\n"
        "Tap 🔥 I've Paid after payment, then send your UTR/Transaction ID to get verified instantly."
    )
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔥 I've Paid", callback_data=f"paid:{order_id}")]]
    )
    await query.delete_message()
    await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=qr_buf,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons,
    )

    # Auto-expire this order if it's still pending after QR_VALID_MINUTES.
    if context.job_queue is not None:
        context.job_queue.run_once(
            expire_order_job, QR_VALID_MINUTES * 60, data={"order_id": order_id, "chat_id": query.message.chat_id}
        )


async def expire_order_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    if db.expire_if_still_pending(data["order_id"]):
        await context.bot.send_message(
            data["chat_id"],
            f"⌛ The payment window for order `{data['order_id']}` has expired. "
            "Please start a new purchase if you'd still like this item.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def buyer_confirmed_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point for the "🔥 I've Paid" button. Instead of trusting the tap
    itself, we ask the buyer for the UTR (transaction reference number) and
    check it against the BharatPe API in receive_utr() below.
    """
    query = update.callback_query
    order_id = query.data.split(":")[1]
    order = db.get_order(order_id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return ConversationHandler.END
    if order["status"] != "pending":
        await query.answer(f"This order is already {order['status']}.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["verify_order_id"] = order_id
    context.user_data["verify_attempts"] = 0
    await context.bot.send_message(
        query.message.chat_id,
        f"🔎 *Verifying payment*\nOrder: `{order_id}`  |  Amount: ₹{order['price']:.2f}\n\n"
        "Send the *UTR / Transaction (Ref) No.* shown in your UPI/BharatPe app for this payment.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return PAID_WAIT_UTR


async def receive_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the buyer's claimed UTR, checks it against the BharatPe API, and:
      - auto-delivers instantly if it's found and the amount matches this order
      - lets the buyer retry (up to MAX_UTR_ATTEMPTS) if it's not found
      - always falls back to the original manual admin Approve/Reject flow if
        the API can't confidently confirm the payment, so nothing is ever
        silently lost.
    """
    order_id = context.user_data.get("verify_order_id")
    order = db.get_order(order_id) if order_id else None
    if order is None:
        await update.message.reply_text(
            "Something went wrong finding that order — please tap 🔥 I've Paid again.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END
    if order["status"] != "pending":
        await update.message.reply_text(f"This order is already {order['status']}.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    utr = update.message.text.strip()
    if len(utr) < 6:
        await update.message.reply_text(
            "That doesn't look like a valid UTR/reference number. Please send it again, or /cancel."
        )
        return PAID_WAIT_UTR

    if db.utr_already_used(utr):
        await update.message.reply_text(
            "⚠️ This UTR has already been used on a different order. Please double-check and send the "
            "correct one, or /cancel.",
        )
        return PAID_WAIT_UTR

    result = verify_bharatpe_payment(utr)
    db.record_utr_attempt(order_id, utr)

    if result.get("success"):
        data = result.get("data") or {}
        try:
            api_amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            api_amount = 0.0

        if abs(api_amount - order["price"]) > BHARATPE_AMOUNT_TOLERANCE:
            # A real transaction, but not for this order's amount — never
            # auto-approve a mismatch, send it to a human instead.
            await update.message.reply_text(
                f"⚠️ That transaction was found, but its amount (₹{api_amount:.2f}) doesn't match this "
                f"order's total (₹{order['price']:.2f}). Sending to an admin for manual review.",
                reply_markup=MAIN_MENU,
            )
            await send_to_admin_for_manual_review(
                context, order, utr,
                note=f"Amount mismatch — BharatPe shows ₹{api_amount:.2f}, order total is ₹{order['price']:.2f}.",
            )
            return ConversationHandler.END

        codes = db.mark_paid_auto(order_id, utr, data)
        if codes is None:
            await update.message.reply_text(
                "✅ Payment verified, but we're out of stock to deliver right now. An admin has been "
                "notified and will sort this out shortly.",
                reply_markup=MAIN_MENU,
            )
            await notify_admins(
                context,
                f"⚠️ *Auto-verified but out of stock*\nOrder: `{order_id}`\nUTR: `{md(utr)}`\n"
                "Payment matched but stock ran out before delivery — add codes with /addcodes and "
                "deliver to the buyer manually.",
            )
            return ConversationHandler.END

        codes_block = "\n".join(f"`{c}`" for c in codes)
        await update.message.reply_text(
            f"🎉 *Payment verified automatically!*\n"
            f"Order: `{order_id}`\n"
            f"Item: {md(order['product_name'])}\n"
            f"Qty: {order['quantity']}\n\n"
            f"🔑 Your code(s):\n{codes_block}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=MAIN_MENU,
        )
        await notify_admins(
            context,
            f"✅ *Auto-verified via BharatPe*\nOrder: `{order_id}`\nUTR: `{md(utr)}`\n"
            f"Amount matched: ₹{api_amount:.2f}\nBuyer: {md(order['username'])} (id `{order['user_id']}`)",
        )
        return ConversationHandler.END

    # API said success:false (not found / not yet settled / bad token / etc.)
    attempts = context.user_data.get("verify_attempts", 0) + 1
    context.user_data["verify_attempts"] = attempts
    if attempts < MAX_UTR_ATTEMPTS:
        await update.message.reply_text(
            f"❌ {result.get('message', 'Transaction not found.')} Please double-check and send the UTR "
            f"again ({attempts}/{MAX_UTR_ATTEMPTS} attempts), or /cancel.",
        )
        return PAID_WAIT_UTR

    await update.message.reply_text(
        "We couldn't auto-verify this payment. It's been sent to an admin for manual review — "
        "you'll be notified here once it's checked.",
        reply_markup=MAIN_MENU,
    )
    await send_to_admin_for_manual_review(
        context, order, utr, note=result.get("message", "Not found by BharatPe auto-verification.")
    )
    return ConversationHandler.END


# ------------------------------------------------------------ admin decision
async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, order_id = query.data.split(":")
    if not is_admin(query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    await query.answer()

    order = db.get_order(order_id)
    if order is None or order["status"] != "pending":
        await query.edit_message_text(f"Order `{order_id}` already handled.", parse_mode=ParseMode.MARKDOWN)
        return

    if action == "approve":
        codes = db.mark_paid(order_id)
        if codes is None:
            await query.edit_message_text(
                f"⚠️ Not enough stock for order `{order_id}` — could not deliver. "
                "Add more codes with /addcodes, then message the buyer manually.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await query.edit_message_text(f"✅ Approved `{order_id}` — code(s) delivered to buyer.", parse_mode=ParseMode.MARKDOWN)
        codes_block = "\n".join(f"`{c}`" for c in codes)
        await context.bot.send_message(
            order["user_id"],
            f"🎉 *Payment confirmed!*\n"
            f"Order: `{order_id}`\n"
            f"Item: {md(order['product_name'])}\n"
            f"Qty: {order['quantity']}\n\n"
            f"🔑 Your code(s):\n{codes_block}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        db.mark_rejected(order_id)
        await query.edit_message_text(f"❌ Rejected `{order_id}`.", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(
            order["user_id"],
            f"❌ Your payment for order `{order_id}` could not be verified. "
            "If you believe this is a mistake, use Support and reference this order.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ------------------------------------------------------------------ My Orders
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = db.user_orders(update.effective_user.id)
    if not orders:
        await update.message.reply_text("You haven't placed any orders yet.")
        return
    lines = ["📦 *Your Orders*\n"]
    for o in orders:
        lines.append(
            f"💎 `{o['order_id']}`\n{md(o['product_name'])} | Qty {o['quantity']}\n"
            f"₹{o['price']:.2f} | *{o['status'].capitalize()}*\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ------------------------------------------------------------ Recover Vouchers
async def recover_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔑 *Recover Vouchers*\nSend your Order ID.\nExample: `SUMIT-20260725-0E629B`\n\nSend /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return RECOVER_WAIT_ID


async def recover_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = update.message.text.strip()
    order = db.get_order(order_id)
    if order is None:
        await update.message.reply_text("No order found with that ID. Try again, or /cancel.")
        return RECOVER_WAIT_ID
    if order["user_id"] != update.effective_user.id and not is_admin(update.effective_user.id):
        await update.message.reply_text("That order doesn't belong to this account.")
        return ConversationHandler.END

    if order["status"] == "paid":
        codes_block = "\n".join(f"`{c}`" for c in order["voucher_code"].split("\n"))
        text = f"✅ Order `{order_id}` — {order['product_name']} (Qty {order['quantity']})\n🔑 Code(s):\n{codes_block}"
    else:
        text = f"Order `{order_id}` status: *{order['status']}*. No code to show yet."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ------------------------------------------------------------------ Support
async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = db.user_orders(update.effective_user.id)
    if not orders:
        await update.message.reply_text("You have no orders yet — nothing to get support on.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(f"{o['order_id']} — {o['product_name']}", callback_data=f"sup:{o['order_id']}")]
        for o in orders
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="sup:cancel")])
    await update.message.reply_text(
        "🆘 *Support*\nSelect the order you need help with:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SUPPORT_PICK_ORDER


async def support_order_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":")[1]
    if order_id == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END
    context.user_data["support_order_id"] = order_id
    await query.edit_message_text(f"Describe your issue with `{order_id}` — send it as your next message.", parse_mode=ParseMode.MARKDOWN)
    return SUPPORT_WAIT_MSG


async def support_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("support_order_id", "unknown")
    user = update.effective_user
    admin_text = (
        f"🆘 *Support message*\nOrder: `{order_id}`\nFrom: {md(user.username or user.full_name)} (id `{user.id}`)\n\n"
        f"{md(update.message.text)}"
    )
    await notify_admins(context, admin_text)
    await update.message.reply_text("Sent to our team — we'll reply here soon.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# -------------------------------------------------------------- admin: reply
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In the admin chat: /reply <user_id> <text> to message a buyer back."""
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return
    _, uid, msg = parts
    await context.bot.send_message(int(uid), f"💬 *Support reply:*\n{md(msg)}", parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("Sent.")


# ------------------------------------------------------------- admin: catalog
async def admin_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        _, price, *name_parts = update.message.text.split(maxsplit=2)
        name = name_parts[0] if name_parts else "Unnamed"
        pid = db.add_product(name, float(price))
        await update.message.reply_text(f"✅ Added product #{pid}: {name} — ₹{price}")
    except Exception:
        await update.message.reply_text("Usage: /addproduct <price> <name>")


async def admin_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    products = db.list_products(active_only=False)
    if not products:
        await update.message.reply_text("No products yet.")
        return
    lines = [
        f"#{p['id']} {'🟢' if p['active'] else '🔴'} {p['name']} — ₹{p['price']:.2f} (Stock: {db.stock_count(p['id'])})"
        for p in products
    ]
    await update.message.reply_text("\n".join(lines))


async def admin_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        pid = int(update.message.text.split(maxsplit=1)[1])
        db.set_product_active(pid, False)
        await update.message.reply_text(f"Deactivated product #{pid}")
    except Exception:
        await update.message.reply_text("Usage: /deactivate <product_id>")


async def admin_addcodes_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        pid = int(update.message.text.split(maxsplit=1)[1])
    except Exception:
        await update.message.reply_text("Usage: /addcodes <product_id>")
        return ConversationHandler.END
    product = db.get_product(pid)
    if product is None:
        await update.message.reply_text("No such product.")
        return ConversationHandler.END
    context.user_data["addcodes_pid"] = pid
    await update.message.reply_text(
        f"Send the voucher codes for *{md(product['name'])}* now, one per line. /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_WAIT_CODES


async def admin_addcodes_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = context.user_data.get("addcodes_pid")
    codes = update.message.text.splitlines()
    n = db.add_codes(pid, codes)
    await update.message.reply_text(f"✅ Added {n} codes. New stock: {db.stock_count(pid)}")
    return ConversationHandler.END


# ----------------------------------------------------------------- error log
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches any exception raised inside a handler (e.g. a Telegram API
    rejection like a bad Markdown message) and logs it clearly, so failures
    show up in Render's Logs tab instead of silently vanishing.
    """
    log.error("Unhandled exception while processing an update", exc_info=context.error)


# ----------------------------------------------------------------------- main
def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🛍 Buy Vouchers$"), buy_vouchers))
    app.add_handler(MessageHandler(filters.Regex("^📦 My Orders$"), my_orders))

    app.add_handler(CallbackQueryHandler(product_selected, pattern=r"^prod:\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_products, pattern=r"^backtoproducts$"))
    app.add_handler(CallbackQueryHandler(qty_preset_selected, pattern=r"^qty:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(agree_and_pay, pattern=r"^agree:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(approve|reject):"))

    paid_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buyer_confirmed_payment, pattern=r"^paid:")],
        states={PAID_WAIT_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_utr)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(paid_conv)

    buy_custom_qty_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(qty_custom_start, pattern=r"^qtyother:\d+$")],
        states={BUY_WAIT_CUSTOM_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, qty_custom_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(buy_custom_qty_conv)

    recover_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔑 Recover Vouchers$"), recover_start)],
        states={RECOVER_WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, recover_receive_id)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(recover_conv)

    support_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🆘 Support$"), support_start)],
        states={
            SUPPORT_PICK_ORDER: [CallbackQueryHandler(support_order_picked, pattern=r"^sup:")],
            SUPPORT_WAIT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_relay)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(support_conv)

    addcodes_conv = ConversationHandler(
        entry_points=[CommandHandler("addcodes", admin_addcodes_start)],
        states={ADMIN_WAIT_CODES: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_addcodes_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(addcodes_conv)

    app.add_handler(CommandHandler("addproduct", admin_add_product))
    app.add_handler(CommandHandler("products", admin_list_products))
    app.add_handler(CommandHandler("deactivate", admin_deactivate))
    app.add_handler(CommandHandler("reply", admin_reply))
    app.add_error_handler(on_error)

    # Only needed when deployed as a Render (or similar) free "Web Service".
    # Harmless locally — it just opens an extra port nobody hits.
    threading.Thread(target=start_health_server, daemon=True).start()

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
