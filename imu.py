"""
QMI8658 accelerometer – orientation detection at boot.

Hardware: Waveshare ESP32-S3-Matrix
  SoftI2C: SDA = GPIO 11, SCL = GPIO 12
  Device address: 0x6B  (WHO_AM_I = 0x05)

Quirk: CTRL7 must enable BOTH accel (bit 0) AND gyro (bit 1) = 0x03,
otherwise the data-ready flag never sets and all registers read zero.

Returns: 'vertical' | 'inverted' | 'left' | 'right' | 'unknown'

Axis → orientation mapping (confirmed by physical testing):
  Y negative dominant    → 'left'     (left side down)
  Y positive dominant    → 'right'    (right side down)
  X dominant & negative  → 'inverted' (upside-down → tap-cycle mode; ax ≈ -1 g)
  X or Z dominant (else) → 'vertical' (upright / any other face down; ax ≈ +1 g)

Raw count scale: 1 g ≈ 1060 counts (as measured on this board).
AXIS_THRESHOLD = 600 ≈ 0.56 g — rejects ambiguous near-45° positions.
"""

import machine
import time

_SDA  = 11
_SCL  = 12
_ADDR = 0x6B

_CTRL1    = 0x02   # address auto-increment
_CTRL2    = 0x03   # accelerometer ODR / full-scale
_CTRL7    = 0x08   # sensor enable  (must be 0x03, not 0x01)
_STATUS0  = 0x2E   # bit 0 = aDA (accelerometer data available)
_AX_L     = 0x35   # first of 6 consecutive accel bytes

AXIS_THRESHOLD = 600   # ≈ 0.56 g  (1 g ≈ 1060 counts on this board)


def _s16(lo, hi):
    v = (hi << 8) | lo
    return v - 65536 if v >= 32768 else v


def get_orientation():
    try:
        i2c = machine.SoftI2C(sda=machine.Pin(_SDA),
                               scl=machine.Pin(_SCL),
                               freq=400_000)

        i2c.writeto_mem(_ADDR, _CTRL1, b'\x40')   # address auto-increment
        i2c.writeto_mem(_ADDR, _CTRL2, b'\x60')   # 1 kHz ODR, ±2 g
        i2c.writeto_mem(_ADDR, _CTRL7, b'\x03')   # accel + gyro on (both needed)

        # Poll for data-ready (typically < 5 ms, timeout 200 ms)
        deadline = time.ticks_add(time.ticks_ms(), 200)
        while not (i2c.readfrom_mem(_ADDR, _STATUS0, 1)[0] & 0x01):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                print('IMU: data-ready timeout')
                return 'unknown'
            time.sleep_ms(5)

        raw = i2c.readfrom_mem(_ADDR, _AX_L, 6)
        ax = _s16(raw[0], raw[1])
        ay = _s16(raw[2], raw[3])
        az = _s16(raw[4], raw[5])
        print('IMU ax=%d ay=%d az=%d' % (ax, ay, az))

        aax, aay, aaz = abs(ax), abs(ay), abs(az)
        dominant = max(aax, aay, aaz)

        if dominant < AXIS_THRESHOLD:
            return 'unknown'   # cube near 45° — use safest default

        if aay >= aax and aay >= aaz:
            # Y axis runs left/right on this board
            return 'left' if ay < 0 else 'right'
        if aax >= aaz and ax < 0:
            # X is the vertical axis on this board: ax ≈ +1 g upright,
            # ≈ -1 g upside-down (measured ~±1000 counts).
            return 'inverted'
        return 'vertical'      # X or Z dominant → upright

    except Exception as e:
        print('IMU error:', e)
        return 'unknown'


class TapReader:
    """Holds the QMI8658 open for continuous accelerometer sampling.

    Used by tap_mode.py (upside-down 'traffic light' mode). Same register
    setup as get_orientation() — CTRL7 must be 0x03 or the data-ready flag
    never sets. read() returns a signed (ax, ay, az) count triple;
    1 g ≈ 1060 counts on this board.
    """

    def __init__(self):
        self.i2c = machine.SoftI2C(sda=machine.Pin(_SDA),
                                   scl=machine.Pin(_SCL),
                                   freq=400_000)
        self.i2c.writeto_mem(_ADDR, _CTRL1, b'\x40')   # address auto-increment
        self.i2c.writeto_mem(_ADDR, _CTRL2, b'\x60')   # 1 kHz ODR, ±2 g
        self.i2c.writeto_mem(_ADDR, _CTRL7, b'\x03')   # accel + gyro on (both needed)
        time.sleep_ms(20)

    def read(self):
        raw = self.i2c.readfrom_mem(_ADDR, _AX_L, 6)
        return (_s16(raw[0], raw[1]),
                _s16(raw[2], raw[3]),
                _s16(raw[4], raw[5]))
