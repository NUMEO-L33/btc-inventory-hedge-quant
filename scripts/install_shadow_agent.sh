#!/usr/bin/env bash
# Kept for the runbook and the recovery scripts: same as `install_agent.sh shadow`.
#
#   bash scripts/install_shadow_agent.sh            # install and start
#   bash scripts/install_shadow_agent.sh --uninstall
exec bash "$(dirname "$0")/install_agent.sh" shadow "$@"
