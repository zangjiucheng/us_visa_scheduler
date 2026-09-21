# visa_rescheduler
The visa_rescheduler is a bot for US VISA (usvisa-info.com) appointment rescheduling. This bot can help you reschedule your appointment to your desired time period.

## Prerequisites
- Having a US VISA appointment scheduled already.
- [Optional] A Discord bot in your server (for notifications)

## Attention
- Right now, there are lots of unsupported embassies in our repository. A list of supported embassies is presented in the 'embassy.py' file.
- To add a new embassy (using English), you should find the embassy's "facility id." To do this, using google chrome, on the booking page of your account, right-click on the location section, then click "inspect." Then the right-hand window will be opened, highlighting the "select" item. You can find the "facility id" here and add this facility id in the 'embassy.py' file. There might be several facility ids for several different embassies. They can be added too. Please use the picture below as an illustration of the process.
![Finding Facility id](https://github.com/Soroosh-N/us_visa_scheduler/blob/main/_img.png?raw=true)

## Initial Setup
- Install Google Chrome [for install goto: https://www.google.com/chrome/]
- Install Python v3 [for install goto: https://www.python.org/downloads/]
- Install the required python packages:
```
pip install -r requirements.txt
```
(selenium 4.6+ ships Selenium Manager, so the matching chromedriver is fetched automatically — no separate webdriver-manager needed.)

## How to use
- Initial setup!
- Edit information [config.ini.example file]. Then remove the ".example" from file name.
- [Optional] Set up a Discord bot and add `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` in `config.ini` (see comments in `config.ini.example`).
- Check the config before starting: `python3 visa.py --check-config` prints the effective settings and exits.
- Run visa.py file, using `python3 visa.py`

The config is looked up in this order: `--config <path>`, `$VISA_CONFIG`, `./config.ini`,
then the copy next to `visa.py` — so the service works from any working directory.

## Configuration worth knowing about

| Key | What it does |
| --- | --- |
| `[TIME] ACTIVE_HOURS` | Only poll during these local-time windows (e.g. `12:00-20:00`, or several comma-separated ranges; a range may wrap past midnight). Outside them the bot signs out, closes the browser and sleeps. Far fewer requests = far less ban exposure. Blank = around the clock. |
| `[TIME] TIMEZONE` | IANA zone (e.g. `America/Toronto`) that `ACTIVE_HOURS` and the daily report are measured in, so a UTC server still lines up with the consulate's clock. |
| `[PERSONAL_INFO] ONLY_EARLIER` | Never book a date at or after the appointment you already hold, even if it is inside the target window. |
| `[PERSONAL_INFO] MAX_RESCHEDULE_ATTEMPTS` | Fall back to notify-only after this many failed bookings, so a contended date can't burn the site's limited reschedule quota. |
| `[TIME] RESCHEDULE_RETRY_COOLDOWN` | Minutes before the same date is attempted again after a failure. |
| `[TIME] EMPTY_LIST_POLICY` | `auto` (default) checks whether the appointment page still renders for us before deciding an empty date list is a ban rather than "the consulate has nothing open". `ban` restores the old always-sleep behaviour. |
| `[TIME] EMPTY_PROBE_MIN_INTERVAL` | Minutes the `auto` verdict is cached. The probe is a full page load, so running it on every empty poll is itself enough traffic to get rate-limited. |
| `[TIME] EMPTY_STREAK_BACKOFF_MAX` | Consecutive empty lists stretch the poll interval by the streak length, capped at this multiple. An empty list is usually the site warming up to rate-limit you, so polling straight through one at full speed is how a soft ban gets earned. |
| `[TIME] NOTIFY_MIN_INTERVAL` | Collapses repeated identical error notifications; booking-relevant ones are never collapsed. |
| `[TIME] ADAPTIVE_PACING` | Polls a little sooner right after the date list changes, drifting back to `RETRY_TIME_U_BOUND` while nothing moves. Always stays inside the configured bounds. |
| `[TIME] WORK_LIMIT_TIME` / `WORK_COOLDOWN_TIME` | Hours of polling, then hours of break. The break is a blind spot — see *Coverage* below. `WORK_LIMIT_TIME = 0` disables it. |
| `[LOGGING] LOG_DIR` / `LOG_RETENTION_DAYS` | Rotating daily log at `LOG_DIR/visa.log`, page dumps at `LOG_DIR/debug/`. |

### Coverage

What catches a cancellation is not how fast you poll, it is how much of the
clock you are watching. Work out the real number before tuning `RETRY_TIME_*`:

```
coverage = WORK_LIMIT_TIME / (WORK_LIMIT_TIME + WORK_COOLDOWN_TIME)
worst-case blind gap = WORK_COOLDOWN_TIME
```

A measured example from a live deployment: `WORK_LIMIT_TIME = 1`,
`WORK_COOLDOWN_TIME = 2` and a 10-hour `ACTIVE_HOURS` window gave 296 polls a
night at a 37-second median interval — but only 3.2h of coverage (32%), in
three stretches separated by 135-minute blind gaps. Moving to `3` / `0.5` and
widening `RETRY_TIME_*` from `10-60` to `30-120` raised coverage to 86% and cut
the worst gap to 30 minutes for about 40% more requests. Slower polls over the
whole window beat a fast burst followed by a long blind spot.

After a reschedule POST the bot reads the appointment back off the account, so a
"success" banner that didn't actually move anything is reported as a failure and
an unrecognised reply usually resolves to a definite answer instead of `UNCERTAIN`.

## Tests

The date/window/classification logic is covered by offline unit tests (no network,
no browser — they load `config.ini.example`):

```
python3 -m unittest discover -s tests
```

## Run as a daemon on NixOS

This repo ships a NixOS module (Chromium + Python deps + systemd service).

### 1. Create your config

```bash
cp config.ini.example /etc/nixos/us-visa-scheduler.ini
# edit credentials, embassy, Discord. The service runs under xvfb-run, so
# HEADLESS can stay False there.
```

### 2. Enable the module in your flake

```nix
{
  inputs.us-visa-scheduler.url = "path:/path/to/us_visa_scheduler";

  outputs = { self, nixpkgs, us-visa-scheduler, ... }: {
    nixosConfigurations.myserver = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        us-visa-scheduler.nixosModules.us-visa-scheduler
        {
          services.us-visa-scheduler = {
            enable = true;
            configFile = "/etc/nixos/us-visa-scheduler.ini";
          };
        }
      ];
    };
  };
}
```

Without a flake, import the module directly:

```nix
{ ... }:

{
  imports = [ /path/to/us_visa_scheduler/nix/module.nix ];

  services.us-visa-scheduler = {
    enable = true;
    configFile = "/etc/nixos/us-visa-scheduler.ini";
  };
}
```

### 3. Deploy

```bash
sudo nixos-rebuild switch
```

### Service commands

```bash
sudo systemctl status us-visa-scheduler
sudo systemctl restart us-visa-scheduler
sudo journalctl -u us-visa-scheduler -f
tail -f /var/lib/us-visa-scheduler/logs/visa.log
```

### Local test (without installing the service)

From the project directory with `config.ini` present:

```bash
nix run . -- 
```

## Run as a daemon on other Linux (systemd)

For non-NixOS Linux servers:

1. Copy and edit `config.ini` (`HEADLESS = True` on a server without a display).
2. Install Google Chrome or Chromium on the server.
3. Install and enable the systemd service:

```bash
chmod +x deploy/install-daemon.sh scripts/run.sh
./deploy/install-daemon.sh
sudo systemctl start visa-scheduler
```

Useful commands:

```bash
sudo systemctl status visa-scheduler   # service status
sudo systemctl restart visa-scheduler  # restart after config changes
tail -f logs/visa.log                  # rotating log written by visa.py
sudo journalctl -u visa-scheduler -f   # stdout/stderr from the daemon
```

The unit uses `Restart=on-failure` on purpose: `visa.py` exits 0 once the
appointment is booked, and `Restart=always` would bring it back up and re-fire a
reschedule for the date it just booked.

To run as a different user or path:

```bash
SERVICE_USER=visa INSTALL_DIR=/opt/us_visa_scheduler ./deploy/install-daemon.sh
```

For a one-off foreground run (without systemd):

```bash
./scripts/run.sh
```

## TODO
- Make timing optimum. (`ACTIVE_HOURS` + `ADAPTIVE_PACING` are a start; the banning algorithm itself is still guesswork.)
- Adding a GUI (Based on PyQt)
- Multi-account support (switching between accounts in Resting times)
- Add a sound alert for different events.
- Extend the embassies list.

## Acknowledgement
Thanks to everyone who participated in this repo. Lots of people are using your excellent product without even appreciating you.
