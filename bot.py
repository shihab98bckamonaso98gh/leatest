"""
STEX SMS Telegram Bot – Persistent Session + Railway Ready
===========================================================
✅ Persistent browser session via storage‑state (cookies saved)
✅ Automatic re‑login if session expired
✅ Health server + heartbeat keep Railway logs alive
✅ All features: OTP, 2FA, Fake Details, Balance, Withdraw, Admin, Fake OTP
"""

import asyncio, logging, re, os, json, time, random, string, signal, sys, threading, sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date
from typing import Optional, Dict, Set, Tuple, List
from pathlib import Path

# Load .env (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Telegram
from telegram import (
    Update,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, ChatMember, CopyTextButton
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
from telegram.error import BadRequest

# 2FA
try:
    import pyotp
    TOTP_AVAILABLE = True
except ImportError:
    TOTP_AVAILABLE = False

# Playwright
from playwright.async_api import async_playwright, Page

# ===================== CONFIGURATION =====================
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_NAME       = os.getenv("BOT_NAME", "SMS OTP Bot")
SMS_EMAIL      = os.getenv("STEX_EMAIL", "")
SMS_PASSWORD   = os.getenv("STEX_PASSWORD", "")
OTP_GROUP_ID   = int(os.getenv("OTP_GROUP_ID", "0"))
OTP_GROUP_LINK = os.getenv("OTP_GROUP_LINK", "https://t.me/your_otp_group")
OWNER_USER_ID  = 5705479420
HEALTH_PORT    = int(os.getenv("PORT", "0"))

DATA_DIR       = os.getenv("DATA_DIR", ".")
DB_FILE        = os.path.join(DATA_DIR, "bot_data.db")
RATE_CONFIG_FILE = os.path.join(DATA_DIR, "rate_config.json")
WITHDRAW_CONFIG_FILE = os.path.join(DATA_DIR, "withdraw_config.json")
ADMIN_USERS_FILE = os.path.join(DATA_DIR, "admin_users.json")
FAKE_OTP_CONFIG_FILE = os.path.join(DATA_DIR, "fake_otp_config.json")
STORAGE_STATE_DIR = os.path.join(DATA_DIR, "browser_states")

Path(STORAGE_STATE_DIR).mkdir(parents=True, exist_ok=True)

_admin_users_env = os.getenv("ADMIN_USERS", "").strip()

def _load_admins() -> Set[int]:
    s = set()
    if _admin_users_env:
        s.update(int(x) for x in _admin_users_env.split(",") if x.strip().isdigit())
    if os.path.exists(ADMIN_USERS_FILE):
        try:
            with open(ADMIN_USERS_FILE, "r") as f:
                s.update(int(x) for x in json.load(f) if str(x).isdigit())
        except:
            pass
    return s

def _save_admins(admins: Set[int]):
    with open(ADMIN_USERS_FILE, "w") as f:
        json.dump(list(admins), f)

ADMIN_USERS = _load_admins()
CHANGE_NUMBER_DELAY = 0   # seconds

def load_sms_rate():
    if os.path.exists(RATE_CONFIG_FILE):
        try:
            with open(RATE_CONFIG_FILE, 'r') as f:
                return float(json.load(f).get('rate', 0.0))
        except:
            pass
    return 0.0

def save_sms_rate(rate: float):
    with open(RATE_CONFIG_FILE, 'w') as f:
        json.dump({'rate': rate}, f)

def load_min_withdraw():
    if os.path.exists(WITHDRAW_CONFIG_FILE):
        try:
            with open(WITHDRAW_CONFIG_FILE, 'r') as f:
                return float(json.load(f).get('min', 10.0))
        except:
            pass
    return 10.0

def save_min_withdraw(min_val: float):
    with open(WITHDRAW_CONFIG_FILE, 'w') as f:
        json.dump({'min': min_val}, f)

SMS_RATE_BDT = load_sms_rate()
MIN_WITHDRAW_BDT = load_min_withdraw()

def load_fake_otp_config() -> Dict[str, List[Dict]]:
    if os.path.exists(FAKE_OTP_CONFIG_FILE):
        try:
            with open(FAKE_OTP_CONFIG_FILE, 'r') as f:
                data = json.load(f)
                result = {"fb": [], "ig": []}
                for k in ("fb", "ig"):
                    val = data.get(k)
                    if isinstance(val, list):
                        result[k] = val
                    elif isinstance(val, dict) and "country_name" in val:
                        result[k] = [val]
                return result
        except:
            pass
    return {"fb": [], "ig": []}

def save_fake_otp_config(config: Dict[str, List[Dict]]):
    with open(FAKE_OTP_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

FAKE_OTP_CONFIG = load_fake_otp_config()

EXCHANGE_RATE = 125.0   # 1 USD = 125 BDT

# ===================== DATABASE =====================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance_bdt REAL DEFAULT 0,
                bkash TEXT,
                rocket TEXT,
                binance TEXT,
                total_otps INTEGER DEFAULT 0,
                today_otps INTEGER DEFAULT 0,
                last_otp_date TEXT,
                today_earned REAL DEFAULT 0,
                total_earned REAL DEFAULT 0,
                numbers_used INTEGER DEFAULT 0
            )
        ''')
        for col, col_def in [
            ('total_otps', 'INTEGER DEFAULT 0'),
            ('today_otps', 'INTEGER DEFAULT 0'),
            ('last_otp_date', 'TEXT'),
            ('today_earned', 'REAL DEFAULT 0'),
            ('total_earned', 'REAL DEFAULT 0'),
            ('numbers_used', 'INTEGER DEFAULT 0')
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                method TEXT,
                account TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id INTEGER,
                site TEXT,
                email TEXT,
                password TEXT,
                PRIMARY KEY (user_id, site)
            )
        ''')
    log.info("✅ Database initialised")

def ensure_user_exists(uid: int):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (uid,))

def get_user_balance(uid: int) -> float:
    ensure_user_exists(uid)
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute('SELECT balance_bdt FROM users WHERE user_id = ?', (uid,)).fetchone()
    return row[0] if row else 0.0

def get_user_wallet(uid: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute('SELECT bkash, rocket, binance FROM users WHERE user_id = ?', (uid,)).fetchone()
    if row:
        return {'bkash': row[0], 'rocket': row[1], 'binance': row[2]}
    return {'bkash': None, 'rocket': None, 'binance': None}

def credit_user(uid: int, amount: float):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('UPDATE users SET balance_bdt = balance_bdt + ? WHERE user_id = ?', (amount, uid))

def deduct_user(uid: int, amount: float):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('UPDATE users SET balance_bdt = balance_bdt - ? WHERE user_id = ?', (amount, uid))

def update_wallet(uid: int, wallet_type: str, number: str):
    col = {'bkash': 'bkash', 'rocket': 'rocket', 'binance': 'binance'}.get(wallet_type)
    if col:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(f'UPDATE users SET {col} = ? WHERE user_id = ?', (number, uid))

def create_withdrawal(uid: int, method: str, account: str, amount: float) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            'INSERT INTO withdrawals (user_id, method, account, amount) VALUES (?,?,?,?)',
            (uid, method, account, amount)
        )
        return cur.lastrowid

def get_pending_withdrawals():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM withdrawals WHERE status = 'pending'").fetchall()

def get_approved_withdrawals():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM withdrawals WHERE status = 'approved' ORDER BY approved_at DESC").fetchall()

