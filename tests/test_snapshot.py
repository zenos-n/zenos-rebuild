import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "snapshot.py"
spec = importlib.util.spec_from_file_location("snapshot", SCRIPT)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.user = self.config / "hosts/test/users/alice"
        self.user.mkdir(parents=True)
        (self.config / "flake.nix").write_text("{}")
        (self.config / "hosts/test/host.zcfg").write_text('_import "users/alice/main.zcfg";')
        self.canonical = self.root / "Users/alice/.private/Config/main.zcfg"
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_text('users.alice.legacy.home = "/Users/alice";')
        self.canonical.chmod(0o600)
        self.link = self.user / "main.zcfg"
        self.link.symlink_to("/Users/alice/.private/Config/main.zcfg")
        self.output = self.root / "snapshot"

    def materialize(self):
        snapshot.materialize(self.config, self.output, machine_root=self.root)

    def test_canonical_sources_are_private_regular_snapshot_files(self):
        reference = self.config / "base-effective.json"
        reference.write_text('{"description":"read-only reference"}')
        self.materialize()
        copied = self.output / "hosts/test/users/alice/main.zcfg"
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.read_bytes(), self.canonical.read_bytes())
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), "/Users/alice/.private/Config/main.zcfg")
        self.assertEqual(list(self.config.rglob("*.nix")), [self.config / "flake.nix"])
        self.assertEqual((self.output / reference.name).read_bytes(), reference.read_bytes())

    def test_arbitrary_root_json_is_rejected(self):
        (self.config / "unexpected.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unexpected configuration root entry"):
            self.materialize()

    def test_all_d19_zcfg_names_are_supported(self):
        self.link.rename(self.user / "apps.zcfg")
        (self.user / "apps.zcfg").unlink()
        (self.user / "apps.zcfg").symlink_to("/Users/alice/.private/Config/apps.zcfg")
        self.canonical.rename(self.canonical.with_name("apps.zcfg"))
        self.materialize()
        self.assertTrue((self.output / "hosts/test/users/alice/apps.zcfg").is_file())

    def test_generated_nix_is_rejected(self):
        (self.config / "hosts/test/host.nix").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Nix belongs outside"):
            self.materialize()

    def test_wrong_canonical_link_is_rejected(self):
        self.link.unlink()
        self.link.symlink_to("/etc/shadow")
        with self.assertRaisesRegex(ValueError, "invalid canonical"):
            self.materialize()

    def test_redirected_canonical_file_is_rejected(self):
        self.canonical.unlink()
        self.canonical.symlink_to(self.config / "flake.nix")
        with self.assertRaises(OSError):
            self.materialize()

    def test_redirected_canonical_parent_is_rejected(self):
        parent = self.canonical.parent
        parent.rename(parent.with_name("real"))
        parent.symlink_to(parent.with_name("real"), target_is_directory=True)
        with self.assertRaises(OSError):
            self.materialize()

    def test_fifo_is_rejected_without_blocking(self):
        self.canonical.unlink()
        os.mkfifo(self.canonical)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.materialize()

    def test_arbitrary_host_symlink_is_rejected(self):
        (self.config / "hosts/test/extra.zcfg").symlink_to(self.canonical)
        with self.assertRaisesRegex(ValueError, "unexpected source symlink"):
            self.materialize()

    def test_directory_redirect_is_rejected(self):
        (self.config / "hosts/other").symlink_to(self.user, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unexpected source symlink"):
            self.materialize()

    def test_existing_destination_is_not_overwritten(self):
        self.output.mkdir()
        (self.output / "keep").write_text("user data")
        with self.assertRaises(FileExistsError):
            self.materialize()
        self.assertEqual((self.output / "keep").read_text(), "user data")

    def test_canonical_parent_swap_cannot_read_redirected_file(self):
        copy = snapshot.copy_regular

        def swap(parent, name, destination):
            if name == "main.zcfg":
                original = self.canonical.parent
                original.rename(original.with_name("held"))
                original.symlink_to(self.config, target_is_directory=True)
            return copy(parent, name, destination)

        with patch.object(snapshot, "copy_regular", side_effect=swap):
            self.materialize()
        self.assertIn("users.alice", (self.output / "hosts/test/users/alice/main.zcfg").read_text())

    def test_nix_uses_only_snapshot_and_cleanup_preserves_sources(self):
        self.link.unlink()
        self.link.write_text("# inline source")
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            uri = next(arg for arg in command if arg.startswith("path:"))
            root = Path(uri.removeprefix("path:").split("#")[0])
            self.assertNotEqual(root, self.config)
            self.assertTrue(root.is_dir())
            self.assertTrue((root / "hosts/test/host.zcfg").is_file())
            (root / "flake.lock").write_text("snapshot-only lock")
            return type("Result", (), {"returncode": 17})()

        with patch("sys.argv", [str(SCRIPT), str(self.config), "test", "--show-trace"]), \
                patch.object(snapshot.subprocess, "run", side_effect=run):
            self.assertEqual(snapshot.main(), 17)
        self.assertEqual(commands[0][:4], ["nix", "flake", "lock", "--offline"])
        self.assertEqual(commands[1][:2], ["nixos-rebuild", "switch"])
        self.assertIn("--show-trace", commands[1])
        root = commands[0][-1].removeprefix("path:")
        self.assertFalse(Path(root).exists())
        self.assertFalse((self.config / "flake.lock").exists())

    def test_generated_view_does_not_switch(self):
        self.link.unlink()
        with patch("sys.argv", [str(SCRIPT), str(self.config), "test", "--show-generated"]), \
                patch.object(snapshot.subprocess, "run") as run:
            run.return_value.returncode = 0
            snapshot.main()
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["nix", "build"])
        self.assertTrue(command[-1].endswith(".config.system.build.zenosGeneratedConfig"))

    def test_invalid_snapshot_never_calls_nix(self):
        self.link.unlink()
        self.link.symlink_to("/etc/shadow")
        with patch("sys.argv", [str(SCRIPT), str(self.config), "test"]), \
                patch.object(snapshot.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                snapshot.main()
        run.assert_not_called()
        self.assertEqual(os.readlink(self.link), "/etc/shadow")


if __name__ == "__main__":
    unittest.main()
