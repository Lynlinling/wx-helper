from __future__ import annotations

import argparse
import ctypes as c
import sys
import time
from pathlib import Path

import pefile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'native'))

from run_probe_42 import K, check, load_remote, modules, remote_call, select_target
from wxbot_framework.account import read_current_account_42
from wxbot_framework.wechat_42 import verify_wechat_42_core

PROBE = ROOT / 'native/build_schedule_42/schedule_probe_42.dll'
EXPORTS = {'WxScheduleNoop42', 'WxScheduleNoopStatus42', 'WxScheduleNoopResult42'}
ALLOWED_IMPORTS = {'kernel32.dll', 'ntdll.dll', 'ucrtbase.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'}


def verify_probe(path: Path) -> dict[str, int]:
    pe = pefile.PE(str(path))
    if pe.FILE_HEADER.Machine != 0x8664:
        raise RuntimeError('schedule probe DLL is not x64')
    exports = {item.name.decode('ascii'): item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name}
    if set(exports) != EXPORTS:
        raise RuntimeError('schedule probe exports differ from its audited no-op interface')
    imports = {item.dll.decode('ascii').lower() for item in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', [])}
    unexpected = imports - ALLOWED_IMPORTS
    if unexpected:
        raise RuntimeError('schedule probe has unexpected imports: ' + ', '.join(sorted(unexpected)))
    return exports


def run_probe() -> None:
    if c.sizeof(c.c_void_p) != 8:
        raise RuntimeError('64-bit Python is required')
    if not PROBE.is_file():
        raise RuntimeError('probe DLL missing; run tools/build_schedule_probe_42.ps1 first')
    exports = verify_probe(PROBE)
    pid, bound_account = select_target()
    loaded = modules(pid)
    core = loaded.get('weixin.dll')
    if not core:
        raise RuntimeError('WeChat core module disappeared')
    verify_wechat_42_core(core[1])
    if {'text_bridge.dll', 'selftest_bridge.dll'} & set(loaded):
        raise RuntimeError('legacy bridge already loaded; refusing to overlap native modules')
    if PROBE.name.lower() in loaded:
        raise RuntimeError('schedule probe is already loaded; refusing to submit again')
    process = check(K.OpenProcess(0x43A, False, pid), 'open verified WeChat process')
    state = {'thread_pending': False}
    try:
        def read(address: int, size: int) -> bytes:
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), 'recheck bound account')
            return buffer.raw[:count.value]

        if read_current_account_42(read, core[0]) != bound_account:
            raise RuntimeError('bound account changed; probe not loaded')
        probe_base = load_remote(process, pid, PROBE, state)
        try:
            result = remote_call(process, probe_base + exports['WxScheduleNoop42'], core[0], timeout=10000)
        except TimeoutError:
            state['thread_pending'] = True
            raise
        if result != 0:
            raise RuntimeError('no-op scheduler call returned status ' + str(result) + '; probe module remains loaded; do not rerun')

        deadline = time.monotonic() + 10.0
        last_status = 0
        while time.monotonic() < deadline:
            try:
                last_status = remote_call(process, probe_base + exports['WxScheduleNoopStatus42'], 0, timeout=5000)
            except TimeoutError:
                state['thread_pending'] = True
                raise
            if last_status & 0x10:
                result_code = remote_call(process, probe_base + exports['WxScheduleNoopResult42'], 0, timeout=5000)
                raise RuntimeError('scheduler reported failure ' + str(result_code) + '; probe module remains loaded; do not rerun')
            if (last_status & 0x0A) == 0x0A and not (last_status & 0x04):
                return
            time.sleep(0.1)
        raise TimeoutError('no-op callback was not confirmed within 10 seconds; module remains loaded; do not rerun')
    finally:
        K.CloseHandle(process)


def main() -> int:
    parser = argparse.ArgumentParser(description='One-shot, no-message 4.2 scheduler probe.')
    parser.add_argument('--run-probe', action='store_true', help='submit one no-op task to the bound 4.2 process')
    args = parser.parse_args()
    if not args.run_probe:
        if PROBE.is_file():
            imports = sorted({item.dll.decode('ascii').lower() for item in pefile.PE(str(PROBE)).DIRECTORY_ENTRY_IMPORT})
            verify_probe(PROBE)
            print('PASS: local x64 no-op probe verified; imports=' + ','.join(imports) + '; no process access')
        else:
            print('Probe DLL missing; no injection performed.')
        return 0
    if sys.platform != 'win32':
        raise SystemExit('Windows is required')
    run_probe()
    print('PASS: one no-op task returned and its callback completed; no message was built or sent.')
    print('The probe DLL stays loaded until WeChat exits to avoid an asynchronous callback unload race.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
