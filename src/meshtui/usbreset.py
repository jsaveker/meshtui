"""Recover a wedged USB-CDC radio by resetting its USB device (Linux only).

ESP32 native-USB radios can wedge their CDC stack (seen after an unclean
host shutdown): the device stays enumerated and its tty node exists, but the
firmware stalls every SET_CONTROL_LINE_STATE request, so opening the port
dies with EPIPE before a single byte moves. Rebooting the host does not help
because USB ports keep power through a reboot. A USBDEVFS_RESET ioctl - the
software equivalent of unplug/replug - clears it.

Resetting needs write access to the device node under /dev/bus/usb, which
udev gives to root only by default; contrib/70-meshtui-usb.rules grants it
to the local user for known radio hardware.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

USBDEVFS_RESET = 0x5514  # _IO('U', 20) from <linux/usbdevice_fs.h>

UDEV_HINT = ("install contrib/70-meshtui-usb.rules into /etc/udev/rules.d "
             "to allow it, or unplug and replug the radio (power-cycle it "
             "if it has its own power source)")


def usb_device_node(tty_path: str) -> str | None:
    """Map a tty (/dev/ttyACM0) to its usbfs node (/dev/bus/usb/BBB/DDD).

    Follows /sys/class/tty/<name>/device, which points at the USB *interface*
    (e.g. .../3-2:1.0); busnum/devnum live on its parent, the USB device.
    """
    name = os.path.basename(tty_path or "")
    if not name:
        return None
    interface = os.path.realpath(f"/sys/class/tty/{name}/device")
    for candidate in (os.path.dirname(interface), interface):
        try:
            with open(os.path.join(candidate, "busnum"), encoding="ascii") as fh:
                bus = int(fh.read())
            with open(os.path.join(candidate, "devnum"), encoding="ascii") as fh:
                dev = int(fh.read())
        except (OSError, ValueError):
            continue
        return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    return None


def try_usb_reset(tty_path: str | None) -> tuple[bool, str]:
    """Send a USB bus reset to the device behind tty_path.

    Never raises: returns (ok, human-readable detail). After a successful
    reset the device re-enumerates, so the tty node vanishes for a moment
    before coming back - callers should wait before reopening.
    """
    if not tty_path:
        return False, "USB reset skipped: no serial port to reset"
    if sys.platform != "linux":
        return False, "USB reset is only available on Linux"
    node = usb_device_node(tty_path)
    if node is None:
        return False, f"USB reset skipped: {tty_path} is not a USB serial device"
    import fcntl

    try:
        fd = os.open(node, os.O_WRONLY)
    except PermissionError:
        return False, f"no permission to USB-reset {node}; {UDEV_HINT}"
    except OSError as exc:
        return False, f"USB reset of {node} failed: {exc}"
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as exc:
        return False, f"USB reset of {node} failed: {exc}"
    finally:
        os.close(fd)
    log.info("USB reset sent to %s (%s)", node, tty_path)
    return True, f"sent a USB reset to {node} ({tty_path}); waiting for it to re-enumerate"
