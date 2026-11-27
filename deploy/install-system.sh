#!/usr/bin/env bash
set -euo pipefail
umask 022
if [[ "$EUID" != 0 || "$(uname -m)" != x86_64 ]]; then
    echo "Run with sudo on an x86-64 Ubuntu 24.04 VPS." >&2
    exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo "This installer targets Ubuntu 24.04." >&2
    exit 1
fi
if [[ ${1:-} != "" && ${1:-} != --with-node ]] || [[ $# -gt 1 ]]; then
    echo "Usage: sudo bash deploy/install-system.sh [--with-node]" >&2
    exit 2
fi

apt-get update
apt-get install -y --no-install-recommends ca-certificates certbot curl git nginx openssl python3 python3-venv rsync sqlite3 xz-utils

if ! getent passwd reglament >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir /var/lib/reglament-ai --shell /usr/sbin/nologin reglament
fi
install -d -m 0755 /opt/reglament-ai /opt/reglament-ai/releases /var/www/reglament-acme
install -d -m 0700 /etc/reglament-ai /var/backups/reglament-ai
install -d -o root -g reglament -m 0750 /var/lib/reglament-ai
install -d -o root -g reglament -m 0750 /var/lib/reglament-ai/{documents,models}
install -d -o reglament -g reglament -m 0750 /var/lib/reglament-ai/{storage,index}

if [[ ${1:-} == --with-node ]]; then
    # Official Linux x64 binary and published SHA256 for this pinned release.
    version=v22.23.5
    name="node-${version}-linux-x64"
    target="/opt/reglament-ai/${name}"
    checksum=cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8
    if [[ ! -x "$target/bin/node" ]]; then
        temp=$(mktemp -d /opt/reglament-ai/.node.XXXXXX)
        trap 'rm -rf -- "$temp"' EXIT
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
            "https://nodejs.org/dist/${version}/${name}.tar.xz" -o "$temp/$name.tar.xz"
        (cd "$temp"; sha256sum --check --strict <<<"$checksum  $name.tar.xz")
        tar -xJf "$temp/$name.tar.xz" -C "$temp" --no-same-owner
        mv -T -- "$temp/$name" "$target"
    fi
    ln -sfnT -- "$target" /opt/reglament-ai/node
    /opt/reglament-ai/node/bin/node --version
fi
