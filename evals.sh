#!/bin/bash
# KERNOS Eval Harness Launcher
# Usage:
#   ./evals.sh                              # run all scenarios under evals/scenarios/
#   ./evals.sh evals/scenarios/01_...md     # run a single scenario
#   ./evals.sh --verbose                    # pass flags through

cd "$(dirname "$0")"

# Activate the virtual environment
source .venv/bin/activate

# Load .env so ANTHROPIC_API_KEY + KERNOS_LLM_* are set
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "Running Kernos eval harness..."
echo ""
python -m kernos.evals "$@"
