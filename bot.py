"""
STEX SMS Telegram Bot — Full A‑Z (Robust & Fixed)
==================================================
• Forced OTP‑group membership
• Owner can manage admins (Add/Remove) via Admin Panel → Admin Set
• "Get 2FA" generates a TOTP code with a 30‑second countdown and Copy button
• After generation the main menu keyboard reappears (invisible message)
• Change‑Number respects an admin‑configurable interval (pop‑up alert if too soon)
• No timeout message for users
• Fast, isolated, clipboard‑based full SMS retrieval
• Copy Number & Copy OTP buttons (native CopyTextButton if available)
• Admin panel with configurable change‑number delay
• Per‑user isolation, serialised browser access
• Fake Details generator (Male/Female) with random identity and password
"""

import asyncio
import logging
import re
import os
import json
import time
import random
import string
from datetime import datetime, timezone
from typing import Optional, Dict, Set

# ── load .env ────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── telegram imports ─────────────────────────────────────────
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    ChatMember,
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.error import BadRequest

# CopyTextButton – available in python-telegram-bot >= 21.1
try:
    from telegram import CopyTextButton
    COPY_SUPPORTED = True
except ImportError:
    COPY_SUPPORTED = False

# ── 2FA support ─────────────────────────────────────────────
try:
    import pyotp
    TOTP_AVAILABLE = True
except ImportError:
    TOTP_AVAILABLE = False

# ── playwright ───────────────────────────────────────────────
from playwright.async_api import async_playwright, Page, BrowserContext


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN",      "")
SMS_EMAIL      = os.getenv("STEX_EMAIL",     "")
SMS_PASSWORD   = os.getenv("STEX_PASSWORD",  "")
OTP_GROUP_ID   = int(os.getenv("OTP_GROUP_ID",   "0"))
OTP_GROUP_LINK = os.getenv("OTP_GROUP_LINK", "https://t.me/your_otp_group")

OWNER_USER_ID  = 5705479420

# Admin users – loaded from .env + persisted file
_admin_users_env = os.getenv("ADMIN_USERS", "").strip()
ADMIN_USERS_FILE = "admin_users.json"

def _load_admins() -> Set[int]:
    s = set()
    if _admin_users_env:
        s.update(int(x) for x in _admin_users_env.split(",") if x.strip().isdigit())
    if os.path.exists(ADMIN_USERS_FILE):
        try:
            with open(ADMIN_USERS_FILE, "r") as f:
                saved = json.load(f)
                s.update(int(x) for x in saved if str(x).isdigit())
        except Exception:
            pass
    return s

def _save_admins(admins: Set[int]):
    with open(ADMIN_USERS_FILE, "w") as f:
        json.dump(list(admins), f)

ADMIN_USERS = _load_admins()

# Global delay for Change Number (admin configurable, default 0s)
CHANGE_NUMBER_DELAY = 0   # seconds

# ── Site definitions ─────────────────────────────────────────
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

POLL_INTERVAL   = 3     # seconds
MONITOR_TIMEOUT = 480   # 8 minutes


# ═══════════════════════════════════════════════════════════════
#  CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════
MAIN_MENU        = 0
SITE_MENU        = 1
AWAIT_RANGE      = 2
ADMIN_MENU       = 3
SET_INTERVAL     = 4
ADMIN_SET_MENU   = 5
ADD_ADMIN_INPUT  = 6
AWAIT_2FA_SECRET = 7


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("telegram._bot").setLevel(logging.WARNING)
log = logging.getLogger("smsbot")


# ═══════════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════════
user_sessions: dict[int, dict] = {}

_playwright_obj = None
_browser        = None
_browser_lock   = asyncio.Lock()
_page_lock      = asyncio.Lock()

_site_pages: Dict[str, Page] = {}

# ── Fake details data ────────────────────────────────────────
MALE_NAMES = [
    "Liam", "Noah", "Oliver", "Elijah", "James", "William", "Benjamin", "Lucas",
    "Henry", "Alexander", "Mason", "Michael", "Ethan", "Daniel", "Jacob", "Logan",
    "Jackson", "Levi", "Sebastian", "Mateo", "Jack", "Owen", "Theodore", "Aiden",
    "Samuel", "Joseph", "John", "David", "Wyatt", "Matthew", "Luke", "Asher",
    "Carter", "Julian", "Grayson", "Leo", "Jayden", "Gabriel", "Isaac", "Lincoln",
    "Anthony", "Hudson", "Dylan", "Ezra", "Thomas", "Charles", "Christopher", "Jaxon",
    "Maverick", "Josiah"
]

