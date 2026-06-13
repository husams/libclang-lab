# Convenience wrapper around the CMake build (the canonical build system).
# `make`               -> default dynamic build in build/
# `make static`        -> static build in build-static/ (-DCIDX_STATIC=ON):
#                         SQLite3 + libstdc++ static; libclang static only when a
#                         wrapper archive is supplied (see static-libclang). On
#                         macOS only SQLite3 is static (libclang/libSystem have
#                         no static form there).
# `make static-libclang` (Linux) -> build the libclang C-API wrapper archive
#                         against a NON-LTO distro LLVM (default apt llvm-18),
#                         then configure+build a cidx with NO libclang.so / no
#                         libsqlite3 / no libstdc++ runtime dependency.
# `make test` / `make test-static` -> run the ctest suite for that build.
# Override libclang with `make static CIDX_LIBCLANG=/path/...`.
#
# These targets only shell out to cmake/the helper script; the build logic
# lives in CMakeLists.txt so there is a single source of truth.

BUILD_DIR        ?= build
STATIC_BUILD_DIR ?= build-static
JOBS             ?= $(shell (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4))
CMAKE_ARGS       ?=
ifdef CIDX_LIBCLANG
CMAKE_ARGS += -DCIDX_LIBCLANG=$(CIDX_LIBCLANG)
endif

# static-libclang knobs: a NON-LTO LLVM prefix (its component .a's must be
# regular ELF, not bitcode) and where to drop the built wrapper archive.
LLVM_PREFIX ?= /usr/lib/llvm-18
LIBCLANG_A  ?= $(CURDIR)/$(STATIC_BUILD_DIR)/libclang.a

.PHONY: all build static static-libclang test test-static clean help

all: build

build:
	cmake -S . -B $(BUILD_DIR) $(CMAKE_ARGS)
	cmake --build $(BUILD_DIR) -j $(JOBS)

static:
	cmake -S . -B $(STATIC_BUILD_DIR) -DCIDX_STATIC=ON $(CMAKE_ARGS)
	cmake --build $(STATIC_BUILD_DIR) -j $(JOBS)

# Linux only: produce a cidx with a statically-linked libclang (+ sqlite +
# libstdc++). Builds the C-API wrapper archive first, then links it.
static-libclang:
	mkdir -p $(STATIC_BUILD_DIR)
	bash scripts/build-static-libclang.sh $(LLVM_PREFIX) $(LIBCLANG_A)
	cmake -S . -B $(STATIC_BUILD_DIR) -DCIDX_STATIC=ON \
	  -DCIDX_LIBCLANG=$(LLVM_PREFIX)/lib/libclang.so \
	  -DCIDX_LIBCLANG_STATIC=$(LIBCLANG_A) \
	  -DCIDX_LLVM_CONFIG=$(LLVM_PREFIX)/bin/llvm-config $(CMAKE_ARGS)
	cmake --build $(STATIC_BUILD_DIR) -j $(JOBS)

test: build
	cd $(BUILD_DIR) && ctest --output-on-failure

test-static: static
	cd $(STATIC_BUILD_DIR) && ctest --output-on-failure

clean:
	rm -rf $(BUILD_DIR) $(STATIC_BUILD_DIR)

help:
	@echo "targets: build (default), static, static-libclang (Linux), test, test-static, clean"
	@echo "vars:    BUILD_DIR, STATIC_BUILD_DIR, JOBS, CIDX_LIBCLANG, CMAKE_ARGS,"
	@echo "         LLVM_PREFIX (static-libclang), LIBCLANG_A (static-libclang)"
