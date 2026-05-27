#!/usr/bin/env bash
#
# arcade-agent — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/takabayashi/arcade-agent/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/takabayashi/arcade-agent/main/install.sh | bash -s -- /custom/install/path
#
# Environment overrides:
#   ARCADE_AGENT_DIR     install directory (default: $HOME/arcade-agent)
#   ARCADE_AGENT_REPO    git URL (default: https://github.com/takabayashi/arcade-agent.git)
#   ARCADE_AGENT_BRANCH  branch / tag / sha (default: main)
#
# What it does (idempotent — safe to re-run):
#   1. Verifies Python >= 3.11 is available.
#   2. Installs `uv` if missing (via astral.sh installer).
#   3. Clones the repo (or `git pull --ff-only` if it already exists).
#   4. Creates a venv with uv and installs the package + dev deps editable.
#   5. Copies .env.example -> .env if .env doesn't exist (does NOT overwrite).
#   6. Prints next steps.

set -euo pipefail

INSTALL_DIR="${1:-${ARCADE_AGENT_DIR:-$HOME/arcade-agent}}"
REPO_URL="${ARCADE_AGENT_REPO:-https://github.com/takabayashi/arcade-agent.git}"
BRANCH="${ARCADE_AGENT_BRANCH:-main}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  bold=$'\e[1m'; dim=$'\e[2m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; cyan=$'\e[36m'; reset=$'\e[0m'
else
  bold=""; dim=""; green=""; yellow=""; red=""; cyan=""; reset=""
fi

step()  { printf "%s==>%s %s\n" "$cyan$bold" "$reset" "$1"; }
ok()    { printf "%s  ✓%s %s\n" "$green" "$reset" "$1"; }
warn()  { printf "%s  ! %s%s\n" "$yellow" "$1" "$reset" >&2; }
fail()  { printf "%s  ✗ %s%s\n" "$red" "$1" "$reset" >&2; exit 1; }

# --- pre-checks ------------------------------------------------------------
step "Checking prerequisites"

if ! command -v git >/dev/null 2>&1; then
  fail "git not found. Install git first: https://git-scm.com/downloads"
fi
ok "git: $(git --version)"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found. Install Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}: https://www.python.org/downloads/"
fi
py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
py_major=${py_ver%.*}
py_minor=${py_ver#*.}
if [ "$py_major" -lt "$MIN_PY_MAJOR" ] || { [ "$py_major" -eq "$MIN_PY_MAJOR" ] && [ "$py_minor" -lt "$MIN_PY_MINOR" ]; }; then
  fail "Python ${py_ver} found, but >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} is required."
fi
ok "python3: $(python3 --version)"

# --- ensure uv -------------------------------------------------------------
step "Ensuring uv is installed"
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version)"
else
  warn "uv not found — installing via astral.sh installer"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The astral installer adds uv to $HOME/.local/bin (Linux) or $HOME/.cargo/bin (macOS in some setups).
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv installation finished but 'uv' is not on PATH. Restart your shell and re-run."
  fi
  ok "uv installed: $(uv --version)"
fi

# --- clone / update --------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  step "Updating existing checkout at $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --quiet origin
  git -C "$INSTALL_DIR" checkout --quiet "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only --quiet
  ok "checkout updated to $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
  fail "$INSTALL_DIR exists and is not empty (and not a git checkout). Move it aside or pass a different path."
else
  step "Cloning $REPO_URL -> $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  ok "cloned at $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
fi

cd "$INSTALL_DIR"

# --- venv + deps -----------------------------------------------------------
if [ -d "$INSTALL_DIR/.venv" ]; then
  step "Re-using existing venv (.venv) and syncing dependencies"
else
  step "Creating venv (.venv) and installing dependencies"
  uv venv --quiet --python "${py_major}.${py_minor}"
fi
uv pip install --quiet -e ".[dev]"
ok "venv ready at $INSTALL_DIR/.venv"

# Smoke: import + --help
if "$INSTALL_DIR/.venv/bin/python" -c "import arcade_agent" >/dev/null 2>&1; then
  ok "import arcade_agent works"
else
  fail "Installed but 'import arcade_agent' failed. Check uv output above."
fi
if "$INSTALL_DIR/.venv/bin/arcade-agent" --help >/dev/null 2>&1; then
  ok "arcade-agent CLI runs"
else
  fail "Installed but 'arcade-agent --help' failed. Check uv output above."
fi

# --- .env ------------------------------------------------------------------
if [ -f "$INSTALL_DIR/.env" ]; then
  step ".env already exists — leaving it alone"
  ok "$INSTALL_DIR/.env"
else
  step "Seeding .env from .env.example"
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  ok "wrote $INSTALL_DIR/.env (mode 0600). Fill in your keys."
fi

# --- final message ---------------------------------------------------------
printf "\n%sInstall complete.%s\n\n" "$green$bold" "$reset"
printf "Next steps:\n"
printf "  1. ${bold}Edit your keys${reset} in ${cyan}%s/.env${reset}:\n" "$INSTALL_DIR"
printf "       ARCADE_API_KEY     from https://api.arcade.dev/dashboard\n"
printf "       ANTHROPIC_API_KEY  from https://console.anthropic.com/settings/keys\n"
printf "  2. ${bold}First-run wizard${reset} (creates a binding; opens browser for Google consent via Arcade):\n"
printf "       cd %s && .venv/bin/arcade-agent init\n" "$INSTALL_DIR"
printf "  3. ${bold}Start the daemon and chat${reset}:\n"
printf "       .venv/bin/arcade-agent daemon start\n"
printf "       .venv/bin/arcade-agent ask \"list my last 3 emails\"\n"
printf "\nDocs: %s/docs\n" "$INSTALL_DIR"
