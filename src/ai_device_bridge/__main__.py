"""Run the desktop application or its packaged node smoke check."""

import os
import sys
import traceback
from pathlib import Path
from tempfile import gettempdir

if __name__ == "__main__":
    if sys.argv[1:] in (["--smoke-node"], ["--smoke-motion"]):
        report = Path(os.environ.get(
            "AI_DEVICE_BRIDGE_SMOKE_REPORT",
            str(Path(gettempdir()) / "ai-device-bridge-smoke-error.txt"),
        ))
        try:
            from ai_device_bridge.services.smoke import run_motion_smoke, run_node_smoke

            result = run_motion_smoke() if sys.argv[1] == "--smoke-motion" else run_node_smoke()
        except BaseException:
            report.write_text(traceback.format_exc(), encoding="utf-8")
            result = 1
        raise SystemExit(result)
    from ai_device_bridge.app import main

    raise SystemExit(main())
