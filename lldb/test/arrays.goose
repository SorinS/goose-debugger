// Fixed arrays, slices and text.
fn use(xs: i64[:], s: const u8[:]) -> i64 { xs.len + s.len }

fn main() {
    let fixed: i32[4] = [1, 2, 3, 4];
    var grow: i64[>..] = [10, 20, 30, 40, 50];
    let view: i64[:] = grow[1..4];
    let bytes: u8[:] = fixed_bytes();
    let lit: const u8[:] = "hello, goose";
    var name: u8[..16] = [];
    format(name, "ada");
    let frozen: i64[] = [7, 8, 9];
    let n = use(view, lit) + bytes.len + frozen.len;
    print(fixed, " ", view, " ", lit, " ", name, " ", n); // BREAK lldb: frame variable view_g[1] fixed_g[2] name_g[0] view_g.len; test-children view_g; test-children name_g; test-children grow_g; goose-as grow_g i64; goose-as frozen_g i64[]; goose-as fixed_g i32
}

var buf: u8[8] = [0x67, 0x6f, 0x6f, 0x73, 0x65, 0, 1, 2];
fn fixed_bytes() -> u8[:] { buf[..] }
