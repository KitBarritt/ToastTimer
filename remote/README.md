# TimerCube BLE remote

A handheld remote that sets the cube's colour over Bluetooth LE and echoes the
cube's colour on its own LED. It needs **no cube firmware changes** — it talks
to the existing BLE mode (`ble_server.py`).

```
remote/
  tc_remote.py          shared BLE client (scan, connect, reconnect, send, state)
  esp32_buttons/main.py ESP32-S3-Zero + push buttons   (this build)
  pico_touch/           Pico W + touch screen          (planned, reuses tc_remote.py)
```

These files run on the **remote**, not the cube — they are deliberately not in
the cube's `manifest.json`, so OTA never touches them.

## Cube side

Boot the cube **right-side-down** to enter BLE mode (it shows `B`). Only one
BLE client can be connected at a time, so close any `ble.html` browser tab first.

## ESP32-S3-Zero build

Wire each button between its GPIO and GND (internal pull-ups are used):

| Button | GPIO | Sends |
|---|---|---|
| 1 | 4 | `off` |
| 2 | 5 | `green` |
| 3 | 6 | `amber` |
| 4 | 7 | `red` |

Change the `BUTTONS` table at the top of `esp32_buttons/main.py` to suit.

Upload (use `connect COMx` if the cube is also plugged in):

```
mpremote connect COMx fs cp remote/tc_remote.py :tc_remote.py
mpremote connect COMx fs cp remote/esp32_buttons/main.py :main.py
mpremote connect COMx reset
```

### LED

| LED | Meaning |
|---|---|
| red → green → blue at power-up | start-up test. If the order is wrong set `LED_ORDER = 'RGB'` |
| blue blinking | scanning / connecting |
| faint white | connected, cube colour `off` |
| green / amber / red | the cube's current colour (`flash` blinks red in step) |

The LED shows what the cube *reports* (state is broadcast every 0.5 s), so it
lags a button press by up to half a second but confirms the command landed.

Set `DEBUG = False` in `main.py` once it works to silence the link log.

## How it works

The cube advertises as `TimerCube` with a Nordic UART Service and exchanges
newline-delimited JSON. The remote scans for that name, connects, enables
notifications on the TX characteristic and writes
`{"type": "set_colour", "colour": "green"}` to the RX characteristic. It
remembers the cube's address for quick reconnects and drops/re-establishes the
link if no state message arrives for 8 s.
