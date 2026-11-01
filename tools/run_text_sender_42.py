from __future__ import annotations
import argparse, ctypes as c, json, sys, time
from pathlib import Path
import pefile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native')); sys.path.insert(0, str(ROOT / 'tools'))
from run_schedule_probe_42 import K, check, load_remote, modules, remote_call, select_target
from wxbot_framework.account import read_current_account_42
from wxbot_framework.wechat_42 import verify_wechat_42_core
PROBE = ROOT / 'native/build_text_42/text_sender_42.dll'
EXPORTS = {
    'WxSendText42', 'WxSenderStatus42', 'WxSenderResult42',
    'WxScheduleNoop42', 'WxScheduleNoopStatus42', 'WxScheduleNoopResult42',
}
ALLOWED_IMPORTS = {'kernel32.dll'}
class SenderConfig(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('target', c.c_char * 128), ('allowed_group', c.c_char * 128), ('text', c.c_char * 1024)]
assert c.sizeof(SenderConfig) == 1288
def audit():
    pe = pefile.PE(str(PROBE)); exports = {s.name.decode() for s in pe.DIRECTORY_ENTRY_EXPORT.symbols if s.name}; imports = {i.dll.decode().lower() for i in pe.DIRECTORY_ENTRY_IMPORT}
    if pe.FILE_HEADER.Machine != 0x8664 or exports != EXPORTS or imports != ALLOWED_IMPORTS: raise RuntimeError(f'PE audit failed: machine={pe.FILE_HEADER.Machine:x}, exports={exports}, imports={imports}')
def send(target: str, allowed_group: str, text: str):
    audit(); pid, account = select_target(); loaded = modules(pid); core = loaded.get('weixin.dll')
    if not core: raise RuntimeError('Weixin.dll missing')
    verify_wechat_42_core(core[1])
    if {'text_bridge.dll','selftest_bridge.dll'} & set(loaded): raise RuntimeError('legacy bridge already loaded; refusing overlap')
    process = check(K.OpenProcess(0x43A, False, pid), 'open verified WeChat process'); state={'thread_pending':False}
    try:
        def read(address, size):
            buf=c.create_string_buffer(size); count=c.c_size_t(); check(K.ReadProcessMemory(process,address,buf,size,c.byref(count)),'recheck account'); return buf.raw[:count.value]
        if read_current_account_42(read, core[0]) != account: raise RuntimeError('bound account changed')
        config=SenderConfig(); config.size=c.sizeof(config); config.pid=pid; config.target=target.encode('ascii'); config.allowed_group=allowed_group.encode('ascii'); config.text=text.encode('utf-8')
        remote=check(K.VirtualAllocEx(process,None,c.sizeof(config),0x3000,0x04),'allocate sender config')
        try:
            written=c.c_size_t(); check(K.WriteProcessMemory(process,remote,c.byref(config),c.sizeof(config),c.byref(written)),'write sender config')
            if written.value != c.sizeof(config): raise RuntimeError('sender config write incomplete')
            exports={s.name.decode():s.address for s in pefile.PE(str(PROBE)).DIRECTORY_ENTRY_EXPORT.symbols if s.name}
            resident=loaded.get(PROBE.name.lower()); reused=False
            if resident:
                if Path(resident[1]).resolve() != PROBE.resolve(): raise RuntimeError('resident sender path differs from audited DLL')
                base=resident[0]; reused=True
                prior_status=remote_call(process,base+exports['WxSenderStatus42'],0,timeout=5000)
                prior_result=remote_call(process,base+exports['WxSenderResult42'],0,timeout=5000)
                if prior_status & 0x01 or (prior_status & 0x98) != 0x98 or not (prior_status & 0x20) or prior_status & 0x40 or prior_result != 0:
                    raise RuntimeError(f'resident sender is not in successful terminal state (status=0x{prior_status:x}, result={prior_result}); refusing reuse')
            else:
                base=load_remote(process,pid,PROBE,state)
            code=remote_call(process,base+exports['WxSendText42'],remote,timeout=10000)
            if code: raise RuntimeError(f'sender entry returned {code}')
            deadline=time.monotonic()+15; status=0
            while time.monotonic()<deadline:
                status=remote_call(process,base+exports['WxSenderStatus42'],0,timeout=5000)
                if status & 0x40:
                    result=remote_call(process,base+exports['WxSenderResult42'],0,timeout=5000); raise RuntimeError(f'sender reported failure {result}, status 0x{status:x}')
                if (status & 0x98) == 0x98:
                    result=remote_call(process,base+exports['WxSenderResult42'],0,timeout=5000)
                    return {'pid':pid,'account':account,'status':status,'result':result,'target':target,'text':text,'dll_loaded':True,'reused_loaded_dll':reused}
                time.sleep(.1)
            raise TimeoutError(f'sender completion timeout status=0x{status:x}; DLL remains loaded; do not rerun')
        finally:
            K.VirtualFreeEx(process,remote,0,0x8000)
    finally:
        K.CloseHandle(process)
def main():
    p=argparse.ArgumentParser(); p.add_argument('--target',required=True); p.add_argument('--allowed-group',default=''); p.add_argument('--text',required=True); p.add_argument('--run-send',action='store_true'); a=p.parse_args()
    if not a.run_send: audit(); print('PASS: local 4.2 sender PE audit; no process access'); return 0
    if a.target != 'filehelper' and not a.target.endswith('@chatroom'): raise SystemExit('target must be filehelper or @chatroom')
    if a.target.endswith('@chatroom') and a.target != a.allowed_group: raise SystemExit('group target must match allowed group')
    result=send(a.target,a.allowed_group,a.text); print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
