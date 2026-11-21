// Generic structs and enums, slice parameters and slice fields.
struct Pair<T> { a: T, b: T }
enum Opt<T> { None, Some { v: T } }
struct View { name: const u8[:], vals: i64[:] }

fn sum(xs: i64[:], label: const u8[:]) -> i64 {
    var t = 0;
    for x in xs { t += x; }
    return t + label.len; // BREAK
}

fn main() {
    let p = Pair<i64> { 1, 2 };
    let q = Pair { a: 0.5, b: 1.5 };
    let s = Opt<i64>.Some { v: 9 };
    let o: Opt<i64> = s;
    let e: Opt<i64> = Opt<i64>.None;
    let arr: i64[3] = [4, 5, 6];
    let v = View { name: "view", vals: arr };
    print(p, " ", q, " ", o, " ", e, " ", sum(v.vals, v.name)); // BREAK2 lldb: test-children o_g; test-children e_g; test-children v_g
}
