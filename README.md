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
- Run visa.py file, using `python3 visa.py`

## Run as a daemon on NixOS

This repo ships a NixOS module (Chromium + Python deps + systemd service).

### 1. Create your config

```bash
cp config.ini.example /etc/nixos/us-visa-scheduler.ini
# edit credentials, embassy, Discord, and set:
#   HEADLESS = True
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
            configFile = /etc/nixos/us-visa-scheduler.ini;
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
    configFile = /etc/nixos/us-visa-scheduler.ini;
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
tail -f /var/lib/us-visa-scheduler/log_*.txt
```

### Local test (without installing the service)

From the project directory with `config.ini` present:

```bash
nix run . -- 
```

## Run as a daemon on other Linux (systemd)

For non-NixOS Linux servers:

1. Copy and edit `config.ini` (set `HEADLESS = True` on a server without a display).
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
tail -f logs/daemon.log                # stdout/stderr from the daemon
```

To run as a different user or path:

```bash
SERVICE_USER=visa INSTALL_DIR=/opt/us_visa_scheduler ./deploy/install-daemon.sh
```

For a one-off foreground run (without systemd):

```bash
./scripts/run.sh
```

## TODO
- Make timing optimum. (There are lots of unanswered questions. How is the banning algorithm? How can we avoid it? etc.)
- Adding a GUI (Based on PyQt)
- Multi-account support (switching between accounts in Resting times)
- Add a sound alert for different events.
- Extend the embassies list.

## Acknowledgement
Thanks to everyone who participated in this repo. Lots of people are using your excellent product without even appreciating you.
