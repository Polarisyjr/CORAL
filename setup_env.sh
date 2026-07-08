#!/bin/bash
# Set up the `coral` conda env + the opencode agent runtime CORAL needs to
# actually run tasks against local vLLM.
#
# What it does (all idempotent — safe to re-run):
#   1. ensure miniconda + conda env `coral` (python 3.11)
#   2. pip install -e .[all]   (CORAL itself, all extras)
#   3. pip install uv into the env (coral-vllm.sh / scripts/coral/start.sh
#      shell out to `uv run coral ...`)
#   4. bash setup_opencode.sh  — installs the opencode CLI + bun +
#      @ai-sdk/openai-compatible (the provider package opencode needs to
#      route requests at an OpenAI-compatible endpoint like vLLM; without it
#      every request fails with "Model not found: vllm/<model>")
#   5. verify: `coral --version`, `uv --version`, `opencode --version` all work
#
# Requires setup_opencode.sh next to this script (already in this repo).
#
# Usage (from anywhere):
#   bash frameworks/CORAL/setup_env.sh                      # base setup
#   bash frameworks/CORAL/setup_env.sh --with-frontier-cs   # + Frontier-CS algorithmic track
#   bash frameworks/CORAL/setup_env.sh --verify             # skip installs, just check
#
# --with-frontier-cs also provisions the Frontier-CS *algorithmic* track (the
# workload behind set:offline / examples/frontier_cs_*): installs the frontier_cs
# package into CORAL's uv venv and builds+starts the go-judge server on :8081, via
# setup_frontier_cs_env.sh. Opt-in because it pulls a docker judge + runs a server;
# the base env (for e.g. the self-contained `math` suite) needs none of it.
#
# Env overrides:
#   CONDA_HOME   conda install prefix   ($CONDA_BASE override > `conda info --base` > ~/miniconda3)
#   CONDA_ENV    env name               (default: coral)
#
# Known gotcha when actually RUNNING tasks (not a setup_env.sh concern, noted
# here so it isn't rediscovered): scripts/coral/coral-vllm.sh resolves the
# coral env's bin dir as `$(conda info --base)/envs/coral/bin`. On a host with
# more than one conda installation (e.g. a system miniconda plus a project one
# configured via .condarc `envs_dirs`), `conda info --base` can point at the
# WRONG installation and this path silently doesn't exist -> "uv not found on
# PATH". If that happens, pass CORAL_CONDA_BIN=/path/to/conda/envs/coral/bin
# explicitly (scripts/coral/coral-vllm.sh already honors this override).

set -euo pipefail

CONDA_HOME="${CONDA_HOME:-${CONDA_BASE:-$(conda info --base 2>/dev/null)}}"; [ -n "$CONDA_HOME" ] || CONDA_HOME="$HOME/miniconda3"
CONDA_ENV="${CONDA_ENV:-coral}"
CORAL_DIR="$(cd "$(dirname "$0")" && pwd)"     # this script lives in frameworks/CORAL
VERIFY_ONLY=0
WITH_FRONTIER_CS=0
for a in "$@"; do
    case "$a" in
        --verify)           VERIFY_ONLY=1 ;;
        --with-frontier-cs) WITH_FRONTIER_CS=1 ;;
        *)                  printf '\033[31mERROR:\033[0m unknown arg: %s (use --verify and/or --with-frontier-cs)\n' "$a" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

conda_run() { "$CONDA_HOME/bin/conda" run --no-capture-output -n "$CONDA_ENV" "$@"; }

if [ "$VERIFY_ONLY" = "0" ]; then
    say "1. conda env '$CONDA_ENV'"
    [ -x "$CONDA_HOME/bin/conda" ] || die "conda not found at $CONDA_HOME — install miniconda there, or set CONDA_HOME"
    if ! "$CONDA_HOME/bin/conda" env list | grep -qE "^${CONDA_ENV}\s"; then
        "$CONDA_HOME/bin/conda" create -y -n "$CONDA_ENV" python=3.11
    fi
    ok "env ready ($(conda_run python --version 2>&1))"

    say "2. pip install -e .[all]"
    ( cd "$CORAL_DIR" && conda_run pip install -e ".[all]" )
    ok "coral installed"

    say "3. uv (into the coral env)"
    conda_run pip install uv
    ok "uv: $(conda_run uv --version)"

    say "4. opencode + bun + provider package"
    bash "$CORAL_DIR/setup_opencode.sh"

    if [ "$WITH_FRONTIER_CS" = "1" ]; then
        say "5. Frontier-CS algorithmic track (frontier_cs pkg + go-judge on :8081)"
        # steps 1-2 (opencode, tmux) are already done above; skip to avoid redoing them.
        SKIP_OPENCODE=1 SKIP_TMUX=1 CORAL_CONDA_BIN="$CONDA_HOME/envs/$CONDA_ENV/bin" \
            bash "$CORAL_DIR/setup_frontier_cs_env.sh"
        ok "Frontier-CS ready (frontier_cs importable; judge on :8081)"
    fi
fi

say "verify"
conda_run coral --version >/dev/null && ok "coral: $(conda_run coral --version)"
conda_run uv --version >/dev/null && ok "uv: $(conda_run uv --version)"
OPENCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
[ -x "$OPENCODE_BIN" ] && ok "opencode: $($OPENCODE_BIN --version)" || die "opencode not found at $OPENCODE_BIN"

printf '\n\033[1;32mCORAL environment ready.\033[0m  Run a task with: bash %s/../../scripts/coral/start.sh <task>\n' "$CORAL_DIR"
