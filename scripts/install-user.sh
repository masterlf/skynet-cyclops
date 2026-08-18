#!/bin/sh
# X-Skynet-Cyclops-Owned: true
set -eu

apply=false
case "${1-}" in
  "") ;;
  --apply) apply=true ;;
  *) printf '%s\n' "usage: install-user.sh [--apply]" >&2; exit 2 ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
unit_dir="$config_home/systemd/user"
app_config_dir="$config_home/skynet-cyclops"
state_dir="$state_home/skynet-cyclops"
marker="X-Skynet-Cyclops-Owned: true"

check_target() {
  target=$1
  if [ -L "$target" ]; then
    printf '%s\n' "refusing symbolic-link target: $target" >&2
    exit 1
  fi
  if [ -e "$target" ]; then
    if [ ! -f "$target" ]; then
      printf '%s\n' "refusing non-regular target: $target" >&2
      exit 1
    fi
    owner=$(stat -c '%u' "$target")
    if [ "$owner" != "$(id -u)" ]; then
      printf '%s\n' "refusing target with different owner: $target" >&2
      exit 1
    fi
    if ! grep -Fq "$marker" "$target"; then
      printf '%s\n' "refusing file not owned by Skynet-Cyclops: $target" >&2
      exit 1
    fi
  fi
}

install_file() {
  source=$1
  target=$2
  mode=$3
  preserve=${4-false}
  if [ "$preserve" = true ] && { [ -e "$target" ] || [ -L "$target" ]; }; then
    if [ -L "$target" ] || [ ! -f "$target" ]; then
      printf '%s\n' "refusing unsafe operator file: $target" >&2
      exit 1
    fi
    owner=$(stat -c '%u' "$target")
    if [ "$owner" != "$(id -u)" ]; then
      printf '%s\n' "refusing operator file with different owner: $target" >&2
      exit 1
    fi
    printf '%s\n' "preserved existing operator file: $target"
    return
  fi
  check_target "$target"
  if [ "$apply" = false ]; then
    printf '%s\n' "dry-run: install $target"
    return
  fi
  directory=$(dirname -- "$target")
  mkdir -p -- "$directory"
  if [ -e "$target" ]; then
    backup="$target.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p -- "$target" "$backup"
    printf '%s\n' "backed up $target to $backup"
  fi
  temporary=$(mktemp "$directory/.skynet-cyclops.XXXXXX")
  trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
  install -m "$mode" -- "$source" "$temporary"
  mv -f -- "$temporary" "$target"
  trap - EXIT HUP INT TERM
  printf '%s\n' "installed $target"
}

install_file "$repo_dir/packaging/systemd/skynet-cyclops.service" "$unit_dir/skynet-cyclops.service" 600
install_file "$repo_dir/packaging/systemd/skynet-cyclops.timer" "$unit_dir/skynet-cyclops.timer" 600
install_file "$repo_dir/examples/config.yaml" "$app_config_dir/config.yaml" 600 true
install_file "$repo_dir/examples/release-observe.yaml" "$app_config_dir/mission.yaml" 600 true

if [ "$apply" = true ]; then
  mkdir -p -- "$state_dir"
  chmod 700 -- "$state_dir"
fi

printf '%s\n' "Next manual commands (not run):"
printf '%s\n' "  python3 -m pip install --user ."
printf '%s\n' "  systemctl --user daemon-reload"
printf '%s\n' "  systemctl --user enable skynet-cyclops.timer"
printf '%s\n' "  systemctl --user start skynet-cyclops.timer"
