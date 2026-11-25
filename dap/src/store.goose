// Python's dicts of the proxy's state, as text. A Store maps a key (an id,
// or a string) to JSON text; the texts are end to end in one grow-only
// array, found by a fixed-size index. A key put again is found at its
// latest entry, and the whole store is emptied at once, which is how the
// proxy forgets a stop: nothing is removed one at a time.
//
// Lookups copy the text out, so no view of a store outlives a call that
// might clear it.

import std;

struct Slot { key: i64, kat: i64, klen: i64, at: i64, len: i64 }

root struct Store {
    idx: Slot[>..] = [],
    text: u8[>..] = []
}

fn store_clear(s: Store&) {
    s.idx.clear();
    s.text.clear();
}

// text under an integer key.
fn store_put(s: Store&, key: i64, text: const u8[:]) {
    let at = s.text.len;
    s.text.append(text);
    s.idx.push(Slot { key: key, kat: 0, klen: -1, at: at, len: text.len });
}

// text under a string key.
fn store_put_named(s: Store&, key: const u8[:], text: const u8[:]) {
    let kat = s.text.len;
    s.text.append(key);
    let at = s.text.len;
    s.text.append(text);
    s.idx.push(Slot { key: 0, kat: kat, klen: key.len, at: at, len: text.len });
}

// The latest text under key, appended to out; false if there is none.
fn store_get(s: Store&, key: i64, out: u8[>..]&) -> bool {
    var i = s.idx.len;
    while i > 0 {
        i--;
        let e = s.idx[i];
        if e.klen == -1 && e.key == key {
            out.append(s.text[e.at..e.at + e.len]);
            return true;
        }
    }
    return false;
}

fn store_has(s: Store&, key: i64) -> bool {
    for e in s.idx { if e.klen == -1 && e.key == key { return true; } }
    return false;
}

fn store_get_named(s: Store&, key: const u8[:], out: u8[>..]&) -> bool {
    var i = s.idx.len;
    while i > 0 {
        i--;
        let e = s.idx[i];
        if e.klen >= 0 && s.text[e.kat..e.kat + e.klen] == key {
            out.append(s.text[e.at..e.at + e.len]);
            return true;
        }
    }
    return false;
}
