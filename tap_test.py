"""
tap_test.py  —  standalone tap-to-cycle-colour test for the ESP32-S3-Matrix.

Same hardware as the timer cube:
  LED matrix : WS2812B 8x8 on GPIO 14  (GRB byte order)
  IMU        : QMI8658 on SoftI2C  SDA=GPIO 11  SCL=GPIO 12  addr 0x6B

What it does
  * streams raw accelerometer counts to the REPL so you can calibrate
  * detects a tap on a chosen axis and steps the matrix colour:
        off  ->  green  ->  amber  ->  red  ->  off ...
  * every tunable can be changed LIVE — no stop / edit / re-run

How to run from Thonny
  1. Save this file onto the board (same folder as main.py).
  2. In the REPL:            import tap_test
                             tap_test.run()
  3. CLICK IN THE SHELL PANE so it has focus, type a command (see below)
     and press Enter. Thonny only sends input one whole line at a time.
     If keystrokes chime or go into the editor, the Shell isn't focused.
  4. Ctrl-C  or the command  q   returns to the REPL and blanks the matrix.

  (Single-key control with no Enter only works from a raw serial terminal
   such as `mpremote repl`, PuTTY or screen — not Thonny's Shell.)

Live commands  (type, then Enter)
  +  [n]        raise threshold by n        (default: current 'step')
  -  [n]        lower threshold by n
  t  <n>        set threshold to n
  step <n>      set the +/- increment
  a            cycle detection axis   x -> y -> z -> mag
  x | y | z    select that axis        mag   select vector magnitude
  alpha <f>    baseline filter factor 0..1 (higher tracks tilt faster)
  deb <n>      set debounce window in ms
  n            advance the colour manually
  r            reset colour to off
  p            pause / resume the data stream
  s            print current settings
  h  or  ?     print this help
  q            quit to REPL

Calibrating
  Leave it sitting still and note 'peak' (largest deviation since the last
  print) — that is your noise floor. Tap the top a few times and note the
  'peak' those produce. Set 'threshold' between the two. If taps on the
  wrong face also trigger it, switch 'axis'.
"""

import machine
import neopixel
import time
import sys
import select

# ── LED matrix ────────────────────────────────────────────────────────────
_MATRIX_PIN = 14
_MATRIX_N   = 64
BRIGHTNESS  = 0.15            # linear 0..1 scale applied to the colours below

# colours in GRB order (matches led_matrix.py on this board)
_COLOUR = {
    'off':   (0,   0,   0),
    'green': (200, 0,   0),
    'amber': (140, 255, 0),
    'red':   (0,   220, 0),
}
_STATES = ('off', 'green', 'amber', 'red')

# ── IMU (QMI8658) ─────────────────────────────────────────────────────────
_SDA, _SCL, _ADDR = 11, 12, 0x6B
_CTRL1, _CTRL2, _CTRL7 = 0x02, 0x03, 0x08
_STATUS0, _AX_L = 0x2E, 0x35
# 0x60 = 1 kHz ODR, +/-2 g  ->  1 g ~= 1060 counts (as measured on this board).
# A firm tap will clip at +/-2 g; that is fine for threshold detection. Bump to
# 0x63 (+/-8 g) if you want unclipped peaks — counts then scale ~4x smaller.
_CTRL2_VALUE = 0x60

# ── live-tunable settings ─────────────────────────────────────────────────
cfg = {
    'axis':        'z',      # 'x' | 'y' | 'z' | 'mag'
    'threshold':   400,      # counts of high-pass deviation that counts as a tap
    'step':        50,       # +/- increment for threshold
    'debounce_ms': 350,      # ignore further taps within this window
    'alpha':       0.15,     # EMA factor for the gravity baseline (0..1)
    'stream':      True,     # print the raw data stream
}

_AXES = ('x', 'y', 'z', 'mag')


def _s16(lo, hi):
    v = (hi << 8) | lo
    return v - 65536 if v >= 32768 else v


