"""
TimerCube remote — ESP32-S3-Zero with push buttons.

Each button sends a colour to the cube over BLE (cube must be in BLE mode,
i.e. booted right-side-down). The on-board RGB LED echoes the cube's colour:

  blue blinking   scanning for / connecting to the cube
  faint white     connected, cube colour 'off'
  green/amber/red the cube's current colour (flash → red blinking in step)

The LED follows what the cube *reports*, not what was pressed, so it also
confirms the command arrived (and tracks the timer if it changes colour on
its own).

Needs tc_remote.py copied alongside this file. See remote/README.md.
"""

import asyncio
import neopixel
from machine import Pin

from tc_remote import TimerCubeLink

# ── configuration ─────────────────────────────────────────────────────────
CUBE_NAME = 'TimerCube'
DEBUG     = True              # print link progress; set False once it works

# (GPIO, colour sent) — wire each button between the pin and GND
BUTTONS = (
    (4, 'off'),
    (5, 'green'),
    (6, 'amber'),
    (7, 'red'),
)

DEBOUNCE_MS = 20              # a change must still hold after this long to count

LED_PIN    = 21               # ESP32-S3-Zero on-board WS2812
LED_ORDER  = 'GRB'            # wire order; if the start-up test isn't red→green→blue, try 'RGB'
LED_BRIGHT = 0.15             # the on-board LED is very bright

# (R, G, B) — same colours the cube's matrix uses
COLOURS = {
    'green': (0, 200, 0),
    'amber': (255, 140, 0),
    'red':   (220, 0, 0),
}
IDLE_GLOW = (4, 4, 4)         # connected + cube 'off'; (0, 0, 0) = fully dark
LINK_BLUE = (0, 0, 200)       # blinks while not connected
OFF       = (0, 0, 0)


def _dim(rgb):
    return tuple(int(c * LED_BRIGHT) for c in rgb)


class Led:
    def __init__(self):
        self._np = neopixel.NeoPixel(Pin(LED_PIN), 1)
        self._last = None

    def set(self, rgb):
        if rgb == self._last:
            return
        self._last = rgb
        r, g, b = rgb
        self._np[0] = (g, r, b) if LED_ORDER == 'GRB' else (r, g, b)
        self._np.write()


async def led_selftest(led):
    print('LED test: expect red, green, blue')
    for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
        led.set(_dim(rgb))
        await asyncio.sleep_ms(300)
    led.set(OFF)


async def led_task(led, link):
    tick = 0
    while True:
        if link.connected:
            state = link.state
            colour = state.get('colour') if state else 'off'
            if colour == 'flash':
                rgb = _dim(COLOURS['red']) if state.get('flash_on') else OFF
            elif colour in COLOURS:
                rgb = _dim(COLOURS[colour])
            else:
                rgb = IDLE_GLOW
        else:
            rgb = _dim(LINK_BLUE) if (tick // 5) % 2 == 0 else OFF   # 0.5 s on / 0.5 s off
        led.set(rgb)
        tick += 1
        await asyncio.sleep_ms(100)


async def send_colour(link, colour):
    if await link.set_colour(colour):
        print('sent', colour)
    else:
        print('not sent (%s):' % link.status, colour)


async def buttons_task(link):
    pins = [(Pin(gpio, Pin.IN, Pin.PULL_UP), colour) for gpio, colour in BUTTONS]
    last = [1] * len(pins)
    while True:
        for i, (pin, colour) in enumerate(pins):
            v = pin.value()
            if v != last[i]:
                await asyncio.sleep_ms(DEBOUNCE_MS)   # debounce: must still hold
                if pin.value() == v:
                    last[i] = v
                    if v == 0:                        # pressed (active low)
                        asyncio.create_task(send_colour(link, colour))
        await asyncio.sleep_ms(10)


async def main():
    led = Led()
    await led_selftest(led)
    link = TimerCubeLink(CUBE_NAME, debug=DEBUG)
    asyncio.create_task(led_task(led, link))
    asyncio.create_task(buttons_task(link))
    await link.run()


asyncio.run(main())
