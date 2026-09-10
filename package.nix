{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  bash,
  coreutils,
  tmux,
  libnotify,
  nix,
  nixos-rebuild,
  shellcheck,
}:
stdenvNoCC.mkDerivation {
  pname = "zenos-rebuild";
  version = "1.1.0";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./scripts
      ./tests
    ];
  };
  nativeBuildInputs = [
    makeWrapper
    python3
    shellcheck
  ];
  doCheck = true;
  checkPhase = ''
    PYTHONDONTWRITEBYTECODE=1 ${python3}/bin/python3 -m unittest discover -s tests -v
    ${bash}/bin/bash -n scripts/zenos-rebuild.sh
    shellcheck scripts/zenos-rebuild.sh
  '';
  installPhase = ''
    install -Dm755 scripts/zenos-rebuild.sh "$out/libexec/zenos-rebuild/zenos-rebuild"
    install -Dm644 scripts/snapshot.py "$out/libexec/zenos-rebuild/snapshot.py"
    patchShebangs "$out/libexec/zenos-rebuild/zenos-rebuild"
    # sudo may replace PATH. Pin the interpreter and helper-side commands too.
    substituteInPlace "$out/libexec/zenos-rebuild/zenos-rebuild" \
      --replace-fail 'sudo python3' 'sudo ${python3}/bin/python3'
    substituteInPlace "$out/libexec/zenos-rebuild/snapshot.py" \
      --replace-fail '["nix",' '["${nix}/bin/nix",' \
      --replace-fail '["nixos-rebuild",' '["${nixos-rebuild}/bin/nixos-rebuild",'
    makeWrapper "$out/libexec/zenos-rebuild/zenos-rebuild" "$out/bin/zenos-rebuild" \
      --prefix PATH : ${
        lib.makeBinPath [
          bash
          coreutils
          tmux
          libnotify
        ]
      }
  '';
  meta = {
    description = "Rebuild ZenOS from private materialized ZCFG sources";
    mainProgram = "zenos-rebuild";
    platforms = lib.platforms.linux;
  };
}
