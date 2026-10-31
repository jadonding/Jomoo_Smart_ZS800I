"""Run inside HA Container to check real imports and optional BLE query.

Usage: python verify_runtime.py [--address MAC]
No Home Assistant auth files are read or changed. No device settings are written.
"""
import argparse
import asyncio
import importlib
import json
import sys

sys.path.insert(0, '/config')

async def main():
    for name in ('config_flow', 'coordinator', 'sensor', 'binary_sensor', 'button', 'switch', 'number'):
        importlib.import_module('custom_components.jomoo_zs800i.' + name)
        print('IMPORT OK:', name, flush=True)
    if not args.address:
        return
    from bleak import BleakClient, BleakScanner
    from custom_components.jomoo_zs800i.protocol import FrameParser, decode_source_fields, status_command
    from custom_components.jomoo_zs800i.const import CHAR_UUID, QUERY
    results = await BleakScanner.discover(timeout=12, return_adv=True)
    target = None
    for dev, adv in results.values():
        if dev.address.upper() == args.address.upper():
            target = dev
            name = adv.local_name or dev.name or ''
            if name != 'JOMOO_SMART_237371':
                raise RuntimeError('Unexpected model advertisement')
            print('TARGET OK:', name, adv.rssi, flush=True)
    if target is None:
        raise RuntimeError('Target not discovered by container BLE')
    fields = {}
    commands = set()
    parser = FrameParser()
    async with BleakClient(target, timeout=20) as client:
        char = client.services.get_characteristic(CHAR_UUID)
        if char is None or 'notify' not in char.properties:
            raise RuntimeError('FFE1 notify missing')
        response = 'write' in char.properties
        if not response and 'write-without-response' not in char.properties:
            raise RuntimeError('FFE1 write property missing')
        def notify(sender, raw):
            for frame in parser.feed(raw):
                cmd = status_command(frame)
                if cmd is None:
                    continue
                commands.add(f'{cmd:02X}')
                fields.update(decode_source_fields(frame))
                print('FRAME:', frame.hex(' '), flush=True)
        await client.start_notify(char, notify)
        try:
            await client.write_gatt_char(char, QUERY, response=response)
            await asyncio.sleep(8)
        finally:
            await client.stop_notify(char)
    result = {'commands': sorted(commands), 'source_derived_fields': fields,
              'valid_status_received': bool(commands)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not commands:
        raise RuntimeError('No valid status notifications')

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--address')
args = p.parse_args()
asyncio.run(main())
