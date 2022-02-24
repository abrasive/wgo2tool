#!/usr/bin/env python3

from typing import Optional
import hid  # cython-hidapi, specifically
import struct
import enum
import time
import calendar
import atexit
from pathlib import Path
import fs
from dateutil import tz
from convert_recording import *

class TXOption(enum.IntEnum):
    BATTERY_PERCENT         = 0x00
    CLOCK                   = 0x01
    CLOCK_LAST_SET          = 0x02

    BUTTON_MODE_0           = 0x04 #  1: button=marker or button=nothing, 0: button=mute
    RECORDING_ENABLE        = 0x05
    RECORDING_UNCOMPRESSED  = 0x06

    BUTTON_MODE_1           = 0xa   # 1: button=marker, 0: button=mute or button=nothing

    LED_BRIGHTNESS          = 0xc   # fe: dim, df: bright, 00: super bright, ff: off
    RECORDING_BACKUP        = 0xd   # 1: backup, 0: always, if RECORDING_ENABLE set
    PAD_DISABLE             = 0xe   # 0: pad, 1: no pad

class TXCommand(enum.IntEnum):
    GET_SERIAL              = 0x33
    ENABLE_MASS_STORAGE     = 0x51
    ENABLE_ATE_MODE         = 0x41
    ERASE_RECORDINGS        = 0x4a
    MAYBE_REBOOT            = 0x52
    GET_FIRMWARE_VERSION    = 0x56

class Wgo2TX(object):
    def __init__(self, serial: Optional[str] = None):
        self.serial = serial
        self.dev = hid.device()
        atexit.register(self.dev.close)
        self.reconnect(retry=False)

    def find_devices(self):
        for result in hid.enumerate():
            if result['vendor_id'] == 0x19f7 and result['product_id'] in [0x20, 0x2d]:
                yield result

    def reconnect(self, retry=True):
        self.dev.close()

        start_time = time.time()

        while time.time() < start_time + 10:
            for device in self.find_devices():
                try:
                    # in ATE mode, the device doesn't expose its s/n via the USB descriptor string, so we have to manually search for it.
                    self.dev.open_path(device['path'])
                    self.dev.read(128, timeout_ms=10)
                    serial_bytes = self.command1(TXCommand.GET_SERIAL)[:4]
                    serial_str = ''.join('%02X' % x for x in serial_bytes)

                    if self.serial and serial_str != self.serial:
                        continue

                    self.serial = serial_str
                    self.usb_path = device['path']
                    return

                except OSError:
                    if not retry:
                        raise

            time.sleep(0.1)

    def raw_command(self, report_id: int, report_len: int, command: int, argbytes=[]):
        if command < 0 or command > 0xff:
            raise ValueError("command value out of range")

        arg = bytearray(report_len + 1)
        arg[0] = report_id
        arg[1] = command

        if argbytes:
            assert len(argbytes) <= len(arg)-2
            arg[2:2+len(argbytes)] = argbytes

        self.dev.write(arg)
        result = self.dev.read(report_len + 1)

        if result[0] != report_id+1:
            raise ValueError(f"expected report ID 2 but got {result}")

        if result[1] != command:
            raise ValueError(f"expected command 0x{command:x} but got {result}")

        if result[2] == 0x4e:
            raise ValueError(f"command 0x{command:x} not recognised by device")

        if result[2] != 0x41:
            raise ValueError(f"command did not succeed")

        return bytearray(result[3:])

    def command1(self, command, argbytes=[]):
        return self.raw_command(1, 8, command, argbytes)

    def command9(self, command, argbytes=[]):
        return self.raw_command(9, 27, command, argbytes)

    def option_read_bool(self, option):
        return bool(self.option_read_byte(option))

    def option_read_byte(self, option):
        return self.command9(option, [1])[0]

    def option_read_long(self, option):
        result = self.command9(option, [1])
        return struct.unpack('<L', result[:4])[0]

    def option_write_bool(self, option, value: bool):
        self.option_write_byte(option, int(value))

    def option_write_byte(self, option, value: int):
        value_bytes = struct.pack('B', value)
        self.command9(option, [0] + list(value_bytes))

    def option_write_long(self, option, value):
        value_bytes = struct.pack('<L', value)
        self.command9(option, [0] + list(value_bytes))

    @property
    def battery(self):
        return self.option_read_byte(TXOption.BATTERY_PERCENT)

    @property
    def clock(self):
        return self.option_read_long(TXOption.CLOCK)

    @property
    def clock_last_set(self):
        return self.option_read_long(TXOption.CLOCK_LAST_SET)

    def sync_clock(self):
        # Rode set the clock to local time
        local_clock = calendar.timegm(time.localtime(time.time()))
        self.option_write_long(TXOption.CLOCK, int(local_clock))


    def enable_mass_storage(self):
        self.dev.write([1, TXCommand.ENABLE_MASS_STORAGE, 0, 0, 0, 0, 0, 0, 0])
        self.reconnect()


    def enable_ate_mode(self):
        # Factory test mode. Acts as a USB microphone.
        # Doesn't respond to the command before reenumerating, so don't use command1()
        self.dev.write([1, TXCommand.ENABLE_ATE_MODE, 0, 0, 0, 0, 0, 0, 0])
        self.reconnect()

    def erase_recordings(self):
        self.command1(TXCommand.ERASE_RECORDINGS)
        self.reconnect()

    def reboot(self):
        # Exits ATE mode.
        self.command1(TXCommand.MAYBE_REBOOT)
        self.reconnect()

    @property
    def firmware_version(self):
        raw = self.command1(TXCommand.GET_FIRMWARE_VERSION)
        major = raw[1]
        minor = raw[0] >> 4
        patch = raw[0] & 0xf
        return major, minor, patch

    @property
    def firmware_version_str(self):
        return '.'.join(map(str, self.firmware_version))

class DeviceFileBrowser(object):
    def __init__(self, tx: 'Wgo2TX'):
        # find the USB device, which is the 1.1 to the HID's 1.0
        usb_name = tx.usb_path[:-1].decode('ascii') + '1'
        usb_path = Path('/sys/bus/usb/devices') / usb_name
        devices = list(usb_path.glob('host*/target*/*:*:*:*/block/*'))
        assert len(devices) == 1
        device = devices[0].name
        self.serial = tx.serial

        self.fs = fs.open_fs(f'fat:///dev/{device}?read_only=True')

    def get_ugg_files(self):
        return self.fs.walk.files(filter=['*.UGG'])

    def convert_all(self, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(exist_ok=True)
        for file in self.get_ugg_files():
            file_time = self.fs.getmodified(file).astimezone(tz.tzlocal())

            outname = dest_dir / (file_time.strftime('%Y%m%d_%H%M%S_') + self.serial + '.flac')

            eggname = eggname_from_uggname(file)
            with self.fs.open(file, 'rb') as ugg, self.fs.open(eggname, 'rb') as egg:
                convert_ugg(ugg, egg, file_time.timestamp(), str(outname))

if __name__ == "__main__":
    dev = Wgo2TX()

    print(time.gmtime(dev.option_read_long(TXOption.CLOCK)))
    dev.sync_clock()
    print(time.gmtime(dev.option_read_long(TXOption.CLOCK)))

    print("Connected to TX s/n", dev.serial)
    print("Firmware version", dev.firmware_version_str)

    dev.enable_mass_storage()

    files = DeviceFileBrowser(dev)
    files.convert_all('out')
