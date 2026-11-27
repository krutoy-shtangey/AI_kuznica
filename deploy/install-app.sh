#!/usr/bin/env bash
set -euo pipefail
umask 022
if [[ "$EUID" == 0 ]]; then
    echo "Build the release as an unprivileged operator." >&2
    exit 1
fi
case ${1:-} in
    ""|--with-retrieval) ;;
    *) echo "Usage: bash deploy/install-app.sh [--with-retrieval]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]]
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"

if [[ ${1:-} == --with-retrieval && (! -f requirements-retrieval.txt || ! -f retrieval/agent_adapter.py) ]]; then
    echo "This release does not contain the retrieval adapter. Use the approved integration release." >&2
    exit 1
fi
if [[ -d /opt/reglament-ai/node/bin ]]; then
    export PATH="/opt/reglament-ai/node/bin:$PATH"
fi
command -v npm >/dev/null || { echo "Node.js 22 (with npm) is required to build frontend." >&2; exit 1; }
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 12)) process.exit(1)' ||
    { echo "Use Node.js 22.12 or later." >&2; exit 1; }

python3 -m venv .venv
python="$root/.venv/bin/python"
"$python" -m pip install -r requirements.txt
if [[ ${1:-} == --with-retrieval ]]; then
    # Install the adapter's pinned PyTorch package from the official CPU index first.
    torch_spec=$(awk '/^torch==[0-9]+\.[0-9]+\.[0-9]+$/ { print }' requirements-retrieval.txt)
    if [[ ! "$torch_spec" =~ ^torch==[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "A single pinned torch version is required in requirements-retrieval.txt." >&2
        exit 1
    fi
    "$python" -m pip install --index-url https://download.pytorch.org/whl/cpu "$torch_spec"
    "$python" -m pip install -r requirements-retrieval.txt
    "$python" -B -c 'import torch; assert torch.version.cuda is None, "Expected CPU PyTorch"'
fi
"$python" -m pip check
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
test -s frontend/dist/index.html
