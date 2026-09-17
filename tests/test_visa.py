"""Offline unit tests for the pure logic in visa.py.

Run: .venv/bin/python -m unittest discover -s tests -v
Nothing here touches the network or a browser; the module is imported against
config.ini.example so a developer without real credentials can still run it.
"""

import os
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("VISA_CONFIG", os.path.join(ROOT, "config.ini.example"))

import visa  # noqa: E402  (import after VISA_CONFIG is set)


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://ais.usvisa-info.com/x"):
        self.text = text
        self.status_code = status_code
        self.url = url


class ExcludeDatesTest(unittest.TestCase):
    def test_single_days_and_ranges(self):
        intervals = visa.parse_exclude_dates("2027-10-15, 2027-11-01:2027-11-07")
        self.assertEqual(len(intervals), 2)
        self.assertTrue(self._excluded(intervals, "2027-10-15"))
        self.assertTrue(self._excluded(intervals, "2027-11-04"))
        self.assertFalse(self._excluded(intervals, "2027-11-08"))

    def test_reversed_range_is_normalized(self):
        intervals = visa.parse_exclude_dates("2027-11-07:2027-11-01")
        self.assertTrue(self._excluded(intervals, "2027-11-04"))

    def test_blank_is_empty(self):
        self.assertEqual(visa.parse_exclude_dates(""), [])
        self.assertEqual(visa.parse_exclude_dates("  ,  "), [])

    def test_invalid_date_exits(self):
        with self.assertRaises(SystemExit):
            visa.parse_exclude_dates("2027-02-30")

    @staticmethod
    def _excluded(intervals, day):
        dt = datetime.strptime(day, "%Y-%m-%d")
        return any(start <= dt <= end for start, end in intervals)


class AvailableDatesTest(unittest.TestCase):
    def setUp(self):
        # config.ini.example targets 2027-10-01 .. 2027-11-30
        self.dates = [
            {"date": "2027-09-30"},          # before the window
            {"date": "2027-10-05"},
            {"date": "2027-11-30"},          # inclusive upper bound
            {"date": "2027-12-01"},          # after the window
            {"date": "not-a-date"},
            {},
            "junk",
        ]

    def test_filters_to_window_and_sorts(self):
        self.assertEqual(visa.get_available_dates(self.dates), ["2027-10-05", "2027-11-30"])

    def test_unsorted_input_is_sorted(self):
        out = visa.get_available_dates([{"date": "2027-11-02"}, {"date": "2027-10-02"}])
        self.assertEqual(out, ["2027-10-02", "2027-11-02"])

    def test_excluded_dates_are_dropped(self):
        with mock.patch.object(visa, "EXCLUDED_INTERVALS",
                               visa.parse_exclude_dates("2027-10-05")):
            self.assertEqual(visa.get_available_dates(self.dates), ["2027-11-30"])


class RescheduleClassificationTest(unittest.TestCase):
    def test_success_banner(self):
        status, _ = visa._classify_reschedule_response(
            FakeResponse("<p>Your appointment has been successfully scheduled</p>"))
        self.assertEqual(status, "SUCCESS")

    def test_http_error_is_failure(self):
        status, reason = visa._classify_reschedule_response(FakeResponse("boom", status_code=500))
        self.assertEqual(status, "FAIL")
        self.assertIn("500", reason)

    def test_bounced_to_sign_in_is_failure(self):
        status, _ = visa._classify_reschedule_response(
            FakeResponse("<form>", url="https://ais.usvisa-info.com/en-ca/niv/users/sign_in"))
        self.assertEqual(status, "FAIL")

    def test_error_phrase_is_failure(self):
        status, _ = visa._classify_reschedule_response(
            FakeResponse("That time is no longer available"))
        self.assertEqual(status, "FAIL")

    def test_unrecognized_reply_is_uncertain_not_success(self):
        status, _ = visa._classify_reschedule_response(FakeResponse("<html>hello</html>"))
        self.assertEqual(status, "UNCERTAIN")

    def test_response_summary_prefers_flash_message(self):
        body = '<div id="flash_messages">Appointment updated</div><p>ignored</p>'
        self.assertEqual(visa._response_summary(body), "Appointment updated")


