# Coupon/Voucher Shop Telegram Bot

A ready-to-run bot for selling coupons/vouchers, styled after the
Buy Vouchers → My Orders → Recover Vouchers → Support flow.

## What it does
- **Buy Vouchers** — shows active products with live stock. Buyer picks one,
  then picks a quantity (1 / 5 / 10 / a custom "Other amount"), then sees
  Terms & Conditions with an I Agree / Cancel step.
- **UPI QR payment page** — after I Agree, the bot creates the order and
  shows a payment page with a scannable UPI QR code, Order ID, Service, Qty,
  Amount, and a "valid for 10 minutes" countdown. Orders left unpaid after
  that window auto-expire.
- **Payment verification** — buyer taps "I've Paid"; the claim is forwarded
  to your admin group with Approve/Reject buttons (manual verification stops
  fake-payment fraud — no auto-trust of a button tap).
- **Instant delivery** — on Approve, the bot atomically pulls the right
  number of unused codes from your stock and DMs them to the buyer.
- **My Orders** — buyer sees their order history, quantity, and status.
- **Recover Vouchers** — buyer re-fetches their code(s) by Order ID.
- **Support** — buyer picks an order and messages you; you reply with
  `/reply <user_id> <message>` in the admin chat.

Order IDs are formatted like `SUMIT-20260725-0E629B` — prefixed with the
buyer's Telegram first name, then the date, then a random suffix.

## 1. Install
```bash
pip install -r requirements.txt
```

## 2. Create your bot
1. Talk to [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.
2. Create a private Telegram group for yourself (this is where payment
   approvals and support messages land). Add your bot to it.
3. Get that group's chat id — easiest way: add [@userinfobot](https://t.me/userinfobot)
   to the group temporarily, or send a message in the group and check the
   bot's logs / use `getUpdates`.

## 2b. Set up MongoDB
Storage (products, stock, orders) is MongoDB, via a free
[MongoDB Atlas](https://www.mongodb.com/cloud/atlas/register) cluster:
1. Create a free account, then a free **M0** cluster.
2. **Database Access** → add a database user with a username/password.
3. **Network Access** → add `0.0.0.0/0` (allow from anywhere) unless you
   have a fixed server IP — Render's free tier doesn't give you one.
4. **Connect → Drivers** → copy the connection string. It looks like:
   `mongodb+srv://<user>:<password>@cluster0.xxxxx.mongodb.net/`
5. You'll set this as `MONGO_URI` in the next step.

(Running MongoDB locally instead is also fine for testing —
`MONGO_URI=mongodb://localhost:27017` is the default if you don't set one.)

## 3. Configure
Set environment variables (or edit the constants at the top of `bot.py`):

```bash
export BOT_TOKEN="123456:ABC-your-bot-token"
export ADMIN_CHAT_ID="-1001234567890"      # your admin group id
export ADMIN_USER_IDS="111111111,222222222" # your personal Telegram user id(s)
export UPI_ID="yourupi@bank"
export SHOP_NAME="My Coupon Shop"
export QR_VALID_MINUTES="10"                # optional, defaults to 10
export TERMS_TEXT="No returns after delivery. Coupons are fresh and verified — please know the usage before buying."
export MONGO_URI="mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/"
export MONGO_DB_NAME="coupon_shop"          # optional, defaults to coupon_shop
```

## 4. Run
```bash
python bot.py
```

## 5. Add products & stock (as admin)
In the admin chat or your DM with the bot:
```
/addproduct 150 Shein 1000 per 800 off
/products                     -> shows #id, price, live stock
/addcodes 1                   -> bot asks for codes, then paste one per line:
SHEIN-CODE-AAA111
SHEIN-CODE-BBB222
/deactivate 1                 -> hide a retired product
```

Each pasted code becomes exactly one unit of stock. When a buyer's payment
is approved, one code is claimed and can never be handed out twice.

## 6. Deploy on Render (free tier)

Render's free tier only offers **Web Services** (things that answer HTTP
requests) for free — **Background Workers cost $7/mo minimum**. This bot
uses Telegram long-polling, not HTTP, so `bot.py` includes a tiny built-in
health-check server (`start_health_server()`) purely so Render sees a live
port. This lets you run it as a free Web Service.

### About persistence now
Previously this bot used SQLite, which lived on Render's disk — and Render's
free Web Services have **no persistent disk**, so the database got wiped on
every restart/redeploy/sleep-wake cycle. Now that storage is MongoDB (e.g.
Atlas), **your data lives outside Render entirely** and survives all of
that just fine. The one remaining free-tier quirk is below.

### ⚠️ The bot still goes offline when Render's free instance sleeps
Render's free Web Services spin down after 15 minutes with no HTTP traffic.
While asleep, the bot can't poll Telegram, so it won't respond — but no
data is lost, and it picks back up automatically once the instance wakes.
To avoid the downtime rather than just recover from it, set up the
keep-alive ping in step 5 below.

### Steps
1. Push this project to a GitHub (or GitLab) repo.
2. In Render: **New → Blueprint**, point it at your repo — it will pick up
   `render.yaml` automatically and pre-fill a free Web Service.
   (No `render.yaml`? Use **New → Web Service** instead, runtime "Python 3",
   build command `pip install -r requirements.txt`, start command
   `python bot.py`, instance type **Free**.)
3. Under **Environment**, set `BOT_TOKEN`, `ADMIN_CHAT_ID`, `ADMIN_USER_IDS`,
   `UPI_ID`, and `MONGO_URI` (these are marked `sync: false` in the
   blueprint so Render will prompt you for them rather than storing them
   in the repo).
4. Deploy. Render gives you a URL like `https://coupon-shop-bot.onrender.com`.
5. **Set up the keep-alive** (skip this and the bot goes offline after 15
   min of inactivity): create a free account at
   [UptimeRobot](https://uptimerobot.com), add an **HTTP(s)** monitor
   pointed at your Render URL, checking every 5 minutes. This keeps the
   service awake so the bot's Telegram connection stays alive.

## Notes / things you may want to change
- **Payment method**: this build uses manual UPI + admin approval, since
  that's the safest default without hooking up a real payment gateway. If
  you want automatic verification, swap in a payment gateway (Razorpay,
  Cashfree, Stripe, etc.) that gives you a webhook, and call `db.mark_paid()`
  from that webhook instead of the admin Approve button.
- **Storage**: MongoDB (`MONGO_URI` / `MONGO_DB_NAME`) — works with a free
  MongoDB Atlas cluster, a self-hosted Mongo instance, or `localhost` for
  local dev. Codes are claimed one at a time with atomic per-document
  updates so two buyers can never receive the same code, even under
  concurrent purchases.
- **Scaling admins**: add as many `ADMIN_USER_IDS` as you like, comma-separated.
- **Hosting**: any VPS, Railway, Render, or a Raspberry Pi works — just keep
  `python bot.py` running (e.g. with `systemd`, `pm2`, or `screen`).
