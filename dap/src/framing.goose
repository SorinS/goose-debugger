// DAP framing and the workers that read it. A message is a header block
// with a Content-Length, a blank line, and that many bytes of JSON.
//
// Every source (the editor on stdin, lldb-dap's stdout and stderr, a
// signal) has a worker that blocks reading it and puts what it reads on one
// queue, as a Msg: main can wait on one type only. Only main writes, to
// the editor, to lldb-dap and to the log, so frames never interleave.

import std;
import os;
import gslib.sys;
import gslib.sys.unix;

let SRC_CLIENT:    i32 = 0;   // the editor, on our stdin
let SRC_CHILD:     i32 = 1;   // lldb-dap's stdout
let SRC_CHILD_ERR: i32 = 2;   // lldb-dap's stderr, a line at a time
let SRC_SIGNAL:    i32 = 3;   // SIGINT

// One message, or the end of a source (eof). gen is the lldb-dap it came
// from: a respawned lldb-dap's predecessor may still have a reader
// draining its pipe, and main drops what it reads.
struct Msg { src: i32, gen: i64, eof: bool, body: u8[varint] }

// The length in a Content-Length header (any case), or -1.
fn content_length(head: const u8[:]) -> i64 {
    let k = "content-length:";
    var i = 0;
    while i + k.len <= head.len {
        // At the start of a line only.
        if i == 0 || head[i - 1] == '\n' {
            var same = true;
            for c, j in k {
                var h = head[i + j];
                if h >= 'A' && h <= 'Z' { h += 32; }
                if h != c { same = false; break; }
            }
            if same {
                var j = i + k.len;
                while j < head.len && (head[j] == ' ' || head[j] == '\t') { j++; }
                var n = 0;
                var digits = 0;
                while j < head.len && head[j] >= '0' && head[j] <= '9' && digits < 18 {
                    n = n * 10 + (head[j] - '0');
                    j++;
                    digits++;
                }
                return if digits > 0 { n } else { -1 };
            }
        }
        i++;
    }
    return -1;
}

// Where the header block ends: past its blank line ("\r\n\r\n", or "\n\n"
// from a writer that leaves out the returns), or -1.
fn header_end(b: const u8[:]) -> i64 {
    var i = 0;
    while i < b.len {
        if b[i] == '\n' {
            var j = i + 1;
            if j < b.len && b[j] == '\r' { j++; }
            if j < b.len && b[j] == '\n' { return j + 1; }
        }
        i++;
    }
    return -1;
}

// Reads fd to its end, a framed message at a time onto the queue; closes
// fd at the end when it is a child's pipe (own), so that its number is not
// given to another pipe while this worker might still read it.
thread_fn reader(fd: i64, src: i32, gen: i64, own: bool) {
    var pending: u8[>..] = [];
    var start = 0;                       // pending[..start] is handled
    var chunk: u8[16384] = [0; 16384];
    loop {
        // Every whole message in pending first.
        loop {
            let rest = pending[start..];
            let he = header_end(rest);
            guard he >= 0 else { break; }
            let n = content_length(rest[..he]);
            if n < 0 {
                // A header block without a length: passed over.
                start += he;
                continue;
            }
            guard rest.len >= he + n else { break; }
            qput(Msg { src: src, gen: gen, eof: false, body: rest[he..he + n] });
            start += he + n;
        }
        // Grow-only: what is left moves to the front by a rebuild.
        if start == pending.len { pending.clear(); start = 0; }
        else if start > 65536 {
            var left: u8[>..] = [];
            left.append(pending[start..]);
            pending.clear();
            pending.append(left);
            start = 0;
        }
        let got = sys::read(fd, chunk[..]);
        guard got > 0 else { break; }
        pending.append(chunk[..got]);
    }
    if own { sys::close(fd); }
    qput(Msg { src: src, gen: gen, eof: true, body: "" });
}

// Reads fd to its end a line at a time (lldb-dap's stderr), each without
// its line end; closes fd at the end.
thread_fn line_reader(fd: i64, src: i32, gen: i64) {
    var pending: u8[>..] = [];
    var chunk: u8[4096] = [0; 4096];
    loop {
        let got = sys::read(fd, chunk[..]);
        guard got > 0 else { break; }
        var from = 0;
        for c, i in chunk[..got] {
            if c != '\n' { continue; }
            pending.append(chunk[from..i]);
            var line = pending.len;
            if line > 0 && pending[line - 1] == '\r' { line--; }
            qput(Msg { src: src, gen: gen, eof: false, body: pending[..line] });
            pending.clear();
            from = i + 1;
        }
        pending.append(chunk[from..got]);
        if pending.len > 65536 {
            qput(Msg { src: src, gen: gen, eof: false, body: pending[..] });
            pending.clear();
        }
    }
    if pending.len > 0 { qput(Msg { src: src, gen: gen, eof: false, body: pending[..] }); }
    sys::close(fd);
}

// Waits for SIGINT (POSIX), which main takes as the end of the session.
thread_fn signal_waiter() {
    loop {
        let sig = unix::wait_signal(-1);
        if sig < 0 { return; }
        if sig == unix::SIGINT { qput(Msg { src: SRC_SIGNAL, gen: 0, eof: true, body: "" }); }
    }
}

// body with its header.
fn frame(body: const u8[:]) -> u8[>..] {
    var out: u8[>..] = [];
    out.append("Content-Length: ");
    format(out, body.len);
    out.append("\r\n\r\n");
    out.append(body);
    return out;
}

// All of data to fd: false if a write fails (the reader has gone).
fn write_fd(fd: i64, data: const u8[:]) -> bool {
    if fd < 0 { return false; }
    return sys::write_all(fd, data) == 0;
}
