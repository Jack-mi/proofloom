"""Run the fixed Lean verifier locally on Linux or through a named Lima guest."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def command(args):
    verifier = ["python3", "-B", args.verifier, args.action, "--runtime", args.runtime]
    if args.action != "doctor":
        if not args.output:
            raise ValueError("--output is required for smoke/validate")
        verifier += ["--output", args.output, "--timeout", str(args.timeout),
                     "--memory-mib", str(args.memory_mib), "--jobs", str(args.jobs)]
    if args.action == "validate":
        if not args.experiment or not args.challenge:
            raise ValueError("--experiment and --challenge are required for full validation")
        verifier += ["--experiment", args.experiment, "--challenge", args.challenge,
                     "--stage", args.stage]
    if args.backend == "native":
        if sys.platform != "linux":
            raise ValueError("native verification needs Linux; choose the Lima backend on macOS")
        return verifier
    binary = shutil.which(args.limactl) if not Path(args.limactl).is_absolute() else args.limactl
    if not binary or not Path(binary).is_file():
        raise ValueError("limactl is unavailable; supply --limactl or configure PATH")
    return [binary, "shell", args.instance, "--", *verifier]


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("action", choices=("doctor", "smoke", "validate"))
    value.add_argument("--backend", choices=("native", "lima"), default="native")
    value.add_argument("--runtime", required=True, help="runtime.json path as seen by the Linux guest")
    value.add_argument("--verifier", default=str(Path(__file__).with_name("lean_verify.py")),
                       help="verifier path in Linux, for example /proofloom/tools/lean_verify.py")
    value.add_argument("--limactl", default="limactl")
    value.add_argument("--lima-home", help="explicit host instance directory for a custom Lima install")
    value.add_argument("--instance", default="proofloom")
    value.add_argument("--output")
    value.add_argument("--experiment")
    value.add_argument("--challenge")
    value.add_argument("--stage", choices=("build", "export", "compare", "all"), default="all")
    value.add_argument("--timeout", type=float, default=180)
    value.add_argument("--memory-mib", type=int, default=4096)
    value.add_argument("--jobs", type=int, default=1)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        # Inherit only transport configuration here. The Linux verifier clears the
        # proof subprocess environment and applies its sandbox/resource policy.
        environment = os.environ.copy()
        if args.backend == "lima" and args.lima_home:
            instance_root = Path(args.lima_home).resolve(strict=True)
            if not instance_root.is_dir():
                raise ValueError("--lima-home must name an existing instance directory")
            environment["LIMA_HOME"] = str(instance_root)
        return subprocess.run(command(args), env=environment).returncode
    except (OSError, ValueError) as error:
        print("backend: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
