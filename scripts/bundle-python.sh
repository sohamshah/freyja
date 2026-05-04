#!/usr/bin/env bash
# Bundle a self-contained Python runtime + all dependencies into the
# Electron app's Resources directory so the packaged .app doesn't
# depend on the user having Python installed.
#
# Prerequisites:
#   uv sync   (installs all deps + maturin)
#
# Strategy:
#   1. Use the project's existing .venv (already has all deps)
#   2. Copy the Python interpreter + stdlib + site-packages into
#      a portable bundle at python-bundle/
#   3. Strip unnecessary files (tests, docs, __pycache__, .pyc)
#   4. Re-sign binaries for TCC permission inheritance
#   5. electron-builder's extraResources copies it into the .app
#
# The bridge.ts pythonCandidates list includes the bundled path, so
# the app finds it automatically.

set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE_DIR="python-bundle"
VENV_DIR=".venv"
PYTHON="$VENV_DIR/bin/python"

if [ ! -f "$PYTHON" ]; then
    echo "error: no .venv found. Run 'uv sync' first."
    exit 1
fi

# Get the Python prefix (the actual interpreter location)
PYTHON_PREFIX=$("$PYTHON" -c "import sys; print(sys.base_prefix)")
PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python: $PYTHON_PREFIX (version $PYTHON_VERSION)"
echo "Venv: $VENV_DIR"

# Clean previous bundle
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

echo "Copying Python runtime..."
# Copy the interpreter binary
mkdir -p "$BUNDLE_DIR/bin"
cp "$PYTHON_PREFIX/bin/python$PYTHON_VERSION" "$BUNDLE_DIR/bin/python3"
ln -sf python3 "$BUNDLE_DIR/bin/python"

# Copy the standard library (WITHOUT site-packages — we copy those
# separately from the venv so we get the project's actual dependencies
# rather than the base Python's empty/minimal site-packages).
echo "Copying stdlib..."
mkdir -p "$BUNDLE_DIR/lib"
cp -R "$PYTHON_PREFIX/lib/python$PYTHON_VERSION" "$BUNDLE_DIR/lib/python$PYTHON_VERSION"
# Remove the base Python's site-packages (will be replaced with venv's)
rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages"

# Copy site-packages from the venv (all our dependencies).
# The target dir was just removed above, so cp -R creates it fresh
# rather than nesting site-packages/site-packages/.
echo "Copying site-packages..."
VENV_SITE=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])")
cp -R "$VENV_SITE" "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages"

# Copy the dynamic libraries the interpreter needs
echo "Copying dylibs..."
mkdir -p "$BUNDLE_DIR/lib"
# libpython
LIBPYTHON=$(find "$PYTHON_PREFIX/lib" -name "libpython*.dylib" -maxdepth 1 2>/dev/null | head -1)
if [ -n "$LIBPYTHON" ]; then
    cp "$LIBPYTHON" "$BUNDLE_DIR/lib/"
fi

# Build and include the native extension (freyja_native).
# Use `uv run` as fallback if maturin isn't in the venv bin directly.
echo "Building freyja_native..."
NATIVE_DIR="native/freyja_native"
if [ -f "$NATIVE_DIR/Cargo.toml" ]; then
    # Try the venv maturin first, then uv run as fallback.
    # Use an absolute path so it works after cd into the native dir.
    if [ -f "$VENV_DIR/bin/maturin" ]; then
        MATURIN="$(cd "$VENV_DIR/bin" && pwd)/maturin"
    elif command -v uv &>/dev/null; then
        MATURIN="uv run maturin"
    else
        echo "  ERROR: maturin not found. Run 'uv sync' first."
        exit 1
    fi
    (cd "$NATIVE_DIR" && $MATURIN develop --release 2>&1 | tail -3)
    NATIVE_PKG=$("$PYTHON" -c "import freyja_native; import os; print(os.path.dirname(freyja_native.__file__))")
    if [ -d "$NATIVE_PKG" ]; then
        # Target doesn't exist yet (stripped above), so cp -R creates it cleanly
        cp -R "$NATIVE_PKG" "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages/freyja_native"
        echo "  freyja_native bundled from $NATIVE_PKG"
    else
        echo "  WARNING: freyja_native not found after build"
    fi
