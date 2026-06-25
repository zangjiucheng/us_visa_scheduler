import time
import json
import random
import re
import os
import sys
import shutil
import traceback
import requests
import configparser
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait as Wait
from selenium.webdriver.common.by import By

from embassy import *

config = configparser.ConfigParser()
config.read('config.ini')

# Personal Info:
# Account and current appointment info from https://ais.usvisa-info.com
USERNAME = config['PERSONAL_INFO']['USERNAME']
PASSWORD = config['PERSONAL_INFO']['PASSWORD']
# Find SCHEDULE_ID in re-schedule page link:
# https://ais.usvisa-info.com/en-am/niv/schedule/{SCHEDULE_ID}/appointment
SCHEDULE_ID = config['PERSONAL_INFO']['SCHEDULE_ID']
# Target Period:
PERIOD_START = config['PERSONAL_INFO']['PERIOD_START']
PERIOD_END = config['PERSONAL_INFO']['PERIOD_END']

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
# When True, automatically reschedule to the earliest date found inside the
# target window and then stop. When False, only notify (manual reschedule).
AUTO_RESCHEDULE = config['PERSONAL_INFO'].getboolean('AUTO_RESCHEDULE', fallback=True)
# Embassy Section:
YOUR_EMBASSY = config['PERSONAL_INFO']['YOUR_EMBASSY'] 
EMBASSY = Embassies[YOUR_EMBASSY][0]
FACILITY_ID = Embassies[YOUR_EMBASSY][1]
REGEX_CONTINUE = Embassies[YOUR_EMBASSY][2]

# Notification via Discord bot (https://discord.com/developers/applications)
DISCORD_BOT_TOKEN = config['NOTIFICATION']['DISCORD_BOT_TOKEN']
DISCORD_CHANNEL_ID = config['NOTIFICATION']['DISCORD_CHANNEL_ID']

# Time Section:
minute = 60
hour = 60 * minute
# Time between steps (interactions with forms)
STEP_TIME = 0.5
# Time between retries/checks for available dates (seconds)
RETRY_TIME_L_BOUND = config['TIME'].getfloat('RETRY_TIME_L_BOUND')
RETRY_TIME_U_BOUND = config['TIME'].getfloat('RETRY_TIME_U_BOUND')
# Cooling down after WORK_LIMIT_TIME hours of work (Avoiding Ban)
WORK_LIMIT_TIME = config['TIME'].getfloat('WORK_LIMIT_TIME')
WORK_COOLDOWN_TIME = config['TIME'].getfloat('WORK_COOLDOWN_TIME')
# Temporary Banned (empty list): wait COOLDOWN_TIME hours
BAN_COOLDOWN_TIME = config['TIME'].getfloat('BAN_COOLDOWN_TIME')

# CHROMEDRIVER
# Details for the script to control Chrome
LOCAL_USE = config['CHROMEDRIVER'].getboolean('LOCAL_USE')
HEADLESS = config['CHROMEDRIVER'].getboolean('HEADLESS', fallback=False)
CHROME_BIN = config['CHROMEDRIVER'].get('CHROME_BIN', '').strip()
CHROMEDRIVER_PATH = config['CHROMEDRIVER'].get('CHROMEDRIVER_PATH', '').strip()
USER_AGENT = config['CHROMEDRIVER'].get('USER_AGENT', '').strip()
MAX_LOGIN_ATTEMPTS = config['CHROMEDRIVER'].getint('MAX_LOGIN_ATTEMPTS', fallback=3)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Optional: HUB_ADDRESS is mandatory only when LOCAL_USE = False
HUB_ADDRESS = config['CHROMEDRIVER']['HUB_ADDRESS']

SIGN_IN_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_in"
APPOINTMENT_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment"
DATE_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{FACILITY_ID}.json?appointments[expedite]=false"
TIME_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{FACILITY_ID}.json?date=%s&appointments[expedite]=false"
SIGN_OUT_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_out"

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


