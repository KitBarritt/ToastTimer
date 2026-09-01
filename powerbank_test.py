"""
powerbank_test.py  —  power-bank keep-alive load test for the ESP32-S3-Matrix.

Many USB power banks switch themselves off when the load drops below roughly
50-150 mA for a few seconds. This script lights a small pattern (a 'T' by
default, or the whole matrix) at an adjustable level so you can walk the
brightness DOWN and find the lowest constant LED load that still keeps your
bank awake.

Same hardware / same style as tap_test.py:
  LED matrix : WS2812B 8x8 on GPIO 14  (GRB byte order)

How to run from Thonny
  1. Save this file onto the board (same folder as main.py).
  2. In the REPL:            import powerbank_test
                             powerbank_test.run()
  3. CLICK IN THE SHELL PANE so it has focus, type a command, press Enter.
     Thonny only sends input one whole line at a time.
  4. Ctrl-C  or the command  q   turns the matrix off and returns to the REPL.

Live commands  (type, then Enter)
  b <n>        set level to n            (raw 0-255 written to each channel)
  +  [n]       raise level by n          (default: current 'step')
  -  [n]       lower level by n
  step <n>     set the +/- increment
  min | max    jump to level 1 / 255
  shape t | shape fill     show the 'T' glyph or light the whole matrix
  white|red|green|blue|off   pick the colour that is lit
  pulse        toggle pulse mode on/off  (brief flash instead of constant on)
  on <ms>      pulse ON  duration        off <ms>   pulse OFF duration
  t            reset the "held for" timer
  s            print current settings
  h  or  ?     print this help
  q            turn matrix off, quit to REPL

Finding the minimum
  Start higher than you need, then step DOWN one notch at a time. After each
  change the "held for" timer resets — leave it sitting and watch whether the
  bank cuts out. The lowest level that survives several minutes is your answer.
  The mA figure printed is a rough estimate of the LED load only; use an
  inline USB power meter for real numbers. It excludes the board's own draw
  (~80-150 mA depending on WiFi state).

  A power bank that needs periodic current *spikes* (not just a level) will
  never hold on a constant load — use 'pulse' mode to test that pattern.
"""

import machine
import neopixel
import time
import sys
import select

_MATRIX_PIN = 14
_MATRIX_N   = 64

# per-pixel current for ONE colour channel fully on (level 255). WS2812B is
# ~20 mA/channel spec; real-world effective draw is a bit lower. Rough only.
_MA_PER_CHANNEL_FULL = 18

# 'T' glyph — one bit per pixel, MSB = left column, row 0 = top.
# Row-major, non-serpentine, pixel 0 = top-left (matches led_matrix.py).
_T_ROWS = (
    0b00000000,
    0b01111110,   # top bar, cols 1-6
    0b00011000,   # stem, cols 3-4
    0b00011000,
    0b00011000,
    0b00011000,
    0b00011000,
    0b00000000,
)
_T_INDICES = tuple(y * 8 + x
                   for y, bits in enumerate(_T_ROWS)
                   for x in range(8) if bits & (1 << (7 - x)))

cfg = {
    'level':  2,         # raw byte value written to each active channel
    'colour': 'white',   # key of _CHANNELS
    'shape':  'T',       # 'T' | 'fill'
    'step':   1,         # +/- increment
    'pulse':  False,     # pulse mode instead of constant on
    'on_ms':  200,       # pulse ON duration
    'off_ms': 2000,      # pulse OFF duration
    'status': True,      # print the periodic status line
}

# channel pattern per colour, in GRB order (matches led_matrix.py on this board)
_CHANNELS = {
    'off':   (0, 0, 0),
    'white': (1, 1, 1),
    'red':   (0, 1, 0),
    'green': (1, 0, 0),
    'blue':  (0, 0, 1),
}


def _lit_count():
    return _MATRIX_N if cfg['shape'] == 'fill' else len(_T_INDICES)


def _make_np():
    np = neopixel.NeoPixel(machine.Pin(_MATRIX_PIN, machine.Pin.OUT), _MATRIX_N)

    def draw(level):
        g, r, b = _CHANNELS[cfg['colour']]
        on = (g * level, r * level, b * level)
        if cfg['shape'] == 'fill':
            for i in range(_MATRIX_N):
                np[i] = on
        else:
            lit = _T_INDICES
            for i in range(_MATRIX_N):
                np[i] = on if i in lit else (0, 0, 0)
        np.write()

    return draw


def _est_ma():
    chans = sum(_CHANNELS[cfg['colour']])
    on = _lit_count() * chans * _MA_PER_CHANNEL_FULL * cfg['level'] / 255
    if cfg['pulse']:
        period = cfg['on_ms'] + cfg['off_ms']
        if period > 0:
            on *= cfg['on_ms'] / period
    return on


_buf = ''


