"""4.4 Macros & inclusions: PARSE_DETAILED_PROCESSING_RECORD surfaces the preprocessor."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, walk, clang_args, in_main_file, loc


def main():
    # Without this option libclang DISCARDS preprocessor entities — no macro or
    # #include cursors exist. The flag retains them as MACRO_* / INCLUSION nodes.
    opt = cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    tu = parse(MANIFESTS / "macros.c", args=clang_args(), options=opt)

    def collect(kind):
        # Sort by (line, col) as ints so output reads in source order — sorting
        # the loc() *string* would put line 12 before line 4 ("12" < "4").
        rows = [(c.location.line, c.location.column, loc(c), c.spelling)
                for c, _ in walk(tu.cursor)
                if c.kind == kind and in_main_file(c)]
        return [(where, name) for _, _, where, name in sorted(rows)]

    print("MACRO_DEFINITION (object- and function-like macros):")
    for where, name in collect(cx.CursorKind.MACRO_DEFINITION):
        print(f"  {where}  {name}")

    print("\nMACRO_INSTANTIATION (expansions in code):")
    for where, name in collect(cx.CursorKind.MACRO_INSTANTIATION):
        print(f"  {where}  {name}")

    print("\nINCLUSION_DIRECTIVE (#include lines):")
    for where, name in collect(cx.CursorKind.INCLUSION_DIRECTIVE):
        print(f"  {where}  {name}")


if __name__ == "__main__":
    main()
