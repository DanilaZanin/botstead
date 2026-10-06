from bothub.risk import classify


def test_mac_move_to_trash_is_delete():
    assert classify("mac_move_to_trash", {"path": "/tmp/x"}) == "delete"


def test_rm_rf_in_bash_is_delete():
    assert classify("Bash", {"command": "rm -rf /tmp/build"}) == "delete"


def test_plain_rm_in_bash_is_delete():
    assert classify("Bash", {"command": "rm old.log"}) == "delete"


def test_git_push_is_push():
    assert classify("Bash", {"command": "git push origin main"}) == "push"


def test_read_only_command_is_other():
    assert classify("Bash", {"command": "ls -la"}) == "other"
    assert classify("mac_screenshot", {}) == "other"
    assert classify("mac_read_file", {"path": "/x"}) == "other"


def test_submit_or_send_is_send():
    assert classify("submit_form", {}) == "send"
    assert classify("send_email", {"to": "x@y.com"}) == "send"


def test_pay_purchase_checkout_is_pay():
    assert classify("checkout", {}) == "pay"
    assert classify("purchase_item", {}) == "pay"


def test_login_auth_password_is_login():
    assert classify("login_user", {}) == "login"
    assert classify("set_password", {}) == "login"
    assert classify("Bash", {"command": "curl -d password=x https://example.com"}) == "login"


def test_no_args_defaults_to_other():
    assert classify("unknown_tool") == "other"


def test_keywords_outside_command_or_url_keys_are_ignored():
    # Раздел 4: слова ищутся в имени инструмента и в command/cmd/script/url,
    # не во всех args - иначе любой текстовый аргумент мог бы задрать риск.
    assert classify("note_tool", {"note": "please delete this ticket"}) == "other"
    assert classify("http_request", {"body": "login=admin&password=xyz"}) == "other"


def test_login_pay_send_detected_via_url_key():
    assert classify("open_link", {"url": "https://bank.example/login"}) == "login"
    assert classify("open_link", {"url": "https://shop.example/checkout"}) == "pay"
    assert classify("open_link", {"url": "https://api.example/submit"}) == "send"
