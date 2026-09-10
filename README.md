# ZenOS Rebuild

`zenos-rebuild [-d CONFIG_ROOT] [-h HOST] [-r | -l] [--show-generated]`
discovers the configuration from the explicit directory, current directory,
last successful discovery, `/Config/ZenOS`, then `/etc/nixos`. It keeps the
canonical `/Config/ZenOS` spelling in discovery state, uses a tmux session,
prints build logs, records private timestamped logs, and sends notifications.
Reboot/logout are requested only after a successful switch. No per-user
`NIX_PROFILE` override is installed or exported.

## Source Contract

Editable configuration contains `flake.nix`, optional `flake.lock`, and
`hosts/<hostname>/host.zcfg` with split ZCFG and Setup JSON metadata.
`README.md`, `.gitignore` and root `.git` metadata are also accepted; `.git` is
excluded from the snapshot. Other root entries and generated host Nix are
rejected. This is the current DSL contract, not a legacy `host.nix` fallback.

Canonical user entries are absolute links from
`hosts/<host>/users/<user>/<file>.zcfg` to
`/Users/<user>/.private/Config/<file>.zcfg`. Rebuild reads them through
descriptor-relative, no-follow directory/file opens. Other symlinks, redirected
canonical parents/files, and special files fail before Nix evaluation.
The privileged helper creates and owns a mode-0700 temporary directory,
copies regular sources with mode 0600, and passes only its materialized
`path:` flake to Nix. It locks offline, switches, and removes the temporary
snapshot on exit. The editable tree, lock and canonical links are untouched.

The flake, not this shell wrapper, must check and compile using its pinned
canonical `zen-dsl`, with the **whole snapshot as the import root**. It must
retain the source root, generated module, hardware inputs and pinned input
sources through `system.extraDependencies`. Setup's installed template and the
live-image seed use the same handoff. Generated Nix belongs in store outputs
or a private compiler cache, never in the editable tree. Sources retained in
the Nix store are not secret storage.

## Generated View

`zenos-rebuild --show-generated` materializes and locks identically to a normal
rebuild, then builds
`nixosConfigurations.<host>.config.system.build.zenosGeneratedConfig` and prints
its store path. It does not switch, reboot or log out. Flakes must export that
derivation to support this command; unsupported templates fail explicitly.
This output is the compiled local ZCFG module, not a flattened full NixOS
configuration. Do not edit it. The live seed additionally exports it as
`packages.<system>.generated-host`.

## Packaging And Checks

Use `pkgs.callPackage ./package.nix { }`. The package includes both scripts,
pins the interpreter and Nix executables across sudo, and runs unit tests at
build time. The previous ZenPkgs recipe that installs only the shell script
must be changed to use this package when the external revision is reviewed
and pinned. Neither repository is committed or pinned by this change.

Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` and
`bash -n scripts/zenos-rebuild.sh`. These are isolated source-contract tests;
real switch/desktop acceptance belongs in a disposable ZenOS VM. Tests never
invoke sudo, a real rebuild, or a VM lifecycle action.