def _requests_fetch(url):
    """Fallback: fetch the same URL with Python requests, reusing EVERY cookie
    the browser holds (incl. WAF clearance cookies) plus its User-Agent. This is
    the same auth path reschedule() uses, and it sidesteps in-page restrictions
    (CSP / cross-origin redirects) that can break the in-browser XHR."""
    try:
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        headers = {
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": APPOINTMENT_URL,
        }
        r = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        return {"status": r.status_code, "url": r.url, "body": r.text}
    except Exception as e:
        return {"status": 0, "error": f"requests fetch failed: {e}"}


def _requests_get_html(url):
    """Like _requests_fetch but asks for an HTML document (no X-Requested-With,
    HTML Accept) so Rails returns the rendered page rather than a JS/JSON reply.
    Used to scrape the reschedule form's hidden fields when the browser DOM is
    unavailable (e.g. the browser is on a WAF block page)."""
    try:
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        headers = {
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": APPOINTMENT_URL,
        }
        r = requests.get(url, headers=headers, cookies=cookies, timeout=30)
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
        print(f"\t(in-browser XHR blocked, served via requests fallback) — {xhr_err}")
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


def reset_appointment_page_state():
    global appointment_page_ready, scheduling_limit_notified
    appointment_page_ready = False
    scheduling_limit_notified = False


def is_scheduling_limit_warning():
    return "Scheduling Limit Warning" in driver.page_source


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
    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": APPOINTMENT_URL,
        "Cookie": "_yatri_session=" + driver.get_cookie("_yatri_session")["value"],
    }
    data = {
        "utf8": driver.find_element(by=By.NAME, value="utf8").get_attribute("value"),
        "authenticity_token": driver.find_element(by=By.NAME, value="authenticity_token").get_attribute("value"),
        "confirmed_limit_message": "1",
        "commit": "Continue",
    }
    requests.post(APPOINTMENT_URL, headers=headers, data=data, timeout=30)
    driver.get(APPOINTMENT_URL)
    time.sleep(STEP_TIME)


