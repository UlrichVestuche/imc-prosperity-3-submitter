import keyring
import requests
import sys
import time
import webbrowser
from collections import defaultdict
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from requests_toolbelt import MultipartEncoder
from typing import Any, Optional

try:
    # This import needs to happen to prevent a 1024-character pasting limit on macOS
    # See https://stackoverflow.com/a/53871077
    import readline
except ImportError:
    pass

KEYRING_SERVICE = "prosperity3submit"
KEYRING_USERNAME = "prosperity-id-token"

API_BASE_URL = "https://bz97lt8b1e.execute-api.eu-west-1.amazonaws.com/prod"

def refresh_token() -> None:
    print("""
prosperity3submit needs your Prosperity ID token to make authenticated requests to Prosperity's internal API.
Your token is stored in the local storage item with the `CognitoIdentityServiceProvider.<some id>.<email>.idToken` key on the Prosperity website.
You can inspect the local storage items of a website by having the website open in the active tab, pressing F12 to open the browser's developer tools, and going to the Application (Chrome) or Storage (Firefox) tab.
From there, click on Local Storage in the sidebar and select the website that appears underneath the sidebar entry.
Your token is stored in your system's credentials store for convenience.
    """.strip())

    token = input("Prosperity ID token: ")
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)

def request_with_token(method: str, url: str, form_data: Optional[dict[str, Any]] = None, attempt: int = 0) -> requests.Response:
    token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if token is None:
        refresh_token()
        return request_with_token(method, url, form_data)

    data = None
    headers = {"Authorization": f"Bearer {token}"}

    if form_data is not None:
        encoder = MultipartEncoder(form_data)
        data = encoder
        headers["content-type"] = encoder.content_type

    response = requests.request(method, url, data=data, headers=headers)

    if response.status_code == 401:
        refresh_token()
        return request_with_token(method, url, form_data)

    if response.status_code == 503:
        print("The Prosperity website is currently in maintenance mode, please try again later")
        sys.exit(1)

    if 500 <= response.status_code < 600:
        print(f"Received unexpected HTTP {response.status_code} response from the Prosperity API, retrying request after 5 seconds (attempt {attempt + 1})")
        time.sleep(5)
        return request_with_token(method, url, form_data, attempt + 1)

    response.raise_for_status()
    return response

def format_path(path: Path) -> str:
    cwd = Path.cwd()
    if path.is_relative_to(cwd):
        return str(path.relative_to(cwd))
    else:
        return str(path)

def get_current_round() -> str:
    print("Retrieving current round")
    rounds = request_with_token("GET", f"{API_BASE_URL}/game/rounds").json()

    open_round = next((r for r in rounds if r["isOpen"]), None)
    if open_round is None:
        raise ValueError("No round is currently accepting submissions")

    return open_round["id"]

def submit_algorithm(algorithm_file: Path) -> None:
    print(f"Submitting {format_path(algorithm_file)}")
    request_with_token(
        "POST",
        f"{API_BASE_URL}/submission/algo",
        {"file": (algorithm_file.name, algorithm_file.read_bytes(), "text/x-python")},
    )

def list_algorithms(round: str) -> list[dict[str, Any]]:
    return request_with_token("GET", f"{API_BASE_URL}/submission/algo/{round}").json()

def get_submission_status(data: dict[str, Any]) -> str:
    status = data["status"]

    if data["selectedForRound"]:
        status += " (active)"

    return status

def monitor_status(round: str, algorithm_file: Path) -> dict[str, Any]:
    print("Monitoring submission status")

    algorithms = list_algorithms(round)
    data = next(a for a in algorithms if a["fileName"] == algorithm_file.name)

    status = get_submission_status(data)
    print(f"Submission status: {status}")

    while data["status"] != "FINISHED" and data["status"] != "ERROR":
        time.sleep(0 if data["selectedForRound"] else 5)

        algorithms = list_algorithms(round)
        data = next(a for a in algorithms if a["id"] == data["id"])

        new_status = get_submission_status(data)
        if new_status != status:
            print(f"Submission status changed: {new_status}")
            status = new_status

    return data

def download_logs(data: dict[str, Any], output_file: Path) -> None:
    try:
        print(f"Downloading submission logs to {format_path(output_file)}")

        # Get the URL for the logs using a token-authenticated request
        url_response = request_with_token("GET", f"{API_BASE_URL}/submission/logs/{data['id']}")
        download_url = url_response.json()

        # Attempt to download the file from the URL
        download_response = requests.get(download_url)
        download_response.raise_for_status()

        # Ensure the directory exists and write the file content
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("wb+") as file:
            file.write(download_response.content)
    except Exception as e:
        print(f"An error occurred while downloading logs: {e}")

def log_profit_loss(output_file: Path) -> None:
    lines = output_file.read_text(encoding="utf-8").splitlines()

    profit_loss_by_timestamp = defaultdict(float)

    activities_log_idx = lines.index("Activities log:")
    for line in lines[activities_log_idx + 2:]:
        if line == "":
            break

        columns = line.split(";")
        timestamp = int(columns[1])
        profit_loss = float(columns[-1])

        profit_loss_by_timestamp[timestamp] += profit_loss

    final_profit_loss = max(profit_loss_by_timestamp.items())[1]
    print(f"Final profit / loss: {final_profit_loss:,.0f}")

class HTTPRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        return super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return

def open_in_visualizer(output_file: Path, no_requests: int) -> None:
    http_handler = partial(HTTPRequestHandler, directory=output_file.parent)
    http_server = HTTPServer(("localhost", 0), http_handler)

    webbrowser.open(f"https://jmerle.github.io/imc-prosperity-2-visualizer/?open=http://localhost:{http_server.server_port}/{output_file.name}")

    # Chrome makes 2 requests: 1 OPTIONS request to check for CORS headers and 1 GET request to get the data
    # Some users reported their browser only makes 1 request, which is covered by the --vis-requests option
    for _ in range(no_requests):
        http_server.handle_request()

def submit(algorithm_file: Path, output_file: Optional[Path], open_visualizer: bool, visualizer_requests: int) -> None:
    round = get_current_round()

    submit_algorithm(algorithm_file)
    data = monitor_status(round, algorithm_file)

    if output_file is not None:
        download_logs(data, output_file)

        if data["status"] == "FINISHED":
            log_profit_loss(output_file)
    with output_file.open("rb") as f:
        content = f.read()
    print("Downloaded file size:", len(content))
    if open_visualizer:
        if data["status"] == "ERROR":
            print("Submission errored, not opening visualizer")
        else:
            open_in_visualizer(output_file, visualizer_requests)
