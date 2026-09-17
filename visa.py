"""US VISA (usvisa-info.com) appointment rescheduler."""

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait as Wait
from selenium.webdriver.common.by import By

from embassy import *

log = logging.getLogger("visa")


def _parse_cli_args():
    # parse_known_args (not parse_args) so importing this module from a test or
    # another script never dies on argv that isn't meant for us.
    parser = argparse.ArgumentParser(description="US visa appointment rescheduler")
    parser.add_argument("--config", help="path to config.ini (default: $VISA_CONFIG, ./config.ini, or next to visa.py)")
    parser.add_argument("--check-config", action="store_true", help="validate the config, print the effective settings, and exit")
    args, _unknown = parser.parse_known_args()
    return args


CLI_ARGS = _parse_cli_args()


def resolve_config_path(explicit=None):
    """First existing config among: --config, $VISA_CONFIG, ./config.ini, and
    the copy next to visa.py. The last one matters because the service may be
    started from a different working directory than the checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        explicit,
        os.environ.get("VISA_CONFIG"),
        os.path.join(os.getcwd(), "config.ini"),
        os.path.join(here, "config.ini"),
    ]
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.abspath(os.path.expanduser(candidate))
        if path in tried:
            continue
        tried.append(path)
        if os.path.isfile(path):
            return path
    raise SystemExit(
        "No config.ini found. Tried:\n  " + "\n  ".join(tried) +
        "\nCopy config.ini.example, fill it in, and pass --config (or set VISA_CONFIG) if it lives elsewhere."
    )


CONFIG_PATH = resolve_config_path(CLI_ARGS.config)
config = configparser.ConfigParser()
# configparser.read() silently ignores a file it cannot open, which used to
# surface much later as a bare KeyError on a section. Open it ourselves so a
# wrong path or a syntax error fails loudly, at startup, with the path in it.
with open(CONFIG_PATH, encoding="utf-8") as _config_file:
    config.read_file(_config_file, source=CONFIG_PATH)


def require(section, key):
    try:
        value = config[section][key]
    except KeyError:
        raise SystemExit(f"Missing [{section}] {key} in {CONFIG_PATH} (see config.ini.example)") from None
    if not value.strip():
        raise SystemExit(f"Empty [{section}] {key} in {CONFIG_PATH} (see config.ini.example)")
    return value.strip()


def require_float(section, key, minimum=None):
    raw = require(section, key)
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"[{section}] {key} must be a number, got {raw!r} in {CONFIG_PATH}") from None
    if minimum is not None and value < minimum:
        raise SystemExit(f"[{section}] {key} must be >= {minimum}, got {value} in {CONFIG_PATH}")
    return value


# Personal Info:
# Account and current appointment info from https://ais.usvisa-info.com
USERNAME = require('PERSONAL_INFO', 'USERNAME')
PASSWORD = require('PERSONAL_INFO', 'PASSWORD')
# Find SCHEDULE_ID in re-schedule page link:
# https://ais.usvisa-info.com/en-am/niv/schedule/{SCHEDULE_ID}/appointment
SCHEDULE_ID = require('PERSONAL_INFO', 'SCHEDULE_ID')
# Target Period:
PERIOD_START = require('PERSONAL_INFO', 'PERIOD_START')
PERIOD_END = require('PERSONAL_INFO', 'PERIOD_END')

def parse_period_date(field_name, value):
    value = value.strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise SystemExit(
            f"Invalid {field_name} in config.ini: '{value}' "
            f"(use YYYY-MM-DD with a valid calendar date, e.g. 2027-11-30). {e}"
        ) from e

PERIOD_START_DT = parse_period_date("PERIOD_START", PERIOD_START)
PERIOD_END_DT = parse_period_date("PERIOD_END", PERIOD_END)
if PERIOD_END_DT <= PERIOD_START_DT:
    raise SystemExit(
        f"PERIOD_END ({PERIOD_END}) must be after PERIOD_START ({PERIOD_START}) in config.ini"
    )

# Dates to skip even when they fall inside the window. Comma-separated; each item
# is either a single day (YYYY-MM-DD) or an inclusive range (YYYY-MM-DD:YYYY-MM-DD).
def parse_exclude_dates(value):
    intervals = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            start_s, _, end_s = chunk.partition(":")
            start = parse_period_date("EXCLUDE_DATES range start", start_s)
            end = parse_period_date("EXCLUDE_DATES range end", end_s)
            intervals.append((min(start, end), max(start, end)))
        else:
            day = parse_period_date("EXCLUDE_DATES", chunk)
            intervals.append((day, day))
    return intervals

EXCLUDED_INTERVALS = parse_exclude_dates(config['PERSONAL_INFO'].get('EXCLUDE_DATES', ''))

def is_excluded_date(dt):
    return any(start <= dt <= end for start, end in EXCLUDED_INTERVALS)

# When True, automatically reschedule to the earliest date found inside the
# target window and then stop. When False, only notify (manual reschedule).
AUTO_RESCHEDULE = config['PERSONAL_INFO'].getboolean('AUTO_RESCHEDULE', fallback=True)
# Safety rail: never move the appointment to a date at or after the one already
# on the account. A wide target window can otherwise contain dates that are
# *worse* than what you hold, and the site charges a reschedule attempt for it.
ONLY_EARLIER = config['PERSONAL_INFO'].getboolean('ONLY_EARLIER', fallback=True)
# The site allows a limited number of reschedules; stop auto-booking after this
# many attempts from one process (0 = unlimited) and fall back to notify-only.
MAX_RESCHEDULE_ATTEMPTS = config['PERSONAL_INFO'].getint('MAX_RESCHEDULE_ATTEMPTS', fallback=6)
# Embassy Section:
YOUR_EMBASSY = require('PERSONAL_INFO', 'YOUR_EMBASSY')
try:
    EMBASSY, FACILITY_ID, REGEX_CONTINUE = Embassies[YOUR_EMBASSY]
except KeyError:
    raise SystemExit(
        f"Unknown YOUR_EMBASSY {YOUR_EMBASSY!r} in {CONFIG_PATH}. "
        f"Known codes: {', '.join(sorted(Embassies))}"
    ) from None

# Notification via Discord bot (https://discord.com/developers/applications)
DISCORD_BOT_TOKEN = config['NOTIFICATION'].get('DISCORD_BOT_TOKEN', '').strip()
DISCORD_CHANNEL_ID = config['NOTIFICATION'].get('DISCORD_CHANNEL_ID', '').strip()

# Time Section:
minute = 60
hour = 60 * minute
# Time between steps (interactions with forms)
STEP_TIME = 0.5
# Time between retries/checks for available dates (seconds)
RETRY_TIME_L_BOUND = require_float('TIME', 'RETRY_TIME_L_BOUND', minimum=1)
RETRY_TIME_U_BOUND = require_float('TIME', 'RETRY_TIME_U_BOUND', minimum=1)
# Cooling down after WORK_LIMIT_TIME hours of work (Avoiding Ban)
WORK_LIMIT_TIME = require_float('TIME', 'WORK_LIMIT_TIME', minimum=0)
WORK_COOLDOWN_TIME = require_float('TIME', 'WORK_COOLDOWN_TIME', minimum=0)
# Temporary Banned (empty list): wait COOLDOWN_TIME hours
BAN_COOLDOWN_TIME = require_float('TIME', 'BAN_COOLDOWN_TIME', minimum=0)
# Don't re-attempt the same date for this many minutes after a failed booking:
# the site counts every attempt, and a date that just failed is usually gone.
RESCHEDULE_RETRY_COOLDOWN = config.getfloat('TIME', 'RESCHEDULE_RETRY_COOLDOWN', fallback=20)
# Repeated identical error notifications are collapsed within this many minutes
# (0 disables the throttle). Booking-relevant titles are never throttled.
NOTIFY_MIN_INTERVAL = config.getfloat('TIME', 'NOTIFY_MIN_INTERVAL', fallback=15)
# Poll a bit faster right after the available-date list changes, drifting back
# to the configured upper bound while nothing moves. Never polls slower than
# RETRY_TIME_U_BOUND nor faster than RETRY_TIME_L_BOUND.
ADAPTIVE_PACING = config.getboolean('TIME', 'ADAPTIVE_PACING', fallback=True)
ADAPTIVE_RAMP_POLLS = config.getint('TIME', 'ADAPTIVE_RAMP_POLLS', fallback=10)
# auto  = decide per response whether an empty list is a soft ban or simply no
#         open days (see empty_list_looks_like_ban)
# ban   = always treat an empty list as a ban and sleep BAN_COOLDOWN_TIME
# retry = never treat it as a ban, just keep polling
EMPTY_LIST_POLICY = config.get('TIME', 'EMPTY_LIST_POLICY', fallback='auto').strip().lower()
if EMPTY_LIST_POLICY not in ("auto", "ban", "retry"):
    raise SystemExit(f"[TIME] EMPTY_LIST_POLICY must be auto, ban or retry — got {EMPTY_LIST_POLICY!r}")
# The `auto` probe costs a full HTML page load. Running it on every empty poll
# doubles our request volume during exactly the streak that precedes a soft ban
# (observed: 58 probes in 37 minutes, then blocked), so cache its verdict.
EMPTY_PROBE_MIN_INTERVAL = config.getfloat('TIME', 'EMPTY_PROBE_MIN_INTERVAL', fallback=10)
# Consecutive empty lists are the site warming up to rate-limit us: stretch the
# wait by the streak length, up to this multiple of the normal interval.
EMPTY_STREAK_BACKOFF_MAX = config.getfloat('TIME', 'EMPTY_STREAK_BACKOFF_MAX', fallback=8)


def _load_timezone(name):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as e:
        raise SystemExit(
            f"[TIME] TIMEZONE {name!r} is not a known IANA zone (e.g. America/Toronto): {e}"
        ) from e


# All scheduling decisions (active window, daily report boundary) use this zone,
# so a server running in UTC still lines up with the consulate's local clock.
TIMEZONE = config.get('TIME', 'TIMEZONE', fallback='').strip()
TZ = _load_timezone(TIMEZONE)


def now():
    return datetime.now(TZ)


def _parse_hhmm(value):
    try:
        hh, _, mm = value.strip().partition(":")
        hh, mm = int(hh), int(mm or 0)
        if not (0 <= hh <= 24 and 0 <= mm < 60):
            raise ValueError
        return hh * 60 + mm
    except ValueError:
        raise SystemExit(
            f"[TIME] ACTIVE_HOURS entries must look like HH:MM-HH:MM — got {value!r}"
        ) from None


def parse_active_hours(value):
    """Minutes-since-midnight ranges the bot is allowed to poll in. Empty means
    around the clock. A range whose end is <= its start wraps past midnight."""
    ranges = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start_s, sep, end_s = chunk.partition("-")
        if not sep:
            raise SystemExit(f"[TIME] ACTIVE_HOURS entry {chunk!r} is missing the '-' separator")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if start == end:
            raise SystemExit(f"[TIME] ACTIVE_HOURS entry {chunk!r} is an empty range")
        ranges.append((start, end))
    return ranges


ACTIVE_HOURS = config.get('TIME', 'ACTIVE_HOURS', fallback='').strip()
ACTIVE_RANGES = parse_active_hours(ACTIVE_HOURS)


def in_active_window(moment=None):
    if not ACTIVE_RANGES:
        return True
    moment = moment or now()
    minutes = moment.hour * 60 + moment.minute
    for start, end in ACTIVE_RANGES:
        if start < end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # wraps past midnight
            return True
    return False


def seconds_until_active(moment=None):
    """Seconds to wait until the next active window opens (0 if open now)."""
    if not ACTIVE_RANGES or in_active_window(moment):
        return 0
    moment = moment or now()
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    waits = []
    for start, _end in ACTIVE_RANGES:
        candidate = midnight + timedelta(minutes=start)
        if candidate <= moment:
            candidate += timedelta(days=1)
        waits.append((candidate - moment).total_seconds())
    return max(0, int(min(waits)))


# Logging Section:
LOG_DIR = config.get('LOGGING', 'LOG_DIR', fallback='logs').strip() or 'logs'
LOG_RETENTION_DAYS = config.getint('LOGGING', 'LOG_RETENTION_DAYS', fallback=14)
DEBUG_ARTIFACT_LIMIT = config.getint('LOGGING', 'DEBUG_ARTIFACT_LIMIT', fallback=20)
DEBUG_DIR = os.path.join(LOG_DIR, "debug")

# CHROMEDRIVER
# Details for the script to control Chrome
LOCAL_USE = config.getboolean('CHROMEDRIVER', 'LOCAL_USE', fallback=True)
HEADLESS = config.getboolean('CHROMEDRIVER', 'HEADLESS', fallback=False)
CHROME_BIN = config.get('CHROMEDRIVER', 'CHROME_BIN', fallback='').strip()
CHROMEDRIVER_PATH = config.get('CHROMEDRIVER', 'CHROMEDRIVER_PATH', fallback='').strip()
USER_AGENT = config.get('CHROMEDRIVER', 'USER_AGENT', fallback='').strip()
MAX_LOGIN_ATTEMPTS = config.getint('CHROMEDRIVER', 'MAX_LOGIN_ATTEMPTS', fallback=3)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Optional: HUB_ADDRESS is mandatory only when LOCAL_USE = False
HUB_ADDRESS = config.get('CHROMEDRIVER', 'HUB_ADDRESS', fallback='').strip()
if not LOCAL_USE and not HUB_ADDRESS:
    raise SystemExit("[CHROMEDRIVER] HUB_ADDRESS is required when LOCAL_USE = False")

SIGN_IN_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_in"
APPOINTMENT_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment"
DATE_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{FACILITY_ID}.json?appointments[expedite]=false"
TIME_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{FACILITY_ID}.json?date=%s&appointments[expedite]=false"
SIGN_OUT_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_out"


def setup_logging():
    """One stream for stdout (journald picks it up) and one rotating daily file
    under LOG_DIR, pruned to LOG_RETENTION_DAYS. Replaces the old print() +
    info_logger() double-write, which grew one unbounded file per day."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            os.path.join(LOG_DIR, "visa.log"), when="midnight",
            backupCount=max(1, LOG_RETENTION_DAYS), encoding="utf-8",
        )
        file_handler.suffix = "%Y-%m-%d"
        handlers.append(file_handler)
    except OSError as e:
        print(f"Could not open the log file in {LOG_DIR!r}, logging to stdout only: {e}")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    # Selenium/urllib3 are chatty at INFO and would drown the scheduler's own log.
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# Fetch the JSON endpoints from inside the logged-in browser with a *synchronous*
# XMLHttpRequest — the same technique the site's own front-end uses, and the only
# one that works reliably here (an injected fetch() gets "TypeError: Failed to
# fetch" on this origin). We wrap it in try/catch so that when the server drops
# or redirects the connection after a soft rate-limit (the sync send() would
# otherwise throw "Failed to execute 'send' on 'XMLHttpRequest'") we return a
# structured error instead of letting Selenium raise a generic exception — which
# lets the main loop re-login cleanly instead of crash-looping on a dead session.
# A sync XHR follows redirects automatically, so req.responseURL/req.status
# reflect the final hop (e.g. a 200 HTML sign-in page if the session expired).
JS_FETCH = """
try {
    var req = new XMLHttpRequest();
    req.open('GET', arguments[0], false);
    req.setRequestHeader('Accept', 'application/json, text/javascript, */*; q=0.01');
    req.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    req.send(null);
    return JSON.stringify({status: req.status, url: req.responseURL, body: req.responseText});
} catch (e) {
    return JSON.stringify({status: 0, error: String(e)});
}
"""


class SessionExpired(Exception):
    """The logged-in session is no longer usable: redirect to sign-in, 401/403,
    or the connection was dropped (typically a soft rate-limit/ban). The main
    loop catches this and forces a fresh login."""


# Titles that always go out: they are the ones you act on. Everything else
# (ERROR/SESSION/LOGIN_FAIL/...) is collapsed when it repeats verbatim, so a
# flapping site can't turn into one Discord message per retry.
ALWAYS_NOTIFY_TITLES = {"SUCCESS", "FOUND", "UNCERTAIN", "LIMIT", "STOP", "DAILY", "STATUS", "REST", "BAN"}
_notify_history = {}


def _notification_fingerprint(title, msg):
    # Digits change every message (counts, timestamps, request ids) but say
    # nothing about *which* failure this is — blank them out before comparing.
    return title, re.sub(r"\d+", "#", msg)[:200]


def _notification_suppressed(title, msg):
    """True when this message repeats one sent less than NOTIFY_MIN_INTERVAL
    minutes ago. Bumps the suppressed counter so the next one that gets through
    can say how many were swallowed."""
    if NOTIFY_MIN_INTERVAL <= 0 or title in ALWAYS_NOTIFY_TITLES:
        return False, 0
    key = _notification_fingerprint(title, msg)
    last_sent, suppressed = _notify_history.get(key, (0.0, 0))
    if time.time() - last_sent < NOTIFY_MIN_INTERVAL * minute:
        _notify_history[key] = (last_sent, suppressed + 1)
        return True, suppressed + 1
    _notify_history[key] = (time.time(), 0)
    return False, suppressed


