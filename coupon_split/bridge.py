"""Per-state-directory page service, independent of short-lived stdio rounds.

Only the local authorized credential is recovered, never a credential from a cart.
The launcher is serialized; the page service uses a random loopback port. It outlives its MCP owners:
a host may start the MCP process per turn or the person may close the chat, and the confirmation link
must keep working for its validity period (ontology `validity`) and while an unpaid order can still be
paid or withdrawn. It exits when no owner is alive and nothing holds it. The cart itself survives for
24 hours. It never binds or changes the legacy port.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid


def identity(root):return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()


_children={}
_leases={}


def process_alive(pid):
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle:return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(int(pid),0);return True
    except OSError:return False


def ensure_bridge(root):
    root=Path(root);info=root/'bridge.json';lock=root/'bridge.lock'
    leases=root/'bridge-leases';leases.mkdir(exist_ok=True)
    lease=leases/(str(os.getpid())+'.json')
    lease.write_text(json.dumps({'pid':os.getpid(),'active':True}),encoding='utf-8')
    _leases[str(root)]=lease
    with lock.open('a+b') as stream:
        if os.name=='nt':
            import msvcrt
            stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX)
        try:
            if info.exists():
                value=json.loads(info.read_text(encoding='utf-8'))
                try:
                    with urllib.request.urlopen(value['url']+'/bridge-health',timeout=2) as response:
                        if json.load(response).get('identity')==identity(root):return value['url']
                except (OSError,ValueError):pass
            instance=uuid.uuid4().hex
            # The inspector must retain the owner's mode to retract expiring
            # unpaid orders. Read-only payment tools never perform this write.
            env=dict(os.environ,COUPON_SPLIT_HOME=str(root),COUPON_SPLIT_STDIO='1',COUPON_SPLIT_BRIDGE_INSTANCE=instance,PYTHONUTF8='1')
            command=[sys.executable,'-m','coupon_split.bridge','--home',str(root),'--instance',instance]
            with (root/'bridge.stderr').open('ab') as log:
                # The page service must not die with the MCP process: leave the host's process group / job
                # (a host that kills its child's tree on exit would take the confirmation page with it).
                if os.name=='nt':
                    base=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
                    try:child=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=base|subprocess.CREATE_BREAKAWAY_FROM_JOB)
                    except OSError:child=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=base)  # job forbids breakaway
                else:
                    child=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,start_new_session=True)
            _children[str(root)]=child
            for _ in range(1200):
                if child is not None and child.poll() is not None:raise RuntimeError('点餐页服务启动失败，请重试')
                if info.exists():
                    value=json.loads(info.read_text(encoding='utf-8'))
                    if value.get('instance')==instance:return value['url']
                time.sleep(.05)
            raise RuntimeError('点餐页服务启动超时，请重试')
        finally:
            if os.name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


async def run():
    import uvicorn
    from coupon_split import server as s
    root=s.runtime;sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    port=sock.getsockname()[1]
    service=uvicorn.Server(uvicorn.Config(s.app,log_level='error',access_log=False))
    task=asyncio.create_task(service.serve(sockets=[sock]))
    while not service.started:await asyncio.sleep(.01)
    value={'url':f'http://127.0.0.1:{port}','pid':os.getpid(),'instance':os.environ.get('COUPON_SPLIT_BRIDGE_INSTANCE',''),'expires':time.time()+24*60*60}
    target=root/'bridge.json';temp=root/'bridge.tmp'
    temp.write_text(json.dumps(value),encoding='utf-8');os.replace(temp,target)
    async def expire():
        held_checked=0
        while True:
            await asyncio.sleep(.25)
            leases=[]
            for file in (root/'bridge-leases').glob('*.json'):
                try:leases.append(json.loads(file.read_text(encoding='utf-8')))
                except (ValueError,OSError):pass
            if not any(v.get('active') and process_alive(v['pid']) for v in leases):
                # No MCP owner is left. A link already issued stays usable for its validity period, and an
                # unpaid order can still be paid or withdrawn; only when nothing holds the page do we leave.
                if time.monotonic()-held_checked>=1:
                    held_checked=time.monotonic()
                    try:hold=max(s.confirmations.live_until(),s.service.hold_until())
                    except Exception:hold=time.time()+5  # cannot tell: stay up, retry
                    if time.time()>hold:
                        service.should_exit=True;return
            with s.carts.connect() as db:
                latest=db.execute('SELECT MAX(expires) FROM carts').fetchone()[0] or 0
            if time.time()>max(value['expires'],latest):
                service.should_exit=True;return
    expiry=asyncio.create_task(expire())
    try:await task
    finally:
        expiry.cancel()
        # Leave metadata for health-checked recovery; never remove another run's files.


async def release_bridge(root):
    key=str(Path(root));lease=_leases.get(key)
    if lease:lease.write_text(json.dumps({'pid':os.getpid(),'active':False}),encoding='utf-8')
    child=_children.get(key)
    if child:
        try:await asyncio.to_thread(child.wait,timeout=4)
        except subprocess.TimeoutExpired:
            # Another live MCP owner can still hold a lease. Its page must stay.
            pass


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--home');parser.add_argument('--instance');args=parser.parse_args()
    if args.home:
        os.environ.update(COUPON_SPLIT_HOME=args.home,COUPON_SPLIT_STDIO='1',COUPON_SPLIT_BRIDGE_INSTANCE=args.instance or '')
        sys.stderr=open(Path(args.home)/'bridge.stderr','a',encoding='utf-8')
        sys.stdout=open(os.devnull,'w')
    asyncio.run(run())
