"""A stand-in ACP agent for tests: echoes prompts, and asks permission when the
prompt contains 'tool'."""
import json
import sys
import urllib.request

MCP = {}


def mcp_call(method, params):
    """Call the client-provided HTTP MCP server, like Claude Code would."""
    req = urllib.request.Request(MCP["url"], json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {h["name"]: h["value"] for h in MCP["headers"]} | {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


for line in sys.stdin:
    msg = json.loads(line)
    method, mid, p = msg.get("method"), msg.get("id"), msg.get("params", {})
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        assert p["systemPrompt"]["mode"] == "append"
        for server in p.get("mcpServers") or []:
            MCP.update(server)
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "s1", "modes": {"currentModeId": "auto",
                                         "availableModes": [{"id": "default"}, {"id": "auto"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        text = "\n".join(b["text"] for b in p["prompt"])
        asked = p["prompt"][-1]["text"]  # the user's own words (context comes before)
        if asked.startswith("tool"):
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
                "sessionId": "s1", "toolCall": {"toolCallId": "t1", "title": "Run ls"},
                "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
            reply = json.loads(sys.stdin.readline())
            update("s1", {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": "permission: " + json.dumps(reply["result"])}})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "echo: "}})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        if asked.startswith("propose") and MCP:
            mcp_call("initialize", {"protocolVersion": "2025-06-18"})
            res = mcp_call("tools/call", {"name": "propose_command",
                                          "arguments": {"command": "ls -la", "reason": "list files"}})
            update("s1", {"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": " tool: " + res["result"]["content"][0]["text"]}})
        if "suggest" in text:
            update("s1", {"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": "\n\nTry **this**:\n\n```bash\nls -la\n```\n\nor:\n\n```bash\nfor f in *; do\n  echo $f\ndone\n```\n"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
