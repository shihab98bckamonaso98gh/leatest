"""
STEX SMS Telegram Bot
=====================
Telegram bot that fetches virtual numbers from SMS provider sites
and monitors them for incoming OTP messages.
"""

import asyncio, logging, re, os, json, time, random, string, signal, sys, threading, sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date
from typing import Optional, Dict, Set, Tuple, List, Any

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, ChatMember
from telegram.helpers import escape_markdown
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from telegram.error import BadRequest
try:
    from telegram import CopyTextButton; COPY_SUPPORTED = True
except ImportError: COPY_SUPPORTED = False
try:
    import pyotp; TOTP_AVAILABLE = True
except ImportError: TOTP_AVAILABLE = False
from playwright.async_api import async_playwright, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# ─────────────────────────────── Config ────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_NAME       = os.getenv("BOT_NAME", "SMS OTP Bot")
SMS_EMAIL      = os.getenv("STEX_EMAIL", "")
SMS_PASSWORD   = os.getenv("STEX_PASSWORD", "")
OTP_GROUP_ID   = int(os.getenv("OTP_GROUP_ID", "0"))
OTP_GROUP_LINK = os.getenv("OTP_GROUP_LINK", "https://t.me/your_otp_group")
OWNER_USER_ID  = 5705479420
HEALTH_PORT    = int(os.getenv("PORT", "0"))
DATA_DIR       = os.getenv("DATA_DIR", ".")
EXCHANGE_RATE  = 125.0
POLL_INTERVAL  = 3
MONITOR_TIMEOUT = 480
FETCH_RETRIES  = 3
CHANGE_NUMBER_DELAY = 0

# File paths
DB_FILE              = os.path.join(DATA_DIR, "bot_data.db")
RATE_CONFIG_FILE     = os.path.join(DATA_DIR, "rate_config.json")
WITHDRAW_CONFIG_FILE = os.path.join(DATA_DIR, "withdraw_config.json")
ADMIN_USERS_FILE     = os.path.join(DATA_DIR, "admin_users.json")
FAKE_OTP_CONFIG_FILE = os.path.join(DATA_DIR, "fake_otp_config.json")

SITES = {
    "stexsms": {"name":"StexSMS",  "login_url":"https://stexsms.com/m29/#/auth/login",  "dialer_url":"https://stexsms.com/m29/#/dialer/getnum?m=n"},
    "voltxsms":{"name":"VoltxSMS", "login_url":"https://voltxsms.com/m29/#/auth/login", "dialer_url":"https://voltxsms.com/m29/#/dialer/getnum?m=n"},
}

# ─────────────────────────────── Logging ───────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", level=logging.INFO)
for lib in ("httpx", "telegram.ext", "telegram._bot"): logging.getLogger(lib).setLevel(logging.WARNING)
log = logging.getLogger("smsbot")

# ──────────────────────────── Config Persistence ───────────────────────
def _load_admins() -> Set[int]:
    s = set()
    env_admins = os.getenv("ADMIN_USERS", "").strip()
    if env_admins: s.update(int(x) for x in env_admins.split(",") if x.strip().isdigit())
    if os.path.exists(ADMIN_USERS_FILE):
        try: s.update(int(x) for x in json.load(open(ADMIN_USERS_FILE)) if str(x).isdigit())
        except: pass
    return s

def _save_admins(admins: Set[int]): json.dump(list(admins), open(ADMIN_USERS_FILE, "w"))

def _load_json_float(path: str, key: str, default: float) -> float:
    try: return float(json.load(open(path)).get(key, default))
    except: return default

def _save_json_float(path: str, key: str, val: float): json.dump({key:val}, open(path, "w"))

ADMIN_USERS = _load_admins()
SMS_RATE_BDT = _load_json_float(RATE_CONFIG_FILE, "rate", 0.0)
MIN_WITHDRAW_BDT = _load_json_float(WITHDRAW_CONFIG_FILE, "min", 10.0)

FAKE_OTP_CONFIG: Dict[str, list] = {"fb": [], "ig": []}
if os.path.exists(FAKE_OTP_CONFIG_FILE):
    try:
        data = json.load(open(FAKE_OTP_CONFIG_FILE))
        for k in ("fb","ig"):
            v = data.get(k)
            if isinstance(v, list): FAKE_OTP_CONFIG[k] = v
            elif isinstance(v, dict): FAKE_OTP_CONFIG[k] = [v]
    except: pass

def save_fake_otp_config(): json.dump(FAKE_OTP_CONFIG, open(FAKE_OTP_CONFIG_FILE, "w"), indent=2, ensure_ascii=False)

