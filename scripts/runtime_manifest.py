"""Print secret-free startup metadata from any working directory."""

import argparse
import json
from pathlib import Path
import sys


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message can include arbitrary supplied arguments.
        self.exit(2, "error: invalid runtime manifest arguments\n")


def main(argv=None):
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    from utils.runtime_manifest import EXECUTION_MODES, build_runtime_manifest

    parser = _SafeArgumentParser(
        prog="runtime_manifest", description="Print secret-free startup metadata as JSON.",
    )
    parser.add_argument("--execution-mode", choices=EXECUTION_MODES)
    arguments = parser.parse_args(argv)
    try:
        manifest = build_runtime_manifest(project_root, execution_mode=arguments.execution_mode)
    except ValueError:
        print("error: runtime manifest could not be built", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