FEMALE_NAMES = [
    "Olivia", "Emma", "Ava", "Charlotte", "Sophia", "Amelia", "Isabella", "Mia",
    "Evelyn", "Harper", "Camila", "Gianna", "Abigail", "Luna", "Ella", "Elizabeth",
    "Sofia", "Emily", "Avery", "Mila", "Scarlett", "Eleanor", "Madison", "Layla",
    "Penelope", "Aria", "Chloe", "Grace", "Ellie", "Nora", "Hazel", "Zoey",
    "Riley", "Victoria", "Lily", "Aurora", "Violet", "Nova", "Hannah", "Emilia",
    "Zoe", "Stella", "Everly", "Isla", "Leah", "Lillian", "Addison", "Willow",
    "Lucy", "Paisley"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen",
    "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera",
    "Campbell", "Mitchell", "Carter", "Roberts"
]


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════
def _stop_monitor(uid: int):
    s = user_sessions.get(uid, {})
    task = s.get("monitor_task")
    if task and not task.done():
        task.cancel()
        log.info(f"🛑 Monitor cancelled for uid={uid}")

def _is_owner(uid: int) -> bool:
    return uid == OWNER_USER_ID

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_USERS or _is_owner(uid)

def _mask_number(number: str) -> str:
    if len(number) <= 6:
        return number
    if number.startswith("+"):
        num = number[1:]
        prefix = "+"
    else:
        num = number
        prefix = "+"
    if len(num) <= 6:
        return prefix + num
    first = num[:3]
    last  = num[-3:]
    middle_count = len(num) - 6
    masked = f"{prefix}{first}{'*' * middle_count}{last}"
    return masked

def _extract_otp(message: str) -> Optional[str]:
    match = re.search(r"\b\d{1,3}(?:\s?\d{1,3})+\b", message)
    if match:
        digits = re.sub(r"\D", "", match.group())
        if 4 <= len(digits) <= 8:
            return digits
    no_spaces = re.sub(r"\s+", "", message)
    fallback = re.findall(r"\b\d{4,8}\b", no_spaces)
    return fallback[0] if fallback else None

def _clean_full_msg(full_msg: str) -> str:
    prefixes = [r"^Facebook:\s*", r"^Instagram:\s*", r"^WhatsApp:\s*"]
    for p in prefixes:
        full_msg = re.sub(p, "", full_msg, count=1)
    return full_msg.strip()

def generate_2fa_code(secret_key: str) -> dict:
    """Generate a TOTP code. Returns {success, code} or {success: False, message: ...}."""
    if not TOTP_AVAILABLE:
        return {"success": False, "message": "pyotp library not installed."}
    try:
        clean_secret = ''.join(secret_key.split()).upper()
        totp = pyotp.TOTP(clean_secret)
        code = totp.now()
        return {"success": True, "code": code}
    except Exception:
        return {"success": False, "message": "Invalid Secret Key"}

def generate_identity(gender: str) -> dict:
    """Generate a random identity (name, username, password) based on gender."""
    today_day = datetime.now().day  # e.g., 16
    day_str = f"{today_day:02d}"

    # Choose random names
    if gender.lower() == "male":
        first_name = random.choice(MALE_NAMES)
    else:
        first_name = random.choice(FEMALE_NAMES)
    last_name = random.choice(LAST_NAMES)
    full_name = f"{first_name} {last_name}"

    # Username: firstnamelastname + random 2-3 digits
    digits = ''.join(random.choices(string.digits, k=random.randint(2,3)))
    username = f"{first_name.lower()}{last_name.lower()}{digits}"

    # Password: base length 8-10 random chars (upper+lower+digit+special) + today's day
    base_len = random.randint(8, 10)
    chars = string.ascii_letters + string.digits + "#$&"
    # ensure at least one of each required type
    base = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("#$&"),
    ]
    for _ in range(base_len - 4):
        base.append(random.choice(chars))
    random.shuffle(base)
    base = ''.join(base)
    password = base + day_str

    return {
        "name": full_name,
        "username": username,
        "password": password,
        "gender": gender
    }


# ═══════════════════════════════════════════════════════════════
#  FORCED GROUP MEMBERSHIP
# ═══════════════════════════════════════════════════════════════
async def _check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=user.id)
        if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
    except BadRequest:
        pass

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Join Channel", url=OTP_GROUP_LINK),
            InlineKeyboardButton("Verify", callback_data="verify_membership")
        ]
    ])
    await update.message.reply_text(
        "🔒 Access Restricted!\n\nPlease join our channel to use this bot.",
        reply_markup=kb
    )
    return False


async def verify_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    try:
        member = await context.bot.get_chat_member(chat_id=OTP_GROUP_ID, user_id=user.id)
        if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await query.edit_message_text("✅ Verified! You can now use the bot.\nUse /start to begin.")
            return
    except BadRequest:
        pass
    await query.answer("You are not yet a member of the channel.", show_alert=True)


# ═══════════════════════════════════════════════════════════════
#  BROWSER / SCRAPER (optimised)
# ═══════════════════════════════════════════════════════════════
async def _ensure_playwright():
    global _playwright_obj, _browser
    async with _browser_lock:
        if _playwright_obj is None or (_browser is not None and not _browser.is_connected()):
            if _playwright_obj is not None:
                await _playwright_obj.stop()
            _playwright_obj = await async_playwright().start()
            _browser = await _playwright_obj.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled"
                ],
            )


