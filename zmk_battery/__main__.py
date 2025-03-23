"""Main entry point for ZMK Battery Monitor."""

import logging
import sys
from pathlib import Path

from zmk_battery.system_tray import BatteryTrayApp

def main() -> None:
    """Run the ZMK Battery Monitor application."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()]
    )
    
    # Get resources directory
    package_dir = Path(__file__).parent
    resources_dir = package_dir / "resources"
    
    # Create and run app
    app = BatteryTrayApp(resources_dir)
    try:
        app.run()
        # Keep the main thread alive
        while True:
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                break
    except KeyboardInterrupt:
        pass
    finally:
        app.exit()

if __name__ == "__main__":
    sys.exit(main())