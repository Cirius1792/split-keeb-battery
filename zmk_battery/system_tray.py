"""System tray application for ZMK battery monitor."""

import asyncio
import logging
import sys
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Thread
from tkinter import ttk
from typing import Dict, Optional, TypeVar, Awaitable, cast, Any

# Import pywin32 types for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from win32typing import PyHKEY

import pystray
from PIL import Image, ImageDraw
from win32api import RegOpenKeyEx, RegCloseKey, RegSetValueEx, RegDeleteValue, RegQueryValueEx
from win32con import (
    HKEY_CURRENT_USER, 
    KEY_ALL_ACCESS, 
    KEY_READ, 
    REG_SZ
)

T = TypeVar('T')

from zmk_battery.battery_monitor import (
    BatteryMonitor,
    BatteryStatus,
    ConnectStatus,
    ReadStatus
)

logger = logging.getLogger(__name__)

# Constants
AUTORUN_REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ZMK Battery Monitor"
BATTERY_LOW_LEVEL_THRESHOLD = 20
BATTERY_LOW_TIP_TITLE = "Low battery"
BATTERY_LOW_TIP_MESSAGE = "{0} battery level is below " + str(BATTERY_LOW_LEVEL_THRESHOLD) + "%"
BATTERY_NOT_CONNECTED_TITLE = "Not Connected"
RECONNECT_INTERVAL = 300  # seconds

# Command line argument constants
STARTUP_ARG_DEVICE_NAME = "--device-name"
STARTUP_ARG_DEVICE_ID = "--device-id"


class ThemeMode(Enum):
    """System theme modes."""
    
    LIGHT = "light"
    DARK = "dark"


@dataclass
class DeviceInfo:
    """Information about a connected device."""
    
    name: str
    id: str