class AppointmentDateScrapeTest(unittest.TestCase):
    def test_consular_appt_markup(self):
        html = (
            '<p class="consular-appt">Consular Appointment'
            '<span> 13 November, 2027, 08:30 Toronto local time</span></p>'
        )
        self.assertEqual(visa._extract_appointment_date(html), datetime(2027, 11, 13))

    def test_generic_date_with_time(self):
        self.assertEqual(
            visa._extract_appointment_date("Appointment: 3 Feb, 2028, 09:15 local time"),
            datetime(2028, 2, 3),
        )

    def test_unparseable_returns_none(self):
        self.assertIsNone(visa._extract_appointment_date("<html>no appointment here</html>"))
        self.assertIsNone(visa._extract_appointment_date(""))


class ChooseTargetTest(unittest.TestCase):
    def setUp(self):
        visa.failed_targets.clear()
        self.addCleanup(visa.failed_targets.clear)

    def test_picks_earliest_when_nothing_blocks(self):
        with mock.patch.object(visa, "current_appointment_date", return_value=None):
            self.assertEqual(visa.choose_target(["2027-11-02", "2027-10-02"])[0], "2027-10-02")

    def test_skips_date_on_cooldown(self):
        visa.failed_targets["2027-10-02"] = time.time()
        with mock.patch.object(visa, "current_appointment_date", return_value=None):
            self.assertEqual(visa.choose_target(["2027-10-02", "2027-11-02"])[0], "2027-11-02")

    def test_expired_cooldown_is_eligible_again(self):
        visa.failed_targets["2027-10-02"] = time.time() - (visa.RESCHEDULE_RETRY_COOLDOWN + 1) * 60
        with mock.patch.object(visa, "current_appointment_date", return_value=None):
            self.assertEqual(visa.choose_target(["2027-10-02"])[0], "2027-10-02")

    def test_never_books_on_or_after_the_appointment_we_hold(self):
        held = datetime(2027, 10, 20)
        with mock.patch.object(visa, "current_appointment_date", return_value=held):
            target, reason = visa.choose_target(["2027-10-20", "2027-11-02"])
            self.assertIsNone(target)
            self.assertIn("2027-10-20", reason)
            self.assertEqual(visa.choose_target(["2027-10-05", "2027-11-02"])[0], "2027-10-05")

    def test_all_on_cooldown_reports_why(self):
        visa.failed_targets["2027-10-02"] = time.time()
        with mock.patch.object(visa, "current_appointment_date", return_value=None):
            target, reason = visa.choose_target(["2027-10-02"])
            self.assertIsNone(target)
            self.assertIn("cooldown", reason)


class ActiveHoursTest(unittest.TestCase):
    def test_empty_means_always_active(self):
        with mock.patch.object(visa, "ACTIVE_RANGES", []):
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 3, 0)))
            self.assertEqual(visa.seconds_until_active(datetime(2026, 9, 15, 3, 0)), 0)

    def test_simple_window(self):
        with mock.patch.object(visa, "ACTIVE_RANGES", visa.parse_active_hours("12:00-20:00")):
            self.assertFalse(visa.in_active_window(datetime(2026, 9, 15, 11, 59)))
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 12, 0)))
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 19, 59)))
            self.assertFalse(visa.in_active_window(datetime(2026, 9, 15, 20, 0)))

    def test_window_wrapping_past_midnight(self):
        with mock.patch.object(visa, "ACTIVE_RANGES", visa.parse_active_hours("22:00-06:00")):
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 23, 30)))
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 5, 59)))
            self.assertFalse(visa.in_active_window(datetime(2026, 9, 15, 12, 0)))

    def test_multiple_windows(self):
        with mock.patch.object(visa, "ACTIVE_RANGES", visa.parse_active_hours("08:00-09:00, 13:00-17:00")):
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 8, 30)))
            self.assertFalse(visa.in_active_window(datetime(2026, 9, 15, 10, 0)))
            self.assertTrue(visa.in_active_window(datetime(2026, 9, 15, 16, 59)))

    def test_seconds_until_next_window(self):
        with mock.patch.object(visa, "ACTIVE_RANGES", visa.parse_active_hours("12:00-20:00")):
            self.assertEqual(visa.seconds_until_active(datetime(2026, 9, 15, 11, 30)), 30 * 60)
            # After the window closes, the next opening is tomorrow noon.
            self.assertEqual(visa.seconds_until_active(datetime(2026, 9, 15, 21, 0)), 15 * 3600)

    def test_bad_format_exits(self):
        for bad in ("12:00", "25:00-26:00", "12:00-12:00"):
            with self.assertRaises(SystemExit, msg=bad):
                visa.parse_active_hours(bad)


