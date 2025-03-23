"""Battery monitor for ZMK split keyboards."""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Protocol, Tuple

import bleak
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

logger = logging.getLogger(__name__)

# Standard Bluetooth GATT Battery Service and Characteristic UUIDs
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_DEFAULT_NAME = "Main"


class DeviceCallback(Protocol):
    """Callback for device discovery."""
    
    def __call__(self, device_name: str, device_id: str) -> None:
        """Called when a device is found."""
        ...


class CompletionCallback(Protocol):
    """Callback for operation completion."""
    
    def __call__(self) -> None:
        """Called when an operation completes."""
        ...


class BatteryLevelChangedCallback(Protocol):
    """Callback for battery level changes."""
    
    def __call__(self) -> None:
        """Called when battery level changes."""
        ...


class ConnectStatus(Enum):
    """Connection status enumeration."""
    
    CONNECTED = auto()
    DEVICE_NOT_FOUND = auto()
    BATTERY_SERVICE_NOT_FOUND = auto()
    BATTERY_LEVEL_CHARACTERISTIC_NOT_FOUND = auto()
    SUBSCRIPTION_FAILURE = auto()


class ReadStatus(Enum):
    """Battery read status enumeration."""
    
    SUCCESS = auto()
    NOT_CONNECTED = auto()
    FAILURE = auto()


@dataclass
class BatteryStatus:
    """Status of a battery."""
    
    name: str
    level: int


@dataclass
class ConnectResult:
    """Result of a connection attempt."""
    
    status: ConnectStatus
    error_message: str = ""


@dataclass
class ReadBatteryLevelResult:
    """Result of a battery level read."""
    
    status: ReadStatus
    batteries: Dict[str, BatteryStatus]
    error_message: str = ""


