import os
import sys
from clang.cindex import CompilationDatabase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from _helpers import _sysroot, _resource_include, parse, fatal_diagnostics  # noqa: E402
import clang.cindex as cx  # noqa: E402


def cpp_toolchain_flags():
    """Toolchain flags the pip libclang wheel lacks, in C++-correct order.

    The wheel ships the dylib but NO builtin headers. For C++ the search order
    must be: sysroot -> libc++ -> clang builtins. (clang_args() adds the builtin
    dir as a plain -I, which lands BEFORE libc++ and breaks <cstddef>'s
    include_next chain -> a fatal that silently truncates the AST.)
    """
    sdk, res = _sysroot(), _resource_include()
    flags = []
    if sdk:
        flags += ["-isysroot", sdk, "-isystem", sdk + "/usr/include/c++/v1"]
    if res:
        flags += ["-isystem", res]
    return flags


DB_DIR = "/Users/husam/workspace/qemu-vms/libclang-lab/test-repo/librdkafka/build"


def load_commands(path):
    cdb = CompilationDatabase.fromDirectory(path)
    return cdb.getAllCompileCommands()


def _abs(p, base):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def strip_for_libclang(cmd):
    """Raw driver invocation -> flags parse() wants. Resolve relative -I."""
    raw, directory = list(cmd.arguments), cmd.directory
    src = {cmd.filename, os.path.basename(cmd.filename)}
    out, it = [], iter(raw[1:])  # drop argv[0] (the driver)
    for tok in it:
        if tok in ("-c", "--"):
            continue
        if tok == "-o":
            next(it, None)
            continue  # drop flag + its arg
        if tok in src:
            continue  # the source file
        matched = False
        for flag in ("-I", "-isystem", "-iquote"):
            if tok == flag:  # space form: -I path
                out += [flag, _abs(next(it, ""), directory)]
                matched = True
                break
            if tok.startswith(flag) and len(tok) > len(flag):  # glued: -Ipath
                out.append(flag + _abs(tok[len(flag) :], directory))
                matched = True
                break
        if not matched:
            out.append(tok)
    return out


# ---- A. settings for a C++ source file (it HAS a DB entry) ----------------
def flags_for_source(cdb_dir, src_path):
    cdb = CompilationDatabase.fromDirectory(cdb_dir)
    cmds = list(cdb.getCompileCommands(src_path))
    if not cmds:
        raise KeyError(f"{src_path} not in DB")
    return strip_for_libclang(cmds[0])


# ---- B. settings for a HEADER (it has NO DB entry) ------------------------
def flags_for_header(commands, header_path):
    """Strategy 1: borrow flags from a TU in the same directory."""
    hdr_dir = os.path.dirname(os.path.abspath(header_path))
    same = [c for c in commands if os.path.dirname(c.filename) == hdr_dir]
    pick = same[0] if same else list(commands)[0]  # fallback: any TU
    return strip_for_libclang(pick)


def main():
    commands = list(load_commands(DB_DIR))

    cpp = "/Users/husam/workspace/qemu-vms/libclang-lab/test-repo/librdkafka/src-cpp/ConfImpl.cpp"
    print("A) C++ source flags (direct DB lookup):")
    print("  ", flags_for_source(DB_DIR, cpp)[:8], "...\n")

    hdr = "/Users/husam/workspace/qemu-vms/libclang-lab/test-repo/librdkafka/src-cpp/rdkafkacpp_int.h"
    print("B) Header flags (borrowed from a sibling TU):")
    print("  ", flags_for_header(commands, hdr)[:8], "...\n")

    # C) Actually parse the header with the borrowed flags + toolchain flags.
    print("C) Parse the header and confirm the AST is not truncated:")
    args = flags_for_header(commands, hdr) + cpp_toolchain_flags()
    tu = parse(hdr, args=args, options=cx.TranslationUnit.PARSE_INCOMPLETE)
    fatals = fatal_diagnostics(tu)
    top = [
        c
        for c in tu.cursor.get_children()
        if c.location.file and c.location.file.name == hdr
    ]
    print(f"   fatals={len(fatals)}  top-level cursors from header={len(top)}")
    for c in top[:6]:
        print(f"     {c.kind.name:22} {c.spelling}")


if __name__ == "__main__":
    main()
