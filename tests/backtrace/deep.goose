// An index out of bounds three calls below main: the abort's backtrace
// names leaf, middle, outer and main with the line of each call.

fn leaf(xs: i64[:], i: i64) -> i64 {
    return xs[i];
}

fn middle(xs: i64[:], i: i64) -> i64 {
    let v = leaf(xs, i + 1);
    return v * 2;
}

fn outer(xs: i64[:]) -> i64 {
    var total: i64 = 0;
    for i in 0..xs.len {
        total = total + middle(xs, i);
    }
    return total;
}

fn main() {
    let xs = [1, 2, 3];
    print(outer(xs));
}
