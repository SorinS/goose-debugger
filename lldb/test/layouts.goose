// Layouts that DWARF cannot describe: globals, pool-relative links,
// variable-size locals, variable-mode enums, fields that are references
// or bools.
struct PNode { v: i64, next: PNode&<u32 in pool>? }
var pool: PNode[>..] = [];
var log: u8[>..] = [];
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }
struct Rec { id: varint, tag: u8[..4] }
struct Cursor { line: i64&, flag: bool }

fn main() {
    pool.push(PNode { v: 1 });
    pool.push(PNode { v: 2, next: pool[0] });
    let second .= pool[1];
    format(log, "x");
    var sized: i64[..] = [..8];
    sized.push(3);
    let short: u8[varint] = "hi";
    let c: Shape.. = Shape.Circle { r: 2.0 };
    var fs: Shape[4] = [Shape.Dot; 4];
    let r = Rec { id: 5, tag: "ab" };
    var count = 0;
    let cur = Cursor { line: count, flag: true };
    cur.line += 1;
    print(pool.len, log, sized, short, c, fs, r.id, count, cur.flag); // BREAK lldb: target variable gs_globals_main; goose-as gs_globals_main.pool_g PNode_g; p *(PNode_g(*)[2])gs_globals_main.pool_g.base; memory read -s1 -c9 -fu c_g; test-children cur_g; test-children r_g; test-children c_g
}
