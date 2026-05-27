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
CFG="$TMP_DIR/openssl.cnf"

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

echo "→ Importing into your login keychain (key + cert as separate PEMs)…"
# Previous versions of this script bundled key+cert into a PKCS#12
# and used `security import` on the .p12. That path is fragile on
# modern macOS because OpenSSL 3.x's PKCS#12 encoding (even with
# `-legacy`) sometimes still trips macOS's MAC-verification check.
# Importing the unencrypted PEM key and the cert as separate
# `security import` calls sidesteps the PKCS#12 layer entirely —
# macOS reads the PEMs directly with no cipher / MAC negotiation.
#
# -T /usr/bin/codesign whitelists codesign to use the private key
# without prompting for a password every build. -T /usr/bin/security
# does the same for security CLI operations.
security import "$KEY" \
  -k "$LOGIN_KEYCHAIN" \
  -t priv \
  -f pemseq \
  -A \
  -T /usr/bin/codesign \
  -T /usr/bin/security >/dev/null
security import "$CRT" \
  -k "$LOGIN_KEYCHAIN" \
  -t cert \
  -f x509 \
  -A >/dev/null

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