async def _do_login_for_site(page: Page, site: str) -> bool:
    site_cfg = SITES[site]
    for attempt in range(1, 4):
        log.info(f"🔐 Logging in to {site_cfg['name']} (attempt {attempt}/3)...")
        await page.goto(site_cfg["login_url"], wait_until="networkidle", timeout=30000)
        await page.fill("input[type='email']",    SMS_EMAIL)
        await page.fill("input[type='password']", SMS_PASSWORD)
        await page.click("button[type='submit']")

        try:
            await page.wait_for_url(
                lambda url: "auth" not in url and "login" not in url,
                timeout=60000
            )
            log.info(f"✅ {site_cfg['name']} login successful (URL changed).")
            return True
        except Exception:
            if "/dialer/" in page.url or "dialer" in page.url:
                log.info(f"✅ {site_cfg['name']} already on dialer page – logged in.")
                return True
            try:
                await page.wait_for_selector("table.gn-tbl, input.gn-range-input", timeout=5000)
                log.info(f"✅ {site_cfg['name']} login confirmed by element presence.")
                return True
            except Exception:
                pass
        log.warning(f"⚠️ Login not confirmed for {site_cfg['name']}.")
        if attempt < 3:
            await asyncio.sleep(2)
    log.error(f"❌ All login attempts failed for {site_cfg['name']}.")
    return False


async def _ensure_page_logged_in(site: str) -> Page:
    await _ensure_playwright()
    page = _site_pages.get(site)
    if page is None:
        context = await _browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        await context.grant_permissions(["clipboard-read"])
        page = await context.new_page()
        _site_pages[site] = page
        await _do_login_for_site(page, site)
    else:
        if page.url and ("login" in page.url or "auth" in page.url):
            log.info(f"🔄 Session for {SITES[site]['name']} looks expired, re‑logging…")
            await _do_login_for_site(page, site)
        else:
            try:
                await page.wait_for_selector("table.gn-tbl, input.gn-range-input", timeout=3000)
            except Exception:
                log.info(f"🔄 Session for {SITES[site]['name']} may be stale, re‑logging…")
                await _do_login_for_site(page, site)
    await page.goto(SITES[site]["dialer_url"], wait_until="domcontentloaded", timeout=20000)
    return page


async def _ensure_dialer(page: Page, site: str):
    dialer_url = SITES[site]["dialer_url"]
    if "/dialer/" not in page.url:
        log.info(f"📍 Navigating to {SITES[site]['name']} dialer page...")
        await page.goto(dialer_url, wait_until="domcontentloaded", timeout=20000)
    try:
        await page.wait_for_selector("table.gn-tbl tbody tr, input.gn-range-input", timeout=15000)
    except Exception:
        log.warning("⚠️ Number table / range input not visible after waiting.")


async def fetch_number(range_str: str, site: str) -> Optional[dict]:
    page = await _ensure_page_logged_in(site)
    async with _page_lock:
        try:
            await _ensure_dialer(page, site)

            first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
            old_number = None
            if await first_row.count() > 0:
                old_number = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")

            inp = page.locator("input.gn-range-input")
            await inp.wait_for(state="visible", timeout=15000)
            await inp.fill("")
            await inp.type(range_str, delay=25)
            await asyncio.sleep(0.15)

            get_btn = page.locator("button.btn.btn-primary:has-text('Get Number')")
            await get_btn.click()
            log.info("🖱️ Clicked 'Get Number', waiting for new number...")

            try:
                await page.wait_for_function(
                    """(old) => {
                        const rows = document.querySelectorAll('table.gn-tbl tbody tr');
                        for (let i = 0; i < rows.length; i++) {
                            const numEl = rows[i].querySelector('.gn-num');
                            if (numEl) {
                                const current = numEl.textContent.trim().replace(/^\\+/, '');
                                if (current !== old) return true;
                                break;
                            }
                        }
                        return false;
                    }""",
                    arg=old_number or "",
                    timeout=15000
                )
            except Exception:
                log.error("❌ New number did not appear in time.")
                return None

            first_row = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num")).first
            if not await first_row.count():
                return None

            number = (await first_row.locator(".gn-num").first.inner_text()).strip().lstrip("+")
            country_el = first_row.locator(".gn-meta").first
            country = (await country_el.inner_text()).strip() if await country_el.count() else "Unknown"
            operator_el = first_row.locator(".gn-meta-sub")
            operator = (await operator_el.first.inner_text()).strip() if await operator_el.count() else "Unknown"
            operator = re.sub(r"\s+", " ", operator).strip()

            if not number:
                return None

            log.info(f"📞 Got number: +{number} | {country} | {operator}")
            return {"number": number, "country": country, "operator": operator}

        except Exception as e:
            log.error(f"❌ fetch_number error: {e}")
            return None


