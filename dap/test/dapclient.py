"""A small DAP client for testing goose-dap: it runs an adapter command
over pipes (no TTY, as an IDE does), sends requests, and keeps every
message it receives so a test can wait for a response or an event in any
order. Grown from tests/dap/dap_session.py."""

import json
import os
import queue
import subprocess
import threading
import time


class AdapterExited(Exception):
    pass


class Dap:
    def __init__(self, cmd, env=None, log=None):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        self.seq = 0
        self.q = queue.Queue()
        self.kept = []
        self.log = log if log is not None else []
        self.stderr = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _err(self):
        for line in iter(self.p.stderr.readline, b""):
            self.stderr.append(line.decode("utf-8", "replace"))

    def _reader(self):
        f = self.p.stdout
        while True:
            hdr = b""
            while not hdr.endswith(b"\r\n\r\n"):
                c = f.read(1)
                if not c:
                    self.q.put(None)
                    return
                hdr += c
            n = None
            for l in hdr.decode().split("\r\n"):
                if l.lower().startswith("content-length"):
                    n = int(l.split(":")[1])
            body = b""
            while len(body) < n:
                chunk = f.read(n - len(body))
                if not chunk:
                    self.q.put(None)
                    return
                body += chunk
            self.q.put(json.loads(body))

    def send(self, command, arguments=None):
        self.seq += 1
        m = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            m["arguments"] = arguments
        b = json.dumps(m).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
        self.p.stdin.flush()
        self.log.append({"->": m})
        return self.seq

    def wait(self, pred, timeout=30):
        for i, m in enumerate(self.kept):
            if pred(m):
                del self.kept[i]
                return m
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if m is None:
                self.q.put(None)
                raise AdapterExited("adapter exited")
            self.log.append({"<-": m})
            if pred(m):
                return m
            self.kept.append(m)
        raise TimeoutError("timed out waiting")

    def request(self, command, arguments=None, timeout=30):
        s = self.send(command, arguments)
        return self.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == s, timeout)

    def event(self, name, timeout=30):
        return self.wait(lambda m: m.get("type") == "event" and m.get("event") == name, timeout)

    def events(self, name):
        """Events of this kind received so far (kept, not consumed)."""
        return [m for m in self.kept if m.get("type") == "event" and m.get("event") == name]

    def drain(self, timeout=0.3):
        try:
            self.wait(lambda m: False, timeout)
        except (TimeoutError, AdapterExited):
            pass

    def close(self, timeout=10):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        return self.p.returncode
