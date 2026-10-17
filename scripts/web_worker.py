"""Private loopback workers owned by the DSH plugin, with OS-allocated ports."""
import argparse
import os
from http.server import ThreadingHTTPServer

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["data", "reports"])
    args = parser.parse_args()
    if args.kind == "data":
        from stock_data_server import Handler
    else:
        from report_web_server import ReportHandler as Handler
    from web_process_owner import watch_owner
    watch_owner()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(f"FINANCE_READY:{server.server_port}", flush=True)
    server.serve_forever()
