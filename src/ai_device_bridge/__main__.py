"""Run the desktop application or its packaged node smoke check."""

import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["--smoke-node"]:
        from ai_device_bridge.services.smoke import run_node_smoke

        raise SystemExit(run_node_smoke())
    from ai_device_bridge.app import main

    raise SystemExit(main())