class _IMU:
    def __init__(self):
        self.i2c = machine.SoftI2C(sda=machine.Pin(_SDA),
                                   scl=machine.Pin(_SCL),
                                   freq=400_000)
        self.i2c.writeto_mem(_ADDR, _CTRL1, b'\x40')            # auto-increment
        self.i2c.writeto_mem(_ADDR, _CTRL2, bytes([_CTRL2_VALUE]))
        self.i2c.writeto_mem(_ADDR, _CTRL7, b'\x03')            # accel + gyro on
        # wait once for the first sample (data-ready never sets if CTRL7 != 0x03)
        deadline = time.ticks_add(time.ticks_ms(), 200)
        while not (self.i2c.readfrom_mem(_ADDR, _STATUS0, 1)[0] & 0x01):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError('IMU data-ready timeout — check wiring / address')
            time.sleep_ms(5)

    def read(self):
        r = self.i2c.readfrom_mem(_ADDR, _AX_L, 6)
        return _s16(r[0], r[1]), _s16(r[2], r[3]), _s16(r[4], r[5])


def _make_show():
    np = neopixel.NeoPixel(machine.Pin(_MATRIX_PIN, machine.Pin.OUT), _MATRIX_N)

    def show(state):
        c = _COLOUR[state]
        px = (int(c[0] * BRIGHTNESS), int(c[1] * BRIGHTNESS), int(c[2] * BRIGHTNESS))
        for i in range(_MATRIX_N):
            np[i] = px
        np.write()

    return show


_buf = ''


def _read_line(poll):
    """Non-blocking: return a completed input line (without newline) or None.

    Accumulates characters as they arrive. Thonny delivers a whole line at
    once on Enter; a raw terminal delivers keystrokes one at a time — both
    end in \\n / \\r, so both work here.
    """
    global _buf
    while poll.poll(0):
        c = sys.stdin.read(1)
        if not c:
            break
        if c in '\r\n':
            line, _buf = _buf, ''
            return line.strip()
        _buf += c
    return None


_KEYS = """Live commands (type, then Enter)
  +  [n]     raise threshold by n (default step)   -  [n]  lower it
  t <n>      set threshold          step <n>       set the +/- increment
  a          cycle axis x->y->z->mag
  x | y | z | mag   select axis directly
  alpha <f>  baseline filter 0..1   deb <n>        debounce window (ms)
  n          next colour            r              reset colour to off
  p          pause/resume stream    s              print settings
  h | ?      this help              q              quit to REPL"""


def _print_help():
    print(_KEYS)


def _print_settings():
    print('  [settings] axis=%s  threshold=%d  step=%d  debounce=%dms  alpha=%.2f  stream=%s'
          % (cfg['axis'], cfg['threshold'], cfg['step'],
             cfg['debounce_ms'], cfg['alpha'], cfg['stream']))


