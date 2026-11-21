// Values on data stacks: variable-size locals and resizables passed by reference.
struct Rec { id: varint, name: u8[varint] }

fn fill(out: i64[>..]&, n: i64) {
    var last: i64 = 0; // Before the loop, so a stop in it shows no uninitialized local.
    for i in n {
        out.push(i * i); // BREAK when out_g.hdr->len == 3 lldb: frame variable out_g.hdr->len; goose-as out_g i64; p *(gs_i64(*)[3])out_g.hdr->base
    }
    last = out.len;
    if last < 0 { print(last); } // BREAK
}

fn main() {
    var squares: i64[>..] = [];
    fill(squares, 4);
    let frozen: i64[] = [7, 8, 9];
    let word: u8[] = "frozen";
    let rec = Rec { id: 300, name: "rec" };
    var words: u8[][>..] = [];
    words.push("one");
    words.push("two");
    let msg = str("n=", squares.len);
    print(frozen, " ", word, " ", rec.name, " ", words, " ", msg); // BREAK2 lldb: goose-as msg_g u8; goose-as word_g u8[]
}
