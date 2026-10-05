{
  description = "Build and compare the Basecamp Campfire ports";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      each = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          inherit pkgs;
          shootout = pkgs.python3Packages.buildPythonApplication {
            pname = "campfire-shootout";
            version = "0.1.0";
            pyproject = true;
            src = pkgs.lib.cleanSourceWith {
              src = ./.;
              filter =
                path: type:
                pkgs.lib.cleanSourceFilter path type
                && !(builtins.elem (baseNameOf path) [
                  "tmp"
                  "result"
                  ".pytest_cache"
                ]);
            };
            build-system = [ pkgs.python3Packages.setuptools ];
            nativeCheckInputs = [
              pkgs.python3Packages.pytestCheckHook
              pkgs.git
            ];
          };
        }
      );
    in
    {
      packages = forAllSystems (system: {
        default = each.${system}.shootout;
        campfire-shootout = each.${system}.shootout;
      });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${each.${system}.shootout}/bin/campfire-shootout";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = each.${system}.pkgs;
        in
        {
          default = pkgs.mkShell {
            packages = [
              each.${system}.shootout
              pkgs.python3Packages.pytest
              pkgs.rustc
              pkgs.cargo
              pkgs.pkg-config
              pkgs.openssl
              pkgs.sqlite
              pkgs.curl
              pkgs.util-linux
              pkgs.git
              pkgs.jq
            ];
            env = {
              RUST_BACKTRACE = "short";
            };
            shellHook = ''
              if [ -z "''${CAMPFIRE_ROOT:-}" ]; then
                if [ -d once-campfire ] && [ -d once-campfire-rust ]; then
                  export CAMPFIRE_ROOT="$PWD"
                elif [ -d ../once-campfire ] && [ -d ../once-campfire-rust ]; then
                  export CAMPFIRE_ROOT="$(cd .. && pwd)"
                fi
              fi
              echo "campfire-shootout"
              echo "  workspace: ''${CAMPFIRE_ROOT:-not found}"
              echo "  campfire-shootout list"
            '';
          };
        }
      );

      formatter = forAllSystems (system: each.${system}.pkgs.nixfmt);
    };
}
