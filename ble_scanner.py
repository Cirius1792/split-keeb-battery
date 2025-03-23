#!/usr/bin/env python3
"""
Minimal BLE scanner that prints device IDs and names to the terminal.
"""

import asyncio
import sys

from bleak import BleakScanner

async def scan_ble_devices():
    """Scan for BLE devices and print their ID and name."""
    print("Scanning for BLE devices...")
    devices = await BleakScanner.discover()
    
    if not devices:
        print("No BLE devices found.")
        return
    
    print("\nFound BLE devices:")
    print("-" * 50)
    print(f"{'Address':<20} | {'Name':<30}")
    print("-" * 50)
    
    for device in devices:
        name = device.name or "Unknown"
        print(f"{device.address:<20} | {name:<30}")

def main():
    """Main entry point."""
    try:
        asyncio.run(scan_ble_devices())
    except KeyboardInterrupt:
        print("\nScan interrupted by user.")
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    main()
