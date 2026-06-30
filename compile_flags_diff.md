# Compile-flag consistency report

- **Index:** `/Users/husam/.cache/cidx/index.db`
- **TUs checked:** 34
- **Distinct PCH-relevant flag sets:** 2
- **Verdict:** INCONSISTENT — a single shared PCH is NOT valid; each group below needs its own.

Excludes `-I` include paths and linker options. `compile_options` is already stripped of driver/source/`-c`/`-o`.

## Differences

- **Common to all TUs:** `(none)`
- **Flags that vary across groups:** `--driver-mode=g++ -std=c++17`

Per group, only the *varying* flags are shown: `[+]` = present, `[-]` = absent.

| Group | # TUs | Differing flags ([+]present / [-]absent) |
| ----- | ----- | ---------------------------------------- |
| 1 | 10 | `[-]--driver-mode=g++ [-]-std=c++17` |
| 2 | 24 | `[+]--driver-mode=g++ [+]-std=c++17` |

## Groups in detail

### Group 1 — `[-]--driver-mode=g++ [-]-std=c++17` (10 TUs)

- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/calls.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/macros.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/messy.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/project/app.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/project/mathlib.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/project/mathlib.h`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/shapes.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/shapes.h`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/versioned/lib/mathx.c`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/versioned/lib/mathx.h`

### Group 2 — `[+]--driver-mode=g++ [+]-std=c++17` (24 TUs)

- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/geometry.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/geometry.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/Client.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/UseCache.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/UseCache.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/cache.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/chain.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/chain.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/containers.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/creatures.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/creatures.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/devirt3.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/devirt3.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/devirt3_caller.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/instantiations.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/main.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/nested.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/nested.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/pipeline.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/pipeline.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/shapes.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/graphlab/shapes.hpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/versioned/cpplib/widget.cpp`
- `/Users/husam/workspace/qemu-vms/libclang-lab/manifests/versioned/cpplib/widget.hpp`