def approve_withdrawal(withdrawal_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute('SELECT user_id, amount FROM withdrawals WHERE id = ?', (withdrawal_id,)).fetchone()
        if not row:
            return False
        uid, amount = row
        deduct_user(uid, amount)
        conn.execute(
            "UPDATE withdrawals SET status = 'approved', approved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (withdrawal_id,)
        )
        conn.commit()
        return True

def update_user_stats(uid: int, earned: float = 0.0, otp_count: int = 0, numbers_used: int = 0):
    today_str = date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        ensure_user_exists(uid)
        row = conn.execute('SELECT last_otp_date, today_otps, today_earned FROM users WHERE user_id = ?', (uid,)).fetchone()
        last_date = row[0] if row else None
        if last_date != today_str:
            conn.execute('''UPDATE users SET today_otps = ?, today_earned = ?, last_otp_date = ?,
                            numbers_used = numbers_used + ? WHERE user_id = ?''',
                         (otp_count, earned, today_str, numbers_used, uid))
        else:
            conn.execute('''UPDATE users SET total_otps = total_otps + ?,
                            today_otps = today_otps + ?,
                            total_earned = total_earned + ?,
                            today_earned = today_earned + ?,
                            numbers_used = numbers_used + ?
                            WHERE user_id = ?''',
                         (otp_count, otp_count, earned, earned, numbers_used, uid))

def get_user_stats(uid: int) -> Dict:
    ensure_user_exists(uid)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM users WHERE user_id = ?', (uid,)).fetchone()
        if not row:
            return {}
        today = date.today().isoformat()
        if row['last_otp_date'] != today:
            today_otps = 0
            today_earned = 0.0
        else:
            today_otps = row['today_otps']
            today_earned = row['today_earned']
        total_withdrawn = sum_withdrawals_for_user(uid)
        return {
            'numbers_used': row['numbers_used'],
            'today_otps': today_otps,
            'total_otps': row['total_otps'],
            'today_earned_bdt': today_earned,
            'total_earned_bdt': row['total_earned'],
            'total_withdrawn_bdt': total_withdrawn,
            'balance_bdt': row['balance_bdt']
        }

def sum_withdrawals_for_user(uid: int) -> float:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT SUM(amount) FROM withdrawals WHERE user_id = ? AND status = 'approved'", (uid,)).fetchone()
    return row[0] or 0.0

def get_all_user_ids() -> List[int]:
    with sqlite3.connect(DB_FILE) as conn:
        return [row[0] for row in conn.execute('SELECT user_id FROM users').fetchall()]

def get_admin_stats() -> Dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        today = date.today().isoformat()
        row = conn.execute('''
            SELECT
                SUM(numbers_used) as total_numbers_used,
                SUM(CASE WHEN last_otp_date = ? THEN today_otps ELSE 0 END) as total_today_otps,
                SUM(total_otps) as total_otps,
                SUM(CASE WHEN last_otp_date = ? THEN today_earned ELSE 0 END) as total_today_earned,
                SUM(total_earned) as total_earned
            FROM users
        ''', (today, today)).fetchone()
        total_withdrawn = conn.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'approved'").fetchone()[0] or 0.0
        return {
            'numbers_used': row['total_numbers_used'] or 0,
            'today_otps': row['total_today_otps'] or 0,
            'total_otps': row['total_otps'] or 0,
            'today_cost_bdt': row['total_today_earned'] or 0.0,
            'total_cost_bdt': row['total_earned'] or 0.0,
            'total_withdrawn_bdt': total_withdrawn
        }

def store_credentials(uid: int, site: str, email: str, password: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('INSERT OR REPLACE INTO user_credentials (user_id, site, email, password) VALUES (?,?,?,?)',
                     (uid, site, email, password))

def get_credentials(uid: int, site: str) -> Optional[Tuple[str, str]]:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute('SELECT email, password FROM user_credentials WHERE user_id = ? AND site = ?', (uid, site)).fetchone()
    return row if row else None

def remove_credentials(uid: int, site: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('DELETE FROM user_credentials WHERE user_id = ? AND site = ?', (uid, site))
    if uid in _user_pages:
        page = _user_pages[uid].pop(site, None)
        if page:
            asyncio.create_task(_safe_close_page(page))
        if not _user_pages[uid]:
            del _user_pages[uid]

# ===================== SITES =====================
SITES = {
    "stexsms": {
        "name": "StexSMS",
        "login_url": "https://stexsms.com/m29/#/auth/login",
        "dialer_url": "https://stexsms.com/m29/#/dialer/getnum?m=n",
    },
    "voltxsms": {
        "name": "VoltxSMS",
        "login_url": "https://voltxsms.com/m29/#/auth/login",
        "dialer_url": "https://voltxsms.com/m29/#/dialer/getnum?m=n",
    },
}

POLL_INTERVAL   = 3
MONITOR_TIMEOUT = 480
FETCH_RETRIES   = 3

# ===================== LOGGING =====================
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("telegram._bot").setLevel(logging.WARNING)
log = logging.getLogger("smsbot")
log.setLevel(logging.INFO)

# ===================== GLOBAL STATE =====================
user_sessions: dict[int, dict] = {}
_playwright_obj = None
_browser        = None
_browser_lock   = asyncio.Lock()
_page_lock      = asyncio.Lock()
GLOBAL_CONTEXTS: Dict[str, any] = {}
GLOBAL_STORAGE_PATHS: Dict[str, str] = {}
_user_pages: Dict[int, Dict[str, Page]] = {}

MALE_NAMES   = ["Liam","Noah","Oliver","Elijah","James","William","Benjamin","Lucas","Henry","Alexander",
                "Mason","Michael","Ethan","Daniel","Jacob","Logan","Jackson","Levi","Sebastian","Mateo",
                "Jack","Owen","Theodore","Aiden","Samuel","Joseph","John","David","Wyatt","Matthew","Luke",
                "Asher","Carter","Julian","Grayson","Leo","Jayden","Gabriel","Isaac","Lincoln","Anthony",
                "Hudson","Dylan","Ezra","Thomas","Charles","Christopher","Jaxon","Maverick","Josiah"]
FEMALE_NAMES = ["Olivia","Emma","Ava","Charlotte","Sophia","Amelia","Isabella","Mia","Evelyn","Harper",
                "Camila","Gianna","Abigail","Luna","Ella","Elizabeth","Sofia","Emily","Avery","Mila",
                "Scarlett","Eleanor","Madison","Layla","Penelope","Aria","Chloe","Grace","Ellie","Nora",
                "Hazel","Zoey","Riley","Victoria","Lily","Aurora","Violet","Nova","Hannah","Emilia",
                "Zoe","Stella","Everly","Isla","Leah","Lillian","Addison","Willow","Lucy","Paisley"]
LAST_NAMES   = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez",
                "Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson","Thomas","Taylor","Moore",
                "Jackson","Martin","Lee","Perez","Thompson","White","Harris","Sanchez","Clark","Ramirez",
                "Lewis","Robinson","Walker","Young","Allen","King","Wright","Scott","Torres","Nguyen",
                "Hill","Flores","Green","Adams","Nelson","Baker","Hall","Rivera","Campbell","Mitchell",
                "Carter","Roberts"]

auto_fake_running = False
auto_fake_task: Optional[asyncio.Task] = None
BOT_USERNAME: Optional[str] = None

# ===================== HEALTH SERVER =====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server(port: int):
    if port <= 0:
        return
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"🌐 Health server listening on port {port}")

# ===================== HEARTBEAT =====================
async def heartbeat_logger(app: Application):
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        log.info("💓 Bot heartbeat – still alive")

# ===================== HELPERS =====================
def _stop_monitor(uid: int):
    s = user_sessions.get(uid, {})
    task = s.get("monitor_task")
    if task and not task.done():
        task.cancel()

def _is_owner(uid: int) -> bool: return uid == OWNER_USER_ID
def _is_admin(uid: int) -> bool: return uid in ADMIN_USERS or _is_owner(uid)

def _mask_number(number: str) -> str:
    if len(number) <= 6: return number
    prefix = "+" if number.startswith("+") else ""
    num = number.lstrip("+")
    if len(num) <= 6: return f"{prefix}{num}"
    first, last = num[:3], num[-3:]
    mid = '*' * (len(num)-6)
    return f"{prefix}{first}{mid}{last}"

def _extract_otp(message: str) -> Optional[str]:
    match = re.search(r"\b\d{1,3}(?:\s?\d{1,3})+\b", message)
    if match:
        digits = re.sub(r"\D", "", match.group())
        if 4 <= len(digits) <= 8: return digits
    no_spaces = re.sub(r"\s+", "", message)
    fallback = re.findall(r"\b\d{4,8}\b", no_spaces)
    return fallback[0] if fallback else None

def _clean_full_msg(full_msg: str) -> str:
    for p in [r"^Facebook:\s*", r"^Instagram:\s*", r"^WhatsApp:\s*"]:
        full_msg = re.sub(p, "", full_msg, count=1)
    return full_msg.strip()

def generate_2fa_code(secret_key: str) -> dict:
    if not TOTP_AVAILABLE:
        return {"success": False, "message": "pyotp library not installed."}
    try:
        clean = ''.join(secret_key.split()).upper()
        code = pyotp.TOTP(clean).now()
        return {"success": True, "code": code}
    except Exception:
        return {"success": False, "message": "Invalid Secret Key"}

def generate_identity(gender: str) -> dict:
    today_day = datetime.now().day
    day_str = f"{today_day:02d}"
    first = random.choice(MALE_NAMES if gender.lower()=="male" else FEMALE_NAMES)
    last  = random.choice(LAST_NAMES)
    full_name = f"{first} {last}"
    digits = ''.join(random.choices(string.digits, k=random.randint(2,3)))
    username = f"{first.lower()}{last.lower()}{digits}"
    chars = string.ascii_letters + string.digits + "#$&"
    base = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase),
            random.choice(string.digits), random.choice("#$&")]
    base += [random.choice(chars) for _ in range(random.randint(8,10)-4)]
    random.shuffle(base)
    password = ''.join(base) + day_str
    return {"name": full_name, "username": username, "password": password, "gender": gender}

async def _safe_close_page(page):
    try: await page.close()
    except: pass

# ===================== FORCED GROUP MEMBERSHIP =====================
async def _check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=user.id)
        if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
    except BadRequest:
        pass
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Join Channel", url=OTP_GROUP_LINK),
        InlineKeyboardButton("Verify", callback_data="verify_membership")
    ]])
    if update.message:
        await update.message.reply_text("🔒 Access Restricted!\n\nPlease join our channel to use this bot.", reply_markup=kb)
    return False

async def verify_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user = query.from_user
    try:
        member = await context.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=user.id)
        if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await query.edit_message_text("✅ Verified! You can now use the bot.\nUse /start to begin.")
            return
    except BadRequest: pass
    await query.answer("You are not yet a member of the channel.", show_alert=True)

