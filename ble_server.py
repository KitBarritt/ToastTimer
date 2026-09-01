"""
Bluetooth Low Energy server for TimerCube.

Advertises as a Nordic UART Service (NUS) peripheral named "TimerCube".
The browser page (web/ble.html) connects via the Web Bluetooth API and
exchanges the same newline-delimited JSON protocol used by UsbServer.

Outgoing messages are chunked into MTU-sized BLE notifications; the
browser reassembles on '\n', exactly as the USB page does.

Architecture:
  _irq            — BLE stack callback; queues received lines, tracks state
  _command_loop   — asyncio task; drains queue, sends initial state on connect
  _broadcast_loop — asyncio task; pushes timer state every 0.5 s
  _matrix_loop    — asyncio task; drives the LED matrix every 0.5 s
"""

import asyncio
import bluetooth
import json
import os

from timer_state import Timer, PRESETS
from config import save_config
from device_info import HARDWARE, HAS_BATTERY, HAS_LETTER_MODE

_SPEAKERS = '/data/speakers.json'

# ── Nordic UART Service UUIDs ──────────────────────────────────────────────
_NUS_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
_TX_UUID  = bluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')  # board → central
_RX_UUID  = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')  # central → board

_FLAG_READ              = 0x0002
_FLAG_WRITE_NO_RESPONSE = 0x0004
_FLAG_WRITE             = 0x0008
_FLAG_NOTIFY            = 0x0010

_IRQ_CENTRAL_CONNECT    = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE        = 3
_IRQ_MTU_EXCHANGED      = 21

_NUS_SERVICE = (
    _NUS_UUID,
    (
        (_TX_UUID, _FLAG_NOTIFY | _FLAG_READ),
        (_RX_UUID, _FLAG_WRITE  | _FLAG_WRITE_NO_RESPONSE),
    ),
)


