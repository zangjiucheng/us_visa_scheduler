{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.us-visa-scheduler;
in
{
  options.services.us-visa-scheduler = {
    enable = lib.mkEnableOption "US Visa appointment scheduler daemon";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      description = "Scheduler package to run.";
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "/etc/nixos/us-visa-scheduler.ini";
      example = "/etc/nixos/us-visa-scheduler.ini";
      description = ''
        Absolute path on the target machine to config.ini with credentials,
        embassy, and Discord settings. Use a string path (not a Nix path) so
        evaluation stays pure. Set HEADLESS = True for headless servers.
      '';
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/us-visa-scheduler";
      description = "Writable directory for logs and Selenium cache.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "us-visa-scheduler";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "us-visa-scheduler";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.configFile != null && cfg.configFile != "";
        message = "services.us-visa-scheduler.configFile must be a non-empty path when the service is enabled.";
      }
    ];

    users.groups.${cfg.group} = { };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.dataDir;
      createHome = false;
      description = "US Visa Scheduler service user";
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.group} -"
      "d ${cfg.dataDir}/logs 0750 ${cfg.user} ${cfg.group} -"
      "d ${cfg.dataDir}/.cache 0750 ${cfg.user} ${cfg.group} -"
    ];

    systemd.services.us-visa-scheduler = {
      description = "US Visa appointment scheduler";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      path = with pkgs; [
        pkgs.xvfb-run
        pkgs.xorg.xvfb
      ];

      preStart = ''
        config_src=${lib.escapeShellArg cfg.configFile}
        if [ ! -f "$config_src" ]; then
          echo "us-visa-scheduler: missing config $config_src" >&2
          exit 1
        fi
        ln -sfn "$config_src" ${cfg.dataDir}/config.ini
      '';

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = "${pkgs.xvfb-run}/bin/xvfb-run -a --server-args='-screen 0 1920x1080x24' ${lib.getExe cfg.package}";
        Restart = "always";
        RestartSec = 30;
        Environment = [
          "HOME=${cfg.dataDir}"
          "XDG_CACHE_HOME=${cfg.dataDir}/.cache"
          "CHROME_BIN=${lib.getExe pkgs.chromium}"
          "CHROMEDRIVER_PATH=${lib.getExe pkgs.chromedriver}"
          "SE_OFFLINE=true"
        ];
      };
    };
  };
}