class BatteryMonitor:
    """Monitor battery levels on ZMK keyboards via Bluetooth LE."""

    def __init__(self, battery_changed_callback: BatteryLevelChangedCallback) -> None:
        """Initialize the battery monitor.
        
        Args:
            battery_changed_callback: Called when battery levels change
        """
        self._battery_changed_callback = battery_changed_callback
        self._batteries: Dict[str, BatteryStatus] = {}
        self._client: Optional[BleakClient] = None
        self._device: Optional[BLEDevice] = None
        self._battery_characteristics: List[Tuple[str, str]] = []  # (handle, description)
        
    @property
    def batteries(self) -> Dict[str, BatteryStatus]:
        """Get current battery status."""
        return self._batteries.copy()

    @staticmethod
    async def list_paired_devices(
        device_callback: DeviceCallback, completion_callback: CompletionCallback
    ) -> None:
        """List paired BLE devices.
        
        Args:
            device_callback: Called for each found device
            completion_callback: Called when enumeration completes
        """
        try:
            devices = await BleakScanner.discover()
            
            for device in devices:
                if device.name:  # Only report devices with names
                    device_callback(device.name, device.address)
            
            completion_callback()
        except Exception as e:
            logger.error(f"Error discovering devices: {e}")
            completion_callback()

    async def connect(self, device_name: str, device_id: str) -> ConnectResult:
        """Connect to a BLE device.
        
        Args:
            device_name: Name of the device to connect to
            device_id: ID (address) of the device to connect to
            
        Returns:
            ConnectResult indicating success or failure
        """
        try:
            self.disconnect()
            
            # Find the device
            device = await BleakScanner.find_device_by_address(device_id)
            if not device:
                return ConnectResult(status=ConnectStatus.DEVICE_NOT_FOUND)
            
            # Connect to the device
            client = BleakClient(device)
            await client.connect()
            if not client.is_connected:
                return ConnectResult(status=ConnectStatus.DEVICE_NOT_FOUND)
            
            # Check for battery service
            services = await client.get_services()
            battery_service = None
            for service in services:
                if service.uuid.lower() == BATTERY_SERVICE_UUID:
                    battery_service = service
                    break
                    
            if not battery_service:
                await client.disconnect()
                return ConnectResult(status=ConnectStatus.BATTERY_SERVICE_NOT_FOUND)
            
            # Get battery level characteristics
            characteristics = []
            for char in battery_service.characteristics:
                if char.uuid.lower() == BATTERY_LEVEL_CHAR_UUID:
                    characteristics.append((char.handle, char.description or BATTERY_DEFAULT_NAME))
            
            if not characteristics:
                await client.disconnect()
                return ConnectResult(status=ConnectStatus.BATTERY_LEVEL_CHARACTERISTIC_NOT_FOUND)
            
            # Set up notification handlers for each battery characteristic
            for handle, description in characteristics:
                char_uuid = f"0000{handle:x}-0000-1000-8000-00805f9b34fb"
                try:
                    await client.start_notify(char_uuid, self._battery_level_changed_handler)
                    self._batteries[handle] = BatteryStatus(name=description, level=-1)
                except Exception as e:
                    logger.error(f"Failed to subscribe to notifications: {e}")
                    await client.disconnect()
                    return ConnectResult(status=ConnectStatus.SUBSCRIPTION_FAILURE, 
                                        error_message=str(e))
            
            # Store the connection
            self._client = client
            self._device = device
            self._battery_characteristics = characteristics
            
            # Initial read of battery levels
            read_result = await self.read_battery_levels()
            if read_result.status == ReadStatus.SUCCESS:
                self._batteries = read_result.batteries
                
            return ConnectResult(status=ConnectStatus.CONNECTED)
            
        except Exception as e:
            logger.error(f"Connection error: {e}")
            if self._client and self._client.is_connected:
                await self._client.disconnect()
            self._client = None
            self._device = None
            self._battery_characteristics = []
            return ConnectResult(
                status=ConnectStatus.DEVICE_NOT_FOUND, 
                error_message=str(e)
            )

    def disconnect(self) -> None:
        """Disconnect from the current device."""
        self._batteries.clear()
        
        if self._client and self._client.is_connected:
            asyncio.create_task(self._client.disconnect())
            
        self._client = None
        self._device = None
        self._battery_characteristics = []

    def is_connected(self) -> bool:
        """Check if connected to a device."""
        return self._client is not None and self._client.is_connected

    async def read_battery_levels(self) -> ReadBatteryLevelResult:
        """Read current battery levels.
        
        Returns:
            ReadBatteryLevelResult with battery status
        """
        if not self.is_connected() or not self._client:
            return ReadBatteryLevelResult(
                status=ReadStatus.NOT_CONNECTED,
                batteries={}
            )
        
        result = ReadBatteryLevelResult(
            status=ReadStatus.SUCCESS,
            batteries={}
        )
        
        try:
            for handle, description in self._battery_characteristics:
                char_uuid = f"0000{handle:x}-0000-1000-8000-00805f9b34fb"
                value = await self._client.read_gatt_char(char_uuid)
                
                if value:
                    # First byte contains the battery percentage
                    level = value[0]
                    if level == 255:
                        level = -1
                    
                    result.batteries[handle] = BatteryStatus(
                        name=description,
                        level=level
                    )
                else:
                    logger.warning(f"No value read for characteristic {char_uuid}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error reading battery levels: {e}")
            return ReadBatteryLevelResult(
                status=ReadStatus.FAILURE,
                batteries={},
                error_message=str(e)
            )

    async def _battery_level_changed_handler(self, sender: int, data: bytearray) -> None:
        """Handle battery level change notifications.
        
        Args:
            sender: Characteristic handle 
            data: Notification data
        """
        if data:
            level = data[0]
            if level == 255:
                level = -1
                
            # Find the characteristic that sent this
            for handle, description in self._battery_characteristics:
                if handle == sender:
                    self._batteries[handle] = BatteryStatus(name=description, level=level)
                    break
                    
            # Notify about the change
            self._battery_changed_callback()