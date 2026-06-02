#!/usr/bin/env bash
# Provision a machine to run CORAL on the Frontier-CS *algorithmic* track against
# a local vLLM fleet (OpenCode runtime + per-agent direct vLLM binding).
#
# This fills the gaps that `uv sync` / setup_opencode.sh alone do NOT cover.
# It is idempotent: re-running skips anything already in place.
#
# What it does:
#   1. OpenCode agent runtime  -> runs ./setup_opencode.sh (opencode + bun + provider pkg)
#   2. tmux                    -> CORAL's default run.session launcher (tdnf/dnf/apt/yum/zypper)
#   3. docker compose v2       -> the Frontier-CS judge auto-starts via `docker compose up`;
#                                 installs the CLI plugin to ~/.docker/cli-plugins if missing
#   4. OpenCode models cache   -> pre-warms ~/.cache/opencode/models.json. A COLD fetch of
#                                 models.dev on the first `opencode run` can hang forever and
#                                 the agent never even contacts vLLM. Pre-populating avoids it.
#   5. frontier_cs package     -> installed into CORAL's uv venv (the legacy eval/grader.py runs
#                                 IN-PROCESS there). Installed WITHOUT skypilot[aws,gcp] (only
#                                 lazily imported); the lighter deps it really needs are added.
#   6. repo.py symlink fix     -> examples/frontier_cs_algo/*/eval/testdata are dangling symlinks
#                                 (absolute paths from the task author's machine). Stock copytree
#                                 aborts `coral start`; patches it to tolerate them. No-op if the
#                                 fork already carries the fix.
#   7. go-judge server         -> builds + starts the Competitive-Programming container on :8081
#                                 and verifies /problems responds.
#
# NOT handled here (run separately):
#   - The vLLM fleet itself:  bash serving/scripts/start_vllm_multi.sh serving/configs/qwen3-coder-30b.yaml:4
#   - uv itself:              expected on PATH or in conda env `coral` (~/miniconda3/envs/coral/bin)
#
# Usage:
#   bash frameworks/CORAL/setup_frontier_cs_env.sh
#
# Env overrides:
#   FRONTIER_CS=/path/to/Frontier-CS   (default: <repo>/data/Frontier-CS)
#   CORAL_CONDA_BIN=~/miniconda3/envs/coral/bin   (where to find uv if not already on PATH)
#   COMPOSE_VERSION=v2.32.4            (docker compose plugin version to fetch)
#   SKIP_OPENCODE=1 SKIP_TMUX=1 SKIP_COMPOSE=1 SKIP_WARMCACHE=1 SKIP_PYDEPS=1 SKIP_PATCH=1 SKIP_JUDGE=1

set -euo pipefail

log()  { printf "\033[1;34m[setup-fcs]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[setup-fcs]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[setup-fcs]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m[setup-fcs]\033[0m %s\n" "$*" >&2; exit 1; }

CORAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CORAL_DIR/../.." && pwd)"
FRONTIER_CS="${FRONTIER_CS:-$REPO_ROOT/data/Frontier-CS}"
COMPOSE_VERSION="${COMPOSE_VERSION:-v2.32.4}"

log "CORAL dir:    $CORAL_DIR"
log "Repo root:    $REPO_ROOT"
log "Frontier-CS:  $FRONTIER_CS"
[ -d "$FRONTIER_CS/algorithmic" ] || die "Frontier-CS/algorithmic not found at $FRONTIER_CS (set FRONTIER_CS=...)"

# --- 1. OpenCode runtime (opencode + bun + @ai-sdk/openai-compatible) --------
if [ "${SKIP_OPENCODE:-0}" != "1" ]; then
    if [ -x "$CORAL_DIR/setup_opencode.sh" ] || [ -f "$CORAL_DIR/setup_opencode.sh" ]; then
        log "[1/7] OpenCode runtime via setup_opencode.sh ..."
        bash "$CORAL_DIR/setup_opencode.sh"
    else
        warn "[1/7] setup_opencode.sh not found in $CORAL_DIR — skipping OpenCode install"
    fi
    [ -d "$HOME/.opencode/bin" ] && export PATH="$HOME/.opencode/bin:$PATH"
    [ -d "$HOME/.bun/bin" ] && export PATH="$HOME/.bun/bin:$PATH"
else
    log "[1/7] OpenCode runtime — skipped"
fi

# --- 2. tmux ----------------------------------------------------------------
if [ "${SKIP_TMUX:-0}" != "1" ]; then
    if command -v tmux >/dev/null 2>&1; then
        ok "[2/7] tmux present: $(tmux -V)"
    else
        log "[2/7] installing tmux ..."
        if   command -v tdnf   >/dev/null 2>&1; then sudo tdnf install -y tmux
        elif command -v dnf    >/dev/null 2>&1; then sudo dnf install -y tmux
        elif command -v yum    >/dev/null 2>&1; then sudo yum install -y tmux
        elif command -v apt-get>/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y tmux
        elif command -v zypper >/dev/null 2>&1; then sudo zypper install -y tmux
        else warn "no known package manager — install tmux manually (or use run.session=local)"; fi
        command -v tmux >/dev/null 2>&1 && ok "tmux installed: $(tmux -V)"
    fi
else
    log "[2/7] tmux — skipped"
fi

# --- 3. docker compose v2 plugin --------------------------------------------
if [ "${SKIP_COMPOSE:-0}" != "1" ]; then
    if docker compose version >/dev/null 2>&1; then
        ok "[3/7] docker compose present: $(docker compose version --short 2>/dev/null || echo v2)"
    else
        log "[3/7] installing docker compose plugin $COMPOSE_VERSION ..."
        mkdir -p "$HOME/.docker/cli-plugins"
        ARCH="$(uname -m)"
        URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}"
        curl -fsSL "$URL" -o "$HOME/.docker/cli-plugins/docker-compose"
        chmod +x "$HOME/.docker/cli-plugins/docker-compose"
        docker compose version >/dev/null 2>&1 && ok "docker compose installed: $(docker compose version --short)" \
            || warn "docker compose still not working — check docker setup"
    fi
