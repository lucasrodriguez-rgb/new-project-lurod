"""
Launch the dashboard server.

Usage:
    python3 -m polymarket_index.dashboard
    python3 -m polymarket_index.dashboard --port 8080
"""
import argparse
from polymarket_index.dashboard.app import start_server

def main():
    parser = argparse.ArgumentParser(description="Polymarket Index Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8050, help="Port (default: 8050)")
    args = parser.parse_args()
    print(f"Dashboard starting at http://localhost:{args.port}")
    start_server(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
