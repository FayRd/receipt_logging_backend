#!/usr/bin/env python3
"""
Test script for Redis connection, key creation, and TTL verification.
Supports standard 'redis' package or fallback built-in RESP socket client.
Includes a '--mock' flag to run against an embedded mock Redis server for self-testing.
"""

__test__ = False

import argparse
import os
import socket
import sys
import threading
import time
from urllib.parse import unquote, urlparse

try:
    import redis
    HAS_REDIS_LIB = True
except ImportError:
    HAS_REDIS_LIB = False


class SimpleRedisClient:
    """Fallback RESP client using Python sockets when 'redis' library is not installed."""
    def __init__(self, host="localhost", port=6379, password=None, db=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10.0)
        self.sock.connect((host, port))
        self.f = self.sock.makefile("rb")
        if password:
            self.execute("AUTH", password)
        if db != 0:
            self.execute("SELECT", db)

    def execute(self, *args):
        parts = [f"*{len(args)}\r\n".encode("utf-8")]
        for arg in args:
            arg_bytes = str(arg).encode("utf-8")
            parts.append(f"${len(arg_bytes)}\r\n".encode("utf-8"))
            parts.append(arg_bytes + b"\r\n")
        self.sock.sendall(b"".join(parts))
        return self._read_response()

    def _read_response(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("Connection closed by Redis.")
        line = line.rstrip(b"\r\n")
        prefix = line[:1]
        payload = line[1:]
        if prefix == b"+":
            return payload.decode("utf-8")
        elif prefix == b"-":
            raise Exception(payload.decode("utf-8"))
        elif prefix == b":":
            return int(payload)
        elif prefix == b"$":
            length = int(payload)
            if length == -1:
                return None
            data = self.f.read(length)
            self.f.read(2)
            return data.decode("utf-8")
        elif prefix == b"*":
            count = int(payload)
            if count == -1:
                return None
            return [self._read_response() for _ in range(count)]
        else:
            raise Exception(f"Unknown RESP protocol prefix: {prefix}")

    def ping(self):
        return self.execute("PING") == "PONG"

    def set(self, key, value, ex=None):
        if ex is not None:
            return self.execute("SET", key, value, "EX", ex)
        return self.execute("SET", key, value)

    def get(self, key):
        return self.execute("GET", key)

    def ttl(self, key):
        return self.execute("TTL", key)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class MockRedisServer(threading.Thread):
    """Lightweight embedded Mock Redis RESP server for testing without an external Redis instance."""
    def __init__(self, host="127.0.0.1", port=0):
        super().__init__(daemon=True)
        self.host = host
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.bind((self.host, port))
        self.server_sock.listen(5)
        self.port = self.server_sock.getsockname()[1]
        self.running = True
        self.store = {}

    def run(self):
        self.server_sock.settimeout(0.5)
        while self.running:
            try:
                conn, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_client(self, conn):
        f = conn.makefile("rb")
        try:
            while self.running:
                line = f.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line.startswith(b"*"):
                    continue
                count = int(line[1:])
                args = []
                for _ in range(count):
                    len_line = f.readline().rstrip(b"\r\n")
                    arg_len = int(len_line[1:])
                    arg_data = f.read(arg_len)
                    f.read(2)  # trailing \r\n
                    args.append(arg_data.decode("utf-8"))

                cmd = args[0].upper()
                if cmd == "PING":
                    conn.sendall(b"+PONG\r\n")
                elif cmd in ("SELECT", "AUTH"):
                    conn.sendall(b"+OK\r\n")
                elif cmd == "SET":
                    key = args[1]
                    val = args[2]
                    ex = 0
                    if len(args) >= 5 and args[3].upper() == "EX":
                        ex = int(args[4])
                    expires_at = (time.time() + ex) if ex > 0 else 0
                    self.store[key] = (val, expires_at)
                    conn.sendall(b"+OK\r\n")
                elif cmd == "GET":
                    key = args[1]
                    if key not in self.store:
                        conn.sendall(b"$-1\r\n")
                    else:
                        val, exp = self.store[key]
                        if exp > 0 and time.time() >= exp:
                            del self.store[key]
                            conn.sendall(b"$-1\r\n")
                        else:
                            val_bytes = val.encode("utf-8")
                            conn.sendall(f"${len(val_bytes)}\r\n".encode("utf-8") + val_bytes + b"\r\n")
                elif cmd == "TTL":
                    key = args[1]
                    if key not in self.store:
                        conn.sendall(b":-2\r\n")
                    else:
                        val, exp = self.store[key]
                        if exp == 0:
                            conn.sendall(b":-1\r\n")
                        elif time.time() >= exp:
                            del self.store[key]
                            conn.sendall(b":-2\r\n")
                        else:
                            rem = int(round(exp - time.time()))
                            conn.sendall(f":{rem}\r\n".encode("utf-8"))
                else:
                    conn.sendall(b"-ERR unknown command\r\n")
        except Exception:
            pass
        finally:
            conn.close()

    def stop(self):
        self.running = False
        try:
            self.server_sock.close()
        except Exception:
            pass


def get_redis_client(host, port, password, db, force_socket=False):
    if HAS_REDIS_LIB and not force_socket:
        print("Using standard 'redis' library client...")
        return redis.Redis(
            host=host, port=port, password=password, db=db, decode_responses=True
        )
    print("Using built-in RESP socket client...")
    return SimpleRedisClient(host=host, port=port, password=password, db=db)


def test_redis_connection(host, port, password=None, db=0, ttl_seconds=20, force_socket=False):
    print(f"Connecting to Redis at {host}:{port} (db={db})...")
    try:
        r = get_redis_client(host, port, password, db, force_socket=force_socket)
        if not r.ping():
            raise Exception("Ping returned False.")
        print("Successfully connected to Redis server!")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}", file=sys.stderr)
        return False

    test_keys = {
        "test:key:1": "value_one",
        "test:key:2": "value_two",
        "test:key:3": "value_three",
    }

    print(f"\nCreating {len(test_keys)} test keys with a {ttl_seconds}s TTL...")
    for key, val in test_keys.items():
        r.set(key, val, ex=ttl_seconds)
        actual_ttl = r.ttl(key)
        read_val = r.get(key)
        print(f"Key: {key} | Value: {read_val} | TTL remaining: {actual_ttl}s")
        if read_val != val:
            print(f"Error: Value mismatch for {key}! Expected '{val}', got '{read_val}'", file=sys.stderr)
            return False
        if actual_ttl < 1 or actual_ttl > ttl_seconds:
            print(f"Error: Unexpected TTL {actual_ttl}s for {key}!", file=sys.stderr)
            return False

    print(f"\nAll test keys successfully created and verified with ~{ttl_seconds}s TTL!")
    return True


