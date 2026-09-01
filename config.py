import json

CONFIG_FILE = '/config.json'

_DEFAULTS = {
    'wifi': {
        'networks': [],
        'ap_ssid': 'TimerCube',
        'ap_password': 'toastmaster',
    },
    'timer': {
        'brightness': 0.6,
        'keepalive_level': 4,     # raw 0-255 level for the dim idle 'T' that keeps a USB power bank awake (0 = blank)
        'tap_threshold': 400,     # high-pass deviation (counts) that registers as a tap in upside-down tap-cycle mode
        'tap_axis': 'x',          # accelerometer axis for tap detection: 'x' | 'y' | 'z' | 'mag' (X is vertical on this board)
    },
    'language': 'en',
}


def _merge(target, defaults):
    for key, val in defaults.items():
        if key not in target:
            target[key] = val
        elif isinstance(val, dict) and isinstance(target.get(key), dict):
            _merge(target[key], val)


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        _merge(data, _DEFAULTS)
        return data
    except Exception:
        import json as _j
        return _j.loads(_j.dumps(_DEFAULTS))  # deep copy of defaults


def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)


_ID_FILE = 'id.py'


def read_device_id():
    try:
        with open(_ID_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith('DEVICE_ID'):
                    val = line.split('=', 1)[1].strip()
                    return val.strip("'\"")
    except Exception:
        pass
    return ''


def save_device_id(new_id):
    try:
        # Store as a bare integer when the value is purely numeric
        try:
            as_int = int(new_id)
            if str(as_int) == str(new_id).strip() and as_int > 0:
                line = 'DEVICE_ID = %d\n' % as_int
            else:
                line = 'DEVICE_ID = %s\n' % json.dumps(str(new_id))
        except (ValueError, TypeError):
            line = 'DEVICE_ID = %s\n' % json.dumps(str(new_id))
        with open(_ID_FILE, 'w') as f:
            f.write(line)
        return True
    except Exception:
        return False
