#!/usr/bin/env bash
# 一键装 `kun` 全局可用 wrapper
# 跑完后任意 cwd 都能用 `kun version` / `kun doctor` / `kun run "..."`

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/kun"

mkdir -p "$BIN_DIR"

cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# KUN CLI wrapper — 全局可用, cd 到项目根再跑 (alembic/skills 找 relative path)
KUN_DIR="$KUN_DIR"
cd "\$KUN_DIR" && exec uv run kun "\$@"
EOF

chmod +x "$WRAPPER"

echo "[ok] installed: $WRAPPER"
echo "[ok] points to: $KUN_DIR"
echo ""

# PATH check
if echo "$PATH" | tr ':' '\n' | grep -q "$BIN_DIR"; then
    echo "[ok] $BIN_DIR is in PATH"
    echo ""
    echo "试试 (任意 cwd):"
    echo "  kun version"
    echo "  kun doctor"
    echo "  kun run \"为新产品写一段 30 字 slogan\""
else
    echo "[warn] $BIN_DIR NOT in PATH — 加到 ~/.zshrc:"
    echo ""
    echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
    echo "  source ~/.zshrc"
    echo ""
    echo "之后任意 cwd 都能用 kun 命令."
fi
