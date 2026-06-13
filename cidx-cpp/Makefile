# Convenience wrapper around the CMake build (the canonical build system).
# `make`        -> default dynamic build in build/
# `make static` -> static build in build-static/ (-DCIDX_STATIC=ON): links the
#                  third-party deps statically where the platform allows
#                  (Linux: libclang/LLVM + SQLite3 + libstdc++; macOS: SQLite3
#                  only — libclang/libSystem have no static form there).
# `make test` / `make test-static` -> run the ctest suite for that build.
# Override the libclang location with `make static CIDX_LIBCLANG=/path/...`.
#
# These targets only shell out to cmake; the build logic itself lives in
# CMakeLists.txt so there is a single source of truth.

BUILD_DIR        ?= build
STATIC_BUILD_DIR ?= build-static
JOBS             ?= $(shell (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4))
CMAKE_ARGS       ?=
ifdef CIDX_LIBCLANG
CMAKE_ARGS += -DCIDX_LIBCLANG=$(CIDX_LIBCLANG)
endif

.PHONY: all build static test test-static clean help

all: build

build:
	cmake -S . -B $(BUILD_DIR) $(CMAKE_ARGS)
	cmake --build $(BUILD_DIR) -j $(JOBS)

static:
	cmake -S . -B $(STATIC_BUILD_DIR) -DCIDX_STATIC=ON $(CMAKE_ARGS)
	cmake --build $(STATIC_BUILD_DIR) -j $(JOBS)

test: build
	cd $(BUILD_DIR) && ctest --output-on-failure

test-static: static
	cd $(STATIC_BUILD_DIR) && ctest --output-on-failure

clean:
	rm -rf $(BUILD_DIR) $(STATIC_BUILD_DIR)

help:
	@echo "targets: build (default), static, test, test-static, clean"
	@echo "vars:    BUILD_DIR, STATIC_BUILD_DIR, JOBS, CIDX_LIBCLANG, CMAKE_ARGS"