def dismiss_scheduling_limit_warning():
    global scheduling_limit_notified
    if not is_scheduling_limit_warning():
        return False

    print("\tScheduling Limit Warning detected, dismissing...")
    remaining = re.search(r"You have (\d+) remaining attempt", driver.page_source)
    _acknowledge_scheduling_limit_checkbox()

    if _click_scheduling_limit_continue() and not is_scheduling_limit_warning():
        print("\tScheduling Limit Warning dismissed via Continue button.")
    elif not is_scheduling_limit_warning():
        pass
    else:
        print("\tContinue button not found or ineffective, submitting warning form via POST...")
        _submit_scheduling_limit_warning()

    if is_scheduling_limit_warning():
        print("\tPOST did not clear warning, trying direct URL...")
        driver.get(f"{APPOINTMENT_URL}?confirmed_limit_message=1&commit=Continue")
        time.sleep(STEP_TIME)

    if is_scheduling_limit_warning():
        raise RuntimeError("Could not dismiss scheduling limit warning")

    Wait(driver, 30).until(lambda d: not is_scheduling_limit_warning())
    print("\tScheduling Limit Warning dismissed.")
    if remaining and not scheduling_limit_notified:
        send_notification(
            "LIMIT",
            f"Scheduling limit warning acknowledged. {remaining.group(1)} reschedule attempt(s) remaining.",
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

def send_notification(title, msg):
    print("Sending notification!")
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print("Discord not configured, skipping notification.")
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    content = f"**VISA - {title}**\n{msg}"
    if len(content) > 2000:
        content = content[:1997] + "..."

    try:
        response = requests.post(url, headers=headers, json={"content": content}, timeout=30)
        response.raise_for_status()
        print("Discord notification sent.")
    except requests.RequestException as e:
        print(f"Discord notification failed: {e}")


def auto_action(label, find_by, el_type, action, value, sleep_time=0):
    print("\t"+ label +":", end="")
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
    print("\t\tCheck!")
    if sleep_time:
        time.sleep(sleep_time)


def is_blocked_page():
    title = (driver.title or "").lower()
    if "403" in title or "forbidden" in title:
        return True
    snippet = driver.page_source[:4000].lower()
    return "403 forbidden" in snippet or "access denied" in snippet


def start_process():
    print(f"\tOpening sign-in: {SIGN_IN_LINK}")
    driver.get(SIGN_IN_LINK)
    time.sleep(STEP_TIME)
    if is_blocked_page():
        save_debug_artifacts("403-forbidden")
        raise RuntimeError(
            "Site returned 403 Forbidden (bot/WAF block). "
            "Set HEADLESS = False in config.ini (NixOS service uses xvfb-run), "
            "or run the scheduler from your home computer instead of the server."
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
        print("\tNo bounce arrow on page, continuing.")
    auto_action("Email", "id", "user_email", "send", USERNAME, STEP_TIME)
    auto_action("Password", "id", "user_password", "send", PASSWORD, STEP_TIME)
    auto_action("Privacy", "class", "icheckbox", "click", "", STEP_TIME)
    auto_action("Enter Panel", "name", "commit", "click", "", STEP_TIME)
    Wait(driver, 90).until(EC.presence_of_element_located((By.XPATH, "//a[contains(text(), '" + REGEX_CONTINUE + "')]")))
    print("\n\tlogin successful!\n")

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


def reschedule(date):
    ensure_appointment_page_ready()
    time_slot = get_time(date)
    form = _reschedule_form_fields()
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": APPOINTMENT_URL,
    }
    data = {
        **form,
        "appointments[consulate_appointment][facility_id]": FACILITY_ID,
        "appointments[consulate_appointment][date]": date,
        "appointments[consulate_appointment][time]": time_slot,
    }
    r = requests.post(APPOINTMENT_URL, headers=headers, cookies=cookies, data=data, timeout=60)
    if r.text.find('Successfully Scheduled') != -1:
        return ["SUCCESS", f"Rescheduled Successfully! {date} {time_slot}"]
    return ["FAIL", f"Reschedule Failed!!! {date} {time_slot} (HTTP {r.status_code})"]


def get_date():
    global appointment_page_ready
    ensure_appointment_page_ready()
    try:
        return fetch_json(DATE_URL)
    except RuntimeError:
        # A non-JSON page can mean the scheduling-limit warning re-appeared;
        # re-prime the appointment page once and retry.
        if is_scheduling_limit_warning():
            appointment_page_ready = False
            ensure_appointment_page_ready()
            return fetch_json(DATE_URL)
        raise

def get_time(date):
    ensure_appointment_page_ready()
    time_url = TIME_URL % date
    data = fetch_json(time_url)
    available_times = data.get("available_times") or []
    if not available_times:
        raise RuntimeError(f"No available times for {date}: {data}")
    time_slot = available_times[-1]
    print(f"Got time successfully! {date} {time_slot}")
    return time_slot


def is_logged_in():
    content = driver.page_source
    if(content.find("error") != -1):
        return False
    return True


def get_available_dates(dates):
    matches = []
    for d in dates:
        date = d.get('date')
        if not date:
            continue
        new_date = datetime.strptime(date, "%Y-%m-%d")
        if PERIOD_END_DT > new_date > PERIOD_START_DT:
            matches.append(date)
    return matches, PERIOD_START_DT, PERIOD_END_DT


def info_logger(file_path, log):
    # file_path: e.g. "log.txt"
    with open(file_path, "a") as file:
        file.write(str(datetime.now().time()) + ":\n" + log + "\n")


class RunReporter:
    def __init__(self):
        self.session_start = datetime.now()
        self.first_round_reported = False
        self.daily_date = datetime.now().date()
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
        today = datetime.now().date()
        if today == self.daily_date:
            return
        uptime = datetime.now() - self.session_start
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
            f"Target period: {PERIOD_START} to {PERIOD_END} (exclusive)\n"
            f"Available: {self._summarize_dates(dates)}\n"
            f"In target period: {len(candidates)}\n"
            f"{detail}"
        )
        send_notification("STATUS", msg)
        self.first_round_reported = True