class AsyncEventLoop:
    """Wrapper for running asyncio event loop in a separate thread."""
    
    def __init__(self) -> None:
        """Initialize the event loop."""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[Thread] = None
        
    def start(self) -> None:
        """Start the event loop in a separate thread."""
        def run_loop() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()
            
        self._thread = Thread(target=run_loop, daemon=True)
        self._thread.start()
        
    def stop(self) -> None:
        """Stop the event loop."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=1.0)
                
    def run_coroutine(self, coro: Awaitable[T]) -> asyncio.Future[Any]:
        """Run a coroutine in the event loop.
        
        Args:
            coro: Coroutine to run
            
        Returns:
            Future for the coroutine result
        """
        if not self._loop:
            raise RuntimeError("Event loop not started")
        
        # Safely wrap any awaitable in asyncio.run_coroutine_threadsafe
        return asyncio.run_coroutine_threadsafe(coro, self._loop)


class IconManager:
    """Manages system tray icons."""
    
    def __init__(self, resources_dir: Path) -> None:
        """Initialize the icon manager.
        
        Args:
            resources_dir: Directory containing icon resources
        """
        self._resources_dir = resources_dir
        self._icon_cache: Dict[str, Image.Image] = {}
        
    def get_battery_icon(self, percentage: int, theme: ThemeMode) -> Image.Image:
        """Get an icon for a battery percentage.
        
        Args:
            percentage: Battery percentage (0-100) or -1 for disconnected
            theme: Current system theme
            
        Returns:
            Icon image
        """
        # Use fallback icon generation if resources are not available
        if not self._resources_dir.exists():
            return self._generate_battery_icon(percentage, theme)
        
        # Determine icon filename
        prefix = "black" if theme == ThemeMode.LIGHT else "white"
        
        if percentage == -1:
            suffix = "dsc"
        else:
            # Round to nearest 10%
            rounded = round(percentage / 10.0) * 10
            suffix = f"{rounded:03d}"
            
        icon_name = f"{prefix}-{suffix}"
        icon_path = self._resources_dir / f"{icon_name}.png"
        
        # Use cached icon if available
        if icon_name in self._icon_cache:
            return self._icon_cache[icon_name]
        
        # Load icon if file exists
        if icon_path.exists():
            try:
                image = Image.open(icon_path)
                self._icon_cache[icon_name] = image
                return image
            except Exception as e:
                logger.error(f"Failed to load icon {icon_path}: {e}")
        
        # Generate fallback icon
        return self._generate_battery_icon(percentage, theme)
    
    def _generate_battery_icon(self, percentage: int, theme: ThemeMode) -> Image.Image:
        """Generate a battery icon.
        
        Args:
            percentage: Battery percentage (0-100) or -1 for disconnected
            theme: Current system theme
            
        Returns:
            Generated icon image
        """
        width, height = 64, 64
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # Colors
        fg_color = "black" if theme == ThemeMode.LIGHT else "white"
        
        # Draw battery outline
        battery_width, battery_height = 40, 25
        left = (width - battery_width) // 2
        top = (height - battery_height) // 2
        
        # Battery body
        draw.rectangle(
            [(left, top), (left + battery_width - 5, top + battery_height)], 
            outline=fg_color, width=2
        )
        
        # Battery terminal
        draw.rectangle(
            [(left + battery_width - 5, top + 5), (left + battery_width, top + battery_height - 5)],
            outline=fg_color, width=2, fill=fg_color
        )
        
        # Fill level
        if percentage >= 0:
            fill_width = int((battery_width - 10) * percentage / 100)
            draw.rectangle(
                [(left + 4, top + 4), (left + 4 + fill_width, top + battery_height - 4)],
                fill=fg_color
            )
        else:
            # Draw an X for disconnected
            draw.line([(left + 5, top + 5), (left + battery_width - 10, top + battery_height - 5)], 
                     fill=fg_color, width=2)
            draw.line([(left + battery_width - 10, top + 5), (left + 5, top + battery_height - 5)], 
                     fill=fg_color, width=2)
        
        return image


class RegistryHelper:
    """Helper for working with Windows registry."""
    
    @staticmethod
    def is_auto_run_enabled() -> bool:
        """Check if application is set to run at startup.
        
        Returns:
            True if enabled, False otherwise
        """
        try:
            # Cast HKEY_CURRENT_USER to Any to avoid type errors with PyHKEY
            # Cast 0 to False for the reserved parameter
            key = RegOpenKeyEx(cast(Any, HKEY_CURRENT_USER), AUTORUN_REG_KEY, cast(bool, 0), KEY_READ)
            try:
                RegQueryValueEx(key, APP_NAME)
                return True
            except Exception:
                return False
            finally:
                RegCloseKey(key)
        except Exception as e:
            logger.error(f"Failed to check auto run: {e}")
            return False
    
    @staticmethod
    def set_auto_run_enabled(enabled: bool, device_name: str = "", device_id: str = "") -> bool:
        """Set application to run at startup.
        
        Args:
            enabled: Whether to enable auto run
            device_name: Device name to connect to on startup
            device_id: Device ID to connect to on startup
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Cast HKEY_CURRENT_USER to Any to avoid type errors with PyHKEY
            # Cast 0 to False for the reserved parameter
            key = RegOpenKeyEx(cast(Any, HKEY_CURRENT_USER), AUTORUN_REG_KEY, cast(bool, 0), KEY_ALL_ACCESS)
            try:
                if enabled:
                    exe_path = sys.executable
                    
                    if device_name and device_id:
                        value = f'"{exe_path}" {STARTUP_ARG_DEVICE_NAME} "{device_name}" {STARTUP_ARG_DEVICE_ID} "{device_id}"'
                    else:
                        value = f'"{exe_path}"'
                        
                    RegSetValueEx(key, APP_NAME, 0, REG_SZ, value)
                else:
                    RegDeleteValue(key, APP_NAME)
                    
                return True
            finally:
                RegCloseKey(key)
        except Exception as e:
            logger.error(f"Failed to set auto run: {e}")
            return False

    @staticmethod
    def is_system_using_light_theme() -> bool:
        """Check if system is using light theme.
        
        Returns:
            True if light theme, False if dark theme
        """
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            # Cast HKEY_CURRENT_USER to Any to avoid type errors with PyHKEY
            # Cast 0 to False for the reserved parameter
            key = RegOpenKeyEx(cast(Any, HKEY_CURRENT_USER), key_path, cast(bool, 0), KEY_READ)
            try:
                value, _ = RegQueryValueEx(key, "SystemUsesLightTheme")
                return bool(value)
            except Exception:
                # Default to light theme
                return True
            finally:
                RegCloseKey(key)
        except Exception as e:
            logger.error(f"Failed to get system theme: {e}")
            return True