def send_notification(title, msg):
    suppressed_now, suppressed_count = _notification_suppressed(title, msg)
    if suppressed_now:
        log.info(f"Notification '{title}' suppressed (repeat #{suppressed_count} within {NOTIFY_MIN_INTERVAL:g} min)")
        return
    log.info("Sending notification!")
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        log.info("Discord not configured, skipping notification.")
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    if suppressed_count:
        msg = f"{msg}\n(+{suppressed_count} identical message(s) suppressed since the last one)"
    content = f"**VISA - {title}**\n{msg}"
    if len(content) > 2000:
        content = content[:1997] + "..."

    try:
        response = requests.post(url, headers=headers, json={"content": content}, timeout=30)
        response.raise_for_status()
        log.info("Discord notification sent.")
    except requests.RequestException as e:
        log.warning(f"Discord notification failed: {e}")


def _browser_xhr(url):
    """Fetch via an in-browser synchronous XHR (best WAF evasion: the browser's
    own TLS fingerprint + cookie jar). Returns {status, url, body} or {status:0}."""
    try:
        raw = driver.execute_script(JS_FETCH, url)
    except Exception as e:
        return {"status": 0, "error": f"execute_script failed: {e}"}
    try:
        return json.loads(raw) if raw else {"status": 0, "error": "empty XHR result"}
    except (json.JSONDecodeError, TypeError):
        return {"status": 0, "error": "unparseable XHR result"}


def _browser_cookies():
    return {c["name"]: c["value"] for c in driver.get_cookies()}


def _browser_user_agent():
    try:
        return driver.execute_script("return navigator.userAgent;")
    except Exception:
        return USER_AGENT or DEFAULT_USER_AGENT


def _requests_fetch(url):
    """Fallback: fetch the same URL with Python requests, reusing EVERY cookie
    the browser holds (incl. WAF clearance cookies) plus its User-Agent. This is
    the same auth path reschedule() uses, and it sidesteps in-page restrictions
    (CSP / cross-origin redirects) that can break the in-browser XHR."""
    try:
        headers = {
            "User-Agent": _browser_user_agent(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": APPOINTMENT_URL,
        }
        r = requests.get(url, headers=headers, cookies=_browser_cookies(), timeout=30)
        return {"status": r.status_code, "url": r.url, "body": r.text}
    except Exception as e:
        return {"status": 0, "error": f"requests fetch failed: {e}"}


def _requests_get_html(url):
    """Like _requests_fetch but asks for an HTML document (no X-Requested-With,
    HTML Accept) so Rails returns the rendered page rather than a JS/JSON reply.
    Used to scrape the reschedule form's hidden fields when the browser DOM is
    unavailable (e.g. the browser is on a WAF block page)."""
    try:
        headers = {
            "User-Agent": _browser_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": APPOINTMENT_URL,
        }
        r = requests.get(url, headers=headers, cookies=_browser_cookies(), timeout=30)
        return {"status": r.status_code, "url": r.url, "body": r.text}
    except Exception as e:
        return {"status": 0, "error": f"requests GET html failed: {e}"}


def _page_diagnostics():
    try:
        return (
            f"url={driver.current_url!r}, title={driver.title!r}, "
            f"page[:200]={driver.page_source[:200]!r}"
        )
    except Exception as e:
        return f"(diagnostics unavailable: {e})"


def _page_text(limit=4000):
    """Visible text of the current page, read inside the browser. Cheaper than
    driver.page_source, which serializes the whole DOM across the wire — this
    runs on every poll, so the difference adds up."""
    try:
        text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        ) or ""
        return text[:limit]
    except Exception:
        try:
            return driver.page_source[:limit]
        except Exception:
            return ""


def fetch_json(url):
    # Primary path: in-browser XHR. Fallback: Python requests with the browser's
    # cookies. Either being blocked/reset is reported as a SessionExpired so the
    # main loop re-logs in; if BOTH fail we attach page diagnostics so the real
    # cause (WAF block page, captcha, redirect) is visible in the notification.
    result = _browser_xhr(url)
    if not result.get("status"):
        xhr_err = result.get("error")
        result = _requests_fetch(url)
        if not result.get("status"):
            raise SessionExpired(
                f"Fetch blocked for {url} (xhr: {xhr_err}; "
                f"requests: {result.get('error')}). {_page_diagnostics()}"
            )
        log.info(f"\t(in-browser XHR blocked, served via requests fallback) — {xhr_err}")
    status = result.get("status")
    body = result.get("body") or ""
    final_url = result.get("url") or ""
    if status in (301, 302, 303, 401, 403) or "sign_in" in final_url:
        raise SessionExpired(
            f"Session expired (HTTP {status}, landed on {final_url}) fetching {url}"
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        if "user_email" in body or body.lstrip()[:1] == "<":
            raise SessionExpired(f"Logged out — got HTML instead of JSON from {url}")
        raise RuntimeError(
            f"Unexpected response (HTTP {status}) when fetching {url}: {body[:500]}"
        )

appointment_page_ready = False
scheduling_limit_notified = False
auto_reschedule_enabled = AUTO_RESCHEDULE
reschedule_attempts_used = 0
# What the site itself last told us is left ("You have N remaining attempt"),
# which is authoritative and usually lower than MAX_RESCHEDULE_ATTEMPTS.
site_remaining_attempts = None
# date string -> unix ts of the last failed attempt, for RESCHEDULE_RETRY_COOLDOWN
failed_targets = {}


def reset_appointment_page_state():
    global appointment_page_ready, scheduling_limit_notified
    appointment_page_ready = False
    scheduling_limit_notified = False


def is_scheduling_limit_warning():
    return "Scheduling Limit Warning" in _page_text()


def _acknowledge_scheduling_limit_checkbox():
    try:
        checkbox = Wait(driver, 15).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//label[contains(normalize-space(.), 'I understand')]/ancestor::div[contains(@class,'icheckbox')]"
                " | //label[contains(normalize-space(.), 'I understand')]/preceding-sibling::div[contains(@class,'icheckbox')]",
            ))
        )
        if "checked" not in (checkbox.get_attribute("class") or "").split():
            checkbox.click()
            time.sleep(STEP_TIME)
    except Exception:
        try:
            driver.find_element(By.XPATH, "//label[contains(., 'I understand')]").click()
            time.sleep(STEP_TIME)
        except Exception:
            pass

    driver.execute_script(
        "var input = document.querySelector(\"input[name='confirmed_limit_message']\");"
        "if (input) { input.value = '1'; input.checked = true; }"
    )
    time.sleep(STEP_TIME)


def _click_scheduling_limit_continue():
    continue_selectors = [
        (By.CSS_SELECTOR, "input[name='commit'][value='Continue']"),
        (By.XPATH, "//input[@type='submit' and @name='commit' and @value='Continue']"),
        (By.XPATH, "//button[@name='commit' and @value='Continue']"),
        (By.XPATH, "//a[contains(@class,'button') and contains(normalize-space(.), 'Continue')]"),
        (By.XPATH, "//input[@type='submit' and contains(@value, 'Continue')]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'Continue')]"),
    ]
    for by, selector in continue_selectors:
        try:
            btn = Wait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            try:
                btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            time.sleep(STEP_TIME)
            return True
        except Exception:
            continue
    return False


def _submit_scheduling_limit_warning():
    # Send the browser's whole cookie jar, not just _yatri_session: the WAF
    # clearance cookies live alongside it and a POST without them comes back
    # 403. (reschedule() has always done it this way.)
    cookies = _browser_cookies()
    if "_yatri_session" not in cookies:
        raise SessionExpired("No _yatri_session cookie — the session is gone, cannot submit the limit warning.")
    headers = {
        "User-Agent": _browser_user_agent(),
        "Referer": APPOINTMENT_URL,
    }
    try:
        utf8 = driver.find_element(by=By.NAME, value="utf8").get_attribute("value")
        token = driver.find_element(by=By.NAME, value="authenticity_token").get_attribute("value")
    except Exception:
        form = _reschedule_form_fields()
        utf8, token = form["utf8"], form["authenticity_token"]
    data = {
        "utf8": utf8,
        "authenticity_token": token,
        "confirmed_limit_message": "1",
        "commit": "Continue",
    }
    r = requests.post(APPOINTMENT_URL, headers=headers, cookies=cookies, data=data, timeout=30)
    if r.status_code >= 400:
        log.warning(f"\tScheduling-limit POST returned HTTP {r.status_code}")
    driver.get(APPOINTMENT_URL)
    time.sleep(STEP_TIME)