async def poll_otp(number: str, site: str) -> Optional[str]:
    page = await _ensure_page_logged_in(site)
    async with _page_lock:
        try:
            await _ensure_dialer(page, site)
            rows = page.locator("table.gn-tbl tbody tr").filter(has=page.locator(".gn-num"))
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)
                num_el = row.locator(".gn-num").first
                if await num_el.count() == 0:
                    continue
                row_num = (await num_el.inner_text()).strip().lstrip("+")
                if row_num != number:
                    continue

                status_el = row.locator(".gn-status-pill")
                if await status_el.count() == 0:
                    continue
                status = (await status_el.first.inner_text()).strip().lower()
                if status != "success":
                    continue

                copy_btn = row.locator("button.gn-otp-copy")
                if await copy_btn.count() == 0:
                    title = await row.locator("button.gn-otp-copy").first.get_attribute("title") or ""
                    if ":" in title:
                        return title.split(":", 1)[1].strip()
                    return None

                await copy_btn.first.click()
                await asyncio.sleep(0.3)
                try:
                    clipboard_text = await page.evaluate("navigator.clipboard.readText()")
                    if clipboard_text:
                        return clipboard_text
                except Exception as e:
                    log.warning(f"⚠️ clipboard read failed: {e}")

                title = await copy_btn.first.get_attribute("title") or ""
                if ":" in title:
                    return title.split(":", 1)[1].strip()
                return None

            return None

        except Exception as e:
            log.error(f"❌ poll_otp error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════
#  MONITOR TASK
# ═══════════════════════════════════════════════════════════════
async def monitor_number(
    app: Application,
    uid: int,
    number: str,
    country: str,
    operator: str,
    site: str,
):
    loop     = asyncio.get_event_loop()
    deadline = loop.time() + MONITOR_TIMEOUT
    bot_user = await app.bot.get_me()

    log.info(f"👀 Monitoring OTP for uid={uid} (+{number}) on {SITES[site]['name']}")

    while loop.time() < deadline:
        s = user_sessions.get(uid)
        if not s or s.get("number") != number:
            log.info(f"⏹️ Monitor stopped (reassigned) for uid={uid}")
            return

        full_msg = await poll_otp(number, site)

        if full_msg:
            clean_otp = _extract_otp(full_msg)
            if not clean_otp:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if s.get("last_otp") == clean_otp:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            s["last_otp"] = clean_otp
            log.info(f"📩 OTP received for uid={uid}: {clean_otp}")

            clean_msg = _clean_full_msg(full_msg)

            # ── DM to user with Copy OTP button ──────────────
            safe_msg = escape_markdown(clean_msg, version=1)
            user_text = (
                f"📩 {country} Message Received!\n\n"
                f"📞 Number: `+{number}`\n"
                f"🔑 OTP Code: `{clean_otp}`\n\n"
                f"💬 Message: {safe_msg}"
            )

            if COPY_SUPPORTED:
                user_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"OTP: {clean_otp}",
                        copy_text=CopyTextButton(text=clean_otp)
                    )
                ]])
            else:
                user_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"OTP: {clean_otp}",
                        callback_data=f"copy_otp_{clean_otp}"
                    )
                ]])

            try:
                await app.bot.send_message(
                    chat_id=uid,
                    text=user_text,
                    parse_mode="Markdown",
                    reply_markup=user_kb,
                )
            except Exception as e:
                log.error(f"❌ DM OTP error uid={uid}: {e}")

            # ── Group message (plain text) ──────────────────
            masked_num = _mask_number(f"+{number}")
            group_text = (
                f"📨 {country} OTP Received\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📞 Number: {masked_num}\n"
                f"🔑 OTP: {clean_otp}\n\n"
                f"💬 {clean_msg}\n"
                f"━━━━━━━━━━━━━━━━"
            )
            bot_link = f"https://t.me/{bot_user.username}?start=start"
            group_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🤖 Get Number", url=bot_link),
            ]])
            try:
                await app.bot.send_message(
                    chat_id=OTP_GROUP_ID,
                    text=group_text,
                    reply_markup=group_kb,
                )
                log.info(f"📢 OTP forwarded to group for +{number}")
            except Exception as e:
                log.error(f"❌ Group OTP error: {e}")

        await asyncio.sleep(POLL_INTERVAL)

    # No timeout message


