"""Entry point so the MCP server can be launched as `python -m kernos.browser`.

The server registration in kernos/server.py and kernos/chat.py uses
StdioServerParameters(command=sys.executable, args=["-m", "kernos.browser"]).
"""

from kernos.browser.server import run_stdio

if __name__ == "__main__":
    run_stdio()
