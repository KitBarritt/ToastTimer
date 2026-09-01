"""
USB serial command server for TimerCube.

Communicates via newline-delimited JSON on stdin/stdout (Hardware CDC).
Protocol mirrors the WebSocket messages used by web_server.py so the
same message types work in both connection modes.

Reconnection: the browser sends "HELLO\n" at any time; the server
responds with "READY_OK\n" plus a full initial-state burst, allowing
the page to reconnect without a board reboot.

Architecture:
  _reader_thread  — blocks on sys.stdin.readline(), queues received lines
  _command_loop   — asyncio task; drains queue and dispatches handlers
  _broadcast_loop — asyncio task; sends timer state every 0.5 s
  _matrix_loop    — asyncio task; drives the LED matrix every 0.5 s
"""

import asyncio
import json
import os
import sys
import _thread

from timer_state import Timer, PRESETS
from config import save_config
from device_info import HARDWARE, HAS_BATTERY, HAS_LETTER_MODE

_SPEAKERS = '/data/speakers.json'


class UsbServer:

    def __init__(self, config, matrix):
        self.config   = config
        self.matrix   = matrix
        self.timer    = Timer(config)
        self.speakers = self._load_speakers()
        self._queue   = []
        self._lock    = _thread.allocate_lock()

    # ── public entry point ─────────────────────────────────────────────────

    async def run(self):
        self._send_initial_state()
        _thread.start_new_thread(self._reader_thread, ())
        asyncio.create_task(self._broadcast_loop())
        asyncio.create_task(self._matrix_loop())
        asyncio.create_task(self._command_loop())
        asyncio.create_task(self._running_indicator_loop())
        while True:
            await asyncio.sleep(3600)

    # ── protocol helpers ───────────────────────────────────────────────────

    def _send(self, obj):
        sys.stdout.write(json.dumps(obj) + '\n')

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

    # ── reader thread ──────────────────────────────────────────────────────

    def _reader_thread(self):
        while True:
            try:
                line = sys.stdin.readline().strip()
                if line:
                    self._lock.acquire()
                    self._queue.append(line)
                    self._lock.release()
            except Exception:
                pass

    # ── asyncio loops ──────────────────────────────────────────────────────

    async def _command_loop(self):
        while True:
            line = None
            self._lock.acquire()
            if self._queue:
                line = self._queue.pop(0)
            self._lock.release()

            if line:
                if line == 'HELLO':
                    # Browser reconnected — send fresh state without rebooting
                    sys.stdout.write('READY_OK\n')
                    self._send_initial_state()
                else:
                    try:
                        await self._handle_msg(json.loads(line))
                    except Exception as e:
                        print('USB cmd error:', e)

            await asyncio.sleep_ms(20)

    async def _broadcast_loop(self):
        while True:
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

    # ── WiFi scan ──────────────────────────────────────────────────────────

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
