"""Quick test: listen on UDP 6666 for 30 seconds and print anything received."""
import socket, time

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('0.0.0.0', 6666))
sock.settimeout(2)
print(f"Listening on UDP 0.0.0.0:6666 for 30 seconds...")
start = time.time()
while time.time() - start < 30:
    try:
        data, addr = sock.recvfrom(2048)
        print(f"[{time.strftime('%H:%M:%S')}] {len(data)} bytes from {addr[0]}:{addr[1]}")
        print(f"  hex: {data.hex()}")
    except socket.timeout:
        print(f"[{time.strftime('%H:%M:%S')}] ... waiting (no data)")
sock.close()
print("Done.")