def dismiss_scheduling_limit_warning():
    global scheduling_limit_notified, auto_reschedule_enabled, site_remaining_attempts
    if not is_scheduling_limit_warning():
        return False

    log.info("\tScheduling Limit Warning detected, dismissing...")
    remaining = re.search(r"You have (\d+) remaining attempt", driver.page_source)
    _acknowledge_scheduling_limit_checkbox()

    if _click_scheduling_limit_continue() and not is_scheduling_limit_warning():
        log.info("\tScheduling Limit Warning dismissed via Continue button.")
    elif not is_scheduling_limit_warning():
        pass
    else:
        log.info("\tContinue button not found or ineffective, submitting warning form via POST...")
        _submit_scheduling_limit_warning()

    if is_scheduling_limit_warning():
        log.info("\tPOST did not clear warning, trying direct URL...")
        driver.get(f"{APPOINTMENT_URL}?confirmed_limit_message=1&commit=Continue")
        time.sleep(STEP_TIME)

    if is_scheduling_limit_warning():
        raise RuntimeError("Could not dismiss scheduling limit warning")

    Wait(driver, 30).until(lambda d: not is_scheduling_limit_warning())
    log.info("\tScheduling Limit Warning dismissed.")
    if remaining:
        remaining_attempts = int(remaining.group(1))
        site_remaining_attempts = remaining_attempts
        log.info(f"\tSite reports {remaining_attempts} remaining reschedule attempt(s).")
        # The site itself says there is nothing left — booking again would fail
        # anyway, so drop to notify-only instead of spending the loop on POSTs.
        if remaining_attempts == 0 and auto_reschedule_enabled:
            auto_reschedule_enabled = False
            send_notification("LIMIT", "The site reports 0 remaining reschedule attempts — switching to notify-only.")
        elif not scheduling_limit_notified:
            send_notification(
                "LIMIT",
                f"Scheduling limit warning acknowledged. {remaining_attempts} reschedule attempt(s) remaining.",
            )
            scheduling_limit_notified = True
    return True


def ensure_appointment_page_ready():
    global appointment_page_ready
    if appointment_page_ready and not is_scheduling_limit_warning():
        return
    driver.get(APPOINTMENT_URL)
    time.sleep(STEP_TIME)
    dismiss_scheduling_limit_warning()
    appointment_page_ready = True


# The appointment page prints the booked slot as e.g.
# "Consular Appointment  13 November, 2027, 08:30 Toronto local time".
CONSULAR_APPT_DATE_RE = re.compile(
    r"consular[-_ ]?appt.{0,600}?(\d{1,2})\s+([A-Za-z]{3,}),?\s+(\d{4})", re.I | re.S)
GENERIC_APPT_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,}),?\s+(\d{4}),\s*\d{1,2}:\d{2}", re.I)

_current_appointment = {"date": None, "fetched_at": 0.0}
CURRENT_APPOINTMENT_TTL = 10 * minute


def _parse_appointment_date(day, month_name, year):
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{day} {month_name} {year}", fmt)
        except ValueError:
            continue
    return None


def _extract_appointment_date(html):
    for regex in (CONSULAR_APPT_DATE_RE, GENERIC_APPT_DATE_RE):
        match = regex.search(html or "")
        if match:
            parsed = _parse_appointment_date(*match.groups())
            if parsed:
                return parsed
    return None


def current_appointment_date(force=False):
    """The appointment currently on the account, or None when it can't be read.
    Two jobs: the safety rail that stops us booking a *later* date than the one
    we already hold, and turning an UNCERTAIN reschedule reply into a definite
    answer. Unknown (None) never blocks rescheduling — the site changes its
    markup often enough that failing closed would break the whole bot."""
    cached = _current_appointment["date"]
    if not force and cached and time.time() - _current_appointment["fetched_at"] < CURRENT_APPOINTMENT_TTL:
        return cached
    html = ""
    try:
        if APPOINTMENT_URL in (driver.current_url or ""):
            html = driver.page_source
    except Exception:
        html = ""
    parsed = _extract_appointment_date(html)
    if parsed is None:
        parsed = _extract_appointment_date(_requests_get_html(APPOINTMENT_URL).get("body") or "")
    if parsed:
        _current_appointment.update(date=parsed, fetched_at=time.time())
    return parsed


def auto_action(label, find_by, el_type, action, value, sleep_time=0):
    # Find Element By
    match find_by.lower():
        case 'id':
            item = driver.find_element(By.ID, el_type)
        case 'name':
            item = driver.find_element(By.NAME, el_type)
        case 'class':
            item = driver.find_element(By.CLASS_NAME, el_type)
        case 'xpath':
            item = driver.find_element(By.XPATH, el_type)
        case _:
            return 0
    # Do Action:
    match action.lower():
        case 'send':
            item.send_keys(value)
        case 'click':
            item.click()
        case _:
            return 0
    log.info(f"\t{label}: Check!")
    if sleep_time:
        time.sleep(sleep_time)


def is_blocked_page():
    title = (driver.title or "").lower()
    if "403" in title or "forbidden" in title:
        return True
    snippet = _page_text().lower()
    return "403 forbidden" in snippet or "access denied" in snippet


def start_process():
    log.info(f"\tOpening sign-in: {SIGN_IN_LINK}")
    driver.get(SIGN_IN_LINK)
    time.sleep(STEP_TIME)
    if is_blocked_page():
        save_debug_artifacts("403-forbidden")
        raise RuntimeError(
            "Site returned 403 Forbidden (bot/WAF block). "
            "Keep the stealth options in build_chrome_options()/apply_stealth() "
            "(a default headless fingerprint is blocked outright), or run the "
            "scheduler from your home computer instead of the server."
        )
    try:
        Wait(driver, 90).until(
            EC.any_of(
                EC.presence_of_element_located((By.NAME, "commit")),
                EC.presence_of_element_located((By.ID, "user_email")),
            )
        )
    except Exception as e:
        save_debug_artifacts("login-page")
        raise RuntimeError(
            f"Login page did not load (title={driver.title!r}, url={driver.current_url!r}): {e}"
        ) from e
    try:
        auto_action("Click bounce", "xpath", '//a[@class="down-arrow bounce"]', "click", "", STEP_TIME)
    except Exception:
        log.info("\tNo bounce arrow on page, continuing.")
    auto_action("Email", "id", "user_email", "send", USERNAME, STEP_TIME)
    auto_action("Password", "id", "user_password", "send", PASSWORD, STEP_TIME)
    auto_action("Privacy", "class", "icheckbox", "click", "", STEP_TIME)
    auto_action("Enter Panel", "name", "commit", "click", "", STEP_TIME)
    Wait(driver, 90).until(EC.presence_of_element_located((By.XPATH, "//a[contains(text(), '" + REGEX_CONTINUE + "')]")))
    log.info("\tlogin successful!")

RESCHEDULE_FIELDS = (
    "utf8",
    "authenticity_token",
    "confirmed_limit_message",
    "use_consulate_appointment_capacity",
)


def _reschedule_form_fields():
    """Hidden form fields required to POST a reschedule. Prefer the live browser
    DOM; if the browser can't render the form (e.g. it's on a WAF block page
    while the JSON endpoints only succeed via the requests fallback), scrape them
    from the appointment page HTML fetched with the browser's cookies."""
    try:
        fields = {n: driver.find_element(By.NAME, n).get_attribute("value")
                  for n in RESCHEDULE_FIELDS}
        if fields.get("authenticity_token"):
            return fields
    except Exception:
        pass  # DOM unavailable -> fall back to scraping the HTML.

    result = _requests_get_html(APPOINTMENT_URL)
    body = result.get("body") or ""
    if not result.get("status"):
        raise RuntimeError(
            f"Could not load appointment form (requests: {result.get('error')}). "
            f"{_page_diagnostics()}"
        )

    def hidden(name):
        m = (re.search(r'name=["\']' + re.escape(name) + r'["\'][^>]*\bvalue=["\']([^"\']*)["\']', body)
             or re.search(r'\bvalue=["\']([^"\']*)["\'][^>]*name=["\']' + re.escape(name) + r'["\']', body))
        return m.group(1) if m else None

    token = hidden("authenticity_token")
    if not token:
        m = (re.search(r'name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', body)
             or re.search(r'content=["\']([^"\']+)["\'][^>]*name=["\']csrf-token["\']', body))
        token = m.group(1) if m else None
    if not token:
        raise RuntimeError(
            f"No authenticity_token on appointment page (HTTP {result.get('status')}). "
            f"{_page_diagnostics()}"
        )
    return {
        "utf8": hidden("utf8") or "✓",
        "authenticity_token": token,
        "confirmed_limit_message": hidden("confirmed_limit_message") or "1",
        "use_consulate_appointment_capacity": hidden("use_consulate_appointment_capacity") or "true",
    }