# ===================== PERSISTENT BROWSER =====================
async def _ensure_playwright():
    global _playwright_obj, _browser, GLOBAL_CONTEXTS
    async with _browser_lock:
        if _playwright_obj is None or (_browser is not None and not _browser.is_connected()):
            if _playwright_obj:
                await _playwright_obj.stop()
            _playwright_obj = await async_playwright().start()
            _browser = await _playwright_obj.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-blink-features=AutomationControlled"]
            )
            # Close any old contexts
            for ctx in GLOBAL_CONTEXTS.values():
                try: await ctx.close()
                except: pass
            GLOBAL_CONTEXTS.clear()

async def _get_global_site_context(site: str):
    """Return a persistent context for the site, logged in with global credentials."""
    await _ensure_playwright()
    path = os.path.join(STORAGE_STATE_DIR, f"{site}_global.json")
    GLOBAL_STORAGE_PATHS[site] = path

    if site in GLOBAL_CONTEXTS:
        ctx = GLOBAL_CONTEXTS[site]
        try:
            # Quick check if still logged in
            page = await ctx.new_page()
            await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=15000)
            if await page.locator(".user-dropdown, table.gn-tbl").count() > 0:
                await page.close()
                return ctx
            await page.close()
        except:
            pass
        # Expired, close old context
        try: await ctx.close()
        except: pass
        del GLOBAL_CONTEXTS[site]

    # Load saved storage state
    storage_state = None
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                storage_state = json.load(f)
        except:
            pass

    ctx = await _browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        viewport={"width":1280,"height":800},
        storage_state=storage_state
    )
    await ctx.grant_permissions(["clipboard-read"])

    # Verify login
    page = await ctx.new_page()
    await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=15000)
    logged_in = await page.locator(".user-dropdown, table.gn-tbl").count() > 0
    await page.close()

    if not logged_in:
        # Need to login
        page = await ctx.new_page()
        try:
            await page.goto(SITES[site]["login_url"], wait_until="networkidle", timeout=30000)
            await page.fill("input[type='email']", SMS_EMAIL)
            await page.fill("input[type='password']", SMS_PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_url(lambda u: "auth" not in u and "login" not in u, timeout=60000)
        except:
            try:
                await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=10000)
            except:
                await page.close()
                await ctx.close()
                raise Exception(f"Global login failed for {site}")
        await page.close()
        # Save new storage state
        storage = await ctx.storage_state()
        with open(path, 'w') as f:
            json.dump(storage, f)

    GLOBAL_CONTEXTS[site] = ctx
    return ctx

async def _get_page_for_global(site: str) -> Page:
    """Get a fresh page from the global persistent context, navigated to dialer URL."""
    ctx = await _get_global_site_context(site)
    page = await ctx.new_page()
    await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
    return page

async def _login_with_credentials(page: Page, site: str, email: str, password: str) -> bool:
    try:
        await page.goto(SITES[site]["login_url"], wait_until="networkidle", timeout=30000)
        await page.fill("input[type='email']", email)
        await page.fill("input[type='password']", password)
        await page.click("button[type='submit']")
        try:
            await page.wait_for_url(lambda u: "auth" not in u and "login" not in u, timeout=60000)
            return True
        except:
            if "/dialer/" in page.url:
                return True
            try:
                await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=10000)
                return True
            except:
                pass
        return False
    except Exception:
        return False

async def _ensure_page_logged_in(site: str, user_id: int = None) -> Page:
    """Return a logged-in page. For global accounts, use persistent context."""
    if user_id is None or get_credentials(user_id, site) is None:
        # Global account
        return await _get_page_for_global(site)
    else:
        # User custom account
        creds = get_credentials(user_id, site)
        user_pages = _user_pages.setdefault(user_id, {})
        page = user_pages.get(site)
        if page is None or page.is_closed():
            ctx = await _browser.new_context(viewport={"width":1280,"height":800})
            await ctx.grant_permissions(["clipboard-read"])
            page = await ctx.new_page()
            user_pages[site] = page
            if not await _login_with_credentials(page, site, creds[0], creds[1]):
                await page.close()
                del user_pages[site]
                raise Exception(f"Login failed for {site} with custom credentials")
        else:
            # Verify still logged in
            try:
                await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=5000)
            except:
                if not await _login_with_credentials(page, site, creds[0], creds[1]):
                    await page.close()
                    del user_pages[site]
                    raise Exception(f"Re‑login failed for {site}")
        await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
        return page

# ===================== FETCH & POLL =====================
async def fetch_number(range_str: str, site: str, user_id: int = None) -> Optional[dict]:
    last_exception = None
    for attempt in range(1, FETCH_RETRIES+1):
        try:
            page = await _ensure_page_logged_in(site, user_id)
            async with _page_lock:
                if "/dialer/" not in page.url:
                    await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
                first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
                old_number = None
                if await first_row.count() > 0:
                    old_number = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")
                inp = page.locator("input.gn-range-input")
                await inp.wait_for(state="visible", timeout=15000)
                await inp.fill(""); await inp.type(range_str, delay=25); await asyncio.sleep(0.15)
                await page.locator("button.btn.btn-primary:has-text('Get Number')").click()

                try:
                    await page.wait_for_function(
                        """(old)=>{const r=document.querySelectorAll('table.gn-tbl tbody tr');
                        for(let i=0;i<r.length;i++){let n=r[i].querySelector('.gn-num');
                        if(n&&n.textContent.trim().replace(/^\\+/,'')!==old)return true;}return false;}""",
                        arg=old_number or "", timeout=15000)
                except PlaywrightTimeoutError:
                    return None

                first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
                if not await first_row.count(): return None
                number = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")
                country = (await first_row.locator(".gn-meta").first.inner_text()).strip() if await first_row.locator(".gn-meta").count() else "Unknown"
                operator = (await first_row.locator(".gn-meta-sub").first.inner_text()).strip() if await first_row.locator(".gn-meta-sub").count() else "Unknown"
                operator = re.sub(r"\s+", " ", operator).strip()
                if not number: return None
                return {"number":number,"country":country,"operator":operator}
        except Exception as e:
            log.error(f"❌ fetch_number attempt {attempt} error: {e}")
            last_exception = e
            # Clean up stale page
            if user_id and user_id in _user_pages:
                pg = _user_pages[user_id].pop(site, None)
                if pg:
                    await _safe_close_page(pg)
                if not _user_pages[user_id]:
                    del _user_pages[user_id]
            await asyncio.sleep(1)
    log.error(f"❌ All {FETCH_RETRIES} fetch attempts failed. Last error: {last_exception}")
    return None

async def poll_otp(number: str, site: str, user_id: int = None) -> Optional[str]:
    try:
        page = await _ensure_page_logged_in(site, user_id)
        async with _page_lock:
            if "/dialer/" not in page.url:
                await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
            rows = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num"))
            count = await rows.count()
            for i in range(count):
                row = rows.nth(i)
                num_el = row.locator(".gn-num").first
                if await num_el.count()==0: continue
                if (await num_el.inner_text()).strip().lstrip("+")!=number: continue
                status_el = row.locator(".gn-status-pill")
                if await status_el.count()==0: continue
                if (await status_el.first.inner_text()).strip().lower()!="success": continue
                copy_btn = row.locator("button.gn-otp-copy")
                if await copy_btn.count()>0:
                    await copy_btn.first.click(); await asyncio.sleep(0.3)
                    try: return await page.evaluate("navigator.clipboard.readText()")
                    except: pass
                title = await copy_btn.first.get_attribute("title") or ""
                if ":" in title: return title.split(":",1)[1].strip()
                return None
            return None
    except Exception as e:
        log.error(f"❌ poll_otp error: {e}")
        return None

# ===================== MONITOR TASK =====================
async def monitor_number(app: Application, uid: int, number: str, country: str, operator: str, site: str):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + MONITOR_TIMEOUT
    bot_user = await app.bot.get_me()
    custom = get_credentials(uid, site) is not None
    while loop.time() < deadline:
        s = user_sessions.get(uid)
        if not s or s.get("number")!=number: return
        full_msg = await poll_otp(number, site, uid)
        if full_msg:
            clean_otp = _extract_otp(full_msg)
            if clean_otp and s.get("last_otp")!=clean_otp:
                s["last_otp"] = clean_otp
                clean_msg = _clean_full_msg(full_msg)
                safe_msg = escape_markdown(clean_msg, version=1)
                if not custom:
                    balance_before = get_user_balance(uid)
                    if SMS_RATE_BDT > 0:
                        credit_user(uid, SMS_RATE_BDT)
                    balance_after = get_user_balance(uid)
                    update_user_stats(uid, earned=SMS_RATE_BDT, otp_count=1, numbers_used=0)
                    bal_line = f"💰 Balance: {balance_before:.2f} BDT → {balance_after:.2f} BDT"
                else:
                    bal_line = "💡 (using your own account – no earnings)"
                user_text = (
                    f"📩 {country} Message Received!\n\n"
                    f"📞 Number: `+{number}`\n"
                    f"🔑 OTP Code: `{clean_otp}`\n\n"
                    f"💬 Message: {safe_msg}\n\n"
                    f"{bal_line}"
                )
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"OTP: {clean_otp}", copy_text=CopyTextButton(text=clean_otp))]])
                try: await app.bot.send_message(uid, user_text, parse_mode="Markdown", reply_markup=kb)
                except Exception as e: log.error(f"❌ DM OTP error: {e}")
                masked_num = _mask_number(f"+{number}")
                group_text = f"📨 {country} OTP Received\n━━━━━━━━━━━━━━━━\n📞 Number: {masked_num}\n🔑 OTP: {clean_otp}\n\n💬 {clean_msg}\n━━━━━━━━━━━━━━━━"
                grp_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Get Number", url=f"https://t.me/{bot_user.username}?start=start")]])
                try: await app.bot.send_message(OTP_GROUP_ID, group_text, reply_markup=grp_kb)
                except Exception as e: log.error(f"❌ Group OTP error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

# ===================== KEYBOARDS =====================
def main_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    btns = [
        [KeyboardButton("📡 Get Number"), KeyboardButton("🔑 Get 2FA")],
        [KeyboardButton("📋 Fake Details"), KeyboardButton("💰 Balance")],
        [KeyboardButton("📊 Status"), KeyboardButton("👤 Accounts")]
    ]
    if _is_admin(uid):
        btns.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def site_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔵 Stexsms"), KeyboardButton("🔵 Voltxsms")],
        [KeyboardButton("🔙 Back")]
    ], resize_keyboard=True)

