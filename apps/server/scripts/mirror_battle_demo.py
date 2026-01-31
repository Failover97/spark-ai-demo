import json
import urllib.request


API_URL = "http://localhost:8000/v1/agent"


def _post(payload: dict) -> str:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8")


def case_all_in_sol() -> None:
    payload = {
        "message": "我想 All in SOL",
        "stream_mode": "raw",
        "battle": {"enabled": True, "selected_dimensions": ["HowMuch", "Exit"]},
    }
    print("Case1 response:")
    print(_post(payload))


def case_user_concede() -> None:
    payload = {
        "message": "你说得对/我考虑一下",
        "stream_mode": "raw",
        "battle": {"enabled": True, "selected_dimensions": ["Why"]},
        "session_id": "battle-demo-concede",
    }
    print("Case2 response:")
    print(_post(payload))


if __name__ == "__main__":
    case_all_in_sol()
    case_user_concede()
