"""3.4 Records: iterate a struct's fields, with type, byte offset, and total size."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level


def main():
    tu = parse(MANIFESTS / "shapes.h", args=clang_args())

    struct = next(c for c in top_level(tu)
                  if c.kind == cx.CursorKind.STRUCT_DECL and c.spelling == "Shape")

    # type.get_fields() yields FIELD_DECL cursors in declaration order. Each
    # field has a name (cursor.spelling) and a type (cursor.type). Layout info
    # (offset, size) comes from the type and is ABI-dependent — these numbers
    # are for this machine's LP64 ABI, not universal.
    print(f"struct Shape  (sizeof = {struct.type.get_size()} bytes)")
    print(f"{'offset':>6}  {'field':<12} type")
    print("-" * 40)
    for f in struct.type.get_fields():
        offset = f.get_field_offsetof() // 8   # API returns BITS
        print(f"{offset:>6}  {f.spelling:<12} {f.type.spelling}")


if __name__ == "__main__":
    main()
