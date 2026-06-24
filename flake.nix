{
  description = "US Visa appointment scheduler";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = [
      "x86_64-linux"
      "aarch64-linux"
    ];
    forAllSystems = nixpkgs.lib.genAttrs systems;
  in {
    nixosModules.us-visa-scheduler = ./nix/module.nix;

    packages = forAllSystems (system: let
      pkgs = import nixpkgs {inherit system;};
    in {
      default = pkgs.callPackage ./nix/package.nix { };
    });

    apps = forAllSystems (system: let
      pkgs = import nixpkgs {inherit system;};
    in {
      default = {
        type = "app";
        program = "${pkgs.callPackage ./nix/package.nix { }}/bin/us-visa-scheduler";
      };
    });
  };
}