def admin_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    btns = [
        [KeyboardButton("Interval")],
        [KeyboardButton("Set SMS Rate"), KeyboardButton("Set Withdraw Rate")],
        [KeyboardButton("Pending"), KeyboardButton("Approved")],
        [KeyboardButton("Users Status"), KeyboardButton("Broadcast")],
        [KeyboardButton("📨 Fake OTP")]
    ]
    if _is_owner(uid):
        btns.append([KeyboardButton("Admin Set")])
    btns.append([KeyboardButton("🔙 Back")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def admin_set_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("Add Admin"), KeyboardButton("Remove Admin")],
        [KeyboardButton("🔙 Back")]
    ], resize_keyboard=True)

def number_ready_kb(number: str) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton("👥 OTP Group", url=OTP_GROUP_LINK),
            InlineKeyboardButton("🔄 Change Number", callback_data="change_number")]
    row2 = [InlineKeyboardButton("📋 Copy Number", copy_text=CopyTextButton(text=f"+{number}"))]
    return InlineKeyboardMarkup([row1, row2])

def balance_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Wallet", callback_data="profile_set_wallet"),
         InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")]
    ])

def wallet_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Bkash", callback_data="wallet_bkash"),
         InlineKeyboardButton("Rocket", callback_data="wallet_rocket")],
        [InlineKeyboardButton("Binance", callback_data="wallet_binance")]
    ])

def withdraw_method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Bkash", callback_data="withdraw_method_bkash"),
         InlineKeyboardButton("Rocket", callback_data="withdraw_method_rocket")],
        [InlineKeyboardButton("Binance", callback_data="withdraw_method_binance"),
         InlineKeyboardButton("Mobile Recharge", callback_data="withdraw_method_mobile")]
    ])

def login_site_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("StexSMS", callback_data="login_site_stexsms"),
         InlineKeyboardButton("VoltxSMS", callback_data="login_site_voltxsms")]
    ])

def accounts_options_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Log In", callback_data="accounts_login"),
         InlineKeyboardButton("🚪 Log Out", callback_data="accounts_logout")]
    ])

def logout_site_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("StexSMS", callback_data="logout_site_stexsms"),
         InlineKeyboardButton("VoltxSMS", callback_data="logout_site_voltxsms")]
    ])

def fake_otp_menu_kb() -> ReplyKeyboardMarkup:
    auto_label = "Auto: ON" if auto_fake_running else "Auto: OFF"
    return ReplyKeyboardMarkup([
        [KeyboardButton("FB Send 5"), KeyboardButton("FB Send 6")],
        [KeyboardButton("IG Send")],
        [KeyboardButton(auto_label)],
        [KeyboardButton("Set Details")],
        [KeyboardButton("🔙 Back")]
    ], resize_keyboard=True)

def fake_details_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Facebook", callback_data="set_fake_details_fb"),
         InlineKeyboardButton("Instagram", callback_data="set_fake_details_ig")]
    ])

def format_balance_message(user_id: int) -> str:
    balance = get_user_balance(user_id)
    wallet = get_user_wallet(user_id)
    usd = balance / EXCHANGE_RATE
    min_wd = MIN_WITHDRAW_BDT
    text = (
        "⚠️ Double‑check your wallet! Wrong details = no refund.\n\n"
        f"🤑 Balance: {balance:.2f} BDT / ${usd:.4f}\n\n"
        f"🌍 Bkash: {wallet['bkash'] or 'Not Set'}\n"
        f"🌍 Rocket: {wallet['rocket'] or 'Not Set'}\n"
        f"🌍 Binance: {wallet['binance'] or 'Not Set'}\n\n"
        f"💳 Minimum Withdrawal: {min_wd:.1f} BDT / ${min_wd/EXCHANGE_RATE:.2f}"
    )
    return text

# ===================== FAKE OTP HELPERS =====================
def generate_random_otp(length: int = 6) -> str:
    return ''.join(random.choices(string.digits, k=length))

def generate_random_last3() -> str:
    return ''.join(random.choices(string.digits, k=3))

def build_fake_otp_message(platform: str, config_override: Optional[Dict] = None) -> Optional[str]:
    base_platform = "fb" if platform.startswith("fb") else "ig"
    if config_override:
        country_name = config_override["country_name"]
        country_code = config_override["country_code"]
    else:
        configs = FAKE_OTP_CONFIG.get(base_platform, [])
        if not configs:
            return None
        cfg = configs[0]
        country_name = cfg["country_name"]
        country_code = cfg["country_code"]

    last3 = generate_random_last3()
    number_display = f"+{country_code}******{last3}"

    if platform == "fb5":
        otp = generate_random_otp(5)
        body = f"<#> {otp} आपका Facebook कोड है H29Q+Fsn4Sr" if random.random()<0.05 else f"<#> {otp} is your Facebook code H29Q+Fsn4Sr"
        otp_display = otp
    elif platform == "fb6":
        otp = generate_random_otp(6)
        body = f"<#> {otp} आपका Facebook कोड है H29Q+Fsn4Sr" if random.random()<0.05 else f"<#> {otp} is your Facebook code H29Q+Fsn4Sr"
        otp_display = otp
    else:  # ig
        otp = generate_random_otp(6)
        suffix = random.choice(["GdDGcwrWHVm", "SIYRxKrru1t"])
        body = f"<#> {otp[:3]} {otp[3:]} is your Instagram code. Don't share it. {suffix}"
        otp_display = otp

    msg = (
        f"📨 {country_name} OTP Received\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📞 Number: {number_display}\n"
        f"🔑 OTP: {otp_display}\n\n"
        f"💬 {body}\n"
        f"━━━━━━━━━━━━━━━━"
    )
    return msg

def get_number_button() -> Optional[InlineKeyboardMarkup]:
    if BOT_USERNAME:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🤖 Get Number", url=f"https://t.me/{BOT_USERNAME}?start=start")
        ]])
    return None

async def send_fake_otp(app: Application, platform: str, config_override: Optional[Dict] = None):
    msg = build_fake_otp_message(platform, config_override)
    if msg is None:
        return False
    try:
        kb = get_number_button()
        await app.bot.send_message(chat_id=OTP_GROUP_ID, text=msg, reply_markup=kb)
        return True
    except Exception as e:
        log.error(f"Failed to send fake OTP ({platform}): {e}")
        return False

async def auto_fake_loop(app: Application):
    global auto_fake_running
    while auto_fake_running:
        platform = random.choice(["fb5", "fb6", "ig"])
        base = "fb" if platform.startswith("fb") else "ig"
        configs = FAKE_OTP_CONFIG.get(base, [])
        if configs:
            cfg = random.choice(configs)
            msg = build_fake_otp_message(platform, cfg)
            if msg:
                try:
                    kb = get_number_button()
                    await app.bot.send_message(chat_id=OTP_GROUP_ID, text=msg, reply_markup=kb)
                except Exception as e:
                    log.error(f"Auto fake OTP error: {e}")
        delay = random.uniform(1, 5)
        extra = random.choice([0, 1, 2])
        for _ in range(extra):
            if not auto_fake_running:
                break
            platform2 = random.choice(["fb5", "fb6", "ig"])
            base2 = "fb" if platform2.startswith("fb") else "ig"
            configs2 = FAKE_OTP_CONFIG.get(base2, [])
            if configs2:
                cfg2 = random.choice(configs2)
                msg2 = build_fake_otp_message(platform2, cfg2)
                if msg2:
                    try:
                        kb = get_number_button()
                        await app.bot.send_message(chat_id=OTP_GROUP_ID, text=msg2, reply_markup=kb)
                    except Exception as e:
                        log.error(f"Auto fake burst error: {e}")
            await asyncio.sleep(0.2)
        await asyncio.sleep(delay)

