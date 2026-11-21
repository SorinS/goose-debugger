// Two overloads of f, whose C names are f_g_1 and f_g_2: both show as f.

fn f(x: i64) -> i64 {
    assert(x < 5);
    return x;
}

fn f(x: f64) -> f64 {
    return x + f(10);
}

fn main() {
    print(f(1));
    print(f(2.0));
}
