import json
import os
import pty
import select
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/zenos-rebuild.sh"
MOCK = '''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ["EVENTS"], "a") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "hostname":
    print("test")
elif name == "nproc":
    print("4")
elif name == "tmux" and sys.argv[1] == "has-session":
    sys.exit(1)
elif name == "sudo":
    if len(sys.argv) > 2 and sys.argv[2].endswith("snapshot.py"):
        print("mock build log", flush=True)
        if os.environ.get("REPORT_TTY"):
            print("HELPER_TTY=" + json.dumps([os.isatty(i) for i in range(3)]), flush=True)
        sys.exit(int(os.environ.get("REBUILD_EXIT", "0")))
elif name == "nom":
    if os.environ.get("NOM_FAIL"):
        sys.exit(9)
    print("RENDERER_TTY=" + json.dumps([os.isatty(i) for i in range(3)]), file=sys.stderr)
    for line in sys.stdin:
        print("RENDERED:" + line.rstrip(), file=sys.stderr, flush=True)
elif name == "sleep":
    pass
'''


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "editable config"
        (self.config / "hosts/test").mkdir(parents=True)
        (self.config / "flake.nix").write_text("{}")
        (self.config / "hosts/test/host.zcfg").write_text('legacy.networking.hostName = "test";')
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("hostname", "nproc", "tmux", "sudo", "notify-send", "sleep", "loginctl", "nom"):
            command = self.bin / name
            command.write_text(f"#!{sys.executable}\n" + MOCK)
            command.chmod(0o755)
        self.events = self.root / "events"
        self.env = os.environ | {
            "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "EVENTS": str(self.events), "TMUX": "test-session",
        }

    def run_script(self, *arguments, **environment):
        return subprocess.run(["bash", str(SCRIPT), *arguments], cwd=self.root,
                              env=self.env | environment, capture_output=True, text=True)

    def run_terminal(self, **environment):
        master, slave = pty.openpty()
        proc = subprocess.Popen(["bash", str(SCRIPT), "-d", str(self.config)],
                                cwd=self.root, stdin=slave, stdout=slave, stderr=slave,
                                env=self.env | environment)
        os.close(slave)
        output = bytearray()
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
            return proc.wait(timeout=1), output.decode()
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_interactive_single_renderer_and_plain_log(self):
        code, output = self.run_terminal(REBUILD_EXIT="42", REPORT_TTY="1")
        self.assertEqual(code, 42, output)
        self.assertIn("RENDERER_TTY=[false, true, true]", output)
        self.assertIn('RENDERED:HELPER_TTY=[true, false, false]', output)
        self.assertEqual(output.count("mock build log"), 1, output)
        self.assertIn("RENDERED:mock build log", output)
        log = next((self.root / "state/zenos/rebuild-logs").glob("*.log")).read_text()
        self.assertIn("mock build log", log)
        self.assertNotIn("RENDERED:", log)
        self.assertNotIn("RENDERER_TTY", log)
        sudo = next(call for call in self.calls() if call[0] == "sudo")
        self.assertNotIn("json-log-path", sudo)
        self.assertNotIn("--log-format", sudo)

    def test_renderer_failure_drains_logs_and_keeps_build_failure(self):
        code, output = self.run_terminal(REBUILD_EXIT="42", NOM_FAIL="1")
        self.assertEqual(code, 42, output)
        self.assertIn("mock build log", output)
        log = next((self.root / "state/zenos/rebuild-logs").glob("*.log")).read_text()
        self.assertIn("mock build log", log)

    def test_interactive_opt_out_keeps_plain_output(self):
        code, output = self.run_terminal(ZENOS_NOM="0")
        self.assertEqual(code, 0, output)
        self.assertIn("mock build log", output)
        self.assertFalse(any(call[0] == "nom" for call in self.calls()))

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_success_preserves_discovery_logs_and_notifications(self):
        result = self.run_script("-d", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        sudo = next(call for call in calls if call[0] == "sudo")
        self.assertEqual(sudo[3:5], [str(self.config), "test"])
        self.assertEqual((self.root / "state/zenos/rebuild-root").read_text().strip(), str(self.config))
        log = next((self.root / "state/zenos/rebuild-logs").glob("*.log"))
        self.assertIn("mock build log", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any("Rebuild Complete" in call for call in calls))
        notifications = [call for call in calls if call[0] == "notify-send"]
        self.assertEqual(len(notifications), 2)
        for call in notifications:
            self.assertIn("string:desktop-entry:com.neg-zero.zenos-rebuild", call)
        self.assertFalse((self.config / "hosts/test/host.nix").exists())

    def test_failure_survives_tee_and_does_not_reboot(self):
        result = self.run_script("-d", str(self.config), "-r", REBUILD_EXIT="42")
        self.assertEqual(result.returncode, 42)
        self.assertTrue(any("Rebuild Failed" in call for call in self.calls()))
        self.assertFalse(any("reboot" in call for call in self.calls()))

    def test_generated_view_never_reboots_or_reports_switch(self):
        result = self.run_script("-d", str(self.config), "--show-generated", "-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any("--show-generated" in call for call in calls))
        self.assertFalse(any("reboot" in call or "Rebuild Complete" in call for call in calls))

    def test_reboot_on_success(self):
        result = self.run_script("-d", str(self.config), "-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["sudo", "reboot"], self.calls())

    def test_remembered_config_root(self):
        state = self.root / "state/zenos"
        state.mkdir(parents=True)
        (state / "rebuild-root").write_text(str(self.config) + "\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(str(self.config) in call for call in self.calls()))

    def test_configuration_alias_is_canonicalized_before_snapshot(self):
        canonical = self.root / "etc/ZenOS"
        canonical.parent.mkdir()
        self.config.rename(canonical)
        alias = self.root / "Config"
        alias.symlink_to(canonical.parent, target_is_directory=True)
        result = self.run_script("-d", str(alias / "ZenOS"))
        self.assertEqual(result.returncode, 0, result.stderr)
        sudo = next(call for call in self.calls() if call[0] == "sudo")
        self.assertEqual(sudo[3], str(canonical))

    def test_old_host_nix_is_not_a_fallback(self):
        (self.config / "hosts/test/host.zcfg").unlink()
        (self.config / "hosts/test/host.nix").write_text("{}")
        result = self.run_script("-d", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] == "sudo" for call in self.calls()))

    def test_tmux_command_quotes_paths_and_metacharacters(self):
        dangerous = str(self.config) + "; touch UNEXPECTED"
        result = self.run_script("-d", dangerous, TMUX="")
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(call[4] for call in self.calls() if call[:2] == ["tmux", "send-keys"])
        invocation = command.split("; echo;")[0]
        self.assertEqual(shlex.split(invocation), ["bash", str(SCRIPT), "-d", dangerous])
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_missing_flag_value_fails(self):
        result = self.run_script("--dir")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