else
    log "[3/7] docker compose — skipped"
fi

# --- 4. Pre-warm OpenCode models cache (avoids first-run hang) ---------------
if [ "${SKIP_WARMCACHE:-0}" != "1" ]; then
    CACHE="$HOME/.cache/opencode/models.json"
    if [ -s "$CACHE" ]; then
        ok "[4/7] OpenCode models cache present ($(du -h "$CACHE" | cut -f1))"
    else
        log "[4/7] pre-warming $CACHE from models.dev ..."
        mkdir -p "$(dirname "$CACHE")"
        if curl -fsSL --max-time 30 https://models.dev/api.json -o "$CACHE"; then
            ok "models cache warmed ($(du -h "$CACHE" | cut -f1))"
        else
            warn "could not fetch models.dev — the first 'opencode run' may hang on a cold fetch"
        fi
    fi
else
    log "[4/7] models cache — skipped"
fi

# --- 5. frontier_cs into CORAL's uv venv ------------------------------------
if [ "${SKIP_PYDEPS:-0}" != "1" ]; then
    CORAL_CONDA_BIN="${CORAL_CONDA_BIN:-$HOME/miniconda3/envs/coral/bin}"
    if ! command -v uv >/dev/null 2>&1 && [ -x "$CORAL_CONDA_BIN/uv" ]; then
        export PATH="$CORAL_CONDA_BIN:$PATH"
    fi
    command -v uv >/dev/null 2>&1 || die "uv not found on PATH or in $CORAL_CONDA_BIN — install it (e.g. conda create -n coral python=3.11 && pip install uv)"
    log "[5/7] uv: $(command -v uv) ($(uv --version))"

    ( cd "$CORAL_DIR"
      [ -d .venv ] || { log "no .venv — running 'uv sync' for CORAL ..."; uv sync; }
      log "installing frontier_cs (editable, --no-deps) ..."
      uv pip install -e "$FRONTIER_CS" --no-deps
      log "installing frontier_cs algorithmic deps (skipping heavy skypilot[aws,gcp]) ..."
      uv pip install numpy requests pyyaml colorlog tqdm python-dotenv \
                     openai anthropic google-genai google-generativeai datasets
      if .venv/bin/python -c "from frontier_cs import SingleEvaluator" >/dev/null 2>&1; then
          ok "frontier_cs importable in CORAL .venv"
      else
          die "frontier_cs still not importable — check the dep list above"
      fi
    )
else
    log "[5/7] frontier_cs python deps — skipped"
fi

# --- 6. Patch copy_eval_to_private to tolerate dangling symlinks ------------
if [ "${SKIP_PATCH:-0}" != "1" ]; then
    REPO_PY="$CORAL_DIR/coral/workspace/repo.py"
    if [ ! -f "$REPO_PY" ]; then
        warn "[6/7] $REPO_PY not found — skipping symlink patch"
    elif grep -q "ignore_dangling_symlinks" "$REPO_PY"; then
        ok "[6/7] repo.py dangling-symlink fix already present"
    else
        log "[6/7] patching copy_eval_to_private (dangling eval/testdata symlinks) ..."
        python3 - "$REPO_PY" <<'PY'
import sys
p = sys.argv[1]
src = open(p).read()
old = "shutil.copytree(eval_src, eval_dst)"
new = "shutil.copytree(eval_src, eval_dst, symlinks=True, ignore_dangling_symlinks=True)"
if old not in src:
    sys.exit("could not find the copytree call to patch in " + p)
open(p, "w").write(src.replace(old, new, 1))
print("patched", p)
PY
        ok "repo.py patched"
    fi
else
    log "[6/7] repo.py patch — skipped"
fi

# --- 7. Build + start the go-judge server -----------------------------------
if [ "${SKIP_JUDGE:-0}" != "1" ]; then
    JUDGE_DIR="$FRONTIER_CS/algorithmic"
    if curl -s --max-time 5 http://localhost:8081/problems >/dev/null 2>&1; then
        ok "[7/7] go-judge already up on :8081"
    else
        log "[7/7] building + starting go-judge (docker compose up -d --build) ..."
        ( cd "$JUDGE_DIR" && docker compose up -d --build )
        log "waiting for judge on :8081 ..."
        for i in $(seq 1 60); do
            if curl -s --max-time 5 http://localhost:8081/problems >/dev/null 2>&1; then
                ok "go-judge ready on :8081"; break
            fi
            sleep 2
            [ "$i" = "60" ] && warn "go-judge not responding after 120s — check: docker logs Competitive-Programming"
        done
    fi
else
    log "[7/7] go-judge — skipped"
fi

echo ""
ok "Environment ready. Next:"
echo "    # 1) start the vLLM fleet (4x Qwen3-Coder-30B on :8000-:8003)"
echo "    bash $REPO_ROOT/serving/scripts/start_vllm_multi.sh $REPO_ROOT/serving/configs/qwen3-coder-30b.yaml:4"
echo "    # 2) run a Frontier-CS algorithmic task on the pool (4 agents, 1:1 vLLM binding)"
echo "    bash $REPO_ROOT/scripts/coral/coral-vllm.sh $CORAL_DIR/examples/frontier_cs_algo/1/task.yaml"
echo "    # 3) monitor"
echo "    cd $CORAL_DIR && uv run coral status   # or: coral log / coral ui"