def _apply_command(line, ctx):
    """Mutate cfg / ctx from a command line. ctx carries colour state + show().
    Returns False to signal 'quit', True otherwise.
    """
    parts = line.split()
    if not parts:
        return True
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    # allow '+50' / '-50' as a single token
    if len(cmd) > 1 and cmd[0] in '+-' and cmd[1:].isdigit():
        arg, cmd = cmd[1:], cmd[0]

    def as_int(default):
        try:
            return int(arg)
        except (TypeError, ValueError):
            return default

    def as_float(default):
        try:
            return float(arg)
        except (TypeError, ValueError):
            return default

    if cmd in ('+', '-'):
        delta = as_int(cfg['step'])
        cfg['threshold'] = max(0, cfg['threshold'] + (delta if cmd == '+' else -delta))
        _print_settings()
    elif cmd == 't':
        cfg['threshold'] = max(0, as_int(cfg['threshold']))
        _print_settings()
    elif cmd == 'step':
        cfg['step'] = max(1, as_int(cfg['step']))
        _print_settings()
    elif cmd == 'a':
        cfg['axis'] = _AXES[(_AXES.index(cfg['axis']) + 1) % len(_AXES)]
        ctx['reseed'] = True
        _print_settings()
    elif cmd in ('x', 'y', 'z', 'mag'):
        cfg['axis'] = cmd
        ctx['reseed'] = True
        _print_settings()
    elif cmd == 'alpha':
        cfg['alpha'] = min(0.99, max(0.01, as_float(cfg['alpha'])))
        ctx['reseed'] = True
        _print_settings()
    elif cmd == 'deb':
        cfg['debounce_ms'] = max(0, as_int(cfg['debounce_ms']))
        _print_settings()
    elif cmd == 'n':
        ctx['state_i'] = (ctx['state_i'] + 1) % len(_STATES)
        ctx['show'](_STATES[ctx['state_i']])
        print('  -> %s' % _STATES[ctx['state_i']].upper())
    elif cmd == 'r':
        ctx['state_i'] = 0
        ctx['show'](_STATES[0])
        print('  colour reset -> OFF')
    elif cmd == 'p':
        cfg['stream'] = not cfg['stream']
        print('  stream =', cfg['stream'])
    elif cmd == 's':
        _print_settings()
    elif cmd in ('h', '?', 'help'):
        _print_help()
    elif cmd == 'q':
        return False
    else:
        print('  ? unknown command: %r  (h for help)' % line)
    return True


def run():
    show = _make_show()
    imu = _IMU()

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)

    ctx = {'state_i': 0, 'show': show, 'reseed': False}
    show(_STATES[0])

    ex = ey = ez = emag = 0.0
    seeded = False
    peak = 0
    now = time.ticks_ms()
    last_tap = now
    last_print = now

    print('tap_test running — matrix should be OFF. Tap the top to cycle colour.')
    _print_help()
    _print_settings()

    try:
        while True:
            ax, ay, az = imu.read()
            mag = (ax * ax + ay * ay + az * az) ** 0.5

            if not seeded or ctx['reseed']:
                ex, ey, ez, emag = float(ax), float(ay), float(az), mag
                seeded = True
                ctx['reseed'] = False
            a = cfg['alpha']
            ex   += a * (ax  - ex)
            ey   += a * (ay  - ey)
            ez   += a * (az  - ez)
            emag += a * (mag - emag)

            axis = cfg['axis']
            if   axis == 'x':   dev = ax  - ex
            elif axis == 'y':   dev = ay  - ey
            elif axis == 'z':   dev = az  - ez
            else:               dev = mag - emag
            adev = abs(dev)
            if adev > peak:
                peak = adev

            now = time.ticks_ms()

            if adev > cfg['threshold'] and time.ticks_diff(now, last_tap) > cfg['debounce_ms']:
                last_tap = now
                ctx['state_i'] = (ctx['state_i'] + 1) % len(_STATES)
                show(_STATES[ctx['state_i']])
                print('  >>> TAP  dev=%+d  axis=%s  ->  %s'
                      % (int(dev), axis, _STATES[ctx['state_i']].upper()))

            if cfg['stream'] and time.ticks_diff(now, last_print) >= 150:
                last_print = now
                print('ax=%6d ay=%6d az=%6d | %-3s dev=%+6d peak=%5d thr=%4d | %s'
                      % (ax, ay, az, axis, int(dev), int(peak),
                         cfg['threshold'], _STATES[ctx['state_i']].upper()))
                peak = 0

            line = _read_line(poll)
            if line is not None and not _apply_command(line, ctx):
                show(_STATES[0])
                print('stopped — matrix OFF, back to REPL.')
                return

            time.sleep_ms(3)

    except KeyboardInterrupt:
        show(_STATES[0])
        print('\ninterrupted — matrix OFF, back to REPL.')


if __name__ == '__main__':
    run()
