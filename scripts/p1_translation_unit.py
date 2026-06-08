"""§1.3 - Index.create() + index.parse() -> a TranslationUnit (one compiled file)."""
import os

import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, fatal_diagnostics


def main():
    src = MANIFESTS / "shapes.c"

    # An Index is a parsing session: it owns one or more TranslationUnits.
    index = cx.Index.create()
    print("index:", type(index).__name__)

    # parse() compiles the file (frontend only) and hands back its TU.
    tu = index.parse(str(src), args=clang_args())
    print("tu type:", type(tu).__name__)

    # tu.spelling is the file the TU represents (basename only -- it is absolute).
    print("tu.spelling (basename):", os.path.basename(tu.spelling))

    # A TU is one *compiled* file: the main source plus everything it #includes,
    # fully preprocessed. Clean parse => no fatal diagnostics.
    print("fatal diagnostics:", len(fatal_diagnostics(tu)))


if __name__ == "__main__":
    main()