RESCHEDULE_SUCCESS_PHRASES = (
    "successfully scheduled",
    "successfully rescheduled",
    "your appointment has been",
    "appointment is scheduled",
)
RESCHEDULE_FAILURE_PHRASES = (
    "no longer available",
    "not available",
    "please try again",
    "you must select",
    "errors prohibited",
    "could not be",
)


def _response_summary(body):
    """A short, human-readable slice of a response: surface a flash/alert/notice
    message if present, else the first bit of visible text. Used so the FAIL/
    SUCCESS notification shows what the server actually returned."""
    body = body or ""
    m = re.search(
        r'(?:id|class)=["\'][^"\']*(?:flash|alert|notice|error|message)[^"\']*["\'][^>]*>(.*?)<',
        body, re.I | re.S,
    )
    text = m.group(1) if m else body
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _classify_reschedule_response(r):
    """Decide whether a reschedule POST went through. Success-banner wording
    varies by locale, so failures need a clear signal (HTTP error, bounced to
    sign-in, or a known error phrase). Anything else is UNCERTAIN rather than
    an assumed success — an unrecognized response must never be trusted enough
    to stop monitoring, since that's how a real reschedule failure gets
    silently reported as booked."""
    body = r.text or ""
    low = body.lower()
    if any(p in low for p in RESCHEDULE_SUCCESS_PHRASES):
        return "SUCCESS", "success banner"
    if r.status_code >= 400:
        return "FAIL", f"HTTP {r.status_code}"
    if "sign_in" in (r.url or "") or "user_email" in low:
        return "FAIL", "bounced to sign-in"
    for p in RESCHEDULE_FAILURE_PHRASES:
        if p in low:
            return "FAIL", f"error phrase {p!r}"
    return "UNCERTAIN", "no recognizable success/failure signal"


def verify_reschedule(date):
    """Read the appointment back off the account: True/False, or None when the
    page can't be parsed. This is what makes an UNCERTAIN reply actionable."""
    booked = current_appointment_date(force=True)
    if booked is None:
        return None
    return booked.strftime("%Y-%m-%d") == date


def reschedule(date):
    global reschedule_attempts_used
    ensure_appointment_page_ready()
    time_slot = get_time(date)
    form = _reschedule_form_fields()
    headers = {
        "User-Agent": _browser_user_agent(),
        "Referer": APPOINTMENT_URL,
    }
    data = {
        **form,
        "appointments[consulate_appointment][facility_id]": FACILITY_ID,
        "appointments[consulate_appointment][date]": date,
        "appointments[consulate_appointment][time]": time_slot,
    }
    reschedule_attempts_used += 1
    r = requests.post(APPOINTMENT_URL, headers=headers, cookies=_browser_cookies(), data=data, timeout=60)
    status, reason = _classify_reschedule_response(r)
    # Don't trust the response body alone: read the appointment back. This turns
    # UNCERTAIN into a real answer and catches a "success" banner that didn't
    # actually move the appointment.
    verified = verify_reschedule(date)
    detail = f"(HTTP {r.status_code}; {reason}; verified={verified}; resp: {_response_summary(r.text)})"
    if verified is True:
        return ["SUCCESS", f"Rescheduled Successfully! {date} {time_slot} {detail}"]
    if verified is False:
        booked = current_appointment_date()
        held = booked.date() if booked else "unknown"
        if status == "SUCCESS":
            # The body claims success but the appointment page still shows the
            # old date. Either the booking silently failed or we misread the
            # page — say so instead of guessing, and keep monitoring.
            return ["UNCERTAIN", (
                f"Server reported success but the appointment page still shows {held} — "
                f"check the site manually! {date} {time_slot} {detail}")]
        return ["FAIL", f"Reschedule did NOT take effect — account still shows {held}. {date} {time_slot} {detail}"]
    # verified is None: the appointment page couldn't be parsed, fall back to
    # whatever the POST response itself suggested.
    if status == "SUCCESS":
        return ["SUCCESS", f"Rescheduled Successfully! {date} {time_slot} {detail}"]
    if status == "UNCERTAIN":
        return ["UNCERTAIN", f"Reschedule response unclear — verify manually on the site! {date} {time_slot} {detail}"]
    return ["FAIL", f"Reschedule Failed!!! {date} {time_slot} {detail}"]


def get_date():
    global appointment_page_ready
    ensure_appointment_page_ready()
    try:
        dates = fetch_json(DATE_URL)
    except RuntimeError:
        # A non-JSON page can mean the scheduling-limit warning re-appeared;
        # re-prime the appointment page once and retry.
        if is_scheduling_limit_warning():
            appointment_page_ready = False
            ensure_appointment_page_ready()
            dates = fetch_json(DATE_URL)
        else:
            raise
    if not isinstance(dates, list):
        # The endpoint answers with a JSON array; anything else (an error
        # object, a captcha payload) would silently iterate into nonsense.
        raise RuntimeError(f"Expected a list of days from {DATE_URL}, got: {str(dates)[:300]}")
    return dates

def get_time(date):
    ensure_appointment_page_ready()
    time_url = TIME_URL % date
    data = fetch_json(time_url)
    available_times = data.get("available_times") or []
    if not available_times:
        raise RuntimeError(f"No available times for {date}: {data}")
    # Don't rely on the API returning times in any particular order — sort
    # and take the earliest explicitly, matching the "earliest slot" goal.
    sorted_times = sorted(available_times)
    time_slot = sorted_times[0]
    log.info(f"Got time successfully! {date} {time_slot} (of {len(sorted_times)} available: {', '.join(sorted_times)})")
    return time_slot


def get_available_dates(dates):
    matches = []
    for d in dates:
        date = d.get('date') if isinstance(d, dict) else None
        if not date:
            continue
        try:
            new_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        if PERIOD_START_DT <= new_date <= PERIOD_END_DT and not is_excluded_date(new_date):
            matches.append(date)
    return sorted(matches)


def choose_target(candidates):
    """Earliest candidate still worth an attempt: not on cooldown from a failed
    booking, and — when ONLY_EARLIER — strictly before the appointment already
    on the account. Returns (date, skip_reason); date is None when nothing is
    eligible right now."""
    held = current_appointment_date() if ONLY_EARLIER else None
    now_ts = time.time()
    cooling = []
    for date in sorted(candidates):
        if held and datetime.strptime(date, "%Y-%m-%d") >= held:
            # Sorted ascending, so every remaining candidate is later too.
            return None, f"all candidates are on/after the appointment you already hold ({held.date()})"
        failed_at = failed_targets.get(date)
        if failed_at and now_ts - failed_at < RESCHEDULE_RETRY_COOLDOWN * minute:
            cooling.append(date)
            continue
        return date, None
    if cooling:
        return None, f"{len(cooling)} candidate(s) on cooldown after a failed attempt: {', '.join(cooling)}"
    return None, "no eligible candidate"


def effective_attempt_cap():
    """How many bookings this process may still POST. MAX_RESCHEDULE_ATTEMPTS is
    our own guard; the count the site prints on the limit warning is the real
    quota, so take whichever is lower. 0 means no cap is known."""
    cap = MAX_RESCHEDULE_ATTEMPTS
    if site_remaining_attempts is None:
        return cap
    if not cap:
        return site_remaining_attempts
    return min(cap, site_remaining_attempts)


_empty_probe = {"at": 0.0, "verdict": None}


def reset_empty_probe():
    _empty_probe.update(at=0.0, verdict=None)


