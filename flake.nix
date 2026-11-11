{
  description = "lw-epub: build EPUB ebooks from LessWrong, Substack, SSC and joecarlsmith.com";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        lw-epub = pkgs.python3Packages.buildPythonApplication {
          pname = "lw-epub";
          version = "0.3.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            requests
            beautifulsoup4
            ebooklib
            lxml
            pillow
          ];
          pythonImportsCheck = [ "lw_epub" ];
          meta = {
            description = "Build EPUB ebooks from LessWrong, Substack, Slate Star Codex and joecarlsmith.com";
            license = pkgs.lib.licenses.mit;
            mainProgram = "lw-epub";
          };
        };
        default = lw-epub;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.default}/bin/lw-epub";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [ requests beautifulsoup4 ebooklib lxml pillow ]))
          ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
