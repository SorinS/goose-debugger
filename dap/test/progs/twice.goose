// Instances of a generic function and of a function taking a function
// value: a function breakpoint on the Goose name binds them all.
fn inc(x: i64) -> i64 {
    return x + 1;
}

fn twice<F>(x: i64) -> i64 {
    return F(F(x));
}

fn pick<T>(a: T, b: T, first: bool) -> T {
    if first { return a; }
    return b;
}

fn main() {
    let a = twice(1, inc);
    let b = twice(3) { it * 2 };
    let c = pick(a, b, true);
    let d = pick(1.5, 2.5, false);
    print(a, " ", b, " ", c, " ", d);
}
