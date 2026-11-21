// A recursion three deep that ends in an integer overflow, a check --debug
// turns on: each recursive call is a frame of its own.

recursive fn down(n: i64, x: i64) -> i64 {
    if n == 0 {
        return x * x;
    }
    return down(n - 1, x) + 1;
}

fn main() {
    print(down(3, 3037000500));
}