# ═══════════════════════════════════════════════════════════════
#  CHANGE NUMBER CALLBACK (with interval enforcement)
# ═══════════════════════════════════════════════════════════════
async def cb_change_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id

    # Check cooldown
    now = time.time()
    last_change = user_sessions.get(uid, {}).get("last_change_time", 0)
    elapsed = now - last_change
    if CHANGE_NUMBER_DELAY > 0 and elapsed < CHANGE_NUMBER_DELAY:
        remaining = int(CHANGE_NUMBER_DELAY - elapsed)
        await query.answer(f"⏳ Please wait {remaining}s before changing again.", show_alert=True)
        return

    await query.answer()

    range_str = user_sessions.get(uid, {}).get("last_range")
    site = user_sessions.get(uid, {}).get("site")
    if not range_str or not site:
        await query.message.reply_text(
            "❌ No previous range or site found. Use 📡 Get Number first.",
            reply_markup=main_menu_kb(uid),
        )
        return

    _stop_monitor(uid)
    # Update last change time
    user_sessions[uid]["last_change_time"] = now

    await query.message.reply_text("🔄 Fetching new number in the same range...")
    await asyncio.sleep(CHANGE_NUMBER_DELAY)

    result = await fetch_number(range_str, site)

    if not result:
        await query.message.reply_text(
            "❌ No number found for that range.\nTry a different range or try again.",
            reply_markup=main_menu_kb(uid),
        )
        return

    await _deliver_number(ctx.application, uid, result, site, edit_msg=None)


# ═══════════════════════════════════════════════════════════════
#  CALLBACK HANDLER FOR COPY FALLBACK
# ═══════════════════════════════════════════════════════════════
async def cb_copy_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("copy_otp_"):
        otp_val = data[len("copy_otp_"):]
        await query.message.reply_text(
            f"🔑 *OTP:*\n`{otp_val}`\n_(tap to copy)_",
            parse_mode="Markdown"
        )
    elif data.startswith("copy_num_"):
        num_val = "+" + data[len("copy_num_"):]
        await query.message.reply_text(
            f"📋 *Number:*\n`{num_val}`\n_(tap to copy)_",
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════════
#  FAKE DETAILS CALLBACKS
# ═══════════════════════════════════════════════════════════════
async def cb_gender_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    gender = "male" if "male" in data else "female"

    identity = generate_identity(gender)
    await _send_identity_message(query.message, identity, edit=False)

async def cb_change_fake_details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # "change_fake_details_male" or "change_fake_details_female"
    gender = "male" if "male" in data else "female"

    identity = generate_identity(gender)
    await _send_identity_message(query.message, identity, edit=True)

async def _send_identity_message(message, identity: dict, edit: bool = False):
    gender_emoji = "👨" if identity["gender"] == "male" else "👩"
    text = (
        f"{gender_emoji} *Generated Identity*\n\n"
        f"👤 *Name:* `{identity['name']}`\n"
        f"🆔 *Username:* `{identity['username']}`\n"
        f"🔑 *Password:* `{identity['password']}`\n\n"
        f"📅 Password ends with today's date."
    )

    # Build inline keyboard with copy buttons
    if COPY_SUPPORTED:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Copy Name", copy_text=CopyTextButton(text=identity['name'])),
                InlineKeyboardButton("📋 Copy User", copy_text=CopyTextButton(text=identity['username'])),
            ],
            [
                InlineKeyboardButton("📋 Copy Pass", copy_text=CopyTextButton(text=identity['password'])),
            ],
            [
                InlineKeyboardButton("🔄 Change Details", callback_data=f"change_fake_details_{identity['gender']}")
            ]
        ])
    else:
        # Fallback to callback-based copy for each field (we'll use the existing cb_copy_fallback but with custom keys)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Copy Name", callback_data=f"copy_name_{identity['name']}"),
                InlineKeyboardButton("📋 Copy User", callback_data=f"copy_username_{identity['username']}"),
            ],
            [
                InlineKeyboardButton("📋 Copy Pass", callback_data=f"copy_password_{identity['password']}"),
            ],
            [
                InlineKeyboardButton("🔄 Change Details", callback_data=f"change_fake_details_{identity['gender']}")
            ]
        ])

    if edit:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except BadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                raise
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

