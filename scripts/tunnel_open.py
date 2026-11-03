"""Open the SSH tunnel to AWS and keep it open (for Tableau Desktop), until Ctrl+C.

    python scripts\\tunnel_open.py

Why: Tableau connects to PostgreSQL at localhost:5433. That port only leads to AWS while the
tunnel is open. run_daily.py opens and closes the tunnel by itself, but Tableau needs it open
for as long as you build or refresh the extracts.

If a tunnel is already open on DB_PORT (for example from Module 1, 2 or 4), it is reused and
this command just waits; pressing Ctrl+C then leaves that other tunnel running.

Exit codes: 0 closed with Ctrl+C, 1 tunnel failed, 2 not configured in .env.
"""
import sys
import time

import _bootstrap
from visa_monitor.env import schema_name
from visa_monitor.tunnel import SshTunnel, TunnelError, port_open, tunnel_settings


def run(sleep=time.sleep, max_checks=None) -> int:
    settings = tunnel_settings()
    if settings is None:
        print("NOT CONFIGURED: the tunnel needs DB_HOST=localhost and TUNNEL_SSH_HOST in .env (see .env.example)")
        return 2
    port = settings["local_port"]
    try:
        with SshTunnel(settings) as tunnel:
            reused = tunnel.process is None
            print(f"Tunnel {'already open (reused)' if reused else 'OPEN'}: localhost:{port} -> AWS database "
                  f"via {settings['ssh_host']}")
            print(f"Tableau: PostgreSQL, server localhost, port {port}, database medibank, schema {schema_name()}")
            print("Keep this window open. Press Ctrl+C to close.")
            checks = 0
            while max_checks is None or checks < max_checks:
                sleep(5)
                checks += 1
                if not port_open("127.0.0.1", port):
                    print("Tunnel dropped (network change or sleep). Run the command again.")
                    return 1
    except TunnelError as exc:
        print(f"TUNNEL FAILED: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nTunnel closed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