def empty_list_looks_like_ban():
    """An empty days list is ambiguous: it is the normal answer when the
    consulate has no open day at all, and also what a soft rate-limit returns.
    If the appointment page still renders the booking form for us, the session
    is healthy and the list is genuinely empty — sleeping for hours there would
    just mean missing the next slot that opens.

    The verdict is cached for EMPTY_PROBE_MIN_INTERVAL minutes: this probe is a
    full HTML page load, and firing it on every empty poll is itself enough
    extra traffic to get us rate-limited."""
    now_ts = time.time()
    if (_empty_probe["verdict"] is not None
            and now_ts - _empty_probe["at"] < EMPTY_PROBE_MIN_INTERVAL * minute):
        return _empty_probe["verdict"]
    verdict = _probe_appointment_page_blocked()
    _empty_probe.update(at=now_ts, verdict=verdict)
    return verdict


def _probe_appointment_page_blocked():
    result = _requests_get_html(APPOINTMENT_URL)
    body = result.get("body") or ""
    if not result.get("status"):
        return True
    if "sign_in" in (result.get("url") or "") or "user_email" in body:
        return True
    return "authenticity_token" not in body


def retry_sleep_seconds(unchanged_polls=0):
    """Random wait inside [RETRY_TIME_L_BOUND, RETRY_TIME_U_BOUND]. With
    ADAPTIVE_PACING the upper bound starts at the midpoint right after the
    available-date list changed (something is moving — look again sooner) and
    ramps back to the configured bound while nothing happens. It never waits
    longer than configured, nor shorter than the lower bound."""
    low, high = sorted((int(RETRY_TIME_L_BOUND), int(RETRY_TIME_U_BOUND)))
    if ADAPTIVE_PACING and high > low and ADAPTIVE_RAMP_POLLS > 0:
        ramp = min(1.0, unchanged_polls / ADAPTIVE_RAMP_POLLS)
        midpoint = low + (high - low) // 2
        high = int(midpoint + (high - midpoint) * ramp)
        high = max(low, high)
    return random.randint(low, high)


class RunReporter:
    def __init__(self):
        self.session_start = now()
        self.first_round_reported = False
        self.daily_date = now().date()
        self._reset_daily()

    def _reset_daily(self):
        self.daily = {
            "requests": 0,
            "errors": 0,
            "bans": 0,
            "rests": 0,
            "reschedule_attempts": 0,
            "last_state": None,
            "min_available": None,
            "max_available": None,
            "max_in_period": 0,
        }

    def _summarize_dates(self, dates):
        if not dates:
            return "none"
        date_strs = [d.get("date") for d in dates if d.get("date")]
        if not date_strs:
            return "none"
        if len(date_strs) <= 6:
            return ", ".join(date_strs)
        return f"{date_strs[0]} .. {date_strs[-1]} ({len(date_strs)} total)"

    def record(self, state, dates=None, candidates=None):
        self.daily["requests"] += 1
        self.daily["last_state"] = state
        if state == "BANNED":
            self.daily["bans"] += 1
        elif state == "ERROR":
            self.daily["errors"] += 1
        if dates is not None:
            count = len(dates)
            if self.daily["min_available"] is None:
                self.daily["min_available"] = count
            self.daily["min_available"] = min(self.daily["min_available"], count)
            self.daily["max_available"] = max(self.daily["max_available"] or 0, count)
        if candidates is not None:
            self.daily["max_in_period"] = max(self.daily["max_in_period"], len(candidates))

    def record_rest(self):
        self.daily["rests"] += 1

    def maybe_send_daily_report(self):
        today = now().date()
        if today == self.daily_date:
            return
        uptime = now() - self.session_start
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes = rem // 60
        d = self.daily
        available_range = "n/a"
        if d["min_available"] is not None:
            if d["min_available"] == d["max_available"]:
                available_range = str(d["min_available"])
            else:
                available_range = f"{d['min_available']}-{d['max_available']}"
        msg = (
            f"Date: {self.daily_date}\n"
            f"Requests: {d['requests']}\n"
            f"Errors: {d['errors']} | Bans: {d['bans']} | Rest breaks: {d['rests']}\n"
            f"Last state: {d['last_state'] or 'n/a'}\n"
            f"Available dates per check: {available_range}\n"
            f"Max in target period: {d['max_in_period']}\n"
            f"Reschedule attempts: {d['reschedule_attempts']}\n"
            f"Session uptime: {hours}h {minutes}m"
        )
        send_notification("DAILY", msg)
        self.daily_date = today
        self._reset_daily()

    def send_first_round_if_needed(self, req_count, state, dates, candidates, detail):
        if self.first_round_reported:
            return
        msg = (
            f"First check complete (request #{req_count})\n"
            f"State: {state}\n"
            f"Target period: {PERIOD_START} to {PERIOD_END} (inclusive)\n"
            f"Available: {self._summarize_dates(dates)}\n"
            f"In target period: {len(candidates)}\n"
            f"{detail}"
        )
        send_notification("STATUS", msg)
        self.first_round_reported = True


