// A write through a null pointer in C of the user's own (crash.h, given
// with --include), since Goose itself has no unchecked access: a crash in
// code TinyCC compiled, and so with its frame.

extern fn crash_store(v: i64);

fn poke(v: i64) {
    crash_store(v);
}

fn main() {
    print("before");
    poke(7);
    print("after");
}
