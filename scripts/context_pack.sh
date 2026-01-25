#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Arquivos obrigatórios
REQUIRED_FILES=(
    "docs/README.md"
    "docs/operations/07_quality_gates.md"
    "docs/operations/06_lessons_learned_v1.md"
    "docs/operations/backlog.txt"
)

# Validar existência dos arquivos
for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "ERRO: Arquivo obrigatório não encontrado: $f" >&2
        exit 1
    fi
done

# --- Output do contexto ---

echo "=========================================="
echo "CONTEXT PACK - Hotelly V2"
echo "=========================================="
echo

echo "## Git HEAD"
git rev-parse --short HEAD
echo

echo "## Git Status"
git status -sb
echo

echo "## Git Log (últimos 10 commits)"
git log --oneline -n 10
echo

echo "## Arquivos em docs/"
find docs -maxdepth 3 -type f | sort
echo

# Dump dos arquivos
for f in "${REQUIRED_FILES[@]}"; do
    echo "=========================================="
    echo "FILE: $f"
    echo "=========================================="
    cat "$f"
    echo
done
