"""4.5 compile_commands.json: drive parses from a CompilationDatabase like a real indexer."""
import os
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, in_main_file, fatal_diagnostics


def strip_for_libclang(cmd):
    """Turn a raw compile command into args libclang.parse() accepts.

    A compile_commands.json entry is a full DRIVER invocation:
        cc -I. -c app.c -o app.o
    libclang.parse() already knows the source file and only wants the *flags*.
    So we drop: the driver token (argv[0]), -c, the -o <output> pair, any '--'
    separator, and the source filename itself. We keep real flags like -I/-D/-std.
    This is exactly what production indexers (clangd, clang-tidy) do.
    """
    raw = list(cmd.arguments)
    out, skip = [], False
    for i, a in enumerate(raw):
        if i == 0 or skip:        # argv[0] driver, or the token after -o
            skip = False
            continue
        if a in ("-c", "--"):     # compile-only flag / arg separator
            continue
        if a == "-o":             # output flag: also skip its filename
            skip = True
            continue
        if a == cmd.filename:     # the source file (parse() supplies it)
            continue
        out.append(a)
    return out


def resolve_includes(args, directory):
    """Make relative -I paths absolute against the command's directory."""
    out = []
    for a in args:
        if a.startswith("-I") and len(a) > 2:
            out.append("-I" + os.path.normpath(os.path.join(directory, a[2:])))
        else:
            out.append(a)
    return out


def main():
    # Single unified DB at manifests/ (sub-project DBs were consolidated).
    cdb = cx.CompilationDatabase.fromDirectory(str(MANIFESTS))

    cmds = sorted(cdb.getAllCompileCommands(), key=lambda c: c.filename)
    print(f"getAllCompileCommands(): {len(cmds)} entries")
    for c in cmds:
        print(f"  {c.filename}: raw args = {list(c.arguments)}")
    print()

    # getCompileCommands(file): look up one file's build command.
    only = list(cdb.getCompileCommands("app.c"))
    print(f"getCompileCommands('app.c'): {len(only)} entry")
    print(f"  stripped (clang-ready) = {strip_for_libclang(only[0])}")
    print()

    # Feed each command's flags into parse() and confirm a clean parse.
    print("parsing each TU from its compile command:")
    for c in cmds:
        flags = resolve_includes(strip_for_libclang(c), c.directory)
        src = os.path.join(c.directory, c.filename)
        tu = parse(src, args=flags + clang_args()[1:])  # + sysroot/builtin headers
        funcs = sorted(x.spelling for x in tu.cursor.get_children()
                       if x.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(x))
        print(f"  {c.filename}: funcs={funcs} fatals={len(fatal_diagnostics(tu))}")


if __name__ == "__main__":
    main()
