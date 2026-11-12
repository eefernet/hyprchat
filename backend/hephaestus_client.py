"""Attach to a broker execution. Closing this connection terminates its descendants."""
import base64
import json
import os
from pathlib import Path
import signal
import socket
import sys


def main():
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ.get('HEPHAESTUS_BROKER_SOCKET', '/run/hephaestus/broker.sock'))
        def cancel(*_):
            # Remain attached until the broker confirms descendants have stopped.
            try: connection.sendall(b'cancel\n')
            except OSError: pass
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        connection.sendall(Path(sys.argv[1]).read_bytes()+b'\n')
        with connection.makefile('rb') as reader:
            for line in reader:
                packet = json.loads(line)
                if 'output' in packet:
                    sys.stdout.buffer.write(base64.b64decode(packet['output'])); sys.stdout.buffer.flush()
                if 'result' in packet:
                    result = packet['result']
                    if result.get('error'): print(result['error'], file=sys.stderr)
                    return result['exit_code']
        print('Runtime broker disconnected; execution is not confirmed complete', file=sys.stderr)
        return 125


if __name__ == '__main__': sys.exit(main())
