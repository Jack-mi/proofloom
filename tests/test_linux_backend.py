import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import linux_backend


class BackendTests(unittest.TestCase):
    def test_native_requires_linux_and_forwards_fixed_arguments(self):
        args = linux_backend.parser().parse_args(["doctor", "--runtime", "/runtime/config.json"])
        with patch.object(linux_backend.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "needs Linux"):
                linux_backend.command(args)
        with patch.object(linux_backend.sys, "platform", "linux"):
            result = linux_backend.command(args)
        self.assertEqual(result[-3:], ["doctor", "--runtime", "/runtime/config.json"])

    def test_lima_paths_are_arguments_and_real_failure_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "fake limactl"
            cli.write_text("#!/bin/sh\nexit 7\n")
            cli.chmod(0o700)
            paths = ["smoke", "--backend", "lima", "--limactl", str(cli),
                     "--runtime", "/runtime ' $(touch nope)/config.json", "--verifier",
                     "/proofloom/tools/lean_verify.py", "--output", "/run ' $()"]
            args = linux_backend.parser().parse_args(paths)
            result = linux_backend.command(args)
            self.assertEqual(result[:4], [str(cli), "shell", "proofloom", "--"])
            self.assertIn(args.runtime, result)
            self.assertIn(args.output, result)
            self.assertEqual(linux_backend.main(paths), 7)
            self.assertFalse((Path(folder) / "nope").exists())

    def test_full_validation_refuses_missing_bound_inputs(self):
        args = linux_backend.parser().parse_args(["validate", "--runtime", "/runtime.json",
                                                  "--output", "/result"])
        with self.assertRaisesRegex(ValueError, "--experiment"):
            linux_backend.command(args)

    def test_custom_lima_instance_directory_is_forwarded(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "limactl"
            cli.write_text("#!/bin/sh\nprintf '%s' \"$LIMA_HOME\"\n")
            cli.chmod(0o700)
            arguments = ["doctor", "--backend", "lima", "--limactl", str(cli),
                         "--lima-home", folder, "--runtime", "/runtime.json"]
            with patch.object(linux_backend.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(linux_backend.main(arguments), 0)
            self.assertEqual(run.call_args.kwargs["env"]["LIMA_HOME"], str(Path(folder).resolve()))


if __name__ == "__main__":
    unittest.main()
