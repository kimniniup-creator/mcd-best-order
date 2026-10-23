"""Host-launched stdio entry point; stdout is reserved for MCP."""
import os
os.environ['COUPON_SPLIT_STDIO']='1'
import asyncio
from coupon_split.server import mcp, close_bridge

def main():
    async def run():
        try:
            await mcp.run_stdio_async()
        finally:
            await close_bridge()
    asyncio.run(run())

if __name__=='__main__':
    main()
