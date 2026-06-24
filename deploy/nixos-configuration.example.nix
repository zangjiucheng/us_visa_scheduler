# Example: add this to your NixOS flake or configuration.nix
#
# Flake hosts.nix:
#   inputs.us-visa-scheduler.url = "path:/path/to/us_visa_scheduler";
#
#   modules = [
#     us-visa-scheduler.nixosModules.us-visa-scheduler
#     ./deploy/nixos-configuration.example.nix
#   ];

{ ... }:

{
  services.us-visa-scheduler = {
    enable = true;
    # String path on the target machine (pure-eval safe).
    configFile = "/etc/nixos/us-visa-scheduler.ini";
  };
}
