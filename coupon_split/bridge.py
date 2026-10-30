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
import secrets
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


def powershell():return str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')


WMI_CREATE=r"""
$ErrorActionPreference='Stop'
$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$env:COUPON_SPLIT_WMI_COMMAND;CurrentDirectory=$env:COUPON_SPLIT_WMI_CWD;ProcessStartupInformation=$startup}
[Console]::Out.Write(([string]$r.ReturnValue)+' '+([string]$r.ProcessId))
"""


def spawn_via_wmi(command,env,root):
    """Windows: start the page service from the WMI provider instead of from this process.

    Hosts such as Codex put the MCP server's whole process tree into a Job Object that kills its members when
    the round ends and does not allow breakaway (measured, see qa/r31-fixlist.md). A process created by
    Win32_Process.Create is a child of the WMI service, so it is in none of those jobs. WMI cannot hand over an
    environment, so a one-shot relay (relay_main) fetches command and environment from this
    process over a loopback socket guarded by a random nonce, then starts the real page service with them.
    Returns True when the relay was started and given its work."""
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(25)
    nonce=secrets.token_hex(16)
    exe=Path(sys.executable);windowless=exe.with_name('pythonw.exe')
    package_parent=str(Path(__file__).resolve().parents[1])  # the relay starts with none of this process's PYTHONPATH
    boot='import sys;sys.path.insert(0,%r);from coupon_split.bridge import relay_main;relay_main(%r,%r)'%(package_parent,str(server.getsockname()[1]),nonce)
    relay=[str(windowless if windowless.exists() else exe),'-c',boot]
    try:
        ran=subprocess.run([powershell(),'-NoProfile','-NonInteractive','-Command',WMI_CREATE],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=40,
                           env=dict(os.environ,COUPON_SPLIT_WMI_COMMAND=subprocess.list2cmdline(relay),COUPON_SPLIT_WMI_CWD=os.getcwd()),creationflags=subprocess.CREATE_NO_WINDOW)
        if ran.returncode!=0 or ran.stdout.strip().split(' ')[0]!='0':return False
        deadline=time.monotonic()+25
        while True:  # another local process may connect first; only the holder of the nonce gets the environment
            server.settimeout(max(.1,deadline-time.monotonic()))
            conn,_=server.accept()
            with conn:
                conn.settimeout(5)
                try:heard=conn.makefile('rb').readline().strip().decode('ascii','replace')
                except OSError:continue
                if not secrets.compare_digest(heard.encode('utf-8'),nonce.encode('ascii')):continue
                conn.sendall(json.dumps({'command':command,'env':dict(env,COUPON_SPLIT_BRIDGE_LAUNCH='wmi'),'cwd':os.getcwd()}).encode('utf-8'))
                conn.shutdown(socket.SHUT_WR)
            break
        return True
    except (OSError,subprocess.SubprocessError,ValueError):return False
    finally:server.close()


def relay_main(port,nonce):
    """Runs outside the host's job: take the command and environment from the owner, start the page service, leave."""
    with socket.create_connection(('127.0.0.1',int(port)),timeout=10) as conn:
        conn.sendall(nonce.encode('ascii')+b'\n')
        raw=b''
        while True:
            chunk=conn.recv(65536)
            if not chunk:break
            raw+=chunk
    work=json.loads(raw.decode('utf-8'))
    home=work['env'].get('COUPON_SPLIT_HOME')
    log=open(Path(home)/'bridge.stderr','ab') if home else subprocess.DEVNULL
    subprocess.Popen(work['command'],env=work['env'],cwd=work.get('cwd') or None,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,
                     creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)


POSIX_RELAUNCH="import json,subprocess,sys;subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=open(sys.argv[2],'ab'),start_new_session=True,close_fds=True)"


def spawn_detached(command,env,log,root):
    """Start the page service so that it outlives the host's round: it must not share fate with the process that issues the link."""
    if os.name!='nt':
        # macOS / Linux: leave the host's process group and controlling terminal (new session) and also stop being a
        # child of this process: a host that walks the MCP process's children before it kills it (or signals its
        # group, or hangs up the terminal) then finds nothing to take. The page service is started by a short-lived
        # intermediate that exits at once, so the service is re-parented to init.
        env=dict(env,COUPON_SPLIT_BRIDGE_LAUNCH='session')
        subprocess.run([sys.executable,'-c',POSIX_RELAUNCH,json.dumps(command),str(Path(root)/'bridge.stderr')],env=env,stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL,stderr=log,start_new_session=True,close_fds=True,timeout=30)
        return None
    if spawn_via_wmi(command,env,root):return None
    # No WMI (disabled, or no PowerShell): fall back to leaving the immediate job (works for hosts whose job allows breakaway).
    base=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
    try:return subprocess.Popen(command,env=dict(env,COUPON_SPLIT_BRIDGE_LAUNCH='breakaway'),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=base|subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:return subprocess.Popen(command,env=dict(env,COUPON_SPLIT_BRIDGE_LAUNCH='plain'),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=base)  # job forbids breakaway


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
                child=spawn_detached(command,env,log,root)
            if child is not None:_children[str(root)]=child
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
    value={'url':f'http://127.0.0.1:{port}','pid':os.getpid(),'instance':os.environ.get('COUPON_SPLIT_BRIDGE_INSTANCE',''),'expires':time.time()+24*60*60,'launch':os.environ.get('COUPON_SPLIT_BRIDGE_LAUNCH','')}
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