class BleServer:

    def __init__(self, config, matrix):
        self.config   = config
        self.matrix   = matrix
        self.timer    = Timer(config)
        self.speakers = self._load_speakers()

        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)

        ((self._tx_handle, self._rx_handle),) = \
            self._ble.gatts_register_services((_NUS_SERVICE,))

        self._conn_handle    = None
        self._mtu            = 20     # updated by _IRQ_MTU_EXCHANGED
        self._rx_buf         = b''
        self._cmd_queue      = []
        self._just_connected = False  # set in IRQ, consumed in _command_loop

    # ── public entry point ─────────────────────────────────────────────────

    async def run(self):
        self._advertise()
        asyncio.create_task(self._command_loop())
        asyncio.create_task(self._broadcast_loop())
        asyncio.create_task(self._matrix_loop())
        asyncio.create_task(self._running_indicator_loop())
        while True:
            await asyncio.sleep(3600)

    # ── BLE IRQ ────────────────────────────────────────────────────────────

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._conn_handle    = conn_handle
            self._just_connected = True

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn_handle = None
            self._rx_buf      = b''
            self._advertise()          # restart advertising for next connection

        elif event == _IRQ_GATTS_WRITE:
            conn_handle, value_handle = data
            if value_handle == self._rx_handle:
                self._rx_buf += self._ble.gatts_read(self._rx_handle)
                while b'\n' in self._rx_buf:
                    line, self._rx_buf = self._rx_buf.split(b'\n', 1)
                    line = line.strip()
                    if line:
                        self._cmd_queue.append(line)

        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data
            if conn_handle == self._conn_handle:
                self._mtu = mtu

    # ── advertising ────────────────────────────────────────────────────────

    def _advertise(self, interval_us=100_000):
        name = b'TimerCube'
        adv  = bytes([2, 0x01, 0x06,
                      len(name) + 1, 0x09]) + name
        self._ble.gap_advertise(interval_us, adv_data=adv)

    # ── protocol helpers ───────────────────────────────────────────────────

    def _send(self, obj):
        if self._conn_handle is None:
            return
        data       = (json.dumps(obj) + '\n').encode()
        chunk_size = max(20, self._mtu - 3)   # 3-byte ATT header overhead
        for i in range(0, len(data), chunk_size):
            try:
                self._ble.gatts_notify(
                    self._conn_handle, self._tx_handle, data[i:i + chunk_size])
            except Exception as e:
                print('BLE send error:', e)

    def _send_initial_state(self):
        self._send({
            'type': 'device_info',
            'hardware': HARDWARE,
            'has_battery': HAS_BATTERY,
            'has_letter_mode': HAS_LETTER_MODE,
        })
        self._send({'type': 'speakers', 'speakers': self.speakers})
        self._send({'type': 'config',   'config':   self.config})
        self._send({'type': 'presets',  'presets':  PRESETS})
        state = self.timer.get_state()
        d = {'type': 'state'}
        d.update(state)
        self._send(d)

    # ── asyncio loops ──────────────────────────────────────────────────────

    async def _command_loop(self):
        while True:
            if self._just_connected:
                self._just_connected = False
                # Brief delay: allow MTU exchange and CCCD subscription to complete
                await asyncio.sleep_ms(300)
                self._send_initial_state()

            if self._cmd_queue:
                line = self._cmd_queue.pop(0)
                try:
                    await self._handle_msg(json.loads(line))
                except Exception as e:
                    print('BLE cmd error:', e)

            await asyncio.sleep_ms(20)

    async def _broadcast_loop(self):
        while True:
            if self._conn_handle is not None:
                state = self.timer.get_state()
                d = {'type': 'state'}
                d.update(state)
                self._send(d)
            await asyncio.sleep(0.5)

    async def _matrix_loop(self):
        from led_matrix import GREEN, AMBER, RED
        flash_on = True
        while True:
            try:
                state  = self.timer.get_state()
                colour = state['colour']
                ka_level = int(self.config['timer'].get('keepalive_level', 4))
                if colour == 'off':
                    if state['running']:
                        pass    # _running_indicator_loop owns this state
                    else:
                        self.matrix.show_keepalive(ka_level)
                elif colour == 'green':
                    self.matrix.fill(GREEN)
                elif colour == 'amber':
                    self.matrix.fill(AMBER)
                elif colour == 'red':
                    self.matrix.fill(RED)
                elif colour == 'flash':
                    flash_on = not flash_on
                    self.timer.flash_on = flash_on
                    self.matrix.fill(RED) if flash_on else self.matrix.clear()
            except Exception as e:
                print('Matrix loop error:', e)
            await asyncio.sleep(0.5)

    async def _running_indicator_loop(self):
        """'Running clock' chase — see WebServer._running_indicator_loop
        for the full explanation. Shown only while running with no
        threshold reached yet; _matrix_loop owns every other state."""
        import time
        from led_matrix import WHITE, PERIMETER_LEN
        step_ms = 1000 // PERIMETER_LEN
        while True:
            try:
                state = self.timer.get_state()
                if state['colour'] == 'off' and state['running']:
                    head = (time.ticks_ms() // step_ms) % PERIMETER_LEN
                    self.matrix.show_chase(head, WHITE)
                    await asyncio.sleep_ms(step_ms)
                    continue
            except Exception as e:
                print('Running indicator error:', e)
            await asyncio.sleep_ms(150)

    # ── message dispatcher ─────────────────────────────────────────────────

    async def _handle_msg(self, msg):
        t = msg.get('type')

        if t == 'start':
            self.timer.start()

        elif t == 'stop':
            self.timer.stop()
            state = self.timer.get_state()
            if state['elapsed'] > 0 and state['speaker']:
                self._record_actual(state['speaker'], state['elapsed'])

        elif t == 'reset':
            self.timer.reset()

        elif t == 'set_colour':
            self.timer.set_colour(msg.get('colour', 'off'))

        elif t == 'set_thresholds':
            self.timer.set_thresholds(msg.get('thresholds', [0, 0, 0, 0]))

        elif t == 'set_speaker':
            self.timer.speaker = msg.get('speaker', '')

        elif t == 'set_brightness':
            b = float(msg.get('brightness', 0.6))
            self.timer.brightness = b
            self.matrix.set_brightness(b)
            self.config['timer']['brightness'] = b
            save_config(self.config)

        elif t == 'get_state':
            state = self.timer.get_state()
            d = {'type': 'state'}
            d.update(state)
            self._send(d)

        elif t == 'get_speakers':
            self._send({'type': 'speakers', 'speakers': self.speakers})

        elif t == 'save_speakers':
            self.speakers = msg.get('speakers', [])
            self._save_speakers()
            self._send({'type': 'speakers_saved', 'ok': True})

        elif t == 'clear_actuals':
            for s in self.speakers:
                s['actual'] = None
            self._save_speakers()
            self._send({'type': 'speakers', 'speakers': self.speakers})

        elif t == 'get_config':
            self._send({'type': 'config', 'config': self.config})

        elif t == 'save_config':
            new_cfg = msg.get('config', {})
            for k in ('wifi', 'timer'):
                if k in new_cfg:
                    self.config[k].update(new_cfg[k])
            if 'language' in new_cfg:
                self.config['language'] = new_cfg['language']
            save_config(self.config)
            self._send({'type': 'config_saved', 'ok': True})

        elif t == 'get_device_id':
            from config import read_device_id
            self._send({'type': 'device_id', 'id': read_device_id()})

        elif t == 'save_device_id':
            from config import save_device_id
            ok = save_device_id(msg.get('id', ''))
            self._send({'type': 'device_id_saved', 'ok': ok})

        elif t == 'get_version':
            try:
                from version import VERSION, VERSION_DATE
                self._send({'type': 'version', 'version': VERSION, 'date': VERSION_DATE})
            except Exception:
                self._send({'type': 'version', 'version': '?', 'date': '?'})

        elif t == 'wifi_scan':
            asyncio.create_task(self._wifi_scan_task())

        elif t == 'reboot':
            self._send({'type': 'rebooting'})
            await asyncio.sleep(0.8)
            import machine
            machine.reset()

    # ── WiFi scan (activates radio temporarily while staying in BLE mode) ──

    async def _wifi_scan_task(self):
        try:
            import network as _net
            sta = _net.WLAN(_net.STA_IF)
            was_active = sta.active()
            sta.active(True)
            await asyncio.sleep(0.5)
            raw = sta.scan()
            seen = set()
            nets = []
            for r in raw:
                try:
                    ssid = r[0].decode('utf-8') if isinstance(r[0], bytes) else str(r[0])
                except Exception:
                    ssid = ''
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    nets.append({'ssid': ssid, 'rssi': r[3], 'auth': r[4]})
            nets.sort(key=lambda x: -x['rssi'])
            if not was_active:
                sta.active(False)
        except Exception as e:
            print('WiFi scan error:', e)
            nets = []
        self._send({'type': 'wifi_scan_result', 'networks': nets})

    # ── persistence helpers ────────────────────────────────────────────────

    def _load_speakers(self):
        try:
            with open(_SPEAKERS) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_speakers(self):
        try:
            os.mkdir('/data')
        except OSError:
            pass
        with open(_SPEAKERS, 'w') as f:
            json.dump(self.speakers, f)

    def _record_actual(self, name, elapsed):
        for s in self.speakers:
            if s.get('name') == name and s.get('actual') is None:
                s['actual'] = round(elapsed)
                self._save_speakers()
                return
