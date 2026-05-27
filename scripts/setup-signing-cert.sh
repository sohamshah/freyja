#!/usr/bin/env bash
#
# scripts/setup-signing-cert.sh — one-shot creation of the self-signed
# code-signing certificate used by ./scripts/rebuild.sh to preserve
# macOS TCC permissions (Screen Recording, Accessibility, Input
# Monitoring) across rebuilds.
#
# This is the CLI equivalent of the Keychain Access → Certificate
# Assistant → Create a Certificate flow described in the README. Use
# this if you don't see the Certificate Assistant menu or just prefer
# a scripted setup.
#
# Run once:
#   ./scripts/setup-signing-cert.sh
#
# Then verify:
#   security find-identity -v -p codesigning | grep "Freyja Dev"
#
# After that, `npm run rebuild` will sign with this identity and TCC
# grants will persist across iterations.

set -euo pipefail

CERT_NAME="${FREYJA_SIGN_IDENTITY:-Freyja Dev}"
LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

KEY="$TMP_DIR/freyja-dev.key"
CRT="$TMP_DIR/freyja-dev.crt"
P12="$TMP_DIR/freyja-dev.p12"
CFG="$TMP_DIR/openssl.cnf"
# Real password for the PKCS#12 envelope. Required — macOS's
# `security import` flat-out rejects empty-password PKCS#12 files
# with "MAC verification failed" even when the password really is
# empty, because OpenSSL and Security framework compute the empty-
# password MAC differently. Any non-empty string works; this one is
# ephemeral and never leaves the script.
P12_PASS="freyja-dev-setup"

# If the identity already exists, bail. Re-running this script would
# create a second cert with the same CN, and `codesign` will then
# refuse to pick one ambiguously.
if security find-identity -v -p codesigning | grep -F "\"$CERT_NAME\"" >/dev/null; then
  echo "✓ Code-signing identity \"$CERT_NAME\" already exists in your login keychain."
  echo "  Nothing to do. If you want to recreate it, first delete it via:"
  echo "    security delete-identity -c \"$CERT_NAME\""
  exit 0
fi

# OpenSSL config that marks the cert as Code Signing (extKeyUsage).
# Without the codeSigning EKU, `codesign` would reject the identity.
cat > "$CFG" <<EOF
[req]
distinguished_name = req_distinguished_name
prompt = no
x509_extensions = v3_ca

[req_distinguished_name]
CN = $CERT_NAME

[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, digitalSignature, keyCertSign
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
EOF

echo "→ Generating 4096-bit RSA key + 10-year self-signed cert…"
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout "$KEY" \
  -out "$CRT" \
  -config "$CFG" \
  2>/dev/null

echo "→ Bundling key + cert into PKCS#12 (legacy encoding for macOS)…"
# OpenSSL 3.x defaults to AES-256 / PBKDF2 for PKCS#12. macOS's
# Security framework only reads the older RC2/3DES + PBKDF1
# encoding, so we need `-legacy`. LibreSSL (which is what /usr/bin/
# openssl is on stock macOS) doesn't need it and doesn't accept it,
# so we detect the openssl flavor first.
OPENSSL_VERSION="$(openssl version 2>/dev/null || echo unknown)"
LEGACY_FLAG=""
if [[ "$OPENSSL_VERSION" =~ ^OpenSSL[[:space:]]+([3-9]|[1-9][0-9]+)\. ]]; then
  LEGACY_FLAG="-legacy"
fi
openssl pkcs12 -export $LEGACY_FLAG \
  -out "$P12" \
  -inkey "$KEY" \
  -in "$CRT" \
  -name "$CERT_NAME" \
  -password "pass:$P12_PASS"

echo "→ Importing into your login keychain…"
# -T /usr/bin/codesign whitelists codesign to use the private key
# without prompting for a password every build. -T /usr/bin/security
# does the same for security CLI operations.
security import "$P12" \
  -k "$LOGIN_KEYCHAIN" \
  -P "$P12_PASS" \
  -T /usr/bin/codesign \
  -T /usr/bin/security >/dev/null

echo "→ Trusting the cert for Code Signing (requires sudo)…"
# Has to be in the System keychain as a trusted root so codesign +
# the kernel's code-signing checker honor it. Otherwise the cert
# imports but isn't trusted, and codesign falls back to ad-hoc.
sudo security add-trusted-cert \
  -d \
  -r trustRoot \
  -p codeSign \
  -k /Library/Keychains/System.keychain \
  "$CRT"

# Verify identity is now picked up by codesign's identity resolver.
if ! security find-identity -v -p codesigning | grep -F "\"$CERT_NAME\"" >/dev/null; then
  cat >&2 <<EOF
ERROR: cert was imported but is not appearing in:
    security find-identity -v -p codesigning

This usually means trust didn't take. Try opening Keychain Access,
finding "$CERT_NAME" in the System keychain, double-clicking it, and
setting Trust → Code Signing to "Always Trust" manually.
EOF
  exit 1
fi

cat <<EOF

✓ Created code-signing identity "$CERT_NAME".

Next steps:
  1. Run a first rebuild to install the new build into /Applications
     and re-grant TCC permissions one final time:
        npm run rebuild
  2. Grant Screen Recording, Accessibility, and Input Monitoring to
     Freyja.app in System Settings → Privacy & Security.
  3. From now on, \`npm run rebuild\` keeps those grants across
     iterations because every build is signed with the same identity.
EOF
