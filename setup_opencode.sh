#!/usr/bin/env bash
# Install runtime prerequisites for using OpenCode as a CORAL agent runtime.
#
# OpenCode requires three things that are easy to miss:
#   1. tmux              — CORAL's default run.session=tmux launcher
#   2. opencode CLI      — the agent binary itself
#   3. bun + @ai-sdk/openai-compatible (or @ai-sdk/anthropic, etc.)
#                        — opencode loads custom providers (`"npm": "..."`
#                          in opencode.json) from ~/.config/opencode/node_modules/.
#                          Without bun, opencode silently parses the provider
#                          config but never registers it, and every request
#                          fails with: "Model not found: <provider>/<model>."
#
# Idempotent: re-running skips anything already present.

set -euo pipefail

log()  { printf "\033[1;34m[setup-opencode]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[setup-opencode]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m[setup-opencode]\033[0m %s\n" "$*" >&2; exit 1; }

# Which npm-loaded providers to pre-install. Add more here as needed
# (matches the `npm:` field in your seed opencode.json).
PROVIDER_PKGS=(
    "@ai-sdk/openai-compatible"  # vLLM / any OpenAI-compatible endpoint
    # "@ai-sdk/anthropic"        # uncomment if you use the anthropic provider
)

# --- 1. tmux ----------------------------------------------------------------
if command -v tmux >/dev/null 2>&1; then
    log "tmux: $(tmux -V) ✓"
else
    warn "tmux missing — install with: sudo apt-get install -y tmux"
    warn "(or run \`coral start ... run.session=local\` to skip tmux)"
fi

# --- 2. opencode CLI --------------------------------------------------------
OPENCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
if [ -x "$OPENCODE_BIN" ]; then
    log "opencode: $($OPENCODE_BIN --version) at $OPENCODE_BIN ✓"
elif command -v opencode >/dev/null 2>&1; then
    OPENCODE_BIN="$(command -v opencode)"
    log "opencode: $(opencode --version) at $OPENCODE_BIN ✓"
else
    log "opencode missing — installing via official script…"
    curl -fsSL https://opencode.ai/install | bash
    OPENCODE_BIN="$HOME/.opencode/bin/opencode"
    [ -x "$OPENCODE_BIN" ] || die "opencode install did not produce $OPENCODE_BIN"
    log "opencode installed: $($OPENCODE_BIN --version)"
fi

# --- 3. bun + npm-loaded providers ------------------------------------------
BUN_BIN="${BUN_BIN:-$HOME/.bun/bin/bun}"
if [ -x "$BUN_BIN" ]; then
    log "bun: $($BUN_BIN --version) at $BUN_BIN ✓"
elif command -v bun >/dev/null 2>&1; then
    BUN_BIN="$(command -v bun)"
    log "bun: $(bun --version) at $BUN_BIN ✓"
else
    log "bun missing — installing via official script…"
    curl -fsSL https://bun.sh/install | bash
    BUN_BIN="$HOME/.bun/bin/bun"
    [ -x "$BUN_BIN" ] || die "bun install did not produce $BUN_BIN"
    log "bun installed: $($BUN_BIN --version)"
fi

OC_CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
mkdir -p "$OC_CFG_DIR"
if [ ! -f "$OC_CFG_DIR/package.json" ]; then
    log "initializing $OC_CFG_DIR/package.json"
    printf '{\n  "dependencies": {}\n}\n' > "$OC_CFG_DIR/package.json"
fi

for pkg in "${PROVIDER_PKGS[@]}"; do
    # @ai-sdk/openai-compatible → @ai-sdk/openai-compatible (path on disk)
    if [ -d "$OC_CFG_DIR/node_modules/$pkg" ]; then
        log "provider pkg $pkg ✓"
    else
        log "installing $pkg into $OC_CFG_DIR …"
        (cd "$OC_CFG_DIR" && "$BUN_BIN" add "$pkg")
    fi
done

log "done. Verify with:"
log "  cd <your-worktree-with-opencode.json> && $OPENCODE_BIN models | grep <your-provider>"