else
    echo "  WARNING: no Cargo.toml at $NATIVE_DIR — skipping native build"
fi

# Strip unnecessary bulk
echo "Stripping unnecessary files..."
find "$BUNDLE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE_DIR" -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE_DIR" -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$BUNDLE_DIR" -name "*.pyo" -delete 2>/dev/null || true
# Remove heavy stdlib modules we don't need
for mod in tkinter turtledemo idlelib ensurepip distutils lib2to3 \
           unittest/test test sqlite3/test email/test ctypes/test; do
    rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/$mod" 2>/dev/null || true
done
# Remove pip/setuptools/maturin from bundled site-packages (not needed at runtime)
rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages/pip" 2>/dev/null || true
rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages/setuptools" 2>/dev/null || true
rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages/_distutils_hack" 2>/dev/null || true
rm -rf "$BUNDLE_DIR/lib/python$PYTHON_VERSION/site-packages/maturin" 2>/dev/null || true

# Write a pyvenv.cfg so the bundled Python finds its stdlib.
# Use an absolute path resolved at bundle time — the bridge.ts sets
# PYTHONHOME to this directory at runtime.
cat > "$BUNDLE_DIR/pyvenv.cfg" << PYEOF
home = $BUNDLE_DIR/bin
include-system-site-packages = false
PYEOF

# Make the interpreter executable
chmod +x "$BUNDLE_DIR/bin/python3" "$BUNDLE_DIR/bin/python"

# Re-sign the Python binary. The copy carries Apple's original code
# signature, which prevents macOS TCC responsibility inheritance from
# attributing this process to the parent Electron/.app. Stripping and
# ad-hoc re-signing fixes this so the bundled Python inherits
# Accessibility + Screen Recording grants from the parent app.
echo "Re-signing Python binary for TCC inheritance..."
ENTITLEMENTS="build/entitlements.mac.inherit.plist"
codesign --remove-signature "$BUNDLE_DIR/bin/python3" 2>/dev/null || true
if [ -f "$ENTITLEMENTS" ]; then
    codesign --force --sign - --entitlements "$ENTITLEMENTS" --timestamp=none "$BUNDLE_DIR/bin/python3"
else
    codesign --force --sign - --timestamp=none "$BUNDLE_DIR/bin/python3"
fi
echo "  python3 re-signed (ad-hoc)"

# Also sign any native .so/.dylib extensions
for lib in $(find "$BUNDLE_DIR" -name '*.so' -o -name '*.dylib' 2>/dev/null); do
    codesign --remove-signature "$lib" 2>/dev/null || true
    codesign --force --sign - --timestamp=none "$lib" 2>/dev/null || true
    echo "  signed: $(basename "$lib")"
done

# Verify the bundle works
echo "Verifying bundle..."
PYTHONHOME="$BUNDLE_DIR" "$BUNDLE_DIR/bin/python3" -c "
import sys
print(f'Python {sys.version}')
print(f'Prefix: {sys.prefix}')

# Verify critical imports
import anthropic, openai, httpx, tiktoken, pydantic, yaml
print('All engine deps: OK')

import freyja_native
print('freyja_native: OK')
" 2>&1 || {
    echo ""
    echo "ERROR: Bundle verification failed."
    echo "  Check that 'uv sync' was run first."
    exit 1
}

BUNDLE_SIZE=$(du -sh "$BUNDLE_DIR" | awk '{print $1}')
echo ""
echo "Bundle ready at $BUNDLE_DIR/ ($BUNDLE_SIZE)"