def _prune_debug_artifacts():
    """Keep only the newest DEBUG_ARTIFACT_LIMIT files: these are full page
    dumps written on every block/login failure and they pile up unbounded."""
    if DEBUG_ARTIFACT_LIMIT <= 0:
        return
    try:
        files = [os.path.join(DEBUG_DIR, f) for f in os.listdir(DEBUG_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        for stale in sorted(files, key=os.path.getmtime, reverse=True)[DEBUG_ARTIFACT_LIMIT:]:
            os.remove(stale)
    except OSError as e:
        log.debug(f"Could not prune debug artifacts: {e}")


def save_debug_artifacts(label):
    ts = now().strftime("%Y%m%d-%H%M%S")
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
    except OSError as e:
        log.warning(f"\tCould not create {DEBUG_DIR}: {e}")
        return
    # 0600: the HTML dump carries session cookies' side effects (CSRF tokens,
    # personal details) and lands next to the logs.
    base = os.path.join(DEBUG_DIR, f"debug_{label}_{ts}")
    try:
        driver.save_screenshot(f"{base}.png")
        log.info(f"\tDebug screenshot: {base}.png")
    except Exception as e:
        log.warning(f"\tCould not save screenshot: {e}")
    try:
        path = f"{base}.html"
        with open(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.info(f"\tDebug HTML: {path}")
    except Exception as e:
        log.warning(f"\tCould not save HTML: {e}")
    _prune_debug_artifacts()


def should_use_headless():
    if HEADLESS:
        return True
    # DISPLAY is an X11 concept: macOS/Windows run a visible Chrome without it,
    # so only treat "no DISPLAY" as headless-only on Linux.
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        log.info("No DISPLAY set; forcing headless Chrome.")
        return True
    return False


def apply_stealth(drv):
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = { runtime: {} };
"""
        })
    except Exception as e:
        log.warning(f"\tStealth CDP script failed: {e}")


def build_chrome_options():
    # Verified against the live site (2026-09-15): a default headless Chrome
    # fingerprint (HeadlessChrome UA + automation flags) gets a flat 403 from
    # the WAF, while this combination returns 200 headless. Don't drop these.
    options = webdriver.ChromeOptions()
    chrome_bin = (
        CHROME_BIN
        or os.environ.get("CHROME_BIN")
        or os.environ.get("SE_BROWSER_BINARY")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
        or shutil.which("chromium-browser")
    )
    if chrome_bin:
        options.binary_location = chrome_bin
    user_agent = USER_AGENT or os.environ.get("USER_AGENT") or DEFAULT_USER_AGENT
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--lang=en-US")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    if should_use_headless():
        options.add_argument("--headless=new")
    return options


def create_driver():
    options = build_chrome_options()
    chromedriver_path = (
        CHROMEDRIVER_PATH
        or os.environ.get("CHROMEDRIVER_PATH")
        or os.environ.get("SE_CHROMEDRIVER")
        or shutil.which("chromedriver")
    )
    if LOCAL_USE:
        if chromedriver_path:
            service = ChromeService(executable_path=chromedriver_path)
            return webdriver.Chrome(service=service, options=options)
        return webdriver.Chrome(options=options)
    return webdriver.Remote(command_executor=HUB_ADDRESS, options=options)


driver = None


def init_driver():
    global driver
    driver = create_driver()
    apply_stealth(driver)
    return driver


def reset_driver():
    # No `global driver` needed: init_driver() does the rebinding.
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    return init_driver()


def sign_out_quietly():
    try:
        driver.get(SIGN_OUT_LINK)
    except Exception:
        pass


def idle_until_active():
    """Outside ACTIVE_HOURS: sign out, drop the browser and sleep until the
    window opens again (plus jitter, so every restart doesn't hit the site at
    the same second). Sleeping here instead of polling is the cheapest way to
    stay under the site's radar — no requests at all for the hours the site
    isn't worth polling."""
    global driver
    wait_s = seconds_until_active() + random.randint(0, 300)
    resume_at = now() + timedelta(seconds=wait_s)
    log.info(f"Outside ACTIVE_HOURS ({ACTIVE_HOURS}); sleeping {wait_s // 60} min until {resume_at:%Y-%m-%d %H:%M %Z}")
    send_notification("IDLE", f"Outside active hours ({ACTIVE_HOURS}). Next check around {resume_at:%Y-%m-%d %H:%M %Z}.")
    sign_out_quietly()
    try:
        driver.quit()
    except Exception:
        pass
    driver = None
    time.sleep(wait_s)
    try:
        init_driver()
    except Exception as e:
        # Leave driver as None: the login loop below calls reset_driver() on
        # failure, which retries the init with the normal backoff.
        log.warning(f"Driver did not come back up after the idle window: {e}")


# When MAX_LOGIN_ATTEMPTS is exhausted and every single attempt failed with
# the browser's ERR_CONNECTION_REFUSED (the site's own backend refusing the
# TCP connection — not something wrong here), retrying at systemd's fixed
# RestartSec just hammers a backend that's already down and spams Discord
# every couple minutes. Persist a counter across process restarts (systemd
# starts a fresh Python process each time) and sleep progressively longer
# before exiting so the crash loop backs off instead of firing at a fixed
# cadence. Only a successful login touches it — driver init succeeding says
# nothing about the target site being reachable, since it never talks to it.
# The site has been observed flapping (briefly reachable, then refusing
# again), so a single success only halves the level instead of zeroing it —
# otherwise one lucky login throws away the whole ladder and every cycle
# restarts from the noisy 60s floor.
CONNECTION_REFUSED_MARKER = "ERR_CONNECTION_REFUSED"
CONNECTION_BACKOFF_STATE_FILE = "connection_backoff_state.json"
CONNECTION_BACKOFF_BASE_SECONDS = 60
CONNECTION_BACKOFF_MAX_SECONDS = 3600


def load_connection_backoff_state():
    try:
        with open(CONNECTION_BACKOFF_STATE_FILE) as f:
            return json.load(f).get("consecutive_refused_exhaustions", 0)
    except (OSError, ValueError):
        return 0


def save_connection_backoff_state(count):
    try:
        with open(CONNECTION_BACKOFF_STATE_FILE, "w") as f:
            json.dump({"consecutive_refused_exhaustions": count}, f)
    except OSError:
        pass


def decay_connection_backoff_state():
    save_connection_backoff_state(load_connection_backoff_state() // 2)


def connection_backoff_seconds(level):
    return min(
        CONNECTION_BACKOFF_BASE_SECONDS * (2 ** (level - 1)),
        CONNECTION_BACKOFF_MAX_SECONDS,
    )


def exit_with_connection_backoff(errors, stop_msg):
    if errors and all(CONNECTION_REFUSED_MARKER in str(e) for e in errors):
        level = load_connection_backoff_state() + 1
        save_connection_backoff_state(level)
        backoff_s = connection_backoff_seconds(level)
        stop_msg = (
            f"{stop_msg} All {len(errors)} attempts were {CONNECTION_REFUSED_MARKER} "
            f"(the site's backend, not us) — sleeping {backoff_s}s before exit "
            f"(backoff level {level})."
        )
        log.error(stop_msg)
        send_notification("STOP", stop_msg)
        time.sleep(backoff_s)
    else:
        save_connection_backoff_state(0)
        send_notification("STOP", stop_msg)
    sys.exit(1)


def print_effective_config():
    settings = [
        ("config", CONFIG_PATH),
        ("embassy", f"{YOUR_EMBASSY} ({EMBASSY}, facility {FACILITY_ID})"),
        ("target period", f"{PERIOD_START} .. {PERIOD_END}"),
        ("excluded", ", ".join(f"{s.date()}..{e.date()}" for s, e in EXCLUDED_INTERVALS) or "none"),
        ("auto reschedule", f"{AUTO_RESCHEDULE} (only earlier: {ONLY_EARLIER}, max attempts: {MAX_RESCHEDULE_ATTEMPTS or 'unlimited'})"),
        ("timezone", TIMEZONE or "system local"),
        ("active hours", ACTIVE_HOURS or "24/7"),
        ("retry window", f"{RETRY_TIME_L_BOUND:g}-{RETRY_TIME_U_BOUND:g}s (adaptive: {ADAPTIVE_PACING})"),
        ("work/cooldown", f"{WORK_LIMIT_TIME:g}h on, {WORK_COOLDOWN_TIME:g}h off; ban cooldown {BAN_COOLDOWN_TIME:g}h"),
        ("empty list policy", EMPTY_LIST_POLICY),
        ("notifications", "discord" if DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID else "disabled"),
        ("browser", f"headless={should_use_headless()} local={LOCAL_USE}"),
        ("logs", f"{LOG_DIR} (keep {LOG_RETENTION_DAYS} days)"),
    ]
    width = max(len(k) for k, _ in settings)
    for key, value in settings:
        print(f"{key.rjust(width)} : {value}")


def empty_streak_multiplier(empty_streak):
    """How much to stretch the poll interval after `empty_streak` consecutive
    empty date lists. At this consulate an empty list is what the site returns
    while it is warming up to rate-limit us, so polling straight through one at
    the normal cadence is how a soft ban gets earned."""
    if empty_streak <= 1:
        return 1.0
    return min(float(empty_streak), max(1.0, EMPTY_STREAK_BACKOFF_MAX))


def empty_list_is_ban():
    if EMPTY_LIST_POLICY == "ban":
        return True
    if EMPTY_LIST_POLICY == "retry":
        return False
    return empty_list_looks_like_ban()


if __name__ == "__main__":
    setup_logging()
    if CLI_ARGS.check_config:
        print_effective_config()
        sys.exit(0)
    log.info(f"Starting US visa scheduler with config {CONFIG_PATH}")
    driver_attempts = 0
    driver_attempt_errors = []
    while True:
        try:
            init_driver()
            break
        except Exception as e:
            driver_attempts += 1
            driver_attempt_errors.append(e)
            msg = f"Driver init failed ({driver_attempts}/{MAX_LOGIN_ATTEMPTS}): {e}\n{traceback.format_exc()}"
            log.error(msg)
            # ERR_CONNECTION_REFUSED attempts get one summary via the STOP
            # notification below (exit_with_connection_backoff) instead of
            # one Discord message per attempt — same failure, no new info.
            if CONNECTION_REFUSED_MARKER not in str(e):
                send_notification("DRIVER_INIT_FAIL", msg[:1900])
            if driver_attempts >= MAX_LOGIN_ATTEMPTS:
                stop_msg = (
                    f"Driver failed to start {driver_attempts} times. "
                    f"Exiting non-zero — check chromedriver/chromium install."
                )
                log.error(stop_msg)
                exit_with_connection_backoff(driver_attempt_errors, stop_msg)
            time.sleep(retry_sleep_seconds())
    first_loop = True
    END_MSG_TITLE = "STOP"
    end_msg = "Scheduler stopped."
    reporter = RunReporter()
    last_notified_candidates = None
    # Adaptive pacing state: how many polls in a row returned the same set of
    # available dates (nothing moving -> drift back to the slower cadence).
    last_dates_key = None
    unchanged_polls = 0
    # Consecutive polls that came back with an empty date list.
    empty_streak = 0
    # t0/total_time/Req_count drive the WORK_LIMIT_TIME anti-ban cooldown.
    # They must only reset when we deliberately take a cooldown break
    # (BANNED, WORK_LIMIT, or an ACTIVE_HOURS idle window below) — never on a
    # plain re-login (e.g. SessionExpired), or repeated soft blocks would keep
    # zeroing the clock and the cooldown that's supposed to catch that exact
    # pattern would never trigger.
    t0 = time.time()
    total_time = 0
    Req_count = 0
    while 1:
        reporter.maybe_send_daily_report()
        if ACTIVE_RANGES and not in_active_window():
            idle_until_active()
            reset_appointment_page_state()
            first_loop = True
            t0 = time.time()
            total_time = 0
            Req_count = 0
            continue
        if first_loop:
            reset_appointment_page_state()
            login_attempts = 0
            login_attempt_errors = []
            while True:
                try:
                    if driver is None:
                        init_driver()
                    start_process()
                    decay_connection_backoff_state()
                    break
                except Exception as e:
                    login_attempts += 1
                    login_attempt_errors.append(e)
                    msg = f"Login failed ({login_attempts}/{MAX_LOGIN_ATTEMPTS}): {e}\n{traceback.format_exc()}"
                    log.error(msg)
                    # Same rationale as the driver-init loop above: skip the
                    # per-attempt Discord ping for ERR_CONNECTION_REFUSED,
                    # the STOP notification already summarizes the cycle.
                    if CONNECTION_REFUSED_MARKER not in str(e):
                        send_notification("LOGIN_FAIL", msg[:1900])
                    if login_attempts >= MAX_LOGIN_ATTEMPTS:
                        stop_msg = (
                            f"Login failed {login_attempts} times. "
                            f"Exiting non-zero so systemd restarts the service after a cooldown."
                        )
                        log.error(stop_msg)
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        # Non-zero exit: Restart=on-failure (nix/module.nix and
                        # deploy/visa-scheduler.service) only retries on
                        # failure — exit(0) reads as "stopped on purpose" and
                        # the service never comes back.
                        exit_with_connection_backoff(login_attempt_errors, stop_msg)
                    wait_s = retry_sleep_seconds()
                    log.info(f"\tRetrying login in {wait_s}s after browser reset...")
                    reset_driver()
                    time.sleep(wait_s)
            first_loop = False
        Req_count += 1
        try:
            log.info("-" * 60)
            log.info(f"Request count: {Req_count}, Log time: {now()}")
            dates = get_date()
            if dates:
                empty_streak = 0
                reset_empty_probe()
            else:
                empty_streak += 1
            if not dates and empty_list_is_ban():
                state = "BANNED"
                detail = f"Sleeping {BAN_COOLDOWN_TIME} hours before retry."
                reporter.record(state, dates=[], candidates=[])
                reporter.send_first_round_if_needed(Req_count, state, [], [], detail)
                msg = (
                    "List is empty and the appointment page no longer renders for us — "
                    f"probably a soft ban. Sleeping {BAN_COOLDOWN_TIME} hours."
                )
                log.warning(msg)
                send_notification("BAN", msg)
                sign_out_quietly()
                time.sleep(BAN_COOLDOWN_TIME * hour)
                empty_streak = 0
                reset_empty_probe()
                first_loop = True
                t0 = time.time()
                total_time = 0
                Req_count = 0
                continue

            candidates = get_available_dates(dates)
            all_dates = [d.get('date') for d in dates if isinstance(d, dict) and d.get('date')]
            log.info("Available dates: " + (", ".join(all_dates) if all_dates else "none"))

            if not dates:
                # Empty, but the session is demonstrably fine: the consulate
                # simply has nothing open. Keep the normal cadence instead of
                # sleeping for hours and missing the next slot that appears.
                state = "NO_SLOTS"
                detail = "No open days at this consulate right now (session healthy)."
            elif candidates:
                state = "IN_PERIOD"
                mode = "auto-reschedule" if (AUTO_RESCHEDULE and auto_reschedule_enabled) else "notify only"
                detail = (
                    f"Found {len(candidates)} date(s) in period "
                    f"({PERIOD_START_DT.date()} to {PERIOD_END_DT.date()}). Mode: {mode}."
                )
            else:
                state = "MONITORING"
                detail = f"No dates in target period ({PERIOD_START_DT.date()} to {PERIOD_END_DT.date()}). Continuing to monitor."
            log.info(f"State: {state} — {detail}")
            reporter.record(state, dates=dates, candidates=candidates)
            reporter.send_first_round_if_needed(Req_count, state, dates, candidates, detail)

            if candidates:
                found_msg = (
                    f"Found {len(candidates)} date(s) in target period "
                    f"({PERIOD_START_DT.date()} to {PERIOD_END_DT.date()}):\n{', '.join(candidates)}"
                )
                log.info(found_msg)
            if candidates and AUTO_RESCHEDULE and auto_reschedule_enabled:
                target_date, skip_reason = choose_target(candidates)
                if target_date is None:
                    log.info(f"Not attempting a booking: {skip_reason}")
                else:
                    reporter.daily["reschedule_attempts"] += 1
                    log.info(f"Auto-rescheduling to earliest eligible date: {target_date}")
                    try:
                        title, r_msg = reschedule(target_date)
                    except SessionExpired:
                        raise
                    except Exception as e:
                        title = "FAIL"
                        r_msg = f"Reschedule attempt for {target_date} errored: {e}"
                    log.info(r_msg)
                    send_notification(title, r_msg)
                    if title == "SUCCESS":
                        # Booked the earliest in-window slot — stop here.
                        END_MSG_TITLE = "SUCCESS"
                        end_msg = r_msg + "\nStopping — appointment booked."
                        break
                    # FAIL or UNCERTAIN: keep monitoring, but put this date on
                    # cooldown. Every POST spends one of the site's limited
                    # reschedule attempts, and a date that just failed is
                    # almost always already taken.
                    failed_targets[target_date] = time.time()
                    cap = effective_attempt_cap()
                    if cap and reschedule_attempts_used >= cap:
                        auto_reschedule_enabled = False
                        source = (
                            f"the site reports {site_remaining_attempts} remaining"
                            if site_remaining_attempts is not None and site_remaining_attempts <= (MAX_RESCHEDULE_ATTEMPTS or site_remaining_attempts)
                            else f"MAX_RESCHEDULE_ATTEMPTS={MAX_RESCHEDULE_ATTEMPTS}"
                        )
                        send_notification(
                            "LIMIT",
                            f"Used {reschedule_attempts_used} reschedule attempt(s) without success "
                            f"and the cap is {cap} ({source}) — switching to notify-only so the "
                            "site's attempt quota isn't burned. Restart the service to re-enable "
                            "auto-booking.",
                        )
            elif candidates:
                candidates_key = tuple(candidates)
                if candidates_key != last_notified_candidates:
                    send_notification("FOUND", found_msg + "\nReschedule manually on the appointment page.")
                    last_notified_candidates = candidates_key
            else:
                last_notified_candidates = None

            dates_key = tuple(all_dates)
            if dates_key == last_dates_key:
                unchanged_polls += 1
            else:
                unchanged_polls = 0
                last_dates_key = dates_key

            total_time = time.time() - t0
            log.info("Working Time: ~ {:.2f} minutes".format(total_time / minute))
            if WORK_LIMIT_TIME and total_time > WORK_LIMIT_TIME * hour:
                reporter.record_rest()
                send_notification("REST", f"Break-time after {WORK_LIMIT_TIME} hours | Repeated {Req_count} times")
                sign_out_quietly()
                time.sleep(WORK_COOLDOWN_TIME * hour)
                first_loop = True
                t0 = time.time()
                total_time = 0
                Req_count = 0
            else:
                wait_s = retry_sleep_seconds(unchanged_polls)
                multiplier = empty_streak_multiplier(empty_streak)
                if multiplier > 1:
                    wait_s = int(wait_s * multiplier)
                    log.info(
                        f"Retry Wait Time: {wait_s} seconds "
                        f"({multiplier:g}x — {empty_streak} empty list(s) in a row)"
                    )
                else:
                    log.info(f"Retry Wait Time: {wait_s} seconds")
                time.sleep(wait_s)
        except SessionExpired as e:
            state = "SESSION"
            reporter.record(state)
            reporter.send_first_round_if_needed(Req_count, state, [], [], str(e))
            msg = (
                f"Session expired/blocked on request #{Req_count}: {e}\n"
                f"Signing out and re-logging in before the next check."
            )
            log.warning(msg)
            send_notification("SESSION", msg[:1900])
            sign_out_quietly()
            reset_appointment_page_state()
            first_loop = True
            time.sleep(retry_sleep_seconds())
        except Exception as e:
            state = "ERROR"
            reporter.record(state)
            reporter.send_first_round_if_needed(Req_count, state, [], [], str(e))
            msg = f"Error on request #{Req_count}: {e}\n{traceback.format_exc()}"
            log.error(msg)
            send_notification("ERROR", msg[:1900])
            time.sleep(retry_sleep_seconds())

    log.info(end_msg)
    send_notification(END_MSG_TITLE, end_msg)
    try:
        driver.get(SIGN_OUT_LINK)
        if hasattr(driver, "stop_client"):
            driver.stop_client()
        driver.quit()
    except Exception as e:
        # Best-effort cleanup only — must not let a teardown error escape as
        # an unhandled exception, or systemd's Restart=on-failure would spin
        # the service back up and re-fire a reschedule for a date we already
        # booked, burning one of the site's limited scheduling attempts.
        log.warning(f"Post-stop teardown failed (ignored): {e}")
    sys.exit(0)
