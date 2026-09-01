"""
tap_mode.py — upside-down 'traffic light' mode for the ESP32-S3-Matrix.

Entered from main.py when the cube boots inverted
(imu.get_orientation() == 'inverted'). Tapping the top face steps the
matrix colour:  keep-alive T → green → amber → red → keep-alive T → …

No networking runs in this mode. The tap threshold, tap axis and the
keep-alive 'T' level are read once from config.json at start — set them on
the Settings page while the cube is in WiFi mode; they persist across
reboots.

Tap detection: an exponential moving average of each axis tracks the
gravity/tilt baseline, and a sample deviating from that baseline by more
than 'tap_threshold' counts (outside a debounce window) is treated as a
tap.  Same algorithm as the tap_test.py / powerbank_test.py calibration
tooling.  X is the vertical axis on this board (ax ≈ +1 g upright), so the
default tap axis is 'x'.
"""

import asyncio
import time

from imu        import TapReader
from led_matrix import GREEN, AMBER, RED

_STATES = ('off', 'green', 'amber', 'red')
_FILL   = {'green': GREEN, 'amber': AMBER, 'red': RED}

_ALPHA       = 0.15    # EMA factor for the gravity baseline
_DEBOUNCE_MS = 350     # ignore further taps within this window
_POLL_MS     = 5       # accelerometer sample interval


def _render(matrix, state, ka_level):
    if state == 'off':
        matrix.show_keepalive(ka_level)
    else:
        matrix.fill(_FILL[state])


async def run(config, matrix):
    t         = config['timer']
    threshold = int(t.get('tap_threshold', 400))
    axis      = t.get('tap_axis', 'x')
    ka_level  = int(t.get('keepalive_level', 4))
    print('Tap mode: threshold=%d  axis=%s  keepalive=%d' % (threshold, axis, ka_level))

    imu = TapReader()

    state_i = 0
    _render(matrix, _STATES[state_i], ka_level)

    ex = ey = ez = emag = 0.0
    seeded   = False
    last_tap = time.ticks_ms()

    while True:
        ax, ay, az = imu.read()
        mag = (ax * ax + ay * ay + az * az) ** 0.5

        if not seeded:
            ex, ey, ez, emag = float(ax), float(ay), float(az), mag
            seeded = True
        ex   += _ALPHA * (ax  - ex)
        ey   += _ALPHA * (ay  - ey)
        ez   += _ALPHA * (az  - ez)
        emag += _ALPHA * (mag - emag)

        if   axis == 'y':   dev = ay  - ey
        elif axis == 'z':   dev = az  - ez
        elif axis == 'mag': dev = mag - emag
        else:               dev = ax  - ex     # 'x' — default vertical axis

        now = time.ticks_ms()
        if abs(dev) > threshold and time.ticks_diff(now, last_tap) > _DEBOUNCE_MS:
            last_tap = now
            state_i = (state_i + 1) % len(_STATES)
            _render(matrix, _STATES[state_i], ka_level)
            print('tap -> %s' % _STATES[state_i])

        await asyncio.sleep_ms(_POLL_MS)
