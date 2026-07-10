#!/usr/bin/env bash
# install_deps.sh — install libclang-lab dependencies on macOS or RHEL-family
# Linux, with uv managing the Python environment.
#
#   ./install_deps.sh
#
# What it does:
#   1. System toolchain
#        macOS : Xcode Command Line Tools (system clang, SDK, builtin headers)
#        RHEL  : clang + clang-libs via dnf (builtin headers + libclang.so)
#   2. uv (installed from astral.sh if missing)
#   3. `uv sync` — creates .venv/ and installs everything from pyproject.toml
#      (libclang/clang bindings, z3-solver, ipython, pytest)
#
# Afterwards run lab scripts through the environment, e.g.:
#   uv run python scripts/z3_ast_prover.py
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- system deps
case "$(uname -s)" in
  Darwin)
    log "macOS detected"
    if xcode-select -p >/dev/null 2>&1; then
      log "Xcode Command Line Tools already installed"
    else
      log "Installing Xcode Command Line Tools (provides clang + SDK headers)"
      xcode-select --install || true
      die "finish the Command Line Tools install dialog, then re-run this script"
    fi
    ;;
  Linux)
    grep -qiE 'rhel|red hat|centos|rocky|alma|fedora' /etc/os-release 2>/dev/null \
      || die "unsupported Linux distribution — this script handles the RHEL family only"
    log "RHEL-family Linux detected"
    SUDO=""
    [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    if command -v clang >/dev/null 2>&1; then
      log "clang already installed ($(clang --version | head -1))"
    else
      log "Installing clang + clang-libs (builtin headers + native libclang.so)"
      $SUDO dnf install -y clang clang-libs
    fi
    ;;
  *)
    die "unsupported OS: $(uname -s) (macOS or RHEL family required)"
    ;;
esac

# ------------------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH — open a new shell and re-run"
fi
log "uv $(uv --version)"

# ------------------------------------------------------- python deps via uv
cd "$LAB_DIR"
log "Syncing Python environment from pyproject.toml into $LAB_DIR/.venv"
uv sync

# ---------------------------------------------------------------- verification
log "Verifying imports"
uv run python - <<'EOF'
import clang.cindex
import z3
print(f"  clang.cindex OK, z3-solver {z3.get_version_string()} OK")
EOF

if [ "$(uname -s)" = "Linux" ]; then
  # The Linux `clang` bindings ship no native library; point tools at the
  # system libclang.so (see the note in pyproject.toml).
  so="$(ls /usr/lib64/libclang.so* 2>/dev/null | head -1 || true)"
  [ -n "$so" ] && log "Linux note: export CIDX_LIBCLANG=$so (native libclang for the bindings)"
fi

log "Done. Example:  cd $LAB_DIR && uv run python scripts/z3_ast_prover.py"
