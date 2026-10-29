"""
tc_remote.py — BLE remote-control client for TimerCube.

Acts as a BLE *central*: scans for a peripheral advertising as "TimerCube"
(the cube's BLE mode, see ble_server.py), connects, subscribes to its
Nordic UART TX characteristic and writes commands to its RX characteristic.
It reconnects automatically whenever the link drops.

  link = TimerCubeLink('TimerCube')
  asyncio.create_task(link.run())          # never returns
  ...
  await link.set_colour('green')           # off | green | amber | red | flash
  link.status                              # 'scanning' | 'connecting' | 'connected'
  link.state                               # latest state dict from the cube, or None

The cube sends newline-delimited JSON. Only {"type": "state", ...} lines are
kept — everything else (speakers, config, presets: several KB sent on connect)
is skipped without being buffered. Raw `bluetooth` IRQs are used rather than
aioble because aioble keeps only the *latest* notification, which would
corrupt the cube's MTU-chunked lines.

Uses only `bluetooth`, `asyncio`, `json` and `time`, so the same file runs on
ESP32 and Pico W MicroPython (1.21+ recommended).
"""

import asyncio
import bluetooth
import json
import time

_IRQ_SCAN_RESULT                 = 5
_IRQ_SCAN_DONE                   = 6
_IRQ_PERIPHERAL_CONNECT          = 7
_IRQ_PERIPHERAL_DISCONNECT       = 8
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE   = 12
_IRQ_GATTC_WRITE_DONE            = 17
_IRQ_GATTC_NOTIFY                = 18
_IRQ_MTU_EXCHANGED               = 21

# Nordic UART Service characteristics, named from the cube's point of view
_TX_UUID = bluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')   # cube → remote (notify)
_RX_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')   # remote → cube (write)

_SCAN_MS          = 4000    # one scan window before trying again
_CONNECT_MS       = 6000    # give up on a connection attempt after this
_STATE_TIMEOUT_MS = 8000    # cube broadcasts state every 0.5 s; silence this long = dead link
_WANT_MTU         = 256     # bigger MTU → a state message fits in one notification
_MAX_LINE         = 1024    # longer partial lines are not state messages — discard them


def _adv_name(adv):
    """Return the local name (bytes) from an advertising payload, or None."""
    i, n = 0, len(adv)
    while i + 1 < n:
        ln = adv[i]
        if ln == 0:
            break
        if adv[i + 1] in (0x08, 0x09):          # shortened / complete local name
            return bytes(adv[i + 2:i + 1 + ln])
        i += 1 + ln
    return None


async def _wait(flag, ms):
    """Wait for a ThreadSafeFlag; True if it fired, False on timeout."""
    try:
        await asyncio.wait_for_ms(flag.wait(), ms)
        return True
    except asyncio.TimeoutError:
        return False


