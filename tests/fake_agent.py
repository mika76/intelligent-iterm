"""A stand-in ACP agent for tests: echoes prompts, and asks permission when the
prompt contains 'tool'."""
import json
import sys


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
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "s1", "modes": {"currentModeId": "auto",
                                         "availableModes": [{"id": "default"}, {"id": "auto"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        text = "\n".join(b["text"] for b in p["prompt"])
        if "tool" in text:
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
                "sessionId": "s1", "toolCall": {"toolCallId": "t1", "title": "Run ls"},
                "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
            reply = json.loads(sys.stdin.readline())
            update("s1", {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": "permission: " + json.dumps(reply["result"])}})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "echo: "}})
        update("s1", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        if "suggest" in text:
            update("s1", {"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": "\n\nTry **this**:\n\n```bash\nls -la\n```\n\nor:\n\n```bash\nfor f in *; do\n  echo $f\ndone\n```\n"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