class RetryPacingTest(unittest.TestCase):
    def test_always_within_configured_bounds(self):
        low, high = sorted((int(visa.RETRY_TIME_L_BOUND), int(visa.RETRY_TIME_U_BOUND)))
        for unchanged in (0, 1, 5, 50):
            for _ in range(50):
                self.assertTrue(low <= visa.retry_sleep_seconds(unchanged) <= high)

    def test_fresh_change_polls_sooner_than_a_long_quiet_stretch(self):
        with mock.patch.object(visa, "ADAPTIVE_PACING", True):
            fresh = max(visa.retry_sleep_seconds(0) for _ in range(200))
            stale = max(visa.retry_sleep_seconds(visa.ADAPTIVE_RAMP_POLLS * 2) for _ in range(200))
            self.assertLess(fresh, stale)

    def test_disabled_uses_full_range(self):
        with mock.patch.object(visa, "ADAPTIVE_PACING", False):
            high = int(max(visa.RETRY_TIME_L_BOUND, visa.RETRY_TIME_U_BOUND))
            self.assertEqual(max(visa.retry_sleep_seconds(0) for _ in range(300)), high)


class ConnectionBackoffTest(unittest.TestCase):
    def test_doubles_and_caps(self):
        self.assertEqual(visa.connection_backoff_seconds(1), 60)
        self.assertEqual(visa.connection_backoff_seconds(2), 120)
        self.assertEqual(visa.connection_backoff_seconds(3), 240)
        self.assertEqual(visa.connection_backoff_seconds(30), visa.CONNECTION_BACKOFF_MAX_SECONDS)


class NotificationThrottleTest(unittest.TestCase):
    def setUp(self):
        visa._notify_history.clear()
        self.addCleanup(visa._notify_history.clear)

    def test_repeat_is_suppressed_then_counted(self):
        with mock.patch.object(visa, "NOTIFY_MIN_INTERVAL", 15):
            self.assertEqual(visa._notification_suppressed("ERROR", "boom on request #1"), (False, 0))
            # Digits are normalized away, so "#2" is the same failure as "#1".
            self.assertEqual(visa._notification_suppressed("ERROR", "boom on request #2"), (True, 1))
            self.assertEqual(visa._notification_suppressed("ERROR", "boom on request #3"), (True, 2))

    def test_booking_titles_are_never_suppressed(self):
        with mock.patch.object(visa, "NOTIFY_MIN_INTERVAL", 15):
            for _ in range(3):
                self.assertEqual(visa._notification_suppressed("SUCCESS", "booked"), (False, 0))

    def test_different_failures_are_not_collapsed(self):
        with mock.patch.object(visa, "NOTIFY_MIN_INTERVAL", 15):
            visa._notification_suppressed("ERROR", "connection refused")
            self.assertEqual(visa._notification_suppressed("ERROR", "timeout waiting for login")[0], False)

    def test_zero_interval_disables_throttle(self):
        with mock.patch.object(visa, "NOTIFY_MIN_INTERVAL", 0):
            for _ in range(3):
                self.assertEqual(visa._notification_suppressed("ERROR", "same")[0], False)


class EmptyListPolicyTest(unittest.TestCase):
    def setUp(self):
        # Each case must exercise a real probe, not a verdict cached by the last.
        visa.reset_empty_probe()
        self.addCleanup(visa.reset_empty_probe)

    def test_healthy_appointment_page_is_not_a_ban(self):
        page = {"status": 200, "url": visa.APPOINTMENT_URL,
                "body": '<form><input name="authenticity_token" value="tok"></form>'}
        with mock.patch.object(visa, "_requests_get_html", return_value=page):
            self.assertFalse(visa.empty_list_looks_like_ban())

    def test_bounce_to_sign_in_is_a_ban(self):
        page = {"status": 200, "url": "https://ais.usvisa-info.com/en-ca/niv/users/sign_in",
                "body": "<input id='user_email'>"}
        with mock.patch.object(visa, "_requests_get_html", return_value=page):
            self.assertTrue(visa.empty_list_looks_like_ban())

    def test_unreachable_page_is_a_ban(self):
        with mock.patch.object(visa, "_requests_get_html", return_value={"status": 0, "error": "nope"}):
            self.assertTrue(visa.empty_list_looks_like_ban())

    def test_block_page_without_form_is_a_ban(self):
        page = {"status": 200, "url": visa.APPOINTMENT_URL, "body": "<h1>403 Forbidden</h1>"}
        with mock.patch.object(visa, "_requests_get_html", return_value=page):
            self.assertTrue(visa.empty_list_looks_like_ban())