def start_auto_fake(app: Application):
    global auto_fake_running, auto_fake_task
    if auto_fake_running:
        return
    auto_fake_running = True
    auto_fake_task = asyncio.create_task(auto_fake_loop(app))

def stop_auto_fake():
    global auto_fake_running, auto_fake_task
    auto_fake_running = False
    if auto_fake_task and not auto_fake_task.done():
        auto_fake_task.cancel()
    auto_fake_task = None

# ===================== CALLBACKS =====================
async def cb_change_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; uid=query.from_user.id
    now = time.time()
    last = user_sessions.get(uid,{}).get("last_change_time",0)
    if CHANGE_NUMBER_DELAY>0 and now-last < CHANGE_NUMBER_DELAY:
        await query.answer(f"⏳ Wait {int(CHANGE_NUMBER_DELAY-(now-last))}s", show_alert=True); return
    await query.answer()
    range_str = user_sessions.get(uid,{}).get("last_range")
    site = user_sessions.get(uid,{}).get("site")
    if not range_str or not site:
        await query.message.reply_text("❌ No previous range/site.", reply_markup=main_menu_kb(uid)); return
    _stop_monitor(uid)
    user_sessions[uid]["last_change_time"] = now
    await query.message.reply_text("🔄 Fetching new number..."); await asyncio.sleep(CHANGE_NUMBER_DELAY)
    result = await fetch_number(range_str, site, uid)
    if result:
        await _deliver_number(ctx.application, uid, result, site)
    else:
        await query.message.reply_text("❌ No number found.", reply_markup=main_menu_kb(uid))

async def cb_copy_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data.startswith("copy_otp_"):
        val = data[9:]
        await query.message.reply_text(f"🔑 *OTP:*\n`{val}`", parse_mode="Markdown")
    elif data.startswith("copy_num_"):
        val = "+"+data[9:]
        await query.message.reply_text(f"📋 *Number:*\n`{val}`", parse_mode="Markdown")

async def cb_gender_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    gender = "male" if "male" in query.data else "female"
    identity = generate_identity(gender)
    await _send_identity_message(query.message, identity, edit=False)

async def cb_change_fake_details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    gender = "male" if "male" in query.data else "female"
    identity = generate_identity(gender)
    await _send_identity_message(query.message, identity, edit=True)

async def _send_identity_message(message, identity: dict, edit: bool=False):
    emoji = "👨" if identity["gender"]=="male" else "👩"
    text = f"{emoji} *Generated Identity*\n\n👤 *Name:* `{identity['name']}`\n🆔 *Username:* `{identity['username']}`\n🔑 *Password:* `{identity['password']}`\n\n📅 Password ends with today's date."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Name", copy_text=CopyTextButton(text=identity['name'])),
         InlineKeyboardButton("📋 Copy User", copy_text=CopyTextButton(text=identity['username']))],
        [InlineKeyboardButton("📋 Copy Pass", copy_text=CopyTextButton(text=identity['password']))],
        [InlineKeyboardButton("🔄 Change Details", callback_data=f"change_fake_details_{identity['gender']}")]
    ])
    if edit:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except BadRequest as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def cb_copy_identity_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data.startswith("copy_name_"):
        val = data[10:]
        await query.message.reply_text(f"👤 *Name:*\n`{val}`", parse_mode="Markdown")
    elif data.startswith("copy_username_"):
        val = data[14:]
        await query.message.reply_text(f"🆔 *Username:*\n`{val}`", parse_mode="Markdown")
    elif data.startswith("copy_password_"):
        val = data[14:]
        await query.message.reply_text(f"🔑 *Password:*\n`{val}`", parse_mode="Markdown")

async def cb_remove_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    query = update.callback_query; await query.answer()
    data = query.data
    if not data.startswith("remove_admin_"): return
    admin_id = int(data[13:])
    if admin_id in ADMIN_USERS and admin_id!=OWNER_USER_ID:
        ADMIN_USERS.remove(admin_id); _save_admins(ADMIN_USERS)
        await query.answer(f"Admin {admin_id} removed.", show_alert=True)
        admins = [u for u in ADMIN_USERS if u!=OWNER_USER_ID]
        if admins:
            new_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"Admin: {u}", callback_data=f"remove_admin_{u}")] for u in admins])
            try: await query.edit_message_reply_markup(reply_markup=new_markup)
            except: pass
        else: await query.edit_message_text("No more admins to remove.")
    else: await query.answer("Cannot remove this admin.", show_alert=True)

async def cb_accounts_options(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data == "accounts_login":
        await query.message.reply_text("🔐 *Select site to log in:*", parse_mode="Markdown", reply_markup=login_site_kb())
    elif data == "accounts_logout":
        await query.message.reply_text("🚪 *Select site to log out:*", parse_mode="Markdown", reply_markup=logout_site_kb())

async def cb_logout_site_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if not data.startswith("logout_site_"): return
    site = data[12:]
    uid = query.from_user.id
    remove_credentials(uid, site)
    await query.message.reply_text(f"✅ Logged out from {SITES[site]['name']}.", reply_markup=main_menu_kb(uid))

async def cb_login_site_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    site = query.data.split('_')[2]
    ctx.user_data['login_site'] = site
    ctx.user_data['awaiting_login_email'] = True
    await query.message.reply_text(f"📧 Enter your *{SITES[site]['name']}* email address:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def cb_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("💸 *Select withdrawal method:*", parse_mode="Markdown", reply_markup=withdraw_method_kb())

async def cb_withdraw_method_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    method = query.data.split('_')[2]
    uid = query.from_user.id
    if method == 'mobile':
        ctx.user_data['withdraw_method'] = method
        ctx.user_data['awaiting_withdraw_account'] = True
        await query.message.reply_text("📱 Enter your *Mobile* number (e.g., 01xxxxxxxxx):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return
    wallets = get_user_wallet(uid)
    stored_number = wallets.get(method)
    if stored_number and stored_number.strip():
        ctx.user_data['withdraw_method'] = method
        ctx.user_data['withdraw_account'] = stored_number.strip()
        ctx.user_data['awaiting_withdraw_amount'] = True
        balance = get_user_balance(uid)
        usd = balance / EXCHANGE_RATE
        min_wd = MIN_WITHDRAW_BDT
        method_display = {'bkash': 'Bkash', 'rocket': 'Rocket', 'binance': 'Binance'}[method]
        await query.message.reply_text(
            f"💰 Current Balance: {balance:.2f} BDT / ${usd:.4f}\n"
            f"💳 Minimum Withdrawal: {min_wd:.1f} BDT / ${min_wd/EXCHANGE_RATE:.2f}\n"
            f"🏦 Using your saved {method_display} number: `{stored_number}`\n\n"
            "Please enter the amount you want to withdraw (in BDT):",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
    else:
        ctx.user_data['withdraw_method'] = method
        ctx.user_data['awaiting_withdraw_account'] = True
        method_names = {'bkash': 'Bkash', 'rocket': 'Rocket', 'binance': 'Binance'}
        await query.message.reply_text(f"📱 Enter your *{method_names[method]}* number:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def cb_complete_withdrawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if not data.startswith("complete_withdrawal_"): return
    withdrawal_id = int(data[len("complete_withdrawal_"):])
    success = approve_withdrawal(withdrawal_id)
    if not success:
        await query.answer("Withdrawal not found or already approved.", show_alert=True)
        return
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        w = conn.execute('SELECT * FROM withdrawals WHERE id = ?', (withdrawal_id,)).fetchone()
    if w:
        user_id = w['user_id']
        method_display = {'bkash':'Bkash','rocket':'Rocket','binance':'Binance','mobile':'Mobile Recharge'}.get(w['method'], w['method'])
        msg = (
            "🎉 *Withdrawal Approved*\n\n"
            f"💵 Amount: {w['amount']:.2f} BDT\n"
            f"🏦 Method: {method_display}\n"
            f"📞 Number: {w['account']}\n"
            f"✅ Status: Complete\n\n"
            "We appreciate your trust!"
        )
        try: await ctx.bot.send_message(user_id, msg, parse_mode="Markdown")
        except: pass
    await query.answer("Withdrawal approved and user notified.", show_alert=True)
    await show_pending_withdrawals(query.message, ctx)

async def show_pending_withdrawals(message, ctx):
    pending = get_pending_withdrawals()
    if not pending:
        await message.reply_text("📋 No pending withdrawals.")
        return
    lines = ["📋 *Pending Withdrawals:*\n"]
    for w in pending:
        lines.append(
            f"🔹 ID: {w['id']} | User: `{w['user_id']}`\n"
            f"   💵 {w['amount']:.1f} BDT via {w['method']} ({w['account']})\n"
            f"   🕒 {w['created_at']}\n"
        )
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Complete", callback_data=f"complete_withdrawal_{w['id']}")] for w in pending
    ])
    await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_approved_withdrawals(message):
    approved = get_approved_withdrawals()
    if not approved:
        await message.reply_text("✅ No approved withdrawals yet.")
        return
    lines = ["✅ *Approved Withdrawals:*\n"]
    for w in approved:
        lines.append(
            f"🔹 ID: {w['id']} | User: `{w['user_id']}`\n"
            f"   💵 {w['amount']:.1f} BDT via {w['method']} ({w['account']})\n"
            f"   📅 {w['approved_at']}\n"
        )
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cb_set_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("📱 *Select wallet type to set:*", parse_mode="Markdown", reply_markup=wallet_type_kb())

async def cb_wallet_type_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    wallet = query.data.split('_')[1]
    ctx.user_data['wallet_type'] = wallet
    ctx.user_data['awaiting_wallet_number'] = True
    wallet_names = {'bkash': 'Bkash', 'rocket': 'Rocket', 'binance': 'Binance'}
    await query.message.reply_text(f"📲 Send your *{wallet_names[wallet]}* number:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def _update_2fa_countdown(message, code: str):
    remaining = 30
    while remaining > 0:
        text = f"🔐 *2FA Code Generated*\n\n🔢 Code: `{code}`\n⏳ Expires in: {remaining} seconds\n\n📌 This code refreshes every 30 seconds."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))]])
        try: await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except BadRequest as e:
            if "message is not modified" not in str(e): break
        except: break
        await asyncio.sleep(1)
        remaining -= 1

