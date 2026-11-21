// Resizables: growable arrays and a struct with a resizable tail.
struct Stack { name: u8[..8], depth: i32, items: i64[>..<] }

fn main() {
    var nums: u32[>..] = [];
    for i in 4 { nums.push((i * 3) as u32); }
    var text: u8[>..] = [];
    format(text, "grow ", 42);
    var s = Stack { name: "main", depth: 2, items: [] };
    s.items.push(7);
    s.items.push(9);
    print(nums, " ", text, " ", s.name, " ", s.items); // BREAK2 lldb: test-children s_g; frame variable s_g.items_g; test-children s_g.items_g
}
