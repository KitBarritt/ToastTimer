"""
8×8 WS2812B LED matrix driver for ESP32-S3-Matrix (Waveshare).
Data pin: GPIO 14  (confirmed from schematic).
Layout:  non-serpentine, row-major, pixel 0 = top-left, rows run left→right.

ROTATION — set to the clockwise angle the board is physically rotated inside
           its case (0, 90, 180, or 270).  Content is counter-rotated so it
           appears upright regardless of board orientation.
"""

import machine
import neopixel

MATRIX_PIN  = 14
MATRIX_SIZE = 8
ROTATION    = 0     # 0 / 90 / 180 / 270 degrees clockwise

# ── colours in GRB byte order (WS2812B on this board sends G, R, B) ────────
BLACK     = (0,   0,   0)
WHITE     = (200, 200, 200)
DIM_WHITE = (60,  60,  60)
GREEN     = (200, 0,   0)   # G=200 R=0   B=0
AMBER     = (140, 255, 0)   # G=140 R=255 B=0
RED       = (0,   220, 0)   # G=0   R=220 B=0
BLUE      = (0,   0,   200) # G=0   R=0   B=200

# ── 3×5 pixel font (3-bit rows, bit 2 = left col) ───────────────────────────
_FONT = {
    '0': [7, 5, 5, 5, 7],
    '1': [2, 6, 2, 2, 7],
    '2': [7, 1, 7, 4, 7],
    '3': [7, 1, 7, 1, 7],
    '4': [5, 5, 7, 1, 1],
    '5': [7, 4, 7, 1, 7],
    '6': [7, 4, 7, 5, 7],
    '7': [7, 1, 1, 1, 1],
    '8': [7, 5, 7, 5, 7],
    '9': [7, 5, 7, 1, 7],
    'P': [7, 5, 7, 4, 4],
    'S': [7, 4, 7, 1, 7],   # same glyph as 5
    'A': [2, 5, 7, 5, 5],
    'C': [7, 4, 4, 4, 7],
    'E': [7, 4, 6, 4, 7],
    'I': [7, 2, 2, 2, 7],   # top/bottom bars + centre stem
    '-': [0, 0, 7, 0, 0],   # middle horizontal bar
    ':': [0, 2, 0, 2, 0],   # two centre dots
    '?': [7, 1, 2, 0, 2],
    'U': [5, 5, 5, 5, 7],
    'B': [6, 5, 6, 5, 6],   # ##. / #.# / ##. / #.# / ##.
}

# Keep-alive 'T' — 8-row bitmap, MSB = left column, row 0 = top.
# Drawn at a raw level by Matrix.show_keepalive(): a dim idle display that
# pulls just enough current to stop a USB power bank auto-switching-off.
# Same shape as powerbank_test.py so a level tuned there carries straight over.
_KEEPALIVE_T = (
    0b00000000,
    0b01111110,   # top bar, cols 1-6
    0b00011000,   # stem, cols 3-4
    0b00011000,
    0b00011000,
    0b00011000,
    0b00011000,
    0b00000000,
)

# 8×8 perimeter path (28 pixels), clockwise from top-left, logical
# (unrotated) coordinates. Used by Matrix.show_chase() — WebServer's
# "running clock" indicator that laps the outer ring once per second while
# a timer runs with no threshold reached yet.
_PERIMETER = (
    [(x, 0) for x in range(MATRIX_SIZE)]
    + [(MATRIX_SIZE - 1, y) for y in range(1, MATRIX_SIZE - 1)]
    + [(x, MATRIX_SIZE - 1) for x in range(MATRIX_SIZE - 1, -1, -1)]
    + [(0, y) for y in range(MATRIX_SIZE - 2, 0, -1)]
)
PERIMETER_LEN = len(_PERIMETER)   # 28 on an 8×8 grid


