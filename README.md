# ZMK Battery Monitor

A Python port of the ZMK Split Battery Monitor application. This system tray application monitors the battery levels of your ZMK split keyboard halves over Bluetooth LE.

## Features

- Monitors battery levels for both halves of a ZMK split keyboard
- System tray icon shows the battery level with different icons
- Low battery notifications
- Auto-start on Windows login
- Reconnects automatically if the keyboard disconnects

## Requirements

- Python 3.10 or higher
- Windows operating system
- ZMK firmware on your split keyboard with battery reporting configured

## Installation

### Using uv package manager

```bash
# Create a virtual environment and install the package
uv venv
uv pip install -e .
```

### Manual Installation

```bash
# Create a virtual environment
python -m venv .venv
# Activate the environment
.venv\Scripts\activate
# Install dependencies
pip install -e .
```

## Usage

```bash
# Run the application
python -m zmk_battery
```

The application will show a window to select your ZMK keyboard from the list of paired Bluetooth devices. Once connected, the application can be minimized and will display the battery level in the system tray.

### Command Line Arguments

- `--device-name "Your Keyboard Name"`: Specify the device name to connect to
- `--device-id "XX:XX:XX:XX:XX:XX"`: Specify the device address to connect to

When both arguments are provided, the application will automatically connect to the specified device without showing the selection window.

## License

MIT License - See LICENSE file for details