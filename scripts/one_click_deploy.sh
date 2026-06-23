#!/usr/bin/env bash
# KUN one-click deploy helper.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AShan0227/KUN/main/scripts/one_click_deploy.sh | bash
#
# Optional environment variables:
#   KUN_REPO=https://github.com/AShan0227/KUN.git
#   KUN_DIR=$HOME/KUN
#   KUN_INSTALL_DAEMON=1

set -euo pipefail

KUN_REPO="${KUN_REPO:-https://github.com/AShan0227/KUN.git}"
KUN_DIR="${KUN_DIR:-$HOME/KUN}"
KUN_INSTALL_DAEMON="${KUN_INSTALL_DAEMON:-1}"
SERVICE_NAME="${KUN_SERVICE_NAME:-com.kun.control-plane.v6}"

log() { printf "\033[36m[kun-deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[kun-deploy]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[31m[kun-deploy]\033[0m %s\n" "$*" >&2; exit 1; }

need_git() {
  if ! command -v git >/dev/null 2>&1; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      fail "git is missing. Install Xcode Command Line Tools first: xcode-select --install"
    fi
    fail "git is missing. Install git with your OS package manager and rerun."
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv install did not add uv to PATH"
}

clone_or_update() {
  if [[ -d "$KUN_DIR/.git" ]]; then
    log "updating KUN at $KUN_DIR"
    git -C "$KUN_DIR" pull --ff-only origin main
  else
    log "cloning KUN into $KUN_DIR"
    git clone "$KUN_REPO" "$KUN_DIR"
  fi
}

install_dependencies() {
  cd "$KUN_DIR"
  log "installing Python dependencies"
  uv sync --extra dev
  mkdir -p .kun-local/logs
}

install_daemon() {
  cd "$KUN_DIR"
  if [[ "$KUN_INSTALL_DAEMON" != "1" ]]; then
    log "daemon install skipped (KUN_INSTALL_DAEMON=$KUN_INSTALL_DAEMON)"
    return
  fi

  case "$(uname -s)" in
    Darwin)
      log "installing launchd daemon"
      uv run kun control-plane daemon-service-install \
        --platform launchd \
        --service-name "$SERVICE_NAME" \
        --working-directory "$KUN_DIR" \
        --overwrite
      launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist"
      ;;
    Linux)
      if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found; daemon not installed. Run manually: cd $KUN_DIR && uv run kun control-plane daemon-run"
        return
      fi
      log "installing systemd user daemon"
      uv run kun control-plane daemon-service-install \
        --platform systemd \
        --service-name "$SERVICE_NAME" \
        --working-directory "$KUN_DIR" \
        --overwrite
      systemctl --user daemon-reload
      systemctl --user enable --now "$SERVICE_NAME.service"
      ;;
    *)
      warn "unsupported OS for daemon install. Run manually: cd $KUN_DIR && uv run kun control-plane daemon-run"
      ;;
  esac
}

precheck_provider() {
  # Audit F043: a daemon with no LLM provider can never produce real answers —
  # the router silently falls back to a stub. Refuse to install the daemon unless
  # at least one provider credential is configured (or the user opts in).
  if [[ "$KUN_INSTALL_DAEMON" != "1" ]]; then
    return
  fi
  local have_provider=0 var
  for var in KUN_OFOX_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY MINIMAX_API_KEY; do
    if [[ -n "${!var:-}" ]]; then have_provider=1; fi
  done
  if command -v claude >/dev/null 2>&1; then have_provider=1; fi
  if command -v codex >/dev/null 2>&1; then have_provider=1; fi
  if [[ "$have_provider" == "1" ]]; then
    log "LLM provider detected — daemon can serve real requests"
    return
  fi
  if [[ "${KUN_ALLOW_NO_PROVIDER:-0}" == "1" ]]; then
    warn "no LLM provider credential detected, but KUN_ALLOW_NO_PROVIDER=1 — installing the daemon anyway. It will NOT produce real answers until you configure a provider."
    return
  fi
  fail "No LLM provider configured — the daemon would fall back to a stub and never produce real answers.
  Set ONE of: ANTHROPIC_API_KEY, KUN_OFOX_API_KEY, OPENAI_API_KEY, MINIMAX_API_KEY,
  or log in the 'claude' or 'codex' CLI, then rerun.
  To install the daemon now and configure a provider later, rerun with KUN_ALLOW_NO_PROVIDER=1."
}

print_next_steps() {
  cd "$KUN_DIR"
  log "verifying KUN CLI"
  uv run kun --help >/dev/null
  uv run kun control-plane daemon-status || true
  cat <<NEXT

KUN is ready.

Path:
  $KUN_DIR

Common commands:
  cd "$KUN_DIR"
  uv run kun --help
  uv run kun control-plane daemon-status
  uv run kun control-plane daemon-stop

NEXT
}

need_git
ensure_uv
clone_or_update
install_dependencies
precheck_provider
install_daemon
print_next_steps
