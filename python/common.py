"""Small helpers shared by the app modules."""

from datetime import datetime

from arduino.app_utils import Bridge


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def mcu_call(name, *args, timeout=5):
    """Call to the sketch that does not bring the loop down if the Bridge does not answer."""
    try:
        return Bridge.call(name, *args, timeout=timeout)
    except Exception as exc:
        print(f"[{now_str()}] Bridge.call({name}) fallita: {exc}")
        return None