# Extended copy fallback to handle the new callback keys
async def cb_copy_identity_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("copy_name_"):
        name = data[len("copy_name_"):]
        await query.message.reply_text(f"👤 *Name:*\n`{name}`", parse_mode="Markdown")
    elif data.startswith("copy_username_"):
        username = data[len("copy_username_"):]
        await query.message.reply_text(f"🆔 *Username:*\n`{username}`", parse_mode="Markdown")
    elif data.startswith("copy_password_"):
        password = data[len("copy_password_"):]
        await query.message.reply_text(f"🔑 *Password:*\n`{password}`", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  ADMIN REMOVAL CALLBACK (inline buttons)
# ═══════════════════════════════════════════════════════════════
async def cb_remove_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if not data.startswith("remove_admin_"):
        return

    admin_id_str = data[len("remove_admin_"):]
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        return

    if admin_id in ADMIN_USERS and admin_id != OWNER_USER_ID:
        ADMIN_USERS.remove(admin_id)
        _save_admins(ADMIN_USERS)
        await query.answer(f"Admin {admin_id} removed.", show_alert=True)

        current_admins = [uid for uid in ADMIN_USERS if uid != OWNER_USER_ID]
        if current_admins:
            buttons = [[InlineKeyboardButton(f"Admin: {uid}", callback_data=f"remove_admin_{uid}")] for uid in current_admins]
            new_markup = InlineKeyboardMarkup(buttons)
            try:
                await query.edit_message_reply_markup(reply_markup=new_markup)
            except BadRequest:
                pass
        else:
            await query.edit_message_text("No more admins to remove.")
    else:
        await query.answer("Cannot remove this admin.", show_alert=True)


# ═══════════════════════════════════════════════════════════════
#  2FA COUNTDOWN TASK  (always starts at 30 seconds)
# ═══════════════════════════════════════════════════════════════
async def _update_2fa_countdown(message, code: str):
    """Edit the 2FA message every second, counting down from 30 to 0."""
    remaining = 30
    while remaining > 0:
        text = (
            f"🔐 *2FA Code Generated*\n\n"
            f"🔢 Code: `{code}`\n"
            f"⏳ Expires in: {remaining} seconds\n\n"
            f"📌 This code refreshes every 30 seconds."
        )
        if COPY_SUPPORTED:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Copy Code: {code}", callback_data=f"copy_otp_{code}")
            ]])

        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except BadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                break
        except Exception:
            break

        await asyncio.sleep(1)
        remaining -= 1


# ═══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════
def main_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("📡 Get Number"), KeyboardButton("🔑 Get 2FA")],
        [KeyboardButton("📋 Fake Details")]
    ]
    if _is_admin(uid):
        buttons.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)

def site_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔵 Stexsms"), KeyboardButton("🔵 Voltxsms")],
            [KeyboardButton("🔙 Back")],
        ],
        resize_keyboard=True,
    )

def admin_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton("Interval")]]
    if _is_owner(uid):
        buttons.append([KeyboardButton("Admin Set")])
    buttons.append([KeyboardButton("🔙 Back")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def admin_set_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Add Admin"), KeyboardButton("Remove Admin")],
            [KeyboardButton("🔙 Back")]
        ],
        resize_keyboard=True,
    )

def number_ready_kb(number: str) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("👥 OTP Group", url=OTP_GROUP_LINK),
        InlineKeyboardButton("🔄 Change Number", callback_data="change_number"),
    ]
    if COPY_SUPPORTED:
        row2 = [
            InlineKeyboardButton(
                "📋 Copy Number",
                copy_text=CopyTextButton(text=f"+{number}")
            )
        ]
    else:
        row2 = [
            InlineKeyboardButton(
                "📋 Copy Number",
                callback_data=f"copy_num_{number}"
            )
        ]
    return InlineKeyboardMarkup([row1, row2])


# ═══════════════════════════════════════════════════════════════
#  SEND NUMBER MESSAGE
# ═══════════════════════════════════════════════════════════════
async def _deliver_number(app_or_bot, uid: int, result: dict, site: str, edit_msg=None):
    number   = result["number"]
    country  = result["country"]
    operator = result["operator"]

    user_sessions[uid].update({
        "number":   number,
        "country":  country,
        "operator": operator,
        "last_otp": None,
        "site":     site,
    })

    text = (
        f"✅ *Number Ready!*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏢 Provider: `{operator}`\n"
        f"🌍 Country: `{country}`\n"
        f"📞 Number: `+{number}`\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏳ Waiting for OTP..."
    )

    kb = number_ready_kb(number)

    if edit_msg:
        try:
            sent = await edit_msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            sent = await app_or_bot.bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    else:
        bot  = app_or_bot if hasattr(app_or_bot, "send_message") else app_or_bot.bot
        sent = await bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)

    user_sessions[uid]["msg_id"] = sent.message_id

    app = app_or_bot if hasattr(app_or_bot, "bot") else None
    if app:
        _stop_monitor(uid)
        task = asyncio.create_task(
            monitor_number(app, uid, number, country, operator, site)
        )
        user_sessions[uid]["monitor_task"] = task


# ═══════════════════════════════════════════════════════════════
#  CONVERSATION HANDLERS
# ═══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await _check_membership(update, ctx):
        return ConversationHandler.END

    if uid not in user_sessions:
        user_sessions[uid] = {}

    await update.message.reply_text(
        f"👋 Welcome *{update.effective_user.first_name}*!\n\n"
        "Tap *📡 Get Number* to begin.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(uid),
    )
    return MAIN_MENU