class TimerCubeLink:

    def __init__(self, name='TimerCube', debug=False):
        self._name  = name.encode() if isinstance(name, str) else name
        self._debug = debug

        self.status = 'scanning'
        self.state  = None

        self._ble     = bluetooth.BLE()
        self._conn    = None
        self._cached  = None          # (addr_type, addr) of the last good cube
        self._found   = None
        self._tx      = None          # value handle: cube → remote
        self._rx      = None          # value handle: remote → cube
        self._mtu     = 23

        self._buf         = b''
        self._skip        = False     # discarding the rest of an oversize line
        self._state_line  = None      # newest unparsed state line
        self._rx_bytes    = 0
        self._write_status = 0
        self._wlock       = asyncio.Lock()

        # one-shot flags, set from the BLE IRQ
        self._scan_done  = asyncio.ThreadSafeFlag()
        self._connected  = asyncio.ThreadSafeFlag()
        self._mtu_evt    = asyncio.ThreadSafeFlag()
        self._chars_done = asyncio.ThreadSafeFlag()
        self._write_evt  = asyncio.ThreadSafeFlag()
        self._state_evt  = asyncio.ThreadSafeFlag()

    @property
    def connected(self):
        return self.status == 'connected'

    def _log(self, *args):
        if self._debug:
            print('[remote]', *args)

    # ── public API ─────────────────────────────────────────────────────────

    async def run(self):
        """Scan / connect / reconnect forever."""
        self._ble.active(True)
        self._ble.irq(self._irq)
        try:
            self._ble.config(mtu=_WANT_MTU)
        except Exception as e:
            self._log('mtu config unsupported:', e)
        while True:
            try:
                await self._cycle()
            except Exception as e:
                print('tc_remote error:', e)
                await asyncio.sleep_ms(1000)
            self._teardown()

    async def send(self, obj):
        """Send one JSON command to the cube. True if it was written."""
        if self.status != 'connected' or self._rx is None:
            return False
        data = (json.dumps(obj) + '\n').encode()
        async with self._wlock:
            step = max(20, self._mtu - 3)            # 3-byte ATT header
            for i in range(0, len(data), step):
                if self._conn is None:
                    return False
                self._write_evt.clear()
                try:
                    self._ble.gattc_write(self._conn, self._rx, data[i:i + step], 1)
                except OSError as e:
                    self._log('write error:', e)
                    return False
                if not await _wait(self._write_evt, 2000) or self._write_status != 0:
                    self._log('write failed, status', self._write_status)
                    return False
        return True

    async def set_colour(self, colour):
        return await self.send({'type': 'set_colour', 'colour': colour})

    # ── connection cycle ───────────────────────────────────────────────────

    async def _cycle(self):
        self.status = 'scanning'
        target = self._cached
        if target is None:
            target = await self._scan()
            if target is None:
                return                                # nothing found; scan again
            self._log('found cube', target[1])

        self.status = 'connecting'
        if not await self._connect(target):
            self._log('connect failed')
            self._cached = None                       # address may be stale — rescan
            await asyncio.sleep_ms(300)
            return

        if not await self._setup():
            self._log('setup failed')
            self._cached = None
            return

        self._cached = target
        self.status  = 'connected'
        self._log('connected, mtu', self._mtu)
        await self._session()
        self._log('link lost')

    async def _scan(self):
        self._found = None
        self._scan_done.clear()
        self._ble.gap_scan(_SCAN_MS, 30000, 30000, False)
        await _wait(self._scan_done, _SCAN_MS + 1000)
        return self._found

    async def _connect(self, target):
        self._conn = None
        self._connected.clear()
        self._ble.gap_connect(target[0], target[1])
        if not await _wait(self._connected, _CONNECT_MS):
            try:
                self._ble.gap_connect(None)           # cancel the attempt
            except OSError:
                pass
        return self._conn is not None

    async def _setup(self):
        """MTU exchange, find the NUS characteristics, enable notifications."""
        conn = self._conn
        self._tx = self._rx = None

        try:
            self._mtu_evt.clear()
            self._ble.gattc_exchange_mtu(conn)
            await _wait(self._mtu_evt, 1500)
        except (AttributeError, OSError) as e:
            self._log('mtu exchange skipped:', e)

        self._chars_done.clear()
        self._ble.gattc_discover_characteristics(conn, 1, 0xFFFF)
        if not await _wait(self._chars_done, 5000):
            return False
        if self._tx is None or self._rx is None:
            self._log('NUS characteristics not found')
            return False

        # Enable notifications: write 0x0001 to the CCCD (value handle + 1)
        self._write_evt.clear()
        self._ble.gattc_write(conn, self._tx + 1, b'\x01\x00', 1)
        if not await _wait(self._write_evt, 3000) or self._write_status != 0:
            self._log('subscribe failed')
            return False
        return True

    async def _session(self):
        """Parse incoming state messages until the link drops or goes quiet."""
        last = time.ticks_ms()
        while self._conn is not None:
            await _wait(self._state_evt, 1000)
            line = self._state_line
            if line is not None:
                self._state_line = None
                try:
                    d = json.loads(line.decode())
                    if isinstance(d, dict) and d.get('type') == 'state':
                        self.state = d
                        last = time.ticks_ms()
                except Exception:
                    pass
            elif time.ticks_diff(time.ticks_ms(), last) > _STATE_TIMEOUT_MS:
                self._log('no state from cube (rx %d bytes) — dropping link' % self._rx_bytes)
                try:
                    self._ble.gap_disconnect(self._conn)
                except OSError:
                    pass
                for _ in range(40):
                    if self._conn is None:
                        break
                    await asyncio.sleep_ms(50)
                break

    def _teardown(self):
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except OSError:
                pass
            self._conn = None
        self._tx = self._rx = None
        self.state = None
        self._buf = b''
        self._skip = False
        self._state_line = None
        self._rx_bytes = 0
        self._mtu = 23

    # ── notification framing ───────────────────────────────────────────────

    def _feed(self, chunk):
        """Reassemble newline-delimited lines; keep only state messages."""
        self._rx_bytes += len(chunk)
        if self._skip:                                # tail of an oversize line
            i = chunk.find(b'\n')
            if i < 0:
                return
            self._skip = False
            chunk = chunk[i + 1:]
        buf = self._buf + chunk
        while True:
            i = buf.find(b'\n')
            if i < 0:
                break
            line = buf[:i]
            buf = buf[i + 1:]
            if b'"state"' in line[:24]:
                self._state_line = line
                self._state_evt.set()
        if len(buf) > _MAX_LINE:                      # speakers/config/presets etc.
            buf = b''
            self._skip = True
        self._buf = buf

    # ── BLE IRQ ────────────────────────────────────────────────────────────

    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, _adv_type, _rssi, adv = data
            if self._found is None and _adv_name(adv) == self._name:
                self._found = (addr_type, bytes(addr))
                self._ble.gap_scan(None)              # stop; SCAN_DONE follows

        elif event == _IRQ_SCAN_DONE:
            self._scan_done.set()

        elif event == _IRQ_PERIPHERAL_CONNECT:
            conn, _addr_type, _addr = data
            self._conn = conn
            self._mtu = 23
            self._connected.set()

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            conn, _addr_type, _addr = data
            if conn == self._conn:
                self._conn = None
            self._connected.set()                     # also wakes a failed connect
            self._state_evt.set()                     # wakes _session

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn, _def_handle, value_handle, _props, uuid = data
            if conn == self._conn:
                if uuid == _TX_UUID:
                    self._tx = value_handle
                elif uuid == _RX_UUID:
                    self._rx = value_handle

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            self._chars_done.set()

        elif event == _IRQ_GATTC_WRITE_DONE:
            _conn, _handle, status = data
            self._write_status = status
            self._write_evt.set()

        elif event == _IRQ_GATTC_NOTIFY:
            conn, value_handle, notify_data = data
            if conn == self._conn and value_handle == self._tx:
                self._feed(bytes(notify_data))

        elif event == _IRQ_MTU_EXCHANGED:
            conn, mtu = data
            if conn == self._conn:
                self._mtu = mtu
                self._mtu_evt.set()
