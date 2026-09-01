"""
TimerCube – ESP32-S3-Matrix entry point.

Boot mode is determined by reading the QMI8658 accelerometer at power-on:

  left side down  → USB serial mode  (web/index.html via Web Serial)
  right side down → Bluetooth mode   (web/ble.html   via Web Bluetooth)
  upright / unknown → WiFi → AP hotspot

USB handshake protocol
  Browser → board : "HELLO\n"
  Board → browser : "READY_OK\n"  then initial-state JSON burst

This handshake is handled both here (first connection) and inside
UsbServer._command_loop (reconnection after page reload).
"""

import asyncio
import sys

from config      import load_config
from led_matrix  import Matrix
from timer_state import Timer
from web_server  import WebServer


# ── boot ───────────────────────────────────────────────────────────────────

async def main():
    config = load_config()
    matrix = Matrix()
    matrix.set_brightness(config['timer']['brightness'])
    matrix.clear()

    print('TimerCube starting…')

    from imu import get_orientation
    orientation = get_orientation()
    print('Orientation:', orientation)

    if orientation == 'left':
        # USB-only: wait indefinitely for browser HELLO
        if _usb_handshake(matrix):
            await _run_usb(config, matrix)

    elif orientation == 'right':
        # BLE mode immediately
        await _run_ble(config, matrix)

    elif orientation == 'inverted':
        # Upside-down: standalone tap-cycle "traffic light" mode, no networking
        await _run_tap(config, matrix)

    else:
        # Upright / unknown: WiFi → AP
        _set_hostname()
        from wifi_manager import try_networks
        result = await try_networks(config, matrix)
        if result:
            ip, iface = result
            print('Network ready: mode=client  ip=%s' % ip)
            _ddns_register(ip)
            timer  = Timer(config)
            server = WebServer(timer, config, matrix, ip, 'client')
            await server.start()
            return

        # WiFi unavailable — start AP hotspot
        await _run_ap(config, matrix)


# ── USB handshake (synchronous, called before asyncio tasks start) ─────────

def _usb_handshake(matrix):
    """
    Show U on LEDs (rotated for left-side-down viewing), wait for "HELLO"
    from the browser, reply "READY_OK".  Blocks indefinitely.
    Returns True on success (always, unless an exception escapes).
    """
    from led_matrix import BLUE
    # rotation=270: cube tipped left rotates the display 90° CW from viewer's
    # perspective; counter-rotating 270° CW makes U read correctly from above.
    # Swap to rotation=90 if U appears upside-down on hardware.
    matrix.show_char('U', BLUE, rotation=270)
    while True:
        line = sys.stdin.readline().strip()
        if line == 'HELLO':
            sys.stdout.write('READY_OK\n')
            return True


# ── mode runners ───────────────────────────────────────────────────────────

async def _run_usb(config, matrix):
    from usb_server import UsbServer
    server = UsbServer(config, matrix)
    await server.run()


async def _run_ble(config, matrix):
    from led_matrix import BLUE
    # rotation=90: cube tipped right rotates the display 90° CCW from viewer's
    # perspective; counter-rotating 90° CW makes B read correctly from above.
    # Swap to rotation=270 if B appears upside-down on hardware.
    matrix.show_char('B', BLUE, rotation=90)
    from ble_server import BleServer
    server = BleServer(config, matrix)
    await server.run()


async def _run_tap(config, matrix):
    from tap_mode import run
    await run(config, matrix)


async def _run_ap(config, matrix):
    _set_hostname()
    from wifi_manager import start_ap
    ip, iface = await start_ap(config, matrix)
    print('Network ready: mode=ap  ip=%s' % ip)
    timer  = Timer(config)
    server = WebServer(timer, config, matrix, ip, 'ap')
    await server.start()


def _ddns_register(ip):
    try:
        from config import read_device_id
        from ddns_client import register
        device_id = read_device_id()
        if device_id:
            register(device_id, ip)
    except Exception as e:
        print('DDNS setup error:', e)


def _set_hostname():
    try:
        import network
        network.hostname('timercube')
    except Exception:
        pass


asyncio.run(main())
