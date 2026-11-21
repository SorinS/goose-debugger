// An index out of bounds in a called function: the "Goose runtime errors"
// filter stops at line 4, in get, with the runtime's message.
fn get(xs: i64[:], i: i64) -> i64 {
    return xs[i];
}

fn main() {
    let a: i64[3] = [1, 2, 3];
    var s: i64 = 0;
    for i in 0..4 {
        s = s + get(a, i);
    }
    print(s);
}
