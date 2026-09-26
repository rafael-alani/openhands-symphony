#!/bin/sh
unset LOCAL_BACKEND_API_KEY GH_TOKEN GITHUB_TOKEN GH_CONFIG_DIR ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY BROWSER_USE_API_KEY
export CODEX_PATH=/opt/provider-clis/node_modules/.bin/codex
export INITIAL_AGENT_MODE=agent
export NO_BROWSER=1
exec /opt/openhands-acp/node_modules/.bin/codex-acp