def _read_line(poll):
    """Non-blocking: return a completed input line (without newline) or None."""
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
  b <n>      set level 0-255        +/- [n]   raise / lower by n (default step)
  step <n>   set the +/- increment  min | max  jump to level 1 / 255
  shape t | shape fill   'T' glyph or whole matrix
  white | red | green | blue | off   pick the lit colour
  pulse      toggle pulse mode      on <ms> / off <ms>   pulse durations
  t          reset the held-for timer
  s          print settings         h | ?     this help
  q          matrix off, quit to REPL"""


def _print_help():
    print(_KEYS)


def _print_settings():
    line = ('  [settings] shape=%s  level=%d  colour=%s  step=%d  %d px  ~%d mA (LED load, est)'
            % (cfg['shape'], cfg['level'], cfg['colour'], cfg['step'],
               _lit_count(), int(_est_ma())))
    if cfg['pulse']:
        line += '  | PULSE on=%dms off=%dms' % (cfg['on_ms'], cfg['off_ms'])
    print(line)


def _apply_command(line, ctx):
    """Mutate cfg from a command line. Returns False to quit, True otherwise."""
    parts = line.split()
    if not parts:
        return True
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    if len(cmd) > 1 and cmd[0] in '+-' and cmd[1:].isdigit():
        arg, cmd = cmd[1:], cmd[0]

    def as_int(default):
        try:
            return int(arg)
        except (TypeError, ValueError):
            return default

    def clamp(v):
        return max(0, min(255, v))

    changed = True
    if cmd == 'b':
        cfg['level'] = clamp(as_int(cfg['level']))
    elif cmd in ('+', '-'):
        d = as_int(cfg['step'])
        cfg['level'] = clamp(cfg['level'] + (d if cmd == '+' else -d))
    elif cmd == 'step':
        cfg['step'] = max(1, as_int(cfg['step']))
        changed = False
    elif cmd == 'min':
        cfg['level'] = 1
    elif cmd == 'max':
        cfg['level'] = 255
    elif cmd == 'shape':
        a = (arg or '').lower()
        if a in ('t', 'fill'):
            cfg['shape'] = 'T' if a == 't' else 'fill'
        else:
            print('  ? shape t | shape fill')
            changed = False
    elif cmd in _CHANNELS:
        cfg['colour'] = cmd
    elif cmd == 'pulse':
        cfg['pulse'] = not cfg['pulse']
    elif cmd == 'on':
        cfg['on_ms'] = max(0, as_int(cfg['on_ms']))
    elif cmd == 'off':
        cfg['off_ms'] = max(0, as_int(cfg['off_ms']))
    elif cmd == 't':
        changed = True
    elif cmd == 's':
        _print_settings()
        changed = False
    elif cmd in ('h', '?', 'help'):
        _print_help()
        changed = False
    elif cmd == 'q':
        return False
    else:
        print('  ? unknown command: %r  (h for help)' % line)
        changed = False

    if changed:
        ctx['changed_at'] = time.ticks_ms()
        ctx['dirty'] = True
        _print_settings()
        print('  held-for timer reset')
    return True


def _fmt_elapsed(ms):
    s = ms // 1000
    return '%dm %02ds' % (s // 60, s % 60)


def run():
    draw = _make_np()

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)

    now = time.ticks_ms()
    ctx = {'changed_at': now, 'dirty': True}
    pulse_ref = now
    last_written = None
    last_status = now

    print('powerbank_test running — shape=%s at level %d (%s).'
          % (cfg['shape'], cfg['level'], cfg['colour']))
    _print_help()
    _print_settings()

    try:
        while True:
            now = time.ticks_ms()

            if cfg['pulse']:
                period = cfg['on_ms'] + cfg['off_ms']
                phase_on = period == 0 or (time.ticks_diff(now, pulse_ref) % period) < cfg['on_ms']
                target = cfg['level'] if phase_on else 0
            else:
                target = cfg['level']

            if ctx['dirty'] or target != last_written:
                draw(target)
                last_written = target
                ctx['dirty'] = False

            if cfg['status'] and time.ticks_diff(now, last_status) >= 2000:
                last_status = now
                print('level=%3d  %-5s  %s  ~%4d mA (est)  held for %s%s'
                      % (cfg['level'], cfg['colour'], cfg['shape'], int(_est_ma()),
                         _fmt_elapsed(time.ticks_diff(now, ctx['changed_at'])),
                         '  [PULSE]' if cfg['pulse'] else ''))

            line = _read_line(poll)
            if line is not None and not _apply_command(line, ctx):
                draw(0)
                print('stopped — matrix OFF, back to REPL.')
                return

            time.sleep_ms(10)

    except KeyboardInterrupt:
        draw(0)
        print('\ninterrupted — matrix OFF, back to REPL.')


if __name__ == '__main__':
    run()