async def main_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await _check_membership(update, ctx):
        return ConversationHandler.END

    text = update.message.text

    if text == "📡 Get Number":
        await update.message.reply_text(
            "🌐 *Select a provider:*",
            parse_mode="Markdown",
            reply_markup=site_menu_kb(),
        )
        return SITE_MENU
    elif text == "🔑 Get 2FA":
        await update.message.reply_text(
            "📲 Paste your 2FA Secret Key",
            reply_markup=ReplyKeyboardRemove()
        )
        return AWAIT_2FA_SECRET
    elif text == "📋 Fake Details":
        # Show gender selection inline keyboard
        gender_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚹 Male", callback_data="gender_male"),
                InlineKeyboardButton("🚺 Female", callback_data="gender_female")
            ]
        ])
        await update.message.reply_text(
            "👤 *Select gender for identity:*",
            parse_mode="Markdown",
            reply_markup=gender_kb
        )
        return MAIN_MENU
    elif text == "⚙️ Admin Panel" and _is_admin(uid):
        await update.message.reply_text(
            f"⚙️ *Admin Panel*\nCurrent Change‑Number delay: `{CHANGE_NUMBER_DELAY}` seconds",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(uid)
        )
        return ADMIN_MENU
    return MAIN_MENU


# ── 2FA handler (FIXED) ──────────────────────────────────────
async def handle_2fa_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    secret = update.message.text.strip()
    result = generate_2fa_code(secret)

    if result["success"]:
        code = result["code"]

        text = (
            f"🔐 *2FA Code Generated*\n\n"
            f"🔢 Code: `{code}`\n"
            f"⏳ Expires in: 30 seconds\n\n"
            f"📌 This code refreshes every 30 seconds."
        )

        if COPY_SUPPORTED:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Copy Code: {code}", copy_text=CopyTextButton(text=code))
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Copy Code: {code}", callback_data=f"copy_otp_{code}")
            ]])

        sent_msg = await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=kb
        )

        # Cancel any existing countdown task for this user
        old_task = user_sessions.get(uid, {}).get("2fa_countdown_task")
        if old_task and not old_task.done():
            old_task.cancel()

        # Start the countdown from 30 seconds
        task = asyncio.create_task(_update_2fa_countdown(sent_msg, code))
        user_sessions[uid]["2fa_countdown_task"] = task

        # Restore main menu keyboard using a minimal visible character (prevents "Text must be non‑empty" error)
        await update.message.reply_text(
            "🔽",   # small arrow – unobtrusive but non‑empty
            reply_markup=main_menu_kb(uid)
        )

    else:
        text = f"❌ {result['message']}"
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=main_menu_kb(uid)
        )

    return MAIN_MENU


# ── Admin menu handlers ──────────────────────────────────────
async def admin_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if text == "Interval":
        await update.message.reply_text(
            "Enter new delay in seconds (1‑60):",
            reply_markup=ReplyKeyboardRemove()
        )
        return SET_INTERVAL
    elif text == "Admin Set" and _is_owner(uid):
        await update.message.reply_text(
            "⚙️ *Admin Management*",
            parse_mode="Markdown",
            reply_markup=admin_set_menu_kb()
        )
        return ADMIN_SET_MENU
    elif text == "🔙 Back":
        await update.message.reply_text(
            "🏠 Main menu:",
            reply_markup=main_menu_kb(uid)
        )
        return MAIN_MENU
    return ADMIN_MENU


# ── Admin Set sub‑menu handlers ──────────────────────────────
async def admin_set_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if text == "Add Admin":
        await update.message.reply_text(
            "👤 Send me the **user ID** of the person you want to add as admin.\n"
            "Example: `6316982441`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADD_ADMIN_INPUT
    elif text == "Remove Admin":
        admins = [u for u in ADMIN_USERS if u != OWNER_USER_ID]
        if not admins:
            await update.message.reply_text("No admins to remove.", reply_markup=admin_set_menu_kb())
            return ADMIN_SET_MENU

        buttons = [[InlineKeyboardButton(f"Admin: {uid}", callback_data=f"remove_admin_{uid}")] for uid in admins]
        reply_markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Select an admin to remove:",
            reply_markup=reply_markup
        )
        return ADMIN_SET_MENU
    elif text == "🔙 Back":
        await update.message.reply_text(
            "⚙️ *Admin Panel*",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(uid)
        )
        return ADMIN_MENU
    return ADMIN_SET_MENU


async def admin_set_add_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ADMIN_USERS
    uid = update.effective_user.id
    text = update.message.text.strip()

    try:
        new_admin_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID. Please send a numeric ID (e.g., `6316982441`).",
            parse_mode="Markdown",
            reply_markup=admin_set_menu_kb()
        )
        return ADMIN_SET_MENU

    ADMIN_USERS.add(new_admin_id)
    _save_admins(ADMIN_USERS)
    await update.message.reply_text(
        f"✅ User `{new_admin_id}` added as admin.",
        parse_mode="Markdown",
        reply_markup=admin_set_menu_kb()
    )
    return ADMIN_SET_MENU


async def admin_set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global CHANGE_NUMBER_DELAY
    uid = update.effective_user.id
    text = update.message.text.strip()
    try:
        value = int(text)
        if value < 1 or value > 60:
            raise ValueError
        CHANGE_NUMBER_DELAY = value
        await update.message.reply_text(
            f"✅ Change‑Number delay set to `{value}` seconds.",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(uid)
        )
        return ADMIN_MENU
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number. Must be between 1 and 60.",
            reply_markup=admin_menu_kb(uid)
        )
        return ADMIN_MENU