class AttemptCapTest(unittest.TestCase):
    def tearDown(self):
        visa.site_remaining_attempts = None

    def test_config_cap_when_site_is_silent(self):
        visa.site_remaining_attempts = None
        with mock.patch.object(visa, "MAX_RESCHEDULE_ATTEMPTS", 6):
            self.assertEqual(visa.effective_attempt_cap(), 6)

    def test_site_quota_wins_when_lower(self):
        visa.site_remaining_attempts = 2
        with mock.patch.object(visa, "MAX_RESCHEDULE_ATTEMPTS", 6):
            self.assertEqual(visa.effective_attempt_cap(), 2)

    def test_config_cap_wins_when_lower(self):
        visa.site_remaining_attempts = 5
        with mock.patch.object(visa, "MAX_RESCHEDULE_ATTEMPTS", 3):
            self.assertEqual(visa.effective_attempt_cap(), 3)

    def test_site_quota_applies_even_with_unlimited_config(self):
        visa.site_remaining_attempts = 2
        with mock.patch.object(visa, "MAX_RESCHEDULE_ATTEMPTS", 0):
            self.assertEqual(visa.effective_attempt_cap(), 2)

    def test_no_cap_at_all(self):
        visa.site_remaining_attempts = None
        with mock.patch.object(visa, "MAX_RESCHEDULE_ATTEMPTS", 0):
            self.assertEqual(visa.effective_attempt_cap(), 0)


class EmptyStreakBackoffTest(unittest.TestCase):
    def test_first_empty_poll_is_not_slowed(self):
        self.assertEqual(visa.empty_streak_multiplier(0), 1.0)
        self.assertEqual(visa.empty_streak_multiplier(1), 1.0)

    def test_streak_stretches_the_interval(self):
        self.assertEqual(visa.empty_streak_multiplier(4), 4.0)

    def test_capped(self):
        with mock.patch.object(visa, "EMPTY_STREAK_BACKOFF_MAX", 8):
            self.assertEqual(visa.empty_streak_multiplier(50), 8.0)

    def test_cap_of_one_disables_the_backoff(self):
        with mock.patch.object(visa, "EMPTY_STREAK_BACKOFF_MAX", 1):
            self.assertEqual(visa.empty_streak_multiplier(50), 1.0)


class EmptyProbeCacheTest(unittest.TestCase):
    def setUp(self):
        visa.reset_empty_probe()
        self.addCleanup(visa.reset_empty_probe)

    def test_probe_runs_once_then_is_cached(self):
        blocked_page = {"status": 200, "url": visa.APPOINTMENT_URL, "body": "<h1>403</h1>"}
        with mock.patch.object(visa, "_requests_get_html", return_value=blocked_page) as probe:
            with mock.patch.object(visa, "EMPTY_PROBE_MIN_INTERVAL", 10):
                self.assertTrue(visa.empty_list_looks_like_ban())
                for _ in range(20):
                    visa.empty_list_looks_like_ban()
        # 20 extra empty polls must not mean 20 extra page loads.
        self.assertEqual(probe.call_count, 1)

    def test_cache_expires(self):
        page = {"status": 200, "url": visa.APPOINTMENT_URL,
                "body": '<input name="authenticity_token" value="t">'}
        with mock.patch.object(visa, "_requests_get_html", return_value=page) as probe:
            with mock.patch.object(visa, "EMPTY_PROBE_MIN_INTERVAL", 10):
                self.assertFalse(visa.empty_list_looks_like_ban())
                visa._empty_probe["at"] -= 11 * 60
                self.assertFalse(visa.empty_list_looks_like_ban())
        self.assertEqual(probe.call_count, 2)

    def test_dates_coming_back_clears_the_cache(self):
        visa._empty_probe.update(at=time.time(), verdict=True)
        visa.reset_empty_probe()
        self.assertIsNone(visa._empty_probe["verdict"])

    def test_explicit_policies_never_probe(self):
        with mock.patch.object(visa, "_requests_get_html") as probe:
            with mock.patch.object(visa, "EMPTY_LIST_POLICY", "ban"):
                self.assertTrue(visa.empty_list_is_ban())
            with mock.patch.object(visa, "EMPTY_LIST_POLICY", "retry"):
                self.assertFalse(visa.empty_list_is_ban())
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