class Matrix:
    def __init__(self):
        pin = machine.Pin(MATRIX_PIN, machine.Pin.OUT)
        self.np = neopixel.NeoPixel(pin, MATRIX_SIZE * MATRIX_SIZE)
        self.brightness = 0.6
        self._extra_rot = None   # per-call rotation override for show_char

    # ── coordinate helpers ─────────────────────────────────────────────────

    def _rotate(self, x, y):
        """
        Counter-rotate (x, y) to compensate for physical rotation.
        Uses _extra_rot if set (per-call override), else the module ROTATION constant.
        """
        S = MATRIX_SIZE - 1
        r = self._extra_rot if self._extra_rot is not None else ROTATION
        if r == 90:   return S - y, x
        if r == 180:  return S - x, S - y
        if r == 270:  return y, S - x
        return x, y

    def _idx(self, x, y):
        """Map logical (x, y) → NeoPixel index (non-serpentine, row-major)."""
        x, y = self._rotate(x, y)
        return y * MATRIX_SIZE + x

    def _scale(self, colour):
        """Scale colour by brightness² for perceptual linearity."""
        b = self.brightness * self.brightness
        return (int(colour[0] * b), int(colour[1] * b), int(colour[2] * b))

    # ── public API ─────────────────────────────────────────────────────────

    def set_brightness(self, value):
        self.brightness = max(0.0, min(1.0, float(value)))

    def clear(self):
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        self.np.write()

    def fill(self, colour):
        c = self._scale(colour)
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = c
        self.np.write()

    def _draw_char(self, char, colour, x_offset):
        """Draw a 3×5 glyph at x_offset; centred vertically (rows 1–5)."""
        glyph = _FONT.get(char, _FONT['?'])
        c = self._scale(colour)
        for row, bits in enumerate(glyph):
            y = row + 1
            for col in range(3):
                x = x_offset + col
                if 0 <= x < MATRIX_SIZE:
                    pixel_on = bool(bits & (1 << (2 - col)))
                    self.np[self._idx(x, y)] = c if pixel_on else BLACK

    def dot(self, colour):
        """Single centre pixel – subtle running indicator."""
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        self.np[self._idx(3, 3)] = self._scale(colour)
        self.np.write()

    def show_chase(self, head, colour, count=4):
        """Light `count` pixels evenly spaced around the 8×8 perimeter,
        starting at perimeter index `head` — the "running clock" indicator.
        Scaled by the configured brightness like fill()/show_char().
        """
        n = PERIMETER_LEN
        spacing = n // count
        c = self._scale(colour)
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        for k in range(count):
            x, y = _PERIMETER[(head + k * spacing) % n]
            self.np[self._idx(x, y)] = c
        self.np.write()

    def show_char(self, char, colour, rotation=None):
        """Show a single character centred on the 8×8 grid.

        rotation overrides the global ROTATION for this call only — use when
        the cube is on its side and the glyph must be pre-rotated so it reads
        correctly from the viewer's angle (e.g. rotation=270 when left-side-down,
        rotation=90 when right-side-down; swap if wrong after physical testing).
        """
        self._extra_rot = rotation
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        self._draw_char(char, colour, x_offset=2)
        self.np.write()
        self._extra_rot = None

    def show_keepalive(self, level):
        """Dim 'T' at a raw 0–255 level, bypassing the brightness curve.

        Idle display used across all modes so the matrix is never fully dark
        on battery — the small constant current stops a USB power bank from
        switching itself off.  level 0 → blank.
        """
        lvl = max(0, min(255, int(level)))
        if lvl == 0:
            self.clear()
            return
        px = (lvl, lvl, lvl)
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        for y, bits in enumerate(_KEEPALIVE_T):
            for x in range(MATRIX_SIZE):
                if bits & (1 << (7 - x)):
                    self.np[self._idx(x, y)] = px
        self.np.write()

    def show_two_chars(self, c1, c2, colour1, colour2):
        """Show two characters side by side (cols 0–2 and 4–6)."""
        for i in range(MATRIX_SIZE * MATRIX_SIZE):
            self.np[i] = BLACK
        self._draw_char(c1, colour1, x_offset=0)
        self._draw_char(c2, colour2, x_offset=4)
        self.np.write()
