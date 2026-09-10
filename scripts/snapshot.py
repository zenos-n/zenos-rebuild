#!/usr/bin/env python3
"""Materialize D19 sources before Nix sees them; never modify authored sources."""

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
REGULAR = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_directory(path):
    fd = os.open("/", DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def copy_regular(parent, name, destination):
    fd = os.open(name, REGULAR, dir_fd=parent)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"source must be a regular file: {name}")
        with destination.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        if (identity(before) != identity(os.fstat(source.fileno()))
                or identity(before) != identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
            raise ValueError(f"source changed during snapshot: {name}")


def materialize(source, destination, *, machine_root="/"):
    """Destination must be new and inside a private, caller-owned directory."""
    source_fd = open_directory(source)
    try:
        destination = Path(destination)
        destination.mkdir(mode=0o700)

        def walk(fd, output, parts=()):
            for name in sorted(os.listdir(fd)):
                relative = (*parts, name)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not parts:
                    if name == ".git":
                        continue
                    if name not in {
                        "flake.nix", "flake.lock", "hosts", "README.md",
                        "base-effective.json", ".gitignore",
                    }:
                        raise ValueError(f"unexpected configuration root entry: {name}")
                elif name.endswith(".nix"):
                    raise ValueError(f"Nix belongs outside the editable hosts tree: {relative}")
                target = output / name
                if stat.S_ISLNK(info.st_mode):
                    if not (len(relative) == 5 and relative[0] == "hosts"
                            and relative[2] == "users" and name.endswith(".zcfg")):
                        raise ValueError(f"unexpected source symlink: {relative}")
                    username = relative[3]
                    canonical = f"/Users/{username}/.private/Config/{name}"
                    if os.readlink(name, dir_fd=fd) != canonical:
                        raise ValueError(f"invalid canonical user link: {relative}")
                    parent = open_directory(
                        Path(machine_root) / "Users" / username / ".private" / "Config"
                    )
                    try:
                        copy_regular(parent, name, target)
                    finally:
                        os.close(parent)
                elif stat.S_ISDIR(info.st_mode):
                    if not parts and name != "hosts":
                        raise ValueError(f"expected a regular root file: {name}")
                    child = os.open(name, DIRECTORY, dir_fd=fd)
                    try:
                        target.mkdir(mode=0o700)
                        walk(child, target, relative)
                    finally:
                        os.close(child)
                else:
                    if not parts and name == "hosts":
                        raise ValueError("hosts must be a directory")
                    if parts and not name.endswith((".zcfg", ".json")):
                        raise ValueError(f"unexpected host source: {relative}")
                    copy_regular(fd, name, target)
                if identity(info) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise ValueError(f"source entry changed during snapshot: {relative}")

        walk(source_fd, destination)
        if not (destination / "flake.nix").is_file() or not (destination / "hosts").is_dir():
            raise ValueError("configuration requires flake.nix and hosts")
    finally:
        os.close(source_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("host")
    parser.add_argument("--show-generated", action="store_true")
    args, options = parser.parse_known_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.host):
        parser.error("invalid host name")
    os.umask(0o077)
    # Do not place privileged output beneath a caller-controlled TMPDIR parent.
    with tempfile.TemporaryDirectory(prefix="zenos-rebuild-", dir="/tmp") as temporary:
        snapshot = Path(temporary) / "sources"
        materialize(args.source, snapshot)
        if not (snapshot / "hosts" / args.host / "host.zcfg").is_file():
            raise ValueError(f"no host.zcfg for {args.host}")
        uri = f"path:{snapshot}"
        subprocess.run(["nix", "flake", "lock", "--offline", uri], check=True)
        if args.show_generated:
            command = ["nix", "build", "--offline", "--no-link", "--print-out-paths",
                       f"{uri}#nixosConfigurations.{args.host}.config.system.build.zenosGeneratedConfig"]
        else:
            command = ["nixos-rebuild", "switch", "--flake", f"{uri}#{args.host}", *options]
        return subprocess.run(command).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"zenos-rebuild: {error}") from error
