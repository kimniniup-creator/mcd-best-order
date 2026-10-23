"""Host-launched stdio entry point; stdout is reserved for MCP."""
import os
os.environ['COUPON_SPLIT_STDIO']='1'
import asyncio
import json
import time
from coupon_split.local import state_dir
from coupon_split.server import mcp, close_bridge

def main():
    async def run():
        ready=state_dir()/'mcp-ready.json'
        ready.write_text(json.dumps({'pid':os.getpid(),'started':time.time()}),encoding='utf-8')
        try:
            await mcp.run_stdio_async()
        finally:
            await close_bridge()
            try:
                if json.loads(ready.read_text(encoding='utf-8')).get('pid')==os.getpid():ready.unlink()
            except (OSError,ValueError):pass
    asyncio.run(run())

if __name__=='__main__':
    main()