async def cb_fake_send_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    parts = data.split("_", 3)
    if len(parts)!=4 or parts[0]!="fake" or parts[1]!="send": return
    platform = parts[2]
    index = int(parts[3])
    base = "fb" if platform.startswith("fb") else "ig"
    configs = FAKE_OTP_CONFIG.get(base, [])
    if 0 <= index < len(configs):
        cfg = configs[index]
        ok = await send_fake_otp(ctx.application, platform, cfg)
        new_text = f"✅ Sent {platform.upper()} OTP from {cfg['country_name']}." if ok else f"❌ Failed to send {platform.upper()} OTP."
        if query.message.text != new_text:
            try: await query.edit_message_text(new_text)
            except BadRequest: pass
    else:
        await query.answer("Invalid selection.", show_alert=True)

async def cb_set_fake_details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    platform = "fb" if "fb" in query.data else "ig"
    ctx.user_data['fake_otp_platform'] = platform
    ctx.user_data['awaiting_fake_country_name'] = True
    await query.message.reply_text(f"🌍 Enter the *country name* for {platform.upper()} fake OTP:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

# ===================== CONVERSATION HANDLERS =====================
MAIN_MENU, SITE_MENU, AWAIT_RANGE, ADMIN_MENU, SET_INTERVAL, ADMIN_SET_MENU, ADD_ADMIN_INPUT, \
AWAIT_2FA_SECRET, SET_RATE, SET_WITHDRAW_RATE, \
LOGIN_EMAIL, LOGIN_PASSWORD, BROADCAST_AWAIT = range(13)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await _check_membership(update, ctx): return ConversationHandler.END
    user_sessions.setdefault(uid, {})
    await update.message.reply_text(f"👋 Welcome *{update.effective_user.first_name}*!\n\nTap *📡 Get Number* to begin.", parse_mode="Markdown", reply_markup=main_menu_kb(uid))
    return MAIN_MENU

async def main_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await _check_membership(update, ctx): return ConversationHandler.END
    if not update.message or not update.message.text:
        return None

    # Pending withdrawal flows
    if ctx.user_data.get('awaiting_withdraw_account'):
        account = update.message.text.strip()
        ctx.user_data['withdraw_account'] = account
        ctx.user_data['awaiting_withdraw_account'] = False
        ctx.user_data['awaiting_withdraw_amount'] = True
        balance = get_user_balance(uid)
        usd = balance / EXCHANGE_RATE
        min_wd = MIN_WITHDRAW_BDT
        await update.message.reply_text(
            f"💰 Current Balance: {balance:.2f} BDT / ${usd:.4f}\n"
            f"💳 Minimum Withdrawal: {min_wd:.1f} BDT / ${min_wd/EXCHANGE_RATE:.2f}\n\n"
            "Please enter the amount you want to withdraw (in BDT):",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
        return MAIN_MENU
    if ctx.user_data.get('awaiting_withdraw_amount'):
        amount_text = update.message.text.strip()
        try: amount = float(amount_text)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.", reply_markup=main_menu_kb(uid))
            ctx.user_data.clear(); return MAIN_MENU
        balance = get_user_balance(uid)
        min_wd = MIN_WITHDRAW_BDT
        if amount < min_wd:
            await update.message.reply_text(f"❌ Minimum withdrawal is {min_wd:.1f} BDT.", reply_markup=main_menu_kb(uid))
            ctx.user_data.clear(); return MAIN_MENU
        if amount > balance:
            await update.message.reply_text("❌ Insufficient balance.", reply_markup=main_menu_kb(uid))
            ctx.user_data.clear(); return MAIN_MENU
        method = ctx.user_data.get('withdraw_method')
        account = ctx.user_data.get('withdraw_account')
        create_withdrawal(uid, method, account, amount)
        ctx.user_data.clear()
        await update.message.reply_text("✅ Withdrawal request submitted.", reply_markup=main_menu_kb(uid))
        return MAIN_MENU

    if ctx.user_data.get('awaiting_wallet_number'):
        wallet_type = ctx.user_data.get('wallet_type')
        number = update.message.text.strip()
        if not wallet_type:
            await update.message.reply_text("❌ Error.", reply_markup=main_menu_kb(uid))
            ctx.user_data.clear(); return MAIN_MENU
        update_wallet(uid, wallet_type, number)
        ctx.user_data['awaiting_wallet_number'] = False
        del ctx.user_data['wallet_type']
        bal_text = format_balance_message(uid)
        await update.message.reply_text(bal_text, parse_mode="Markdown", reply_markup=balance_inline_kb())
        await update.message.reply_text("✅ Wallet updated.", reply_markup=main_menu_kb(uid))
        return MAIN_MENU

    if ctx.user_data.get('awaiting_login_email'):
        email = update.message.text.strip()
        site = ctx.user_data.get('login_site')
        if not site:
            await update.message.reply_text("❌ Session error.", reply_markup=main_menu_kb(uid))
            ctx.user_data.clear(); return MAIN_MENU
        ctx.user_data['login_email'] = email
        ctx.user_data['awaiting_login_email'] = False
        ctx.user_data['awaiting_login_password'] = True
        await update.message.reply_text("🔑 Now enter your *password*:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return LOGIN_PASSWORD

    if ctx.user_data.get('awaiting_login_password'):
        password = update.message.text.strip()
        site = ctx.user_data.get('login_site')
        email = ctx.user_data.get('login_email')
        ctx.user_data.clear()
        try:
            await _ensure_playwright()
            context = await _browser.new_context(viewport={"width":1280,"height":800})
            page = await context.new_page()
            success = await _login_with_credentials(page, site, email, password)
            await context.close()
            if success:
                store_credentials(uid, site, email, password)
                await update.message.reply_text(f"✅ Successfully logged in to {SITES[site]['name']}!", reply_markup=main_menu_kb(uid))
            else:
                await update.message.reply_text("❌ Login failed.", reply_markup=main_menu_kb(uid))
        except Exception as e:
            log.error(f"Login test error: {e}")
            await update.message.reply_text("❌ An error occurred.", reply_markup=main_menu_kb(uid))
        return MAIN_MENU

    text = update.message.text
    if text == "📡 Get Number":
        await update.message.reply_text("🌐 *Select a provider:*", parse_mode="Markdown", reply_markup=site_menu_kb())
        return SITE_MENU
    elif text == "🔑 Get 2FA":
        await update.message.reply_text("📲 Paste your 2FA Secret Key", reply_markup=ReplyKeyboardRemove())
        return AWAIT_2FA_SECRET
    elif text == "📋 Fake Details":
        gk = InlineKeyboardMarkup([[InlineKeyboardButton("🚹 Male", callback_data="gender_male"),
                                    InlineKeyboardButton("🚺 Female", callback_data="gender_female")]])
        await update.message.reply_text("👤 *Select gender for identity:*", parse_mode="Markdown", reply_markup=gk)
        return MAIN_MENU
    elif text == "💰 Balance":
        bal_text = format_balance_message(uid)
        await update.message.reply_text(bal_text, parse_mode="Markdown", reply_markup=balance_inline_kb())
        return MAIN_MENU
    elif text == "📊 Status":
        stats = get_user_stats(uid)
        usd_factor = EXCHANGE_RATE
        msg = (
            "📊 YOUR STATISTICS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📞 Numbers Used: {stats.get('numbers_used',0)}\n"
            f"📩 Today's OTPs: {stats.get('today_otps',0)}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Today's Earned: ${stats.get('today_earned_bdt',0.0)/usd_factor:.3f} USDT\n"
            f"💵 Total Earned: ৳ {stats.get('total_earned_bdt',0.0):.3f} BDT/ ${stats.get('total_earned_bdt',0.0)/usd_factor:.3f} USDT\n"
            f"💳 Total Withdrawn: ৳ {stats.get('total_withdrawn_bdt',0.0):.3f} BDT/ ${stats.get('total_withdrawn_bdt',0.0)/usd_factor:.3f} USDT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Your Balance: ৳ {stats.get('balance_bdt',0.0):.3f} BDT/ ${stats.get('balance_bdt',0.0)/usd_factor:.3f} USDT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📢 {BOT_NAME}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return MAIN_MENU
    elif text == "👤 Accounts":
        await update.message.reply_text("👤 *Accounts Management*\nChoose an action:", parse_mode="Markdown", reply_markup=accounts_options_kb())
        return MAIN_MENU
    elif text == "⚙️ Admin Panel" and _is_admin(uid):
        await update.message.reply_text(
            f"⚙️ *Admin Panel*\n"
            f"Change‑Number delay: `{CHANGE_NUMBER_DELAY}`s\n"
            f"SMS Rate: `{SMS_RATE_BDT}` BDT/OTP\n"
            f"Min Withdraw: `{MIN_WITHDRAW_BDT}` BDT",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(uid)
        )
        return ADMIN_MENU
    return MAIN_MENU

async def login_password_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    password = update.message.text.strip()
    site = ctx.user_data.get('login_site')
    email = ctx.user_data.get('login_email')
    ctx.user_data.clear()
    if not site or not email:
        await update.message.reply_text("❌ Session expired.", reply_markup=main_menu_kb(uid))
        return MAIN_MENU
    try:
        await _ensure_playwright()
        context = await _browser.new_context(viewport={"width":1280,"height":800})
        page = await context.new_page()
        success = await _login_with_credentials(page, site, email, password)
        await context.close()
        if success:
            store_credentials(uid, site, email, password)
            await update.message.reply_text(f"✅ Logged in to {SITES[site]['name']}!", reply_markup=main_menu_kb(uid))
        else:
            await update.message.reply_text("❌ Login failed.", reply_markup=main_menu_kb(uid))
    except Exception as e:
        log.error(f"Login error: {e}")
        await update.message.reply_text("❌ Error.", reply_markup=main_menu_kb(uid))
    return MAIN_MENU

async def handle_2fa_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    secret = update.message.text.strip()
    result = generate_2fa_code(secret)
    if result["success"]:
        code = result["code"]
        text = f"🔐 *2FA Code Generated*\n\n🔢 Code: `{code}`\n⏳ Expires in: 30 seconds\n\n📌 This code refreshes every 30 seconds."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))]])
        sent_msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
        old_task = user_sessions.get(uid, {}).get("2fa_countdown_task")
        if old_task and not old_task.done(): old_task.cancel()
        task = asyncio.create_task(_update_2fa_countdown(sent_msg, code))
        user_sessions[uid]["2fa_countdown_task"] = task
        await update.message.reply_text("🔽", reply_markup=main_menu_kb(uid))
    else:
        await update.message.reply_text(f"❌ {result['message']}", parse_mode="Markdown", reply_markup=main_menu_kb(uid))
    return MAIN_MENU