# ── Site / range handlers ────────────────────────────────────
async def site_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text

    if text == "🔙 Back":
        await update.message.reply_text("🏠 Main menu:", reply_markup=main_menu_kb(uid))
        return MAIN_MENU

    site = None
    if text == "🔵 Stexsms":
        site = "stexsms"
    elif text == "🔵 Voltxsms":
        site = "voltxsms"

    if site:
        if uid not in user_sessions:
            user_sessions[uid] = {}
        user_sessions[uid]["site"] = site
        ctx.user_data["site"] = site

        last = user_sessions[uid].get("last_range", "")
        last_line = f"\n\n📌 Last used range: `{last}`" if last else ""
        await update.message.reply_text(
            f"✏️ *{SITES[site]['name']} – Please send the range:*\n\n"
            "📝 Example: `2250163333XXX`\n"
            "⚠️ Must contain `XXX`"
            + last_line,
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return AWAIT_RANGE

    return SITE_MENU


async def handle_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid        = update.effective_user.id
    range_text = update.message.text.strip().upper()

    if "XXX" not in range_text:
        await update.message.reply_text(
            "❌ Invalid format. Range must contain `XXX`.\n"
            "📝 Example: `2250163333XXX`",
            parse_mode="Markdown",
        )
        return AWAIT_RANGE

    site = ctx.user_data.get("site")
    if not site:
        await update.message.reply_text("❌ No site selected. Use /start again.")
        return ConversationHandler.END

    _stop_monitor(uid)
    if uid not in user_sessions:
        user_sessions[uid] = {}
    user_sessions[uid]["last_range"] = range_text
    user_sessions[uid]["site"]       = site
    user_sessions[uid]["last_otp"]   = None

    wait_msg = await update.message.reply_text(
        "⏳ *Fetching your number...*",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(uid),
    )

    result = await fetch_number(range_text, site)

    if not result:
        try:
            await wait_msg.edit_text(
                "❌ No number found for that range.\n"
                "Try a different range or try again.",
                reply_markup=main_menu_kb(uid),
            )
        except Exception:
            await update.message.reply_text(
                "❌ No number found for that range.\n"
                "Try a different range or try again.",
                reply_markup=main_menu_kb(uid),
            )
        return MAIN_MENU

    await _deliver_number(ctx.application, uid, result, site, edit_msg=wait_msg)
    return MAIN_MENU


# ─── Error handler ────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception:", exc_info=ctx.error)


# ═══════════════════════════════════════════════════════════════
#  STARTUP CHECK
# ═══════════════════════════════════════════════════════════════
def _check_config():
    missing = []
    if not BOT_TOKEN:      missing.append("BOT_TOKEN")
    if not SMS_EMAIL:      missing.append("STEX_EMAIL")
    if not SMS_PASSWORD:   missing.append("STEX_PASSWORD")
    if OTP_GROUP_ID == 0:  missing.append("OTP_GROUP_ID")
    if missing:
        raise SystemExit(
            f"❌ Missing required .env values: {', '.join(missing)}\n"
            "Please fill in your .env file and restart."
        )
    if not TOTP_AVAILABLE:
        log.warning("⚠️ pyotp not installed. 'Get 2FA' will not work.")
    log.info(f"✅ Config OK | CopyTextButton supported: {COPY_SUPPORTED}")
    if not COPY_SUPPORTED:
        log.warning("ℹ️ CopyTextButton not available – falling back to manual copy.")
        log.warning("💡 Upgrade python-telegram-bot: pip install -U python-telegram-bot")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    _check_config()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU:        [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],
            SITE_MENU:        [MessageHandler(filters.TEXT & ~filters.COMMAND, site_menu_handler)],
            AWAIT_RANGE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_range)],
            ADMIN_MENU:       [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_handler)],
            ADMIN_SET_MENU:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_menu_handler)],
            ADD_ADMIN_INPUT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_add_input)],
            SET_INTERVAL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_interval)],
            AWAIT_2FA_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_secret)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_change_number, pattern="^change_number$"))
    app.add_handler(CallbackQueryHandler(cb_copy_fallback, pattern="^copy_"))  # old copy fallback
    app.add_handler(CallbackQueryHandler(verify_membership_callback, pattern="^verify_membership$"))
    app.add_handler(CallbackQueryHandler(cb_remove_admin, pattern="^remove_admin_"))
    # New fake details callbacks
    app.add_handler(CallbackQueryHandler(cb_gender_select, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(cb_change_fake_details, pattern="^change_fake_details_"))
    # Extended copy fallback for identity fields
    app.add_handler(CallbackQueryHandler(cb_copy_identity_fallback, pattern="^copy_name_|^copy_username_|^copy_password_"))
    app.add_error_handler(error_handler)

    log.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()