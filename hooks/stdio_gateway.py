"""Fast, stdlib-only tool discovery; lazily proxy calls to the fixed release.

No account access here. The versioned schema is generated from the release tools.
The real service still performs every validation and owns all ordering state.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

SOURCE='https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.7.6/coupon_split-0.7.6-py3-none-any.whl'
ROOT=Path(__file__).resolve().parent


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    lock=threading.Lock()
    def emit(value):
        with lock:
            print(json.dumps(value,ensure_ascii=False),flush=True)
    worker=None
    ready=threading.Event()
    def relay():
        try:
            for line in worker.stdout:
                value=json.loads(line)
                if value.get('id')==-90001:
                    ready.set()
                else:emit(value)
        finally:
            ready.set()
    try:
        for line in sys.stdin:
            value=json.loads(line)
            method=value.get('method')
            if method=='initialize':
                emit({'jsonrpc':'2.0','id':value['id'],'result':{
                    'protocolVersion':value['params']['protocolVersion'],
                    'capabilities':{'tools':{},'prompts':{}},'serverInfo':{'name':'最优下单','version':'0.7.6'},
                    'instructions':json.loads((ROOT/'protocol.json').read_text(encoding='utf-8'))['instructions']}})
            elif method=='prompts/list':
                emit({'jsonrpc':'2.0','id':value['id'],'result':{'prompts':[{'name':'order_mcdonalds','description':'麦当劳点餐：报价后等确认，再展示付款卡','arguments':[{'name':'utterance','description':'用户点餐原话','required':False}]}]}})
            elif method=='prompts/get':
                if value['params']['name']!='order_mcdonalds':
                    emit({'jsonrpc':'2.0','id':value['id'],'error':{'code':-32602,'message':'未知 prompt'}})
                else:
                    text=json.loads((ROOT/'protocol.json').read_text(encoding='utf-8'))['instructions']+'\n用户点餐原话：'+value['params'].get('arguments',{}).get('utterance','我要吃麦当劳')
                    emit({'jsonrpc':'2.0','id':value['id'],'result':{'messages':[{'role':'user','content':{'type':'text','text':text}}]}})
            elif method=='tools/list':
                emit({'jsonrpc':'2.0','id':value['id'],'result':{'tools':json.loads((ROOT/'tool-schema.json').read_text(encoding='utf-8'))}})
                state=Path(os.environ.get('COUPON_SPLIT_HOME',Path.home()/'.coupon-split'))
                state.mkdir(parents=True,exist_ok=True)
                (state/'mcp-ready.json').write_text(json.dumps({'pid':os.getpid(),'started':time.time(),'phase':'tools_listed'}),encoding='utf-8')
            elif method=='ping':
                emit({'jsonrpc':'2.0','id':value['id'],'result':{}})
            elif method=='tools/call':
                if worker is None:
                    base=['uv','tool','run','--python','3.14','--from',SOURCE]
                    cached=subprocess.run([*base[:3],'--offline',*base[3:],'python','-c','pass'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
                    command=[*base[:3],*(['--offline'] if cached else []),*base[3:],'coupon-split-mcp']
                    worker=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,encoding='utf-8',env=dict(os.environ))
                    threading.Thread(target=relay,daemon=True).start()
                    worker.stdin.write(json.dumps({'jsonrpc':'2.0','id':-90001,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'coupon-split-gateway','version':'0.7.6'}}})+'\n');worker.stdin.flush()
                    if not ready.wait(120) or worker.poll() is not None:
                        emit({'jsonrpc':'2.0','id':value['id'],'error':{'code':-32603,'message':'点餐服务启动失败，请检查 uv 和网络后重开对话。'}})
                        continue
                    worker.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');worker.stdin.flush()
                worker.stdin.write(line);worker.stdin.flush()
            elif worker and method and method.startswith('notifications/'):
                worker.stdin.write(line);worker.stdin.flush()
            elif 'id' in value:
                emit({'jsonrpc':'2.0','id':value['id'],'error':{'code':-32601,'message':'不支持此请求'}})
    finally:
        if worker:
            worker.stdin.close()
            try:worker.wait(timeout=8)
            except subprocess.TimeoutExpired:worker.terminate();worker.wait(timeout=8)


if __name__=='__main__':main()