async def admin_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    text = update.message.text.strip()

    # Fake OTP flows
    if ctx.user_data.get('awaiting_fake_country_name'):
        country_name = text
        ctx.user_data['fake_country_name'] = country_name
        ctx.user_data['awaiting_fake_country_name'] = False
        ctx.user_data['awaiting_fake_country_code'] = True
        await update.message.reply_text("📞 Now enter the *country code* (e.g., 224):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return ADMIN_MENU
    if ctx.user_data.get('awaiting_fake_country_code'):
        country_code = text.strip()
        platform = ctx.user_data.pop('fake_otp_platform')
        country_name = ctx.user_data.pop('fake_country_name')
        ctx.user_data.pop('awaiting_fake_country_code')
        FAKE_OTP_CONFIG.setdefault(platform, [])
        FAKE_OTP_CONFIG[platform].append({"country_name": country_name, "country_code": country_code})
        save_fake_otp_config(FAKE_OTP_CONFIG)
        await update.message.reply_text(f"✅ New fake OTP details for {platform.upper()} saved.", reply_markup=fake_otp_menu_kb())
        return ADMIN_MENU

    if ctx.user_data.get('in_fake_otp_menu'):
        if text in ("FB Send 5", "FB Send 6", "IG Send"):
            base = "fb" if text.startswith("FB") else "ig"
            configs = FAKE_OTP_CONFIG.get(base, [])
            if not configs:
                await update.message.reply_text("❌ No details set. Use 'Set Details' first.", reply_markup=fake_otp_menu_kb())
                return ADMIN_MENU
            platform_code = "fb5" if text == "FB Send 5" else ("fb6" if text == "FB Send 6" else "ig")
            buttons = []
            for idx, cfg in enumerate(configs):
                label = f"{cfg['country_name']} (+{cfg['country_code']})"
                buttons.append([InlineKeyboardButton(label, callback_data=f"fake_send_{platform_code}_{idx}")])
            kb = InlineKeyboardMarkup(buttons)
            await update.message.reply_text("📋 Select a country configuration:", reply_markup=kb)
            return ADMIN_MENU
        elif text.startswith("Auto:"):
            if auto_fake_running:
                stop_auto_fake()
                await update.message.reply_text("⏹ Auto fake OTP stopped.", reply_markup=fake_otp_menu_kb())
            else:
                if not FAKE_OTP_CONFIG.get("fb") and not FAKE_OTP_CONFIG.get("ig"):
                    await update.message.reply_text("❌ Set at least one platform details first.", reply_markup=fake_otp_menu_kb())
                    return ADMIN_MENU
                start_auto_fake(ctx.application)
                await update.message.reply_text("▶ Auto fake OTP started.", reply_markup=fake_otp_menu_kb())
            return ADMIN_MENU
        elif text == "Set Details":
            await update.message.reply_text("📋 *Select platform to configure:*", parse_mode="Markdown", reply_markup=fake_details_kb())
            return ADMIN_MENU
        elif text == "🔙 Back":
            ctx.user_data.pop('in_fake_otp_menu', None)
            await update.message.reply_text("⚙️ *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb(uid))
            return ADMIN_MENU
        else:
            await update.message.reply_text("❓ Unknown command.", reply_markup=fake_otp_menu_kb())
            return ADMIN_MENU

    # Regular admin menu
    if text == "📨 Fake OTP":
        ctx.user_data['in_fake_otp_menu'] = True
        await update.message.reply_text("📨 *Fake OTP Controls*", parse_mode="Markdown", reply_markup=fake_otp_menu_kb())
        return ADMIN_MENU
    elif text == "Interval":
        await update.message.reply_text("Enter new delay in seconds (1‑60):", reply_markup=ReplyKeyboardRemove())
        return SET_INTERVAL
    elif text == "Set SMS Rate":
        await update.message.reply_text("💰 Enter the amount (BDT) users will earn per OTP:", reply_markup=ReplyKeyboardRemove())
        return SET_RATE
    elif text == "Set Withdraw Rate":
        await update.message.reply_text("💳 Enter the minimum withdrawal amount (BDT):", reply_markup=ReplyKeyboardRemove())
        return SET_WITHDRAW_RATE
    elif text == "Pending":
        await show_pending_withdrawals(update.message, ctx)
        await update.message.reply_text("✅ Done.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    elif text == "Approved":
        await show_approved_withdrawals(update.message)
        await update.message.reply_text("✅ Done.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    elif text == "Users Status":
        stats = get_admin_stats()
        usd = EXCHANGE_RATE
        msg = (
            "📊 USERS STATISTICS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📞 Numbers Used: {stats['numbers_used']}\n"
            f"📩 Today's OTPs: {stats['today_otps']}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Today's Cost : ৳ {stats['today_cost_bdt']:.3f} BDT/ ${stats['today_cost_bdt']/usd:.3f} USDT\n"
            f"💵 Total Cost : ৳ {stats['total_cost_bdt']:.3f} BDT/ ${stats['total_cost_bdt']/usd:.3f} USDT\n"
            f"💳 Total Withdrawn: ৳ {stats['total_withdrawn_bdt']:.3f} BDT/ ${stats['total_withdrawn_bdt']/usd:.3f} USDT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📢 {BOT_NAME}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return ADMIN_MENU
    elif text == "Broadcast":
        await update.message.reply_text("📣 Send the message you want to broadcast. Type /cancel to abort.", reply_markup=ReplyKeyboardRemove())
        return BROADCAST_AWAIT
    elif text == "Admin Set" and _is_owner(uid):
        await update.message.reply_text("⚙️ *Admin Management*", parse_mode="Markdown", reply_markup=admin_set_menu_kb())
        return ADMIN_SET_MENU
    elif text == "🔙 Back":
        await update.message.reply_text("🏠 Main menu:", reply_markup=main_menu_kb(uid))
        return MAIN_MENU
    return ADMIN_MENU

async def broadcast_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message: return None
    if update.message.text and update.message.text == "/cancel":
        await update.message.reply_text("Broadcast cancelled.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    all_users = get_all_user_ids()
    success = 0
    for user_id in all_users:
        try:
            await update.message.copy(chat_id=user_id)
            success += 1
        except: pass
    await update.message.reply_text(f"✅ Broadcast sent to {success}/{len(all_users)} users.", reply_markup=admin_menu_kb(uid))
    return ADMIN_MENU

async def set_rate_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global SMS_RATE_BDT
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    try:
        rate = float(update.message.text.strip())
        if rate < 0: raise ValueError
        SMS_RATE_BDT = rate
        save_sms_rate(rate)
        await update.message.reply_text(f"✅ SMS rate set to `{rate}` BDT per OTP.", parse_mode="Markdown", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    except ValueError:
        await update.message.reply_text("❌ Invalid number.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU

async def set_withdraw_rate_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MIN_WITHDRAW_BDT
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    try:
        min_val = float(update.message.text.strip())
        if min_val < 0: raise ValueError
        MIN_WITHDRAW_BDT = min_val
        save_min_withdraw(min_val)
        await update.message.reply_text(f"✅ Minimum withdrawal set to `{min_val}` BDT.", parse_mode="Markdown", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    except ValueError:
        await update.message.reply_text("❌ Invalid number.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU

async def admin_set_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    text = update.message.text
    if text == "Add Admin":
        await update.message.reply_text("👤 Send me the **user ID** to add as admin.", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return ADD_ADMIN_INPUT
    elif text == "Remove Admin":
        admins = [u for u in ADMIN_USERS if u!=OWNER_USER_ID]
        if not admins:
            await update.message.reply_text("No admins to remove.", reply_markup=admin_set_menu_kb()); return ADMIN_SET_MENU
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Admin: {u}", callback_data=f"remove_admin_{u}")] for u in admins])
        await update.message.reply_text("Select an admin to remove:", reply_markup=kb)
        return ADMIN_SET_MENU
    elif text == "🔙 Back":
        await update.message.reply_text("⚙️ *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    return ADMIN_SET_MENU

async def admin_set_add_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    try: new_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.", reply_markup=admin_set_menu_kb()); return ADMIN_SET_MENU
    ADMIN_USERS.add(new_id); _save_admins(ADMIN_USERS)
    await update.message.reply_text(f"✅ User `{new_id}` added as admin.", parse_mode="Markdown", reply_markup=admin_set_menu_kb())
    return ADMIN_SET_MENU

async def admin_set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global CHANGE_NUMBER_DELAY
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    try:
        val = int(update.message.text.strip())
        if val<1 or val>60: raise ValueError
        CHANGE_NUMBER_DELAY = val
        await update.message.reply_text(f"✅ Change‑Number delay set to `{val}` seconds.", parse_mode="Markdown", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU
    except ValueError:
        await update.message.reply_text("❌ Invalid number.", reply_markup=admin_menu_kb(uid))
        return ADMIN_MENU

async def site_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    text = update.message.text
    if text == "🔙 Back":
        await update.message.reply_text("🏠 Main menu:", reply_markup=main_menu_kb(uid)); return MAIN_MENU
    site = None
    if text=="🔵 Stexsms": site="stexsms"
    elif text=="🔵 Voltxsms": site="voltxsms"
    if site:
        user_sessions.setdefault(uid,{})["site"] = site
        ctx.user_data["site"] = site
        last = user_sessions[uid].get("last_range","")
        last_line = f"\n\n📌 Last used range: `{last}`" if last else ""
        await update.message.reply_text(f"✏️ *{SITES[site]['name']} – Please send the range:*\n\n📝 Example: `2250163333XXX`\n⚠️ Must contain `XXX`{last_line}", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return AWAIT_RANGE
    return SITE_MENU

async def handle_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not update.message or not update.message.text: return None
    range_text = update.message.text.strip().upper()
    if "XXX" not in range_text:
        await update.message.reply_text("❌ Invalid format. Range must contain `XXX`.", parse_mode="Markdown"); return AWAIT_RANGE
    site = ctx.user_data.get("site")
    if not site:
        await update.message.reply_text("❌ No site selected."); return ConversationHandler.END
    _stop_monitor(uid)
    user_sessions.setdefault(uid,{})["last_range"] = range_text
    user_sessions[uid]["site"] = site
    user_sessions[uid]["last_otp"] = None
    update_user_stats(uid, earned=0, otp_count=0, numbers_used=1)
    wait_msg = await update.message.reply_text("⏳ *Fetching your number...*", parse_mode="Markdown", reply_markup=main_menu_kb(uid))
    result = await fetch_number(range_text, site, uid)
    if not result:
        try: await wait_msg.edit_text("❌ No number found. Try a different range.", reply_markup=main_menu_kb(uid))
        except: await update.message.reply_text("❌ No number found. Try a different range.", reply_markup=main_menu_kb(uid))
        return MAIN_MENU
    await _deliver_number(ctx.application, uid, result, site, edit_msg=wait_msg)
    return MAIN_MENU

async def _deliver_number(app_or_bot, uid, result, site, edit_msg=None):
    number, country, operator = result["number"], result["country"], result["operator"]
    user_sessions[uid].update({"number":number,"country":country,"operator":operator,"last_otp":None,"site":site})
    text = f"✅ *Number Ready!*\n━━━━━━━━━━━━━━━━\n🏢 Provider: `{operator}`\n🌍 Country: `{country}`\n📞 Number: `+{number}`\n━━━━━━━━━━━━━━━━\n⏳ Waiting for OTP..."
    kb = number_ready_kb(number)
    if edit_msg:
        try: sent = await edit_msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except: sent = await app_or_bot.bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    else:
        bot = app_or_bot if hasattr(app_or_bot,"send_message") else app_or_bot.bot
        sent = await bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    user_sessions[uid]["msg_id"] = sent.message_id
    app = app_or_bot if hasattr(app_or_bot,"bot") else None
    if app:
        _stop_monitor(uid)
        task = asyncio.create_task(monitor_number(app, uid, number, country, operator, site))
        user_sessions[uid]["monitor_task"] = task

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception:", exc_info=ctx.error)

# ===================== MAIN =====================
def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        log.critical("❌ BOT_TOKEN is not set. Please set it in your Railway environment variables.")
        sys.exit(1)

    init_db()
    if HEALTH_PORT > 0:
        start_health_server(HEALTH_PORT)

    log.info("⏳ Waiting 5 seconds to let old container release the polling lock...")
    time.sleep(5)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Get bot username
    BOT_USERNAME = loop.run_until_complete(app.bot.get_me()).username
    log.info(f"🤖 Bot username: @{BOT_USERNAME}")

    # Start heartbeat
    asyncio.ensure_future(heartbeat_logger(app), loop=loop)

    # Build conversation handler
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU:         [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],
            SITE_MENU:         [MessageHandler(filters.TEXT & ~filters.COMMAND, site_menu_handler)],
            AWAIT_RANGE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_range)],
            ADMIN_MENU:        [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_handler)],
            SET_INTERVAL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_interval)],
            SET_RATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rate_handler)],
            SET_WITHDRAW_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_withdraw_rate_handler)],
            ADMIN_SET_MENU:    [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_menu_handler)],
            ADD_ADMIN_INPUT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_add_input)],
            AWAIT_2FA_SECRET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_secret)],
            LOGIN_PASSWORD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password_handler)],
            BROADCAST_AWAIT:   [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_handler)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_change_number, pattern="^change_number$"))
    app.add_handler(CallbackQueryHandler(cb_copy_fallback, pattern="^copy_"))
    app.add_handler(CallbackQueryHandler(verify_membership_callback, pattern="^verify_membership$"))
    app.add_handler(CallbackQueryHandler(cb_remove_admin, pattern="^remove_admin_"))
    app.add_handler(CallbackQueryHandler(cb_gender_select, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(cb_change_fake_details, pattern="^change_fake_details_"))
    app.add_handler(CallbackQueryHandler(cb_copy_identity_fallback, pattern="^copy_name_|^copy_username_|^copy_password_"))
    app.add_handler(CallbackQueryHandler(cb_set_wallet, pattern="^profile_set_wallet$"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern="^profile_withdraw$"))
    app.add_handler(CallbackQueryHandler(cb_wallet_type_selected, pattern="^wallet_"))
    app.add_handler(CallbackQueryHandler(cb_withdraw_method_selected, pattern="^withdraw_method_"))
    app.add_handler(CallbackQueryHandler(cb_complete_withdrawal, pattern="^complete_withdrawal_"))
    app.add_handler(CallbackQueryHandler(cb_login_site_selected, pattern="^login_site_"))
    app.add_handler(CallbackQueryHandler(cb_accounts_options, pattern="^accounts_login$|^accounts_logout$"))
    app.add_handler(CallbackQueryHandler(cb_logout_site_selected, pattern="^logout_site_"))
    app.add_handler(CallbackQueryHandler(cb_fake_send_inline, pattern="^fake_send_"))
    app.add_handler(CallbackQueryHandler(cb_set_fake_details, pattern="^set_fake_details_"))
    app.add_error_handler(error_handler)

    # Shutdown
    async def shutdown():
        log.info("🛑 Shutting down bot...")
        stop_auto_fake()
        if app.running:
            await app.stop()
            await app.shutdown()
        async with _browser_lock:
            if _browser:
                await _browser.close()
            if _playwright_obj:
                await _playwright_obj.stop()
        log.info("✅ Shutdown complete")

    def signal_handler():
        asyncio.ensure_future(shutdown(), loop=loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: signal_handler())

    try:
        log.info("🤖 Bot is running...")
        app.run_polling(drop_pending_updates=True)
    except (KeyboardInterrupt, SystemExit):
        log.info("Keyboard interrupt received")
    finally:
        loop.run_until_complete(shutdown())
        loop.close()

if __name__ == "__main__":
    main()
