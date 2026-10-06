from bothub_mac.protocol import heartbeat_message, hello_message, result_message


def test_hello_message_shape():
    msg = hello_message()
    assert msg["type"] == "hello"
    assert isinstance(msg["host"], str)
    assert msg["os"].startswith("macOS")
    assert set(msg["permissions"]) == {"files", "screen", "automation", "accessibility"}


def test_heartbeat_message_shape():
    msg = heartbeat_message(locked=True, battery=42)
    assert msg == {"type": "heartbeat", "locked": True, "battery": 42}


def test_result_message_ok():
    msg = result_message("c1", ok=True, data={"a": 1})
    assert msg == {"type": "result", "id": "c1", "ok": True, "data": {"a": 1}}


def test_result_message_error():
    msg = result_message("c1", ok=False, error="boom")
    assert msg == {"type": "result", "id": "c1", "ok": False, "error": "boom"}
