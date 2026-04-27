#!/bin/bash
# KERNOS Discord Bot Launcher
# Double-click this file (or run from terminal: ./start.sh)

cd "$(dirname "$0")"

# Activate the virtual environment
source .venv/bin/activate

# IWL v3 thin-path soak: route turns through the decoupled-cognition
# path (TurnRunner + IntegrationService + EnactmentService).
# Conversational kinds flow end-to-end with per-turn
# ProductionResponseDelivery + telemetry binding + synthetic
# reasoning.* aggregation. Full-machinery dispatch is gated behind
# _UnwiredDescriptorLookup until INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING
# threads request context. Unset this var to revert to legacy path.
export KERNOS_USE_DECOUPLED_TURN_RUNNER=1

# Start the bot
echo "Starting Kernos..."
echo "Decoupled turn runner: ${KERNOS_USE_DECOUPLED_TURN_RUNNER:-OFF}"
echo "Press Ctrl+C to stop."
echo ""
python kernos/server.py