class BatteryTrayApp:
    """System tray application for displaying ZMK keyboard battery levels."""
    
    def __init__(self, resources_dir: Path) -> None:
        """Initialize the tray application.
        
        Args:
            resources_dir: Directory containing resources
        """
        # Setup components
        self._loop = AsyncEventLoop()
        self._loop.start()
        
        self._battery_monitor = BatteryMonitor(self._on_battery_level_changed)
        self._icon_manager = IconManager(resources_dir)
        
        # Application state
        self._device_name = ""
        self._device_id = ""
        self._last_min_level = -1
        self._reconnect_counter = RECONNECT_INTERVAL
        self._reconnect_timer_running = False
        
        # UI components
        self._root: Optional[tk.Tk] = None
        self._devices_list: Optional[ttk.Treeview] = None
        self._status_label: Optional[ttk.Label] = None
        self._connect_button: Optional[ttk.Button] = None
        self._reload_button: Optional[ttk.Button] = None
        self._auto_run_var: Optional[tk.BooleanVar] = None
        
        # Create tray icon
        self._create_tray_icon()
        
        # Start by processing command line
        self._process_command_line()
    
    def run(self) -> None:
        """Run the application."""
        if self._device_name and self._device_id:
            # Start with pre-defined device
            self._last_min_level = 100  # Ensure notification on first connect
            self._start_reconnect_timer()
        else:
            # Show UI to select device
            self._show_main_window()
    
    def exit(self) -> None:
        """Exit the application."""
        self._battery_monitor.disconnect()
        if self._root:
            self._root.destroy()
        self._icon.stop()
        self._loop.stop()
        
    def _create_tray_icon(self) -> None:
        """Create the system tray icon."""
        theme = ThemeMode.LIGHT if RegistryHelper.is_system_using_light_theme() else ThemeMode.DARK
        icon_image = self._icon_manager.get_battery_icon(-1, theme)
        
        self._icon = pystray.Icon(
            APP_NAME,
            icon_image,
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Show", self._show_main_window),
                pystray.MenuItem("Exit", self.exit)
            )
        )
        
        # Start the icon in a separate thread
        Thread(target=self._icon.run, daemon=True).start()
    
    def _show_main_window(self) -> None:
        """Show the main application window."""
        if self._root:
            # Window already exists, just show it
            self._root.deiconify()
            return
            
        # Create the window
        self._root = tk.Tk()
        self._root.title(APP_NAME)
        self._root.geometry("400x300")
        self._root.protocol("WM_DELETE_WINDOW", self._hide_window)
        
        # Create UI components
        frame = ttk.Frame(self._root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Devices list
        ttk.Label(frame, text="Paired Devices:").pack(anchor=tk.W, pady=(0, 5))
        
        self._devices_list = ttk.Treeview(frame, columns=("name",), show="headings", height=6)
        self._devices_list.heading("name", text="Device Name")
        self._devices_list.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self._devices_list.bind("<Double-1>", self._on_device_double_click)
        
        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=(0, 10))
        
        self._reload_button = ttk.Button(button_frame, text="Reload Devices", command=self._list_ble_devices)
        self._reload_button.pack(side=tk.LEFT, padx=(0, 5))
        
        self._connect_button = ttk.Button(button_frame, text="Connect", command=self._on_connect_button)
        self._connect_button.pack(side=tk.LEFT)
        
        # Status
        status_frame = ttk.Frame(frame)
        status_frame.pack(fill=tk.X)
        
        ttk.Label(status_frame, text="Status:").pack(side=tk.LEFT)
        self._status_label = ttk.Label(status_frame, text="Ready")
        self._status_label.pack(side=tk.LEFT, padx=(5, 0))
        
        # Auto-run
        auto_run_frame = ttk.Frame(frame)
        auto_run_frame.pack(fill=tk.X, pady=(10, 0))
        
        self._auto_run_var = tk.BooleanVar(value=RegistryHelper.is_auto_run_enabled())
        auto_run_check = ttk.Checkbutton(
            auto_run_frame, 
            text="Run at startup", 
            variable=self._auto_run_var,
            command=self._on_auto_run_changed
        )
        auto_run_check.pack(side=tk.LEFT)
        
        # List devices
        self._list_ble_devices()
    
    def _hide_window(self) -> None:
        """Hide the main window."""
        if self._root:
            self._root.withdraw()
    
    def _list_ble_devices(self) -> None:
        """List paired BLE devices."""
        if not self._devices_list or not self._reload_button or not self._status_label:
            return
            
        # Clear list
        for item in self._devices_list.get_children():
            self._devices_list.delete(item)
            
        # Disable reload button
        self._reload_button.configure(text="Looking for devices..", state=tk.DISABLED)
        
        # Start device discovery
        def device_callback(device_name: str, device_id: str) -> None:
            if self._root and self._devices_list:
                devices_list = self._devices_list  # Local variable for type checker
                self._root.after(0, lambda: devices_list.insert("", "end", values=(device_name,), tags=(device_id,)))
        
        def completion_callback() -> None:
            if self._root and self._reload_button:
                reload_button = self._reload_button  # Local variable for type checker
                self._root.after(0, lambda: reload_button.configure(text="Reload Devices", state=tk.NORMAL))
        
        self._loop.run_coroutine(
            BatteryMonitor.list_paired_devices(device_callback, completion_callback)
        )
    
    def _process_command_line(self) -> None:
        """Process command line arguments."""
        args = sys.argv
        for i in range(1, len(args)):
            if args[i] == STARTUP_ARG_DEVICE_ID and i + 1 < len(args):
                self._device_id = args[i + 1].strip('"')
            elif args[i] == STARTUP_ARG_DEVICE_NAME and i + 1 < len(args):
                self._device_name = args[i + 1].strip('"')
    
    def _start_reconnect_timer(self) -> None:
        """Start the reconnect timer."""
        if self._reconnect_timer_running:
            return
            
        self._reconnect_timer_running = True
        self._reconnect_counter = RECONNECT_INTERVAL
        
        def timer_tick() -> None:
            if not self._reconnect_timer_running:
                return
                
            self._reconnect_counter -= 1
            
            if self._reconnect_counter <= 0:
                # Time to reconnect
                if self._status_label:
                    self._status_label.configure(text=f"Connecting to '{self._device_name}'..")
                
                # Attempt connection
                future = self._loop.run_coroutine(
                    self._battery_monitor.connect(self._device_name, self._device_id)
                )
                
                def on_connect_done(fut: asyncio.Future) -> None:
                    try:
                        result = fut.result()
                        if result.status == ConnectStatus.CONNECTED:
                            self._reconnect_timer_running = False
                            if self._status_label:
                                status_label = self._status_label  # Local variable for type checker
                                status_label.configure(text=f"Connected to {self._device_name}")
                            if self._connect_button:
                                connect_button = self._connect_button  # Local variable for type checker
                                connect_button.configure(text="Disconnect")
                            self._update_tray_icon()
                        else:
                            # Failed to connect, retry
                            self._reconnect_counter = RECONNECT_INTERVAL
                            if self._status_label:
                                status_label = self._status_label  # Local variable for type checker
                                error_msg = result.error_message or result.status.name
                                status_label.configure(
                                    text=f"Could not connect to '{self._device_name}': {error_msg}"
                                )
                            # Schedule next attempt
                            if self._root:
                                self._root.after(1000, timer_tick)
                    except Exception as e:
                        logger.error(f"Connection error: {e}")
                        self._reconnect_counter = RECONNECT_INTERVAL
                        if self._root:
                            self._root.after(1000, timer_tick)
                
                future.add_done_callback(on_connect_done)
            else:
                # Update status and continue waiting
                if self._status_label:
                    self._status_label.configure(
                        text=f"Connecting to '{self._device_name}' in {self._reconnect_counter} seconds.."
                    )
                
                # Check again in 1 second
                if self._root:
                    self._root.after(1000, timer_tick)
                else:
                    # Without a root window, we need an alternative timer
                    Thread(target=lambda: (asyncio.sleep(1), timer_tick())).start()
        
        # Start the timer
        if self._root:
            self._root.after(1000, timer_tick)
        else:
            # Without a root window, start in a thread
            Thread(target=timer_tick).start()
    
    def _on_device_double_click(self, event: tk.Event) -> None:
        """Handle double click on a device in the list."""
        if not self._devices_list or not self._connect_button:
            return
            
        if self._connect_button["state"] == tk.NORMAL and not self._battery_monitor.is_connected():
            self._on_connect_button()
    
    def _on_connect_button(self) -> None:
        """Handle connect/disconnect button click."""
        if not self._devices_list or not self._connect_button or not self._status_label:
            return
            
        # Disable connect button during operation
        self._connect_button.configure(state=tk.DISABLED)
        
        if self._battery_monitor.is_connected():
            # Disconnect
            self._battery_monitor.disconnect()
            self._device_name = ""
            self._device_id = ""
            self._status_label.configure(text="Ready")
            self._connect_button.configure(text="Connect", state=tk.NORMAL)
            self._update_tray_icon()
        else:
            # Connect to selected device
            selection = self._devices_list.selection()
            if not selection:
                self._connect_button.configure(state=tk.NORMAL)
                return
                
            device_name = self._devices_list.item(selection[0], "values")[0]
            device_id = self._devices_list.item(selection[0], "tags")[0]
            
            self._status_label.configure(text=f"Connecting to '{device_name}'..")
            
            # Attempt connection
            future = self._loop.run_coroutine(
                self._battery_monitor.connect(device_name, device_id)
            )
            
            def on_connect_done(fut: asyncio.Future) -> None:
                try:
                    result = fut.result()
                    if result.status == ConnectStatus.CONNECTED:
                        self._device_name = device_name
                        self._device_id = device_id
                        status_label = self._status_label  # Local variable for type checker
                        connect_button = self._connect_button  # Local variable for type checker
                        status_label.configure(text=f"Connected to {device_name}")
                        connect_button.configure(text="Disconnect", state=tk.NORMAL)
                        self._update_tray_icon()
                        
                        # Update auto-run if enabled
                        if self._auto_run_var and self._auto_run_var.get():
                            RegistryHelper.set_auto_run_enabled(True, device_name, device_id)
                    else:
                        error_msg = result.error_message or result.status.name
                        status_label = self._status_label  # Local variable for type checker
                        connect_button = self._connect_button  # Local variable for type checker
                        status_label.configure(
                            text=f"Could not connect to '{device_name}': {error_msg}"
                        )
                        connect_button.configure(text="Connect", state=tk.NORMAL)
                except Exception as e:
                    logger.error(f"Connection error: {e}")
                    status_label = self._status_label  # Local variable for type checker
                    connect_button = self._connect_button  # Local variable for type checker
                    status_label.configure(text=f"Connection error: {e}")
                    connect_button.configure(text="Connect", state=tk.NORMAL)
            
            future.add_done_callback(on_connect_done)
    
    def _on_auto_run_changed(self) -> None:
        """Handle auto run checkbox changes."""
        if not self._auto_run_var:
            return
            
        enabled = self._auto_run_var.get()
        if enabled and self._device_name and self._device_id:
            RegistryHelper.set_auto_run_enabled(True, self._device_name, self._device_id)
        else:
            RegistryHelper.set_auto_run_enabled(enabled)
    
    def _on_battery_level_changed(self) -> None:
        """Handle battery level change notifications."""
        self._update_tray_icon()
    
    def _update_tray_icon(self) -> None:
        """Update the system tray icon based on current battery status."""
        batteries = self._battery_monitor.batteries
        min_level = 100
        tooltip_text = ""
        
        if not self._battery_monitor.is_connected() or not batteries:
            min_level = -1
            tooltip_text = BATTERY_NOT_CONNECTED_TITLE
        elif len(batteries) == 1:
            status = next(iter(batteries.values()))
            min_level = status.level
            tooltip_text = f"{self._device_name}: {min_level}%"
        else:
            tooltip_text = f"{self._device_name}\n"
            for handle, status in batteries.items():
                tooltip_text += f"{status.name}: {status.level}%\n"
                if status.level != -1:  # Ignore disconnected parts
                    min_level = min(min_level, status.level)
        
        # Update icon
        theme = ThemeMode.LIGHT if RegistryHelper.is_system_using_light_theme() else ThemeMode.DARK
        icon_image = self._icon_manager.get_battery_icon(min_level, theme)
        self._icon.icon = icon_image
        self._icon.title = tooltip_text
        
        # Check for low battery
        if (self._last_min_level > BATTERY_LOW_LEVEL_THRESHOLD and 
            min_level != -1 and min_level <= BATTERY_LOW_LEVEL_THRESHOLD):
            self._show_notification(
                BATTERY_LOW_TIP_TITLE,
                BATTERY_LOW_TIP_MESSAGE.format(self._device_name)
            )
        
        self._last_min_level = min_level
    
    def _show_notification(self, title: str, message: str) -> None:
        """Show a system notification.
        
        Args:
            title: Notification title
            message: Notification message
        """
        # Use the tray icon to show notifications
        self._icon.notify(message, title)