# ───────────────────────────── Database ────────────────────────────────
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, balance_bdt REAL DEFAULT 0,
            bkash TEXT, rocket TEXT, binance TEXT,
            total_otps INTEGER DEFAULT 0, today_otps INTEGER DEFAULT 0,
            last_otp_date TEXT, today_earned REAL DEFAULT 0,
            total_earned REAL DEFAULT 0, numbers_used INTEGER DEFAULT 0)""")
        for col, col_def in [("total_otps","INTEGER DEFAULT 0"),("today_otps","INTEGER DEFAULT 0"),("last_otp_date","TEXT"),("today_earned","REAL DEFAULT 0"),("total_earned","REAL DEFAULT 0"),("numbers_used","INTEGER DEFAULT 0")]:
            try: conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            except sqlite3.OperationalError: pass
        conn.execute("CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, method TEXT, account TEXT, amount REAL, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, approved_at TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS user_credentials (user_id INTEGER, site TEXT, email TEXT, password TEXT, PRIMARY KEY (user_id, site))")
    log.info("DB initialized")

def db(sql: str, *params) -> sqlite3.Cursor:
    conn = sqlite3.connect(DB_FILE)
    try: return conn.execute(sql, params), conn
    except: conn.close(); raise

def dbr(sql: str, *params) -> list:
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    try: return conn.execute(sql, params).fetchall()
    finally: conn.close()

def ensure_user(uid: int): db("INSERT OR IGNORE INTO users (user_id) VALUES (?)", uid)[1].close()
def get_balance(uid: int) -> float:
    ensure_user(uid)
    row = sqlite3.connect(DB_FILE).execute("SELECT balance_bdt FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0.0
def get_wallet(uid: int) -> dict:
    row = sqlite3.connect(DB_FILE).execute("SELECT bkash, rocket, binance FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(zip(("bkash","rocket","binance"), row)) if row else {"bkash":None,"rocket":None,"binance":None}
def credit(uid: int, amt: float): db("UPDATE users SET balance_bdt=balance_bdt+? WHERE user_id=?", amt, uid)[1].close()
def deduct(uid: int, amt: float): db("UPDATE users SET balance_bdt=balance_bdt-? WHERE user_id=?", amt, uid)[1].close()
def set_wallet(uid: int, typ: str, val: str):
    if typ in ("bkash","rocket","binance"): db(f"UPDATE users SET {typ}=? WHERE user_id=?", val, uid)[1].close()
def new_withdrawal(uid: int, method: str, account: str, amount: float) -> int:
    c, conn = db("INSERT INTO withdrawals (user_id,method,account,amount) VALUES (?,?,?,?)", uid, method, account, amount)
    rid = c.lastrowid; conn.close(); return rid
def get_pending() -> list: return dbr("SELECT * FROM withdrawals WHERE status='pending'")
def get_approved() -> list: return dbr("SELECT * FROM withdrawals WHERE status='approved' ORDER BY approved_at DESC")
def approve_wd(wid: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT user_id, amount FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not row: conn.close(); return False
    deduct(row[0], row[1])
    conn.execute("UPDATE withdrawals SET status='approved', approved_at=CURRENT_TIMESTAMP WHERE id=?", (wid,))
    conn.commit(); conn.close(); return True

def update_stats(uid: int, earned: float=0.0, otp_count: int=0, nums: int=0):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT last_otp_date,today_otps,today_earned FROM users WHERE user_id=?", (uid,)).fetchone()
    if row[0] != today:
        conn.execute("UPDATE users SET today_otps=?, today_earned=?, last_otp_date=?, numbers_used=numbers_used+? WHERE user_id=?", (otp_count, earned, today, nums, uid))
    else:
        conn.execute("UPDATE users SET total_otps=total_otps+?, today_otps=today_otps+?, total_earned=total_earned+?, today_earned=today_earned+?, numbers_used=numbers_used+? WHERE user_id=?", (otp_count, otp_count, earned, earned, nums, uid))
    conn.commit(); conn.close()

def user_sum_withdrawn(uid: int) -> float:
    row = sqlite3.connect(DB_FILE).execute("SELECT SUM(amount) FROM withdrawals WHERE user_id=? AND status='approved'", (uid,)).fetchone()
    return row[0] or 0.0

def get_stats(uid: int) -> dict:
    ensure_user(uid)
    row = sqlite3.connect(DB_FILE, row_factory=sqlite3.Row).execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: return {}
    today = date.today().isoformat()
    t_otps = 0 if row["last_otp_date"]!=today else row["today_otps"]
    t_earn = 0.0 if row["last_otp_date"]!=today else row["today_earned"]
    wd = user_sum_withdrawn(uid)
    return dict(numbers_used=row["numbers_used"], today_otps=t_otps, total_otps=row["total_otps"], today_earned_bdt=t_earn, total_earned_bdt=row["total_earned"], total_withdrawn_bdt=wd, balance_bdt=row["balance_bdt"])

def all_user_ids() -> list: return [r[0] for r in sqlite3.connect(DB_FILE).execute("SELECT user_id FROM users").fetchall()]

def admin_stats() -> dict:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE, row_factory=sqlite3.Row)
    row = conn.execute("SELECT SUM(numbers_used) as nu, SUM(CASE WHEN last_otp_date=? THEN today_otps ELSE 0 END) as to_, SUM(total_otps) as tot, SUM(CASE WHEN last_otp_date=? THEN today_earned ELSE 0 END) as te, SUM(total_earned) as tte FROM users", (today,today)).fetchone()
    tw = conn.execute("SELECT SUM(amount) FROM withdrawals WHERE status='approved'").fetchone()[0] or 0.0
    conn.close()
    return dict(numbers_used=row["nu"] or 0, today_otps=row["to_"] or 0, total_otps=row["tot"] or 0, today_cost_bdt=row["te"] or 0.0, total_cost_bdt=row["tte"] or 0.0, total_withdrawn_bdt=tw)

def store_creds(uid: int, site: str, email: str, pw: str):
    db("INSERT OR REPLACE INTO user_credentials (user_id,site,email,password) VALUES (?,?,?,?)", uid, site, email, pw)[1].close()
def get_creds(uid: int, site: str):
    row = sqlite3.connect(DB_FILE).execute("SELECT email,password FROM user_credentials WHERE user_id=? AND site=?", (uid,site)).fetchone()
    return row if row else None
def remove_creds(uid: int, site: str):
    db("DELETE FROM user_credentials WHERE user_id=? AND site=?", uid, site)[1].close()

# ──────────────────────────── Browser Manager ──────────────────────────
PAGE_TTL = 7200    # 2 hours in seconds
BROWSER_TTL = 21600  # 6 hours in seconds

class BrowserManager:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        self._pages: Dict[str, Page] = {}
        self._user_pages: Dict[int, Dict[str, Page]] = {}
        self._contexts: Dict[int, Any] = {}
        self._page_created: Dict[int, float] = {}
        self._browser_start_time: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None

    async def _is_stale(self, page: Page) -> bool:
        created = self._page_created.get(id(page))
        return created is not None and (time.monotonic() - created) >= PAGE_TTL

    async def _close_all_pages(self):
        for pid in list(self._contexts.keys()): self._page_created.pop(pid, None)
        for ctx in list(self._contexts.values()):
            try: await ctx.close()
            except: pass
        self._contexts.clear()
        self._pages.clear()
        self._user_pages.clear()

    async def _restart_browser(self):
        async with self._lock:
            log.info("Browser TTL expired or watchdog triggered — restarting browser...")
            await self._close_all_pages()
            if self._browser:
                try: await self._browser.close()
                except: pass
            if self._playwright:
                try: await self._playwright.stop()
                except: pass
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-blink-features=AutomationControlled"])
            if not self._browser: raise RuntimeError("Browser launch failed")
            self._browser_start_time = time.monotonic()
            log.info("Browser restarted successfully")

    async def ensure_browser(self):
        async with self._lock:
            needs_restart = False
            if self._browser is None or not self._browser.is_connected():
                needs_restart = True
            elif self._browser_start_time > 0 and (time.monotonic() - self._browser_start_time) >= BROWSER_TTL:
                log.info("Browser reached 6-hour TTL — restarting")
                needs_restart = True
            if needs_restart:
                await self._close_all_pages()
                if self._browser:
                    try: await self._browser.close()
                    except: pass
                if self._playwright:
                    try: await self._playwright.stop()
                    except: pass
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-blink-features=AutomationControlled"])
                if not self._browser: raise RuntimeError("Browser launch failed")
                self._browser_start_time = time.monotonic()

    async def _new_page(self) -> Page:
        ctx = await self._browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", viewport={"width":1280,"height":800})
        await ctx.grant_permissions(["clipboard-read"])
        page = await ctx.new_page()
        pid = id(page)
        self._contexts[pid] = ctx
        self._page_created[pid] = time.monotonic()
        return page

    async def _close_page(self, page: Page):
        pid = id(page)
        self._page_created.pop(pid, None)
        ctx = self._contexts.pop(pid, None)
        try: await page.close()
        except: pass
        if ctx:
            try: await ctx.close()
            except: pass

    async def _login(self, page: Page, site: str, email: str, password: str) -> bool:
        cfg = SITES[site]
        try:
            await page.goto(cfg["login_url"], wait_until="networkidle", timeout=30000)
            await page.fill("input[type='email']", email)
            await page.fill("input[type='password']", password)
            await page.click("button[type='submit']")
            try:
                await page.wait_for_url(lambda u: "auth" not in u and "login" not in u, timeout=60000)
                return True
            except:
                if "/dialer/" in page.url: return True
                try: await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=10000); return True
                except: return False
        except Exception as e:
            log.error(f"Login error: {e}"); return False

    async def _fresh_page_for_site(self, site: str, pages_dict: dict, email: str, password: str) -> Page:
        """Create a new page, log in, and store it."""
        page = await self._new_page()
        pages_dict[site] = page
        if not await self._login(page, site, email, password):
            await self._close_page(page); pages_dict.pop(site, None)
            raise RuntimeError(f"Login failed for {site}")
        return page

    async def get_page(self, site: str, uid: int = None) -> Page:
        await self.ensure_browser()
        creds = get_creds(uid, site) if uid else None
        if creds:
            pages = self._user_pages.setdefault(uid, {})
            page = pages.get(site)
            if page is None or page.is_closed() or await self._is_stale(page):
                if page and not page.is_closed(): await self._close_page(page)
                page = await self._fresh_page_for_site(site, pages, creds[0], creds[1])
            else:
                try: await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=5000)
                except:
                    await self._close_page(page)
                    page = await self._fresh_page_for_site(site, pages, creds[0], creds[1])
        else:
            page = self._pages.get(site)
            if page is None or page.is_closed() or await self._is_stale(page):
                if page and not page.is_closed(): await self._close_page(page)
                page = await self._fresh_page_for_site(site, self._pages, SMS_EMAIL, SMS_PASSWORD)
            else:
                try: await page.wait_for_selector(".user-dropdown, table.gn-tbl", timeout=5000)
                except:
                    await self._close_page(page)
                    page = await self._fresh_page_for_site(site, self._pages, SMS_EMAIL, SMS_PASSWORD)
        await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
        if "login" in page.url or "auth" in page.url:
            raise RuntimeError(f"Session expired for {site}, redirected to login")
        return page

    async def cleanup_page(self, site: str, uid: int = None):
        if uid and uid in self._user_pages:
            page = self._user_pages[uid].pop(site, None)
            if page: await self._close_page(page)
            if not self._user_pages[uid]: del self._user_pages[uid]
        else:
            page = self._pages.pop(site, None)
            if page: await self._close_page(page)

    async def force_refresh_session(self, site: str, uid: int = None):
        """Completely tear down and re-establish a session for a site."""
        await self.cleanup_page(site, uid)
        await asyncio.sleep(2)
        page = await self.get_page(site, uid)
        await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
        if "login" in page.url or "auth" in page.url:
            raise RuntimeError(f"Session refresh failed for {site}")

    async def fetch_number(self, range_str: str, site: str, uid: int = None) -> Optional[dict]:
        last_error = None
        for attempt in range(1, FETCH_RETRIES+1):
            try:
                page = await self.get_page(site, uid)
                async with self._page_lock:
                    if "/dialer/" not in page.url:
                        await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
                    first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
                    old_num = None
                    if await first_row.count() > 0:
                        old_num = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")
                    inp = page.locator("input.gn-range-input")
                    await inp.wait_for(state="visible", timeout=15000)
                    await inp.fill("")
                    await inp.type(range_str, delay=25)
                    await asyncio.sleep(0.15)
                    await page.locator("button.btn.btn-primary:has-text('Get Number')").click()
                    await page.wait_for_function(
                        """(old)=>{const r=document.querySelectorAll('table.gn-tbl tbody tr');
                        for(let i=0;i<r.length;i++){const n=r[i].querySelector('.gn-num');
                        if(n&&n.textContent.trim().replace(/^\\+/,'')!==old)return true;break;}return false;}""",
                        arg=old_num or "", timeout=15000)
                    first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
                    if not await first_row.count(): raise RuntimeError("No rows after click")
                    number = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")
                    if not number: raise RuntimeError("Empty number")
                    country = (await first_row.locator(".gn-meta").first.inner_text()).strip() if await first_row.locator(".gn-meta").count() else "Unknown"
                    operator = (await first_row.locator(".gn-meta-sub").first.inner_text()).strip() if await first_row.locator(".gn-meta-sub").count() else "Unknown"
                    return {"number":number,"country":country,"operator":re.sub(r"\s+"," ",operator).strip()}
            except Exception as e:
                log.error(f"fetch attempt {attempt} failed: {e}"); last_error = e
                await self.force_refresh_session(site, uid)
        log.error(f"All {FETCH_RETRIES} attempts failed: {last_error}"); return None

    async def poll_otp(self, number: str, site: str, uid: int = None) -> Optional[str]:
        try:
            page = await self.get_page(site, uid)
            async with self._page_lock:
                if "/dialer/" not in page.url:
                    await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
                rows = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num"))
                for i in range(await rows.count()):
                    row = rows.nth(i)
                    n_el = row.locator(".gn-num").first
                    if await n_el.count()==0: continue
                    if (await n_el.inner_text()).strip().lstrip("+")!=number: continue
                    s_el = row.locator(".gn-status-pill")
                    if await s_el.count()==0: continue
                    if (await s_el.first.inner_text()).strip().lower()!="success": continue
                    btn = row.locator("button.gn-otp-copy")
                    if await btn.count()>0:
                        await btn.first.click(); await asyncio.sleep(0.3)
                        try: return await page.evaluate("navigator.clipboard.readText()")
                        except: pass
                        title = await btn.first.get_attribute("title") or ""
                        if ":" in title: return title.split(":",1)[1].strip()
                    return None
                return None
        except Exception as e:
            log.error(f"poll_otp error: {e}"); return None

    def start_watchdog(self):
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                if self._browser and self._browser.is_connected():
                    _ = self._browser.contexts
                else:
                    raise RuntimeError("Browser not connected")
            except Exception as e:
                log.warning(f"Watchdog detected dead browser: {e}")
                try: await self._restart_browser()
                except Exception as e2: log.error(f"Watchdog restart failed: {e2}")

    async def shutdown(self):
        await self.stop_watchdog()
        async with self._lock:
            self._page_created.clear()
            for ctx in list(self._contexts.values()):
                try: await ctx.close()
                except: pass
            self._contexts.clear()
            self._pages.clear()
            self._user_pages.clear()
            if self._browser:
                try: await self._browser.close()
                except: pass
                self._browser = None
            if self._playwright:
                try: await self._playwright.stop()
                except: pass
                self._playwright = None

# ────────────────────────────── Health Server ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

def start_health(port: int):
    if port>0:
        t = threading.Thread(target=HTTPServer(("0.0.0.0",port), HealthHandler).serve_forever, daemon=True); t.start()
        log.info(f"Health server on port {port}")

# ─────────────────────────────── Globals ───────────────────────────────
browser = BrowserManager()
user_sessions: Dict[int, dict] = {}
BOT_USERNAME: Optional[str] = None
auto_fake_running = False
auto_fake_task: Optional[asyncio.Task] = None

MALE_NAMES = ["Liam","Noah","Oliver","Elijah","James","William","Benjamin","Lucas","Henry","Alexander","Mason","Michael","Ethan","Daniel","Jacob","Logan","Jackson","Levi","Sebastian","Mateo","Jack","Owen","Theodore","Aiden","Samuel","Joseph","John","David","Wyatt","Matthew","Luke","Asher","Carter","Julian","Grayson","Leo","Jayden","Gabriel","Isaac","Lincoln","Anthony","Hudson","Dylan","Ezra","Thomas","Charles","Christopher","Jaxon","Maverick","Josiah"]
FEMALE_NAMES = ["Olivia","Emma","Ava","Charlotte","Sophia","Amelia","Isabella","Mia","Evelyn","Harper","Camila","Gianna","Abigail","Luna","Ella","Elizabeth","Sofia","Emily","Avery","Mila","Scarlett","Eleanor","Madison","Layla","Penelope","Aria","Chloe","Grace","Ellie","Nora","Hazel","Zoey","Riley","Victoria","Lily","Aurora","Violet","Nova","Hannah","Emilia","Zoe","Stella","Everly","Isla","Leah","Lillian","Addison","Willow","Lucy","Paisley"]
LAST_NAMES = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson","Thomas","Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson","White","Harris","Sanchez","Clark","Ramirez","Lewis","Robinson","Walker","Young","Allen","King","Wright","Scott","Torres","Nguyen","Hill","Flores","Green","Adams","Nelson","Baker","Hall","Rivera","Campbell","Mitchell","Carter","Roberts"]

# ─────────────────────────────── Helpers ───────────────────────────────
def is_owner(uid: int) -> bool: return uid == OWNER_USER_ID
def is_admin(uid: int) -> bool: return uid in ADMIN_USERS or is_owner(uid)

def mask_num(n: str) -> str:
    if len(n)<=6: return n
    pre = "+" if n.startswith("+") else ""
    num = n.lstrip("+")
    if len(num)<=6: return f"{pre}{num}"
    return f"{pre}{num[:3]}{'*'*(len(num)-6)}{num[-3:]}"

def extract_otp(msg: str) -> Optional[str]:
    m = re.search(r"\b\d{1,3}(?:\s?\d{1,3})+\b", msg)
    if m:
        d = re.sub(r"\D","",m.group())
        if 4<=len(d)<=8: return d
    fb = re.findall(r"\b\d{4,8}\b", re.sub(r"\s+","",msg))
    return fb[0] if fb else None

def clean_msg(msg: str) -> str:
    for p in [r"^Facebook:\s*",r"^Instagram:\s*",r"^WhatsApp:\s*"]: msg = re.sub(p,"",msg,count=1)
    return msg.strip()

def gen_2fa(secret: str) -> dict:
    if not TOTP_AVAILABLE: return {"success":False,"message":"pyotp not installed"}
    try: return {"success":True,"code":pyotp.TOTP(''.join(secret.split()).upper()).now()}
    except: return {"success":False,"message":"Invalid secret"}

def gen_identity(gender: str) -> dict:
    day = f"{datetime.now().day:02d}"
    first = random.choice(MALE_NAMES if gender=="male" else FEMALE_NAMES)
    last = random.choice(LAST_NAMES)
    username = f"{first.lower()}{last.lower()}{''.join(random.choices(string.digits,k=random.randint(2,3)))}"
    chars = string.ascii_letters+string.digits+"#$&"
    pw = random.sample(string.ascii_uppercase,1)+random.sample(string.ascii_lowercase,1)+random.sample(string.digits,1)+random.sample("#$&",1)
    pw += random.choices(chars,k=random.randint(4,6))
    random.shuffle(pw)
    return {"name":f"{first} {last}","username":username,"password":''.join(pw)+day,"gender":gender}

def stop_monitor(uid: int):
    t = user_sessions.get(uid,{}).get("monitor_task")
    if t and not t.done(): t.cancel()

def balance_text(uid: int) -> str:
    bal = get_balance(uid); w = get_wallet(uid)
    return (f"⚠️ Double-check your wallet! Wrong details = no refund.\n\n🤑 Balance: {bal:.2f} BDT / ${bal/EXCHANGE_RATE:.4f}\n\n"
            f"🌍 Bkash: {w['bkash'] or 'Not Set'}\n🌍 Rocket: {w['rocket'] or 'Not Set'}\n🌍 Binance: {w['binance'] or 'Not Set'}\n\n"
            f"💳 Minimum Withdrawal: {MIN_WITHDRAW_BDT:.1f} BDT / ${MIN_WITHDRAW_BDT/EXCHANGE_RATE:.2f}")

# ──────────────────────────── Fake OTP Helpers ─────────────────────────
def rand_otp(n=6): return ''.join(random.choices(string.digits,k=n))
def rand_last3(): return ''.join(random.choices(string.digits,k=3))

def build_fake_msg(platform: str, cfg: dict = None) -> Optional[str]:
    base = "fb" if platform.startswith("fb") else "ig"
    if not cfg:
        cfgs = FAKE_OTP_CONFIG.get(base,[])
        if not cfgs: return None
        cfg = cfgs[0]
    nd = f"+{cfg['country_code']}******{rand_last3()}"
    if platform=="fb5":
        o=rand_otp(5); b=f"<#> {o} is your Facebook code H29Q+Fsn4Sr"
    elif platform=="fb6":
        o=rand_otp(6); b=f"<#> {o} is your Facebook code H29Q+Fsn4Sr"
    else:
        o=rand_otp(6); s=random.choice(["GdDGcwrWHVm","SIYRxKrru1t"]); b=f"<#> {o[:3]} {o[3:]} is your Instagram code. Don't share it. {s}"
    if random.random()<0.05: b=f"<#> {o} आपका Facebook कोड है H29Q+Fsn4Sr"
    return f"📨 {cfg['country_name']} OTP Received\n━━━━━━━━━━━━━━━━\n📞 Number: {nd}\n🔑 OTP: {o}\n\n💬 {b}\n━━━━━━━━━━━━━━━━"

def get_bot_btn():
    if BOT_USERNAME: return InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Get Number",url=f"https://t.me/{BOT_USERNAME}?start=start")]])
    return None

async def send_fake(app, platform: str, cfg: dict = None) -> bool:
    msg = build_fake_msg(platform, cfg)
    if not msg: return False
    try: await app.bot.send_message(chat_id=OTP_GROUP_ID, text=msg, reply_markup=get_bot_btn()); return True
    except Exception as e: log.error(f"Fake send error: {e}"); return False

async def auto_fake_loop(app: Application):
    global auto_fake_running
    while auto_fake_running:
        pf = random.choice(["fb5","fb6","ig"]); base = "fb" if pf.startswith("fb") else "ig"; cfgs = FAKE_OTP_CONFIG.get(base,[])
        if cfgs:
            msg = build_fake_msg(pf, random.choice(cfgs))
            if msg:
                try: await app.bot.send_message(chat_id=OTP_GROUP_ID, text=msg, reply_markup=get_bot_btn())
                except: pass
        for _ in range(random.choice([0,1,2])):
            if not auto_fake_running: break
            pf2 = random.choice(["fb5","fb6","ig"]); base2 = "fb" if pf2.startswith("fb") else "ig"; cfgs2 = FAKE_OTP_CONFIG.get(base2,[])
            if cfgs2:
                m2 = build_fake_msg(pf2, random.choice(cfgs2))
                if m2:
                    try: await app.bot.send_message(chat_id=OTP_GROUP_ID, text=m2, reply_markup=get_bot_btn())
                    except: pass
            await asyncio.sleep(0.2)
        await asyncio.sleep(random.uniform(1,5))

def start_auto(app):
    global auto_fake_running, auto_fake_task
    if auto_fake_running: return
    auto_fake_running=True; auto_fake_task=asyncio.create_task(auto_fake_loop(app))
def stop_auto():
    global auto_fake_running, auto_fake_task
    auto_fake_running=False
    if auto_fake_task and not auto_fake_task.done(): auto_fake_task.cancel()
    auto_fake_task=None

# ────────────────────────────── Keyboards ──────────────────────────────
def main_kb(uid: int) -> ReplyKeyboardMarkup:
    b = [[KeyboardButton("📡 Get Number"),KeyboardButton("🔑 Get 2FA")],[KeyboardButton("📋 Fake Details"),KeyboardButton("💰 Balance")],[KeyboardButton("📊 Status"),KeyboardButton("👤 Accounts")]]
    if is_admin(uid): b.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(b, resize_keyboard=True)

def site_kb(): return ReplyKeyboardMarkup([[KeyboardButton("🔵 Stexsms"),KeyboardButton("🔵 Voltxsms")],[KeyboardButton("🔙 Back")]], resize_keyboard=True)

def admin_kb(uid: int) -> ReplyKeyboardMarkup:
    b = [[KeyboardButton("Interval")],[KeyboardButton("Set SMS Rate"),KeyboardButton("Set Withdraw Rate")],[KeyboardButton("Pending"),KeyboardButton("Approved")],[KeyboardButton("Users Status"),KeyboardButton("Broadcast")],[KeyboardButton("📨 Fake OTP")]]
    if is_owner(uid): b.append([KeyboardButton("Admin Set")])
    b.append([KeyboardButton("🔙 Back")]); return ReplyKeyboardMarkup(b, resize_keyboard=True)

def admin_set_kb(): return ReplyKeyboardMarkup([[KeyboardButton("Add Admin"),KeyboardButton("Remove Admin")],[KeyboardButton("🔙 Back")]], resize_keyboard=True)

def num_ready_kb(num: str) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton("👥 OTP Group",url=OTP_GROUP_LINK),InlineKeyboardButton("🔄 Change Number",callback_data="change_number")]
    r2 = [InlineKeyboardButton("📋 Copy Number", copy_text=CopyTextButton(text=f"+{num}"), style="success")] if COPY_SUPPORTED else [InlineKeyboardButton("📋 Copy Number", callback_data=f"copy_num_{num}")]
    return InlineKeyboardMarkup([r1,r2])

def bal_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("Set Wallet",callback_data="profile_set_wallet"),InlineKeyboardButton("Withdraw",callback_data="profile_withdraw")]])
def wallet_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("Bkash",callback_data="wallet_bkash"),InlineKeyboardButton("Rocket",callback_data="wallet_rocket")],[InlineKeyboardButton("Binance",callback_data="wallet_binance")]])
def wd_method_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("Bkash",callback_data="withdraw_method_bkash"),InlineKeyboardButton("Rocket",callback_data="withdraw_method_rocket")],[InlineKeyboardButton("Binance",callback_data="withdraw_method_binance"),InlineKeyboardButton("Mobile Recharge",callback_data="withdraw_method_mobile")]])
def login_site_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("StexSMS",callback_data="login_site_stexsms"),InlineKeyboardButton("VoltxSMS",callback_data="login_site_voltxsms")]])
def accts_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Log In",callback_data="accounts_login"),InlineKeyboardButton("🚪 Log Out",callback_data="accounts_logout")]])
def logout_site_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("StexSMS",callback_data="logout_site_stexsms"),InlineKeyboardButton("VoltxSMS",callback_data="logout_site_voltxsms")]])
def fake_menu_kb(): return ReplyKeyboardMarkup([[KeyboardButton("FB Send 5"),KeyboardButton("FB Send 6")],[KeyboardButton("IG Send")],[KeyboardButton(f"Auto: {'ON' if auto_fake_running else 'OFF'}")],[KeyboardButton("Set Details")],[KeyboardButton("🔙 Back")]], resize_keyboard=True)
def fake_det_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("Facebook",callback_data="set_fake_details_fb"),InlineKeyboardButton("Instagram",callback_data="set_fake_details_ig")]])

# ────────────────────────── Membership Check ───────────────────────────
async def check_membership(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await ctx.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=upd.effective_user.id)
        if m.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER): return True
    except: pass
    if upd.message:
        await upd.message.reply_text("🔒 Access Restricted!\n\nPlease join our channel to use this bot.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel",url=OTP_GROUP_LINK),InlineKeyboardButton("Verify",callback_data="verify_membership")]]))
    return False

# ────────────────────────────── OTP Monitor ────────────────────────────
async def monitor_number(app: Application, uid: int, number: str, country: str, operator: str, site: str):
    deadline = asyncio.get_event_loop().time() + MONITOR_TIMEOUT
    bot_user = await app.bot.get_me()
    custom = get_creds(uid, site) is not None
    while asyncio.get_event_loop().time() < deadline:
        s = user_sessions.get(uid)
        if not s or s.get("number")!=number: return
        full = await browser.poll_otp(number, site, uid)
        if full:
            otp = extract_otp(full)
            if otp and s.get("last_otp")!=otp:
                s["last_otp"] = otp
                clean = clean_msg(full)
                safe = escape_markdown(clean, version=1)
                if not custom:
                    bef = get_balance(uid)
                    if SMS_RATE_BDT>0: credit(uid, SMS_RATE_BDT)
                    bal = f"💰 Balance: {bef:.2f} BDT → {get_balance(uid):.2f} BDT"
                    update_stats(uid, earned=SMS_RATE_BDT, otp_count=1, nums=0)
                else: bal = "💡 (using your own account – no earnings)"
                txt = f"📩 {country} Message Received!\n\n📞 Number: `+{number}`\n🔑 OTP Code: `{otp}`\n\n💬 Message: {safe}\n\n{bal}"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"OTP: {otp}", copy_text=CopyTextButton(text=otp), style="success")]]) if COPY_SUPPORTED else InlineKeyboardMarkup([[InlineKeyboardButton(f"OTP: {otp}", callback_data=f"copy_otp_{otp}")]])
                try: await app.bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=kb)
                except Exception as e: log.error(f"DM error: {e}")
                masked = mask_num(f"+{number}")
                gtxt = f"📨 {country} OTP Received\n━━━━━━━━━━━━━━━━\n📞 Number: {masked}\n🔑 OTP: {otp}\n\n💬 {clean}\n━━━━━━━━━━━━━━━━"
                try: await app.bot.send_message(OTP_GROUP_ID, gtxt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Get Number",url=f"https://t.me/{bot_user.username}?start=start")]]))
                except Exception as e: log.error(f"Group error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

# ──────────────────────────── Deliver Number ───────────────────────────
async def deliver_number(bot, uid: int, result: dict, site: str, edit_msg=None):
    n,c,o = result["number"], result["country"], result["operator"]
    user_sessions[uid].update(number=n,country=c,operator=o,last_otp=None,site=site)
    txt = f"✅ *Number Ready!*\n━━━━━━━━━━━━━━━━\n🏢 Provider: `{o}`\n🌍 Country: `{c}`\n📞 Number: `+{n}`\n━━━━━━━━━━━━━━━━\n⏳ Waiting for OTP..."
    if edit_msg:
        try: sent = await edit_msg.edit_text(txt, parse_mode="Markdown", reply_markup=num_ready_kb(n))
        except: sent = await bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=num_ready_kb(n))
    else: sent = await bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=num_ready_kb(n))
    user_sessions[uid]["msg_id"] = sent.message_id
    if hasattr(bot,"bot"):
        stop_monitor(uid)
        task = asyncio.create_task(monitor_number(bot, uid, n, c, o, site))
        user_sessions[uid]["monitor_task"] = task

# ═══════════════════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════════════════

# ── Conversation states ──
MAIN_MENU, SITE_MENU, AWAIT_RANGE, ADMIN_MENU, SET_INTERVAL, ADMIN_SET_MENU, ADD_ADMIN_INPUT, AWAIT_2FA_SECRET, SET_RATE, SET_WITHDRAW_RATE, LOGIN_PASSWORD, BROADCAST_AWAIT = range(12)

async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not await check_membership(upd, ctx): return ConversationHandler.END
    user_sessions.setdefault(uid, {})
    await upd.message.reply_text(f"👋 Welcome *{upd.effective_user.first_name}*!\n\nTap *📡 Get Number* to begin.", parse_mode="Markdown", reply_markup=main_kb(uid))
    return MAIN_MENU

async def on_main_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not await check_membership(upd, ctx): return ConversationHandler.END
    if not upd.message or not upd.message.text: return None

    # Withdrawal flows
    if ctx.user_data.get('awaiting_withdraw_account'):
        ctx.user_data['withdraw_account'] = upd.message.text.strip()
        ctx.user_data['awaiting_withdraw_account']=False; ctx.user_data['awaiting_withdraw_amount']=True
        bal = get_balance(uid)
        await upd.message.reply_text(f"💰 Balance: {bal:.2f} BDT / ${bal/EXCHANGE_RATE:.4f}\n💳 Min Withdrawal: {MIN_WITHDRAW_BDT:.1f} BDT / ${MIN_WITHDRAW_BDT/EXCHANGE_RATE:.2f}\n\nEnter amount (BDT):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return MAIN_MENU
    if ctx.user_data.get('awaiting_withdraw_amount'):
        try: amt = float(upd.message.text.strip())
        except: await upd.message.reply_text("❌ Invalid amount.", reply_markup=main_kb(uid)); ctx.user_data.clear(); return MAIN_MENU
        bal = get_balance(uid)
        if amt<MIN_WITHDRAW_BDT: await upd.message.reply_text(f"❌ Min withdrawal {MIN_WITHDRAW_BDT:.1f} BDT.", reply_markup=main_kb(uid)); ctx.user_data.clear(); return MAIN_MENU
        if amt>bal: await upd.message.reply_text("❌ Insufficient balance.", reply_markup=main_kb(uid)); ctx.user_data.clear(); return MAIN_MENU
        new_withdrawal(uid, ctx.user_data.get('withdraw_method'), ctx.user_data.get('withdraw_account'), amt)
        ctx.user_data.clear(); await upd.message.reply_text("✅ Withdrawal submitted.", reply_markup=main_kb(uid)); return MAIN_MENU
    if ctx.user_data.get('awaiting_wallet_number'):
        wt = ctx.user_data.get('wallet_type')
        if not wt: await upd.message.reply_text("❌ Error.", reply_markup=main_kb(uid)); ctx.user_data.clear(); return MAIN_MENU
        set_wallet(uid, wt, upd.message.text.strip())
        ctx.user_data.clear(); await upd.message.reply_text("✅ Wallet updated.", reply_markup=main_kb(uid)); return MAIN_MENU
    if ctx.user_data.get('awaiting_login_email'):
        email = upd.message.text.strip(); site = ctx.user_data.get('login_site')
        if not site: await upd.message.reply_text("❌ Session error.", reply_markup=main_kb(uid)); ctx.user_data.clear(); return MAIN_MENU
        ctx.user_data['login_email']=email; ctx.user_data['awaiting_login_email']=False; ctx.user_data['awaiting_login_password']=True
        await upd.message.reply_text("🔑 Now enter your *password*:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()); return LOGIN_PASSWORD

    text = upd.message.text
    if text=="📡 Get Number": await upd.message.reply_text("🌐 *Select provider:*", parse_mode="Markdown", reply_markup=site_kb()); return SITE_MENU
    if text=="🔑 Get 2FA": await upd.message.reply_text("📲 Paste your 2FA Secret Key", reply_markup=ReplyKeyboardRemove()); return AWAIT_2FA_SECRET
    if text=="📋 Fake Details":
        gk = InlineKeyboardMarkup([[InlineKeyboardButton("🚹 Male",callback_data="gender_male"),InlineKeyboardButton("🚺 Female",callback_data="gender_female")]])
        await upd.message.reply_text("👤 *Select gender:*", parse_mode="Markdown", reply_markup=gk); return MAIN_MENU
    if text=="💰 Balance": await upd.message.reply_text(balance_text(uid), parse_mode="Markdown", reply_markup=bal_kb()); return MAIN_MENU
    if text=="📊 Status":
        s = get_stats(uid)
        await upd.message.reply_text(
            f"📊 YOUR STATISTICS\n━━━━━━━━━━━━━━━━━━━━\n📞 Numbers Used: {s.get('numbers_used',0)}\n📩 Today's OTPs: {s.get('today_otps',0)}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n💰 Today's Earned: ${s.get('today_earned_bdt',0.0)/EXCHANGE_RATE:.3f} USDT\n"
            f"💵 Total Earned: ৳ {s.get('total_earned_bdt',0.0):.3f} BDT/ ${s.get('total_earned_bdt',0.0)/EXCHANGE_RATE:.3f} USDT\n"
            f"💳 Total Withdrawn: ৳ {s.get('total_withdrawn_bdt',0.0):.3f} BDT/ ${s.get('total_withdrawn_bdt',0.0)/EXCHANGE_RATE:.3f} USDT\n"
            f"━━━━━━━━━━━━━━━━━━━━\n💰 Balance: ৳ {s.get('balance_bdt',0.0):.3f} BDT/ ${s.get('balance_bdt',0.0)/EXCHANGE_RATE:.3f} USDT\n"
            f"━━━━━━━━━━━━━━━━━━━━\n📢 {BOT_NAME}", parse_mode="Markdown"); return MAIN_MENU
    if text=="👤 Accounts": await upd.message.reply_text("👤 *Accounts Management*\nChoose an action:", parse_mode="Markdown", reply_markup=accts_kb()); return MAIN_MENU
    if text=="⚙️ Admin Panel" and is_admin(uid):
        await upd.message.reply_text(f"⚙️ *Admin Panel*\nChange delay: `{CHANGE_NUMBER_DELAY}`s\nSMS Rate: `{SMS_RATE_BDT}` BDT/OTP\nMin Withdraw: `{MIN_WITHDRAW_BDT}` BDT", parse_mode="Markdown", reply_markup=admin_kb(uid))
        return ADMIN_MENU
    return MAIN_MENU

async def on_site_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    text = upd.message.text
    if text=="🔙 Back": await upd.message.reply_text("🏠 Main menu:", reply_markup=main_kb(uid)); return MAIN_MENU
    site = {"🔵 Stexsms":"stexsms","🔵 Voltxsms":"voltxsms"}.get(text)
    if site:
        user_sessions.setdefault(uid,{})["site"]=site; ctx.user_data["site"]=site
        last = user_sessions[uid].get("last_range","")
        await upd.message.reply_text(f"✏️ *{SITES[site]['name']} – Send range:*\n\nExample: `2250163333XXX`\n⚠️ Must contain `XXX`{f'\n\n📌 Last: `{last}`' if last else ''}", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return AWAIT_RANGE
    return SITE_MENU

async def on_range(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    r = upd.message.text.strip().upper()
    if "XXX" not in r: await upd.message.reply_text("❌ Range must contain `XXX`.", parse_mode="Markdown"); return AWAIT_RANGE
    site = ctx.user_data.get("site")
    if not site: await upd.message.reply_text("❌ No site selected."); return ConversationHandler.END
    stop_monitor(uid)
    user_sessions.setdefault(uid,{}).update(last_range=r,site=site,last_otp=None)
    update_stats(uid, earned=0, otp_count=0, nums=1)
    wm = await upd.message.reply_text("⏳ *Fetching your number...*", parse_mode="Markdown", reply_markup=main_kb(uid))
    result = await browser.fetch_number(r, site, uid)
    if not result:
        try: await wm.edit_text("❌ No number found. Try a different range.", reply_markup=main_kb(uid))
        except: await upd.message.reply_text("❌ No number found.", reply_markup=main_kb(uid))
        return MAIN_MENU
    await deliver_number(ctx.application, uid, result, site, edit_msg=wm)
    return MAIN_MENU

async def on_admin_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    text = upd.message.text.strip()

    if ctx.user_data.get('awaiting_fake_country_name'):
        ctx.user_data['fake_country_name']=text; ctx.user_data['awaiting_fake_country_name']=False; ctx.user_data['awaiting_fake_country_code']=True
        await upd.message.reply_text("📞 Enter *country code* (e.g., 224):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()); return ADMIN_MENU
    if ctx.user_data.get('awaiting_fake_country_code'):
        pl = ctx.user_data.pop('fake_otp_platform'); cn = ctx.user_data.pop('fake_country_name'); ctx.user_data.pop('awaiting_fake_country_code')
        FAKE_OTP_CONFIG.setdefault(pl,[]).append({"country_name":cn,"country_code":text.strip()}); save_fake_otp_config()
        await upd.message.reply_text(f"✅ Saved for {pl.upper()}.", reply_markup=fake_menu_kb()); return ADMIN_MENU

    if ctx.user_data.get('in_fake_otp_menu'):
        if text in ("FB Send 5","FB Send 6","IG Send"):
            base = "fb" if text.startswith("FB") else "ig"; cfgs = FAKE_OTP_CONFIG.get(base,[])
            if not cfgs: await upd.message.reply_text("❌ No details set. Use 'Set Details' first.", reply_markup=fake_menu_kb()); return ADMIN_MENU
            pc = "fb5" if text=="FB Send 5" else ("fb6" if text=="FB Send 6" else "ig")
            btns = [[InlineKeyboardButton(f"{c['country_name']} (+{c['country_code']})",callback_data=f"fake_send_{pc}_{i}")] for i,c in enumerate(cfgs)]
            await upd.message.reply_text("Select country:", reply_markup=InlineKeyboardMarkup(btns)); return ADMIN_MENU
        if text.startswith("Auto:"):
            if auto_fake_running: stop_auto(); await upd.message.reply_text("⏹ Stopped.", reply_markup=fake_menu_kb())
            else:
                if not FAKE_OTP_CONFIG.get("fb") and not FAKE_OTP_CONFIG.get("ig"): await upd.message.reply_text("❌ Set details first.", reply_markup=fake_menu_kb()); return ADMIN_MENU
                start_auto(ctx.application); await upd.message.reply_text("▶ Started.", reply_markup=fake_menu_kb())
            return ADMIN_MENU
        if text=="Set Details": await upd.message.reply_text("Select platform:", parse_mode="Markdown", reply_markup=fake_det_kb()); return ADMIN_MENU
        if text=="🔙 Back": ctx.user_data.pop('in_fake_otp_menu',None); await upd.message.reply_text("⚙️ Admin Panel", parse_mode="Markdown", reply_markup=admin_kb(uid)); return ADMIN_MENU
        await upd.message.reply_text("❓ Unknown.", reply_markup=fake_menu_kb()); return ADMIN_MENU

    if text=="📨 Fake OTP": ctx.user_data['in_fake_otp_menu']=True; await upd.message.reply_text("📨 *Fake OTP*", parse_mode="Markdown", reply_markup=fake_menu_kb()); return ADMIN_MENU
    if text=="Interval": await upd.message.reply_text("Enter delay (1-60s):", reply_markup=ReplyKeyboardRemove()); return SET_INTERVAL
    if text=="Set SMS Rate": await upd.message.reply_text("💰 Enter BDT per OTP:", reply_markup=ReplyKeyboardRemove()); return SET_RATE
    if text=="Set Withdraw Rate": await upd.message.reply_text("💳 Enter min withdrawal (BDT):", reply_markup=ReplyKeyboardRemove()); return SET_WITHDRAW_RATE
    if text=="Pending": await show_pending(upd.message, ctx); await upd.message.reply_text("✅ Done.", reply_markup=admin_kb(uid)); return ADMIN_MENU
    if text=="Approved": await show_approved(upd.message); await upd.message.reply_text("✅ Done.", reply_markup=admin_kb(uid)); return ADMIN_MENU
    if text=="Users Status":
        s = admin_stats()
        await upd.message.reply_text(
            f"📊 USERS STATISTICS\n━━━━━━━━━━━━━━━━━━━━\n📞 Numbers Used: {s['numbers_used']}\n📩 Today's OTPs: {s['today_otps']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n💰 Today's Cost: ৳ {s['today_cost_bdt']:.3f} BDT/ ${s['today_cost_bdt']/EXCHANGE_RATE:.3f} USDT\n"
            f"💵 Total Cost: ৳ {s['total_cost_bdt']:.3f} BDT/ ${s['total_cost_bdt']/EXCHANGE_RATE:.3f} USDT\n"
            f"💳 Total Withdrawn: ৳ {s['total_withdrawn_bdt']:.3f} BDT/ ${s['total_withdrawn_bdt']/EXCHANGE_RATE:.3f} USDT\n"
            f"━━━━━━━━━━━━━━━━━━━━\n📢 {BOT_NAME}", parse_mode="Markdown"); return ADMIN_MENU
    if text=="Broadcast": await upd.message.reply_text("📣 Send message to broadcast. /cancel to abort.", reply_markup=ReplyKeyboardRemove()); return BROADCAST_AWAIT
    if text=="Admin Set" and is_owner(uid): await upd.message.reply_text("⚙️ Admin Mgmt", parse_mode="Markdown", reply_markup=admin_set_kb()); return ADMIN_SET_MENU
    if text=="🔙 Back": await upd.message.reply_text("🏠 Main menu:", reply_markup=main_kb(uid)); return MAIN_MENU
    return ADMIN_MENU

async def on_set_interval(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global CHANGE_NUMBER_DELAY
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    try:
        v = int(upd.message.text.strip())
        if v<1 or v>60: raise ValueError
        CHANGE_NUMBER_DELAY=v; await upd.message.reply_text(f"✅ Delay set to {v}s.", parse_mode="Markdown", reply_markup=admin_kb(uid)); return ADMIN_MENU
    except: await upd.message.reply_text("❌ Invalid (1-60).", reply_markup=admin_kb(uid)); return ADMIN_MENU

async def on_set_rate(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global SMS_RATE_BDT
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    try:
        r = float(upd.message.text.strip())
        if r<0: raise ValueError
        SMS_RATE_BDT=r; _save_json_float(RATE_CONFIG_FILE,"rate",r); await upd.message.reply_text(f"✅ Rate set to {r} BDT.", parse_mode="Markdown", reply_markup=admin_kb(uid)); return ADMIN_MENU
    except: await upd.message.reply_text("❌ Invalid.", reply_markup=admin_kb(uid)); return ADMIN_MENU

async def on_set_withdraw(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MIN_WITHDRAW_BDT
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    try:
        v = float(upd.message.text.strip())
        if v<0: raise ValueError
        MIN_WITHDRAW_BDT=v; _save_json_float(WITHDRAW_CONFIG_FILE,"min",v); await upd.message.reply_text(f"✅ Min withdraw set to {v} BDT.", parse_mode="Markdown", reply_markup=admin_kb(uid)); return ADMIN_MENU
    except: await upd.message.reply_text("❌ Invalid.", reply_markup=admin_kb(uid)); return ADMIN_MENU

async def on_broadcast(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message: return None
    if upd.message.text and upd.message.text=="/cancel": await upd.message.reply_text("Cancelled.", reply_markup=admin_kb(uid)); return ADMIN_MENU
    users = all_user_ids(); ok=0
    for u in users:
        try: await upd.message.copy(chat_id=u); ok+=1
        except: pass
    await upd.message.reply_text(f"✅ Sent to {ok}/{len(users)}.", reply_markup=admin_kb(uid)); return ADMIN_MENU

async def on_admin_set(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    text = upd.message.text
    if text=="Add Admin": await upd.message.reply_text("👤 Send user ID:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()); return ADD_ADMIN_INPUT
    if text=="Remove Admin":
        admins = [u for u in ADMIN_USERS if u!=OWNER_USER_ID]
        if not admins: await upd.message.reply_text("No admins.", reply_markup=admin_set_kb()); return ADMIN_SET_MENU
        await upd.message.reply_text("Select:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Admin: {u}",callback_data=f"remove_admin_{u}")] for u in admins])); return ADMIN_SET_MENU
    if text=="🔙 Back": await upd.message.reply_text("⚙️ Admin Panel", parse_mode="Markdown", reply_markup=admin_kb(uid)); return ADMIN_MENU
    return ADMIN_SET_MENU

async def on_add_admin(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    try: nid = int(upd.message.text.strip())
    except: await upd.message.reply_text("❌ Invalid ID.", reply_markup=admin_set_kb()); return ADMIN_SET_MENU
    ADMIN_USERS.add(nid); _save_admins(ADMIN_USERS); await upd.message.reply_text(f"✅ User {nid} added.", parse_mode="Markdown", reply_markup=admin_set_kb()); return ADMIN_SET_MENU

async def on_2fa(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    res = gen_2fa(upd.message.text.strip())
    if not res["success"]: await upd.message.reply_text(f"❌ {res['message']}", parse_mode="Markdown", reply_markup=main_kb(uid)); return MAIN_MENU
    code = res["code"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))]]) if COPY_SUPPORTED else InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", callback_data=f"copy_otp_{code}")]])
    sent = await upd.message.reply_text(f"🔐 *2FA Code Generated*\n\n🔢 Code: `{code}`\n⏳ Expires in: 30 seconds", parse_mode="Markdown", reply_markup=kb)
    old = user_sessions.get(uid,{}).get("2fa_countdown_task")
    if old and not old.done(): old.cancel()
    user_sessions[uid]["2fa_countdown_task"] = asyncio.create_task(_countdown_2fa(sent, code))
    await upd.message.reply_text("🔽", reply_markup=main_kb(uid)); return MAIN_MENU

async def _countdown_2fa(msg, code: str):
    for rem in range(30,0,-1):
        txt = f"🔐 *2FA Code Generated*\n\n🔢 Code: `{code}`\n⏳ Expires in: {rem} seconds\n\n📌 This code refreshes every 30 seconds."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))]]) if COPY_SUPPORTED else InlineKeyboardMarkup([[InlineKeyboardButton(f"Copy Code: {code}", callback_data=f"copy_otp_{code}")]])
        try: await msg.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e): break
        except: break
        await asyncio.sleep(1)

# ── Callback Query Handlers ──
async def cb_change_number(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; uid = q.from_user.id
    now = time.time()
    last = user_sessions.get(uid,{}).get("last_change_time",0)
    if CHANGE_NUMBER_DELAY>0 and now-last < CHANGE_NUMBER_DELAY:
        await q.answer(f"⏳ Wait {int(CHANGE_NUMBER_DELAY-(now-last))}s", show_alert=True); return
    await q.answer()
    rs = user_sessions.get(uid,{}).get("last_range"); site = user_sessions.get(uid,{}).get("site")
    if not rs or not site: await q.message.reply_text("❌ No previous range/site.", reply_markup=main_kb(uid)); return
    stop_monitor(uid); user_sessions[uid]["last_change_time"]=now
    await q.message.reply_text("🔄 Fetching new number..."); await asyncio.sleep(CHANGE_NUMBER_DELAY)
    r = await browser.fetch_number(rs, site, uid)
    if r: await deliver_number(ctx.application, uid, r, site)
    else: await q.message.reply_text("❌ No number found.", reply_markup=main_kb(uid))

async def cb_copy(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer(); d=q.data
    if d.startswith("copy_otp_"): await q.message.reply_text(f"🔑 *OTP:*\n`{d[9:]}`", parse_mode="Markdown")
    elif d.startswith("copy_num_"): await q.message.reply_text(f"📋 *Number:*\n`+{d[9:]}`", parse_mode="Markdown")

async def cb_verify(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    try:
        m = await ctx.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=q.from_user.id)
        if m.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            await q.edit_message_text("✅ Verified! Use /start to begin."); return
    except: pass
    await q.answer("Not a member.", show_alert=True)

async def cb_remove_admin(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    q = upd.callback_query; await q.answer(); d = q.data
    if not d.startswith("remove_admin_"): return
    aid = int(d[13:])
    if aid in ADMIN_USERS and aid!=OWNER_USER_ID:
        ADMIN_USERS.remove(aid); _save_admins(ADMIN_USERS)
        await q.answer(f"Admin {aid} removed.", show_alert=True)
        admins = [u for u in ADMIN_USERS if u!=OWNER_USER_ID]
        if admins: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Admin: {u}",callback_data=f"remove_admin_{u}")] for u in admins]))
        else: await q.edit_message_text("No more admins.")
    else: await q.answer("Cannot remove.", show_alert=True)

async def cb_gender(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    g = "male" if "male" in q.data else "female"
    id_ = gen_identity(g)
    em = "👨" if id_["gender"]=="male" else "👩"
    txt = f"{em} *Generated Identity*\n\n👤 Name: `{id_['name']}`\n🆔 Username: `{id_['username']}`\n🔑 Password: `{id_['password']}`\n\n📅 Password ends with today's date."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Name",copy_text=CopyTextButton(text=id_['name'])),InlineKeyboardButton("📋 Copy User",copy_text=CopyTextButton(text=id_['username']))],
        [InlineKeyboardButton("📋 Copy Pass",copy_text=CopyTextButton(text=id_['password']),style="success")],
        [InlineKeyboardButton("🔄 Change Details",callback_data=f"change_fake_details_{id_['gender']}")]
    ]) if COPY_SUPPORTED else InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Name",callback_data=f"copy_name_{id_['name']}"),InlineKeyboardButton("📋 Copy User",callback_data=f"copy_username_{id_['username']}")],
        [InlineKeyboardButton("📋 Copy Pass",callback_data=f"copy_password_{id_['password']}",style="success")],
        [InlineKeyboardButton("🔄 Change Details",callback_data=f"change_fake_details_{id_['gender']}")]
    ])
    await q.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)

async def cb_change_fake(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    g = "male" if "male" in q.data else "female"
    id_ = gen_identity(g)
    em = "👨" if id_["gender"]=="male" else "👩"
    txt = f"{em} *Generated Identity*\n\n👤 Name: `{id_['name']}`\n🆔 Username: `{id_['username']}`\n🔑 Password: `{id_['password']}`\n\n📅 Password ends with today's date."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Name",copy_text=CopyTextButton(text=id_['name'])),InlineKeyboardButton("📋 Copy User",copy_text=CopyTextButton(text=id_['username']))],
        [InlineKeyboardButton("📋 Copy Pass",copy_text=CopyTextButton(text=id_['password']),style="success")],
        [InlineKeyboardButton("🔄 Change Details",callback_data=f"change_fake_details_{id_['gender']}")]
    ]) if COPY_SUPPORTED else InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Name",callback_data=f"copy_name_{id_['name']}"),InlineKeyboardButton("📋 Copy User",callback_data=f"copy_username_{id_['username']}")],
        [InlineKeyboardButton("📋 Copy Pass",callback_data=f"copy_password_{id_['password']}",style="success")],
        [InlineKeyboardButton("🔄 Change Details",callback_data=f"change_fake_details_{id_['gender']}")]
    ])
    try: await q.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e): raise

async def cb_copy_id(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer(); d = q.data
    prefix_map = {"copy_name_":"👤 Name","copy_username_":"🆔 Username","copy_password_":"🔑 Password"}
    for pre, label in prefix_map.items():
        if d.startswith(pre): await q.message.reply_text(f"{label}:\n`{d[len(pre):]}`", parse_mode="Markdown"); return

async def cb_set_wallet(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    await q.message.reply_text("📱 *Select wallet type:*", parse_mode="Markdown", reply_markup=wallet_kb())

async def cb_wallet_sel(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    w = q.data.split('_')[1]; ctx.user_data['wallet_type']=w; ctx.user_data['awaiting_wallet_number']=True
    names = {"bkash":"Bkash","rocket":"Rocket","binance":"Binance"}
    await q.message.reply_text(f"📲 Send your *{names[w]}* number:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def cb_withdraw(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    await q.message.reply_text("💸 *Select method:*", parse_mode="Markdown", reply_markup=wd_method_kb())

async def cb_wd_method(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    method = q.data.split('_')[2]; uid = q.from_user.id
    if method=='mobile':
        ctx.user_data['withdraw_method']=method; ctx.user_data['awaiting_withdraw_account']=True
        await q.message.reply_text("📱 Enter Mobile number (01xxxxxxxxx):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()); return
    wallets = get_wallet(uid); stored = wallets.get(method)
    if stored and stored.strip():
        ctx.user_data['withdraw_method']=method; ctx.user_data['withdraw_account']=stored.strip(); ctx.user_data['awaiting_withdraw_amount']=True
        bal = get_balance(uid); md = {"bkash":"Bkash","rocket":"Rocket","binance":"Binance"}[method]
        await q.message.reply_text(f"💰 Balance: {bal:.2f} BDT / ${bal/EXCHANGE_RATE:.4f}\n💳 Min: {MIN_WITHDRAW_BDT:.1f} BDT\n🏦 Saved {md}: `{stored}`\n\nEnter amount (BDT):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    else:
        ctx.user_data['withdraw_method']=method; ctx.user_data['awaiting_withdraw_account']=True
        names = {"bkash":"Bkash","rocket":"Rocket","binance":"Binance"}
        await q.message.reply_text(f"📱 Enter *{names[method]}* number:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def cb_complete_wd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer(); d = q.data
    if not d.startswith("complete_withdrawal_"): return
    wid = int(d[len("complete_withdrawal_"):])
    if not approve_wd(wid): await q.answer("Not found.", show_alert=True); return
    w = sqlite3.connect(DB_FILE, row_factory=sqlite3.Row).execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if w:
        md = {"bkash":"Bkash","rocket":"Rocket","binance":"Binance","mobile":"Mobile Recharge"}.get(w['method'],w['method'])
        try: await ctx.bot.send_message(w['user_id'], f"🎉 *Withdrawal Approved*\n\n💵 Amount: {w['amount']:.2f} BDT\n🏦 Method: {md}\n📞 Number: {w['account']}\n✅ Status: Complete", parse_mode="Markdown")
        except: pass
    await q.answer("Approved.", show_alert=True); await show_pending(q.message, ctx)

async def cb_login_site(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    site = q.data.split('_')[2]; ctx.user_data['login_site']=site; ctx.user_data['awaiting_login_email']=True
    await q.message.reply_text(f"📧 Enter your *{SITES[site]['name']}* email:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def cb_accts(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    if q.data=="accounts_login": await q.message.reply_text("🔐 *Select site:*", parse_mode="Markdown", reply_markup=login_site_kb())
    elif q.data=="accounts_logout": await q.message.reply_text("🚪 *Select site:*", parse_mode="Markdown", reply_markup=logout_site_kb())

async def cb_logout(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer(); d = q.data
    if not d.startswith("logout_site_"): return
    site = d[12:]; uid = q.from_user.id
    remove_creds(uid, site)
    await browser.cleanup_page(site, uid)
    await q.message.reply_text(f"✅ Logged out from {SITES[site]['name']}.", reply_markup=main_kb(uid))

async def cb_fake_send(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer(); parts = q.data.split("_",3)
    if len(parts)!=4: return
    pf = parts[2]; idx = int(parts[3]); base = "fb" if pf.startswith("fb") else "ig"
    cfgs = FAKE_OTP_CONFIG.get(base,[])
    if 0<=idx<len(cfgs):
        ok = await send_fake(ctx.application, pf, cfgs[idx])
        nt = f"✅ Sent {pf.upper()} from {cfgs[idx]['country_name']}." if ok else f"❌ Failed."
        if q.message.text!=nt:
            try: await q.edit_message_text(nt)
            except: pass
    else: await q.answer("Invalid.", show_alert=True)

async def cb_set_fake(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    pl = "fb" if "fb" in q.data else "ig"
    ctx.user_data['fake_otp_platform']=pl; ctx.user_data['awaiting_fake_country_name']=True
    await q.message.reply_text(f"🌍 Enter *country name* for {pl.upper()}:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

async def show_pending(msg, ctx):
    pending = get_pending()
    if not pending: await msg.reply_text("📋 No pending withdrawals."); return
    lines = ["📋 *Pending Withdrawals:*\n"] + [f"🔹 ID: {w['id']} | User: `{w['user_id']}`\n   💵 {w['amount']:.1f} BDT via {w['method']} ({w['account']})\n   🕒 {w['created_at']}\n" for w in pending]
    await msg.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Complete",callback_data=f"complete_withdrawal_{w['id']}")] for w in pending]))

async def show_approved(msg):
    approved = get_approved()
    if not approved: await msg.reply_text("✅ No approved withdrawals yet."); return
    lines = ["✅ *Approved Withdrawals:*\n"] + [f"🔹 ID: {w['id']} | User: `{w['user_id']}`\n   💵 {w['amount']:.1f} BDT via {w['method']} ({w['account']})\n   📅 {w['approved_at']}\n" for w in approved]
    await msg.reply_text("\n".join(lines), parse_mode="Markdown")

async def on_login_pw(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if not upd.message or not upd.message.text: return None
    pw = upd.message.text.strip(); site = ctx.user_data.get('login_site'); email = ctx.user_data.get('login_email')
    ctx.user_data.clear()
    if not site or not email: await upd.message.reply_text("❌ Session expired.", reply_markup=main_kb(uid)); return MAIN_MENU
    try:
        await browser.ensure_browser()
        ctx2 = await browser._browser.new_context(viewport={"width":1280,"height":800}); p = await ctx2.new_page()
        ok = await browser._login(p, site, email, pw)
        await ctx2.close()
        if ok: store_creds(uid, site, email, pw); await upd.message.reply_text(f"✅ Logged in to {SITES[site]['name']}!", reply_markup=main_kb(uid))
        else: await upd.message.reply_text("❌ Login failed.", reply_markup=main_kb(uid))
    except Exception as e: log.error(f"Login error: {e}"); await upd.message.reply_text("❌ Error.", reply_markup=main_kb(uid))
    return MAIN_MENU

async def on_error(upd: object, ctx: ContextTypes.DEFAULT_TYPE): log.error("Unhandled exception:", exc_info=ctx.error)

# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    global BOT_USERNAME
    if not BOT_TOKEN: log.critical("BOT_TOKEN not set."); sys.exit(1)
    init_db(); start_health(HEALTH_PORT)
    log.info("Waiting 5s for old container release..."); time.sleep(5)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    BOT_USERNAME = loop.run_until_complete(app.bot.get_me()).username
    log.info(f"Bot: @{BOT_USERNAME}")
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_main_menu)],
            SITE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_site_menu)],
            AWAIT_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_range)],
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_menu)],
            SET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_set_interval)],
            SET_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_set_rate)],
            SET_WITHDRAW_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_set_withdraw)],
            ADMIN_SET_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_set)],
            ADD_ADMIN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_add_admin)],
            AWAIT_2FA_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_2fa)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_login_pw)],
            BROADCAST_AWAIT: [MessageHandler(filters.ALL & ~filters.COMMAND, on_broadcast)],
        }, fallbacks=[CommandHandler("start", cmd_start)], allow_reentry=True)
    app.add_handler(conv)
    for pat, cb in [
        ("^change_number$", cb_change_number), ("^copy_", cb_copy), ("^verify_membership$", cb_verify), ("^remove_admin_", cb_remove_admin),
        ("^gender_", cb_gender), ("^change_fake_details_", cb_change_fake), ("^copy_name_|^copy_username_|^copy_password_", cb_copy_id),
        ("^profile_set_wallet$", cb_set_wallet), ("^profile_withdraw$", cb_withdraw), ("^wallet_", cb_wallet_sel),
        ("^withdraw_method_", cb_wd_method), ("^complete_withdrawal_", cb_complete_wd), ("^login_site_", cb_login_site),
        ("^accounts_login$|^accounts_logout$", cb_accts), ("^logout_site_", cb_logout), ("^fake_send_", cb_fake_send),
        ("^set_fake_details_", cb_set_fake),
    ]: app.add_handler(CallbackQueryHandler(cb, pattern=pat))
    app.add_error_handler(on_error)
    browser.start_watchdog()
    async def shutdown():
        log.info("Shutting down..."); stop_auto()
        if app.running: await app.stop(); await app.shutdown()
        await browser.shutdown(); log.info("Shutdown complete")
    def sig_handler(): asyncio.ensure_future(shutdown(), loop=loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, sig_handler)
        except NotImplementedError: signal.signal(sig, lambda s,f: sig_handler())
    try:
        log.info("Bot running..."); app.run_polling(drop_pending_updates=True)
    except (KeyboardInterrupt, SystemExit): pass
    finally: loop.run_until_complete(shutdown()); loop.close()

if __name__ == "__main__": main()
