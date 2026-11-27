[CmdletBinding()]
param([switch]$Install)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$state = Join-Path $root 'storage\windows'
$docs = Join-Path $state 'documents'
$registry = Join-Path $state 'registry.json'
$index = Join-Path $state 'index'
$models = Join-Path $state 'models'
$env:PYTHONUTF8 = '1'

function Invoke-Python {
    & $python @args
    if ($LASTEXITCODE) { throw 'Retrieval preparation failed. See the Python output above.' }
}

Push-Location $root
try {
    if (Test-Path -LiteralPath (Join-Path $state 'run-retrieval\server.json')) { throw 'Stop the running retrieval mode before rebuilding.' }
    if (@(Get-ChildItem -LiteralPath $docs -File -Filter '*.docx' | Where-Object { $_.Name -notlike '~$*' }).Count -ne 4) {
        throw 'Expected four regulation DOCX files in storage\windows\documents.'
    }
    if ($Install) {
        Invoke-Python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.9.1
        Invoke-Python -m pip install -r requirements-retrieval.txt
        Invoke-Python -m pip check
    }
    Invoke-Python -c 'import torch; assert torch.version.cuda is None'
    if (-not (Test-Path -LiteralPath $registry)) {
        Invoke-Python -m retrieval.metadata --documents $docs --draft $registry
    }
    if ($Install) {
        @'
from pathlib import Path
import sys
from huggingface_hub import snapshot_download
from retrieval.embeddings import MODEL, REVISION

root = Path(sys.argv[1]) / MODEL / REVISION
if not (root / "model.safetensors").is_file():
    snapshot_download(MODEL, revision=REVISION, local_dir=root,
                      allow_patterns=["*.json", "sentencepiece.bpe.model", "model.safetensors"])
'@ | & $python - $models
        if ($LASTEXITCODE) { throw 'Unable to download the pinned E5 embedding model.' }
    }
    Invoke-Python -m retrieval.index --documents $docs --registry $registry --index $index --model-root $models --device cpu
    Invoke-Python -m retrieval.index --index $index --check
} finally { Pop-Location }
