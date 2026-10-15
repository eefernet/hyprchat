"""Byte-only stdout limiter for configured MCP children; protocol stays in SDK."""
import asyncio
import sys

MAX_LINE = 8 * 1024 * 1024


async def main():
    proc=await asyncio.create_subprocess_exec(*sys.argv[1:],stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    reader=asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin.buffer)
    async def input_stream():
        while data:=await reader.read(65536):
            proc.stdin.write(data);await proc.stdin.drain()
        proc.stdin.close()
    async def output_stream():
        line_size=0
        while data:=await proc.stdout.read(65536):
            pieces=data.split(b'\n')
            for i,piece in enumerate(pieces):
                line_size+=len(piece)
                if line_size>MAX_LINE:raise ValueError('MCP response line exceeds 8 MB')
                if i<len(pieces)-1:line_size=0
            sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
    tasks=[asyncio.create_task(input_stream()),asyncio.create_task(output_stream())]
    try:
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done:task.result()
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:await asyncio.wait_for(proc.wait(),2)
            except asyncio.TimeoutError:proc.kill();await proc.wait()
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


if __name__=='__main__':
    asyncio.run(main())