def save_debug_artifacts(label):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"debug_{label}_{ts}"
    try:
        driver.save_screenshot(f"{base}.png")
        print(f"\tDebug screenshot: {base}.png")
    except Exception as e:
        print(f"\tCould not save screenshot: {e}")
    try:
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"\tDebug HTML: {base}.html")
    except Exception as e:
        print(f"\tCould not save HTML: {e}")


def should_use_headless():
    if HEADLESS:
        return True
    if os.environ.get("DISPLAY"):
        print(f"Using headed Chrome on DISPLAY={os.environ['DISPLAY']}")
        return False
    print("No DISPLAY set; forcing headless Chrome.")
    return True


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
        print(f"\tStealth CDP script failed: {e}")


def build_chrome_options():
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
    global driver
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    return init_driver()


if __name__ == "__main__":
    init_driver()
    first_loop = True
    END_MSG_TITLE = "STOP"
    reporter = RunReporter()
    last_notified_candidates = None
    while 1:
        LOG_FILE_NAME = "log_" + str(datetime.now().date()) + ".txt"
        reporter.maybe_send_daily_report()
        if first_loop:
            t0 = time.time()
            total_time = 0
            Req_count = 0
            reset_appointment_page_state()
            login_attempts = 0
            while True:
                try:
                    start_process()
                    break
                except Exception as e:
                    login_attempts += 1
                    msg = f"Login failed ({login_attempts}/{MAX_LOGIN_ATTEMPTS}): {e}\n{traceback.format_exc()}"
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                    send_notification("LOGIN_FAIL", msg[:1900])
                    if login_attempts >= MAX_LOGIN_ATTEMPTS:
                        stop_msg = (
                            f"Login failed {login_attempts} times. "
                            f"Stopping scheduler — fix config/network and restart the service."
                        )
                        print(stop_msg)
                        info_logger(LOG_FILE_NAME, stop_msg)
                        send_notification("STOP", stop_msg)
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        sys.exit(0)
                    retry_low = int(RETRY_TIME_L_BOUND)
                    retry_high = int(RETRY_TIME_U_BOUND)
                    if retry_low > retry_high:
                        retry_low, retry_high = retry_high, retry_low
                    wait_s = random.randint(retry_low, retry_high)
                    print(f"\tRetrying login in {wait_s}s after browser reset...")
                    reset_driver()
                    time.sleep(wait_s)
            first_loop = False
        Req_count += 1
        try:
            msg = "-" * 60 + f"\nRequest count: {Req_count}, Log time: {datetime.today()}\n"
            print(msg)
            info_logger(LOG_FILE_NAME, msg)
            dates = get_date()
            if not dates:
                state = "BANNED"
                detail = f"Sleeping {BAN_COOLDOWN_TIME} hours before retry."
                reporter.record(state, dates=[], candidates=[])
                reporter.send_first_round_if_needed(Req_count, state, [], [], detail)
                msg = f"List is empty, Probabely banned!\n\tSleep for {BAN_COOLDOWN_TIME} hours!\n"
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                send_notification("BAN", msg)
                driver.get(SIGN_OUT_LINK)
                time.sleep(BAN_COOLDOWN_TIME * hour)
                first_loop = True
            else:
                candidates, PSD, PED = get_available_dates(dates)
                msg = ""
                for d in dates:
                    msg = msg + "%s" % (d.get('date')) + ", "
                msg = "Available dates:\n"+ msg
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                if candidates:
                    state = "IN_PERIOD"
                    mode = "auto-reschedule" if AUTO_RESCHEDULE else "notify only"
                    detail = (
                        f"Found {len(candidates)} date(s) in period "
                        f"({PSD.date()} to {PED.date()}). Mode: {mode}."
                    )
                else:
                    state = "MONITORING"
                    detail = f"No dates in target period ({PSD.date()} to {PED.date()}). Continuing to monitor."
                reporter.record(state, dates=dates, candidates=candidates)
                reporter.send_first_round_if_needed(Req_count, state, dates, candidates, detail)
                if candidates:
                    target_date = min(candidates)
                    dates_list = ", ".join(candidates)
                    msg = (
                        f"Found {len(candidates)} date(s) in target period "
                        f"({PSD.date()} to {PED.date()}):\n{dates_list}"
                    )
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                    if AUTO_RESCHEDULE:
                        reporter.daily["reschedule_attempts"] += 1
                        print(f"Auto-rescheduling to earliest in-period date: {target_date}")
                        try:
                            title, r_msg = reschedule(target_date)
                        except SessionExpired:
                            raise
                        except Exception as e:
                            title = "FAIL"
                            r_msg = f"Reschedule attempt for {target_date} errored: {e}"
                        print(r_msg)
                        info_logger(LOG_FILE_NAME, r_msg)
                        send_notification(title, r_msg)
                        if title == "SUCCESS":
                            # Booked the earliest in-window slot — stop here.
                            END_MSG_TITLE = "SUCCESS"
                            msg = r_msg + "\nStopping — appointment booked."
                            break
                        # FAIL: keep monitoring; the date may already be taken.
                    else:
                        candidates_key = tuple(candidates)
                        if candidates_key != last_notified_candidates:
                            notify = msg + "\nReschedule manually on the appointment page."
                            send_notification("FOUND", notify)
                            last_notified_candidates = candidates_key
                else:
                    last_notified_candidates = None
                    msg = f"No available dates between ({PSD.date()}) and ({PED.date()})!"
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                retry_low = int(RETRY_TIME_L_BOUND)
                retry_high = int(RETRY_TIME_U_BOUND)
                if retry_low > retry_high:
                    retry_low, retry_high = retry_high, retry_low
                RETRY_WAIT_TIME = random.randint(retry_low, retry_high)
                t1 = time.time()
                total_time = t1 - t0
                msg = "\nWorking Time:  ~ {:.2f} minutes".format(total_time/minute)
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                if total_time > WORK_LIMIT_TIME * hour:
                    reporter.record_rest()
                    send_notification("REST", f"Break-time after {WORK_LIMIT_TIME} hours | Repeated {Req_count} times")
                    driver.get(SIGN_OUT_LINK)
                    time.sleep(WORK_COOLDOWN_TIME * hour)
                    first_loop = True
                else:
                    msg = "Retry Wait Time: "+ str(RETRY_WAIT_TIME)+ " seconds"
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                    time.sleep(RETRY_WAIT_TIME)
        except SessionExpired as e:
            state = "SESSION"
            detail = str(e)
            reporter.record(state)
            reporter.send_first_round_if_needed(Req_count, state, [], [], detail)
            msg = (
                f"Session expired/blocked on request #{Req_count}: {e}\n"
                f"Signing out and re-logging in before the next check."
            )
            print(msg)
            info_logger(LOG_FILE_NAME, msg)
            send_notification("SESSION", msg[:1900])
            try:
                driver.get(SIGN_OUT_LINK)
            except Exception:
                pass
            reset_appointment_page_state()
            first_loop = True
            retry_low = int(RETRY_TIME_L_BOUND)
            retry_high = int(RETRY_TIME_U_BOUND)
            if retry_low > retry_high:
                retry_low, retry_high = retry_high, retry_low
            time.sleep(random.randint(retry_low, retry_high))
        except Exception as e:
            state = "ERROR"
            detail = str(e)
            reporter.record(state)
            reporter.send_first_round_if_needed(Req_count, state, [], [], detail)
            msg = f"Error on request #{Req_count}: {e}\n{traceback.format_exc()}"
            print(msg)
            info_logger(LOG_FILE_NAME, msg)
            send_notification("ERROR", msg[:1900])
            retry_low = int(RETRY_TIME_L_BOUND)
            retry_high = int(RETRY_TIME_U_BOUND)
            if retry_low > retry_high:
                retry_low, retry_high = retry_high, retry_low
            time.sleep(random.randint(retry_low, retry_high))

    print(msg)
    info_logger(LOG_FILE_NAME, msg)
    send_notification(END_MSG_TITLE, msg)
    driver.get(SIGN_OUT_LINK)
    if hasattr(driver, "stop_client"):
        driver.stop_client()
    driver.quit()
