#!/usr/bin/env bash
# Compatibility shim — the installer was renamed to install.sh. This
# forwards to it so existing docs and muscle memory keep working.
exec "$(dirname "${BASH_SOURCE[0]}")/install.sh" "$@"
