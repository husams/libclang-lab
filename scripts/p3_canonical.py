"""3.2 Canonical types, pointers, arrays, qualifiers: peel sugar, follow pointees."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level


def main():
    tu = parse(MANIFESTS / "shapes.h", args=clang_args())

    fn = next(c for c in top_level(tu)
              if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == "shape_area")
    param = list(fn.get_arguments())[0]
    ptype = param.type                      # const Shape *

    print("=== pointer + canonical (shape_area's 'const Shape *') ===")
    print(f"param type      : {ptype.spelling}  [{ptype.kind.name}]")
    pointee = ptype.get_pointee()           # const Shape  (a typedef => sugar)
    print(f"get_pointee()   : {pointee.spelling}  [{pointee.kind.name}]"
          f"  const={pointee.is_const_qualified()}")
    canon = pointee.get_canonical()         # const struct Shape  (sugar removed)
    print(f"get_canonical() : {canon.spelling}  [{canon.kind.name}]")

    struct = next(c for c in top_level(tu)
                  if c.kind == cx.CursorKind.STRUCT_DECL and c.spelling == "Shape")
    fields = {f.spelling: f.type for f in struct.type.get_fields()}

    print("\n=== array field 'double dimensions[3]' ===")
    arr = fields["dimensions"]
    print(f"type            : {arr.spelling}  [{arr.kind.name}]")
    print(f"element type    : {arr.get_array_element_type().spelling}")
    print(f"element count   : {arr.element_count}")

    print("\n=== qualifiers on 'const char *name' ===")
    name_t = fields["name"]
    print(f"type            : {name_t.spelling}  [{name_t.kind.name}]")
    np = name_t.get_pointee()
    print(f"pointee const={np.is_const_qualified()} volatile={np.is_volatile_qualified()}")


if __name__ == "__main__":
    main()