def load_env_file(env_path=None):
    """Load environment variables from a .env file."""
    candidates = []
    if env_path:
        candidates.append(env_path)
    candidates.extend([
        ".env",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    ])

    try:
        from dotenv import load_dotenv
        for candidate in candidates:
            if os.path.exists(candidate):
                load_dotenv(candidate)
                print(f"Loaded environment variables from: {os.path.abspath(candidate)}")
                return True
    except ImportError:
        pass

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip()
                        if (val.startswith('"') and val.endswith('"')) or (
                            val.startswith("'") and val.endswith("'")
                        ):
                            val = val[1:-1]
                        if key and key not in os.environ:
                            os.environ[key] = val
                print(f"Loaded environment variables from: {os.path.abspath(path)}")
                return True
            except Exception as e:
                print(f"Warning: Failed to parse env file {path}: {e}", file=sys.stderr)
    return False


def parse_redis_url(url):
    """Parse a redis connection string like redis://default:password@host:port/db"""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    password = unquote(parsed.password) if parsed.password else None
    db = 0
    if parsed.path and len(parsed.path) > 1:
        try:
            db = int(parsed.path.lstrip("/"))
        except ValueError:
            db = 0
    return host, port, password, db


def get_default_redis_config():
    """Determine default Redis configuration from environment variables or REDIS_CONNECTION_STRING."""
    host = "localhost"
    port = 6379
    password = None
    db = 0

    conn_str = (
        os.getenv("REDIS_CONNECTION_STRING")
        or os.getenv("REDIS_URL")
        or os.getenv("REDIS_URI")
    )
    if conn_str:
        host, port, password, db = parse_redis_url(conn_str)

    if "REDIS_HOST" in os.environ:
        host = os.environ["REDIS_HOST"]
    if "REDIS_PORT" in os.environ:
        try:
            port = int(os.environ["REDIS_PORT"])
        except ValueError:
            pass
    if "REDIS_PASSWORD" in os.environ:
        password = os.environ["REDIS_PASSWORD"]
    if "REDIS_DB" in os.environ:
        try:
            db = int(os.environ["REDIS_DB"])
        except ValueError:
            pass

    if password == "":
        password = None

    return host, port, password, db


def main():
    load_env_file()
    default_host, default_port, default_password, default_db = get_default_redis_config()

    parser = argparse.ArgumentParser(description="Test Redis connection and verify key TTL.")
    parser.add_argument("--host", default=default_host, help=f"Redis host (default: {default_host})")
    parser.add_argument("--port", type=int, default=default_port, help=f"Redis port (default: {default_port})")
    parser.add_argument("--password", default=default_password, help="Redis password")
    parser.add_argument("--db", type=int, default=default_db, help=f"Redis database index (default: {default_db})")
    parser.add_argument("--ttl", type=int, default=20, help="TTL in seconds for test keys (default: 20)")
    parser.add_argument("--env-file", default=None, help="Path to .env file to load")
    parser.add_argument("--mock", action="store_true", help="Run against an embedded mock Redis server for self-testing")
    parser.add_argument("--force-socket", action="store_true", help="Force using the built-in RESP socket client even if redis lib is installed")

    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
        env_host, env_port, env_password, env_db = get_default_redis_config()
        if args.host == default_host:
            args.host = env_host
        if args.port == default_port:
            args.port = env_port
        if args.password == default_password:
            args.password = env_password
        if args.db == default_db:
            args.db = env_db

    mock_server = None
    host = args.host
    port = args.port

    if args.mock:
        print("Starting embedded Mock Redis server for testing...")
        mock_server = MockRedisServer()
        mock_server.start()
        host = mock_server.host
        port = mock_server.port

    try:
        success = test_redis_connection(
            host=host,
            port=port,
            password=args.password,
            db=args.db,
            ttl_seconds=args.ttl,
            force_socket=args.force_socket or args.mock,
        )
        sys.exit(0 if success else 1)
    finally:
        if mock_server:
            mock_server.stop()


if __name__ == "__main__":
    main()
