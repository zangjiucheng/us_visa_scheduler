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
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/nixos/us-visa-scheduler.ini";
      description = ''
        Path to config.ini with credentials, embassy, and Discord settings.
        Set HEADLESS = True for headless servers.
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
        assertion = cfg.configFile != null;
        message = "services.us-visa-scheduler.configFile must be set when the service is enabled.";
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
      "L+ ${cfg.dataDir}/config.ini - - - - ${cfg.configFile}"
    ];

    systemd.services.us-visa-scheduler = {
      description = "US Visa appointment scheduler";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = lib.getExe cfg.package;
        Restart = "always";
        RestartSec = 30;
        Environment = [
          "HOME=${cfg.dataDir}"
          "SE_CACHE_PATH=${cfg.dataDir}/.cache/selenium"
          "XDG_CACHE_HOME=${cfg.dataDir}/.cache"
        ];
      };
    };
  };
}
