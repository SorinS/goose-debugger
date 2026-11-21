// References, optionals, relative references and function values.
struct Node { v: i64, next: Node? }
struct Tree { key: i32, left: Tree&<u32>? }

fn twice<F>(x: i64) -> i64 { F(F(x)) }
fn inc(x: i64) -> i64 { x + 1 }

fn bump(r: i64&, o: Node?) -> i64 {
    r += 1;
    let found = if o { o.v } else { -1 };
    return found + r; // BREAK lldb: test-children o_g; test-children r_g
}

fn main() {
    var n = 5;
    var c = Node { v: 3, next: null };
    var b = Node { v: 2, next: c };
    let some: Node? = b;
    let none: Node? = null;
    var pool: Tree[>..] = [];
    pool.push(Tree { key: 1 });
    pool.push(Tree { key: 2, left: pool[0] });
    let k = bump(n, some) + bump(n, none) + twice(n, inc) + twice(n) { it * 2 };
    print(k, " ", pool[1].key); // BREAK2 lldb: goose-as pool_g Tree_g
}
