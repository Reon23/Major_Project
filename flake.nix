{
  description = "Python 3.9 project";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
      };
      sysLibs = with pkgs; [
        stdenv.cc.cc
        zstd
        glib
        dbus
        libGL
        wayland
        wayland-protocols
        libdecor
        xorg.libxcb
        xorg.xcbutil
        xorg.xcbutilcursor
        xorg.xcbutilimage
        xorg.xcbutilkeysyms
        xorg.xcbutilrenderutil
        xorg.xcbutilwm
        xorg.libX11
        xorg.libXext
        xorg.libXrender
        xorg.libXcursor
        xorg.libXi
        xorg.libXrandr
        xorg.libXfixes
        libxkbcommon
        fontconfig
        freetype
        harfbuzz
      ];
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages =
          with pkgs;
          [
            python39
            ruff
            mininet
            iproute2
            iptables
            openvswitch
          ]
          ++ sysLibs;
        shellHook = ''
          export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath sysLibs}:$LD_LIBRARY_PATH
          export QT_QPA_PLATFORM=wayland
          export PATH=${pkgs.mininet}/bin:${pkgs.iproute2}/bin:${pkgs.iptables}/bin:${pkgs.openvswitch}/bin:$PATH
          source ./venv/bin/activate
        '';
      };
    };
}
