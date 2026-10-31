"""Keep MCP's optional Windows Job Objects from becoming a server dependency.

Only the SDK utilities module gets guarded imports; keyring and all other
modules keep Python's normal import semantics. No site-packages files change.
"""
import builtins
import importlib.abc
import importlib.machinery
import re
import sys

FAILURES=[]
NATIVE={'pywintypes','win32api','win32con','win32job'}
TARGET='mcp.os.win32.utilities'


def blocked_component(exc):
    message=str(exc)
    if not any(word in message.lower() for word in ('应用程序控制','应用控制','4551','577','smart app control','application control','signing level')):
        return ''
    files=re.findall(r'[^\s\"\'<>]*\.(?:pyd|dll|exe)\b',message,re.I)
    name=getattr(exc,'path',None) or getattr(exc,'filename',None) or (files[-1] if files else '')
    if not name:
        name=next((part for part in ('_win32sysloader','pywintypes','win32api','win32job','pythoncom') if part in message),getattr(exc,'name',None) or '未知原生组件')
    try:
        spec=importlib.machinery.PathFinder.find_spec(name)
        if spec and spec.origin:name=spec.origin
    except (ImportError,ValueError):pass
    return ('Windows 智能应用控制（Smart App Control）阻止了未签名组件：'+str(name)+
            '。请使用修复后的 coupon-split；若仍无法启动，请将此文件名交给开发者，使用受信任签名的组件或 Python 发行版。无需关闭系统保护。')


class GuardedLoader(importlib.abc.Loader):
    def __init__(self,loader):self.loader=loader
    def create_module(self,spec):return self.loader.create_module(spec)
    def exec_module(self,module):
        failures=[]
        def guarded(name,*args,**kwargs):
            try:return builtins.__import__(name,*args,**kwargs)
            except (ImportError,OSError) as exc:
                if name not in NATIVE:raise
                failures.append(exc)
                return None
        module.__dict__['__builtins__']=dict(vars(builtins),__import__=guarded)
        try:
            self.loader.exec_module(module)
            FAILURES.extend(failures)
            if failures:
                # A partially available pywin32 must not create a Job Object.
                for name in NATIVE:setattr(module,name,None)
        finally:
            module.__dict__['__builtins__']=vars(builtins)


class GuardedFinder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname!=TARGET:return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and spec.loader:spec.loader=GuardedLoader(spec.loader)
        return spec


def install():
    if sys.platform=='win32' and not any(isinstance(finder,GuardedFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0,GuardedFinder())
