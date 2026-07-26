Coupon/Voucher Shop Telegram Bot
A ready-to-run bot for selling coupons/vouchers, styled after the
Buy Vouchers → My Orders → Recover Vouchers → Support flow.
What it does
Buy Vouchers — shows active products with live stock. Buyer picks one,
then picks a quantity (1 / 5 / 10 / a custom "Other amount"), then sees
Terms & Conditions with an I Agree / Cancel step.
UPI QR payment page — after I Agree, the bot creates the order and
shows a payment page with a scannable UPI QR code, Order ID, Service, Qty,
Amount, and a "valid for 10 minutes" countdown. Orders left unpaid after
that window auto-expire.
Payment verification — buyer taps "I've Paid" and sends their UTR/
transaction reference number; the bot checks it against the BharatPe
transaction-checker API (BHARATPE_TOKEN) and, if the amount matches
this order, delivers instantly with no admin involved. If the UTR isn't
found, the amount doesn't match, or the API is unreachable, the order
falls back to the original manual flow — every admin in ADMIN_USER_IDS
gets a DM with Approve/Reject buttons. A UTR can never be reused across
two orders.
Instant delivery — on Approve, the bot atomically pulls the right
number of unused codes from your stock and DMs them to the buyer.
Colorful buttons — every button (menu, product list, quantity picker,
Approve/Reject, etc.) uses Telegram's built-in button colors: green for
"go" actions and in-stock products, blue for neutral choices, red for
cancel/reject/sold-out. This uses each Telegram client's native button
styling (Bot API 9.4+), not custom images, so it looks right in every
client automatically. Optionally set PREMIUM_EMOJI_IDS (JSON, e.g.
{"buy": "5368324170671202286"}) to show your own premium emoji as an
icon on specific buttons — this only renders for chats where the bot
owner's Telegram account has an active Premium subscription; everyone
else just sees the button without an icon, so it's safe to leave set
either way.
My Orders — buyer sees their order history, quantity, and status.
Recover Vouchers — buyer re-fetches their code(s) by Order ID.
Support — buyer picks an order and messages you; every admin gets it
as a DM, and any admin can reply with /reply <user_id>  from
their own DM with the bot.
All admin actions happen in a private DM with the bot — no admin group
needed.
Order IDs are formatted like SUMIT-20260725-0E629B — prefixed with the
buyer's Telegram first name, then the date, then a random suffix.
Install
Bash
Create your bot
Talk to @BotFather on Telegram → /newbot → copy the token.
Every admin needs to open a DM with the new bot and send /start at
least once. Telegram only lets a bot message someone after that person
has messaged it first — skip this and that admin silently won't get any
order/support notifications.
Get each admin's numeric Telegram user id: DM @userinfobot
and it replies with your id. Collect one per admin.
2b. Set up MongoDB
Storage (products, stock, orders) is MongoDB, via a free
MongoDB Atlas cluster:
Create a free account, then a free M0 cluster.
Database Access → add a database user with a username/password.
Network Access → add 0.0.0.0/0 (allow from anywhere) unless you
have a fixed server IP — Render's free tier doesn't give you one.
Connect → Drivers → copy the connection string. It looks like:
mongodb+srv://:@cluster0.xxxxx.mongodb.net/
You'll set this as MONGO_URI in the next step.
(Running MongoDB locally instead is also fine for testing —
MONGO_URI=mongodb://localhost:27017 is the default if you don't set one.)
Configure
Set environment variables (or edit the constants at the top of bot.py):
Bash
Run
Bash
Add products & stock (as admin)
Send these directly to the bot in your own DM (each admin has their own
access, once they've sent /start):
Code
Each pasted code becomes exactly one unit of stock. When a buyer's payment
is approved, one code is claimed and can never be handed out twice.
Deploy on Render (free tier)
Render's free tier only offers Web Services (things that answer HTTP
requests) for free — Background Workers cost $7/mo minimum. This bot
uses Telegram long-polling, not HTTP, so bot.py includes a tiny built-in
health-check server (start_health_server()) purely so Render sees a live
port. This lets you run it as a free Web Service.
About persistence now
Previously this bot used SQLite, which lived on Render's disk — and Render's
free Web Services have no persistent disk, so the database got wiped on
every restart/redeploy/sleep-wake cycle. Now that storage is MongoDB (e.g.
Atlas), your data lives outside Render entirely and survives all of
that just fine. The one remaining free-tier quirk is below.
⚠️ The bot still goes offline when Render's free instance sleeps
Render's free Web Services spin down after 15 minutes with no HTTP traffic.
While asleep, the bot can't poll Telegram, so it won't respond — but no
data is lost, and it picks back up automatically once the instance wakes.
To avoid the downtime rather than just recover from it, set up the
keep-alive ping in step 5 below.
Steps
Push this project to a GitHub (or GitLab) repo.
In Render: New → Blueprint, point it at your repo — it will pick up
render.yaml automatically and pre-fill a free Web Service.
(No render.yaml? Use New → Web Service instead, runtime "Python 3",
build command pip install -r requirements.txt, start command
python bot.py, instance type Free.)
Under Environment, set BOT_TOKEN, ADMIN_USER_IDS,
UPI_ID, and MONGO_URI (these are marked sync: false in the
blueprint so Render will prompt you for them rather than storing them
in the repo).
Deploy. Render gives you a URL like https://coupon-shop-bot.onrender.com.
Set up the keep-alive (skip this and the bot goes offline after 15
min of inactivity): create a free account at
UptimeRobot, add an HTTP(s) monitor
pointed at your Render URL, checking every 5 minutes. This keeps the
service awake so the bot's Telegram connection stays alive.
Notes / things you may want to change
Payment method: UPI QR + auto-verification via the BharatPe
transaction-checker API. Set BHARATPE_TOKEN to your BharatPe token; if
it's unset, the API call fails, or the UTR/amount doesn't check out, the
bot always falls back to the manual admin Approve/Reject buttons so a
real payment is never lost. MAX_UTR_ATTEMPTS (default 3) caps how many
UTR guesses a buyer gets before falling back, and
BHARATPE_AMOUNT_TOLERANCE (default ₹0.01, i.e. exact match) is only
there to absorb genuine float noise — it's intentionally too tight to
let an underpayment (e.g. paying ₹1 for a ₹2 coupon) slip through as a
"match".
Storage: MongoDB (MONGO_URI / MONGO_DB_NAME) — works with a free
MongoDB Atlas cluster, a self-hosted Mongo instance, or localhost for
local dev. Codes are claimed one at a time with atomic per-document
updates so two buyers can never receive the same code, even under
concurrent purchases.
Scaling admins: add as many ADMIN_USER_IDS as you like, comma-separated.
Hosting: any VPS, Railway, Render, or a Raspberry Pi works — just keep
python bot.py running (e.g. with systemd, pm2, or screen).
