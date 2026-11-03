"""SSH tunnel to the private AWS database (copied from Modules 2 and 4, same behaviour).

    laptop:5433  --ssh-->  jumpbox (EC2, public IP)  --->  RDS:5432 (private)

The database has no public address, so nothing on the internet can reach it. The jumpbox is
the only door. ``ssh -L`` ("local port forwarding") opens port 5433 on your laptop and
carries everything sent to it, encrypted, through the jumpbox to the database.

28 Oct: Module 1's logs showed the tunnel failing intermittently right after the laptop woke
(network still reconnecting, connection reset). So opening is RETRIED (TUNNEL_ATTEMPTS, default
4, TUNNEL_RETRY_WAIT_SECONDS apart, default 20) and a final failure is explained in plain
English by explain_ssh_error(). Copied (not imported) so this repo stays independent of
the other modules: a teammate can clone Module_3 alone and it still works.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

from .env import LOG_DIR

TUNNEL_WAIT_SECONDS = 30
TUNNEL_ATTEMPTS = 4
TUNNEL_RETRY_WAIT_SECONDS = 20
CREATE_NO_WINDOW = 0x08000000      # Windows: do not flash a console window for the ssh process


class TunnelError(RuntimeError):
    """The SSH tunnel to the database could not be opened."""


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _positive_int(text, default):
    """A whole number >= 1 from .env text, or ``default`` when it is missing or not sensible."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def tunnel_settings(environ=None):
    """Tunnel settings from the environment, or None when no tunnel is configured/needed."""
    env = os.environ if environ is None else environ
    host = (env.get('TUNNEL_SSH_HOST') or '').strip()
    if not host or (env.get('DB_HOST') or '').strip() not in ('localhost', '127.0.0.1'):
        return None
    return {'ssh_host': host,
            'ssh_user': (env.get('TUNNEL_SSH_USER') or 'ec2-user').strip(),
            'ssh_key': (env.get('TUNNEL_SSH_KEY') or '').strip(),
            'remote_host': (env.get('TUNNEL_REMOTE_HOST') or '').strip(),
            'remote_port': int((env.get('TUNNEL_REMOTE_PORT') or '5432').strip()),
            'local_port': int((env.get('DB_PORT') or '5433').strip()),
            'attempts': _positive_int(env.get('TUNNEL_ATTEMPTS'), TUNNEL_ATTEMPTS),
            'retry_wait': _positive_int(env.get('TUNNEL_RETRY_WAIT_SECONDS'), TUNNEL_RETRY_WAIT_SECONDS)}


def ssh_command(settings):
    command = ['ssh']
    if settings['ssh_key']:
        command += ['-i', settings['ssh_key']]
    command += ['-o', 'ServerAliveInterval=30',
                '-o', 'ExitOnForwardFailure=yes',    # ssh exits if the local port cannot be opened
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'BatchMode=yes',                # never wait for a password prompt nobody will answer
                '-o', 'ConnectTimeout=15',
                '-N',                                  # no remote command: just hold the tunnel
                '-L', f"127.0.0.1:{settings['local_port']}:{settings['remote_host']}:{settings['remote_port']}",
                f"{settings['ssh_user']}@{settings['ssh_host']}"]
    return command


def explain_ssh_error(text):
    """Turn the raw words ssh wrote to logs/tunnel.log into a plain-English reason and a next step.

    Pure function (text in, text out) so it is easy to test. Order matters: specific causes first.
    """
    low = (text or '').lower()
    rules = [
        (('permission denied', 'publickey'),
         'the jumpbox rejected the key: TUNNEL_SSH_KEY is not the key pair this jumpbox was created with'),
        (('unprotected private key', 'bad permissions', 'identity file', 'no such file'),
         'the SSH key file cannot be used: check the TUNNEL_SSH_KEY path in .env and that only you can read the file'),
        (('could not resolve', 'name or service not known'),
         'the jumpbox address could not be looked up: no internet connection, or TUNNEL_SSH_HOST is wrong'),
        (('address already in use', 'cannot listen to port', 'bind '),
         'the local database port is already used by another program: close the other tunnel or change DB_PORT'),
        (('timed out',),
         'the jumpbox did not answer in time: it may be stopped, or your public IP is no longer allowed in its '
         'security group (add "My IP" to the jumpbox security group; if the instance was stopped and started, '
         'update TUNNEL_SSH_HOST)'),
        (('connection refused', 'banner exchange'),
         'the jumpbox refused the connection: it may still be starting up, or this network blocks SSH (port 22)'),
        (('connection reset', 'connection closed', 'broken pipe', 'send disconnect'),
         'the connection was cut off (a network blip or a Wi-Fi change); trying again usually fixes this'),
        (('unknown error', 'network is unreachable', 'no route to host', 'network error'),
         'no usable network right now (Wi-Fi off or still reconnecting after sleep)'),
    ]
    for needles, reason in rules:
        if any(n in low for n in needles):
            return reason
    last = next((line.strip() for line in reversed((text or '').splitlines()) if line.strip()), '')
    return f'unknown reason ({last})' if last else 'no further detail from ssh'


class TunnelAttemptFailed(Exception):
    """One try at opening the tunnel failed (internal: SshTunnel retries and then raises TunnelError)."""

    def __init__(self, summary, detail=''):
        super().__init__(summary)
        self.summary, self.detail = summary, detail


class SshTunnel:
    """Context manager: open the tunnel, wait until the local port answers, close it afterwards.

    If the local port is already open (for example you started a tunnel by hand to use Tableau)
    it is reused and left running when we finish.

    Opening is retried ``attempts`` times, ``retry_wait`` seconds apart. Most failures on a laptop are
    short-lived (the network is still reconnecting after sleep, or a connection was reset), so a
    short pause and another try fixes them. If every try fails the error names the likely cause.
    """

    def __init__(self, settings, popen=subprocess.Popen, is_open=port_open, wait_seconds=TUNNEL_WAIT_SECONDS,
                 log_dir=LOG_DIR, sleep=time.sleep, attempts=None, retry_wait=None):
        self.settings, self.popen, self.is_open = settings, popen, is_open
        self.wait_seconds, self.log_dir, self.sleep = wait_seconds, Path(log_dir), sleep
        # Explicit arguments win (tests); otherwise the .env settings; otherwise the defaults above.
        self.attempts = max(1, int(attempts if attempts is not None else settings.get('attempts', TUNNEL_ATTEMPTS)))
        self.retry_wait = retry_wait if retry_wait is not None else settings.get('retry_wait', TUNNEL_RETRY_WAIT_SECONDS)
        self.process = None

    def __enter__(self):
        port = self.settings['local_port']
        if self.is_open('127.0.0.1', port):
            return self                                   # someone else's tunnel: reuse it
        if not self.settings['remote_host']:
            raise TunnelError('TUNNEL_REMOTE_HOST is not set in .env')
        self.log_dir.mkdir(parents=True, exist_ok=True)
        failure = None
        for attempt in range(1, self.attempts + 1):
            try:
                self._open_once(port)
                return self
            except TunnelAttemptFailed as exc:
                failure = exc
                if attempt < self.attempts:
                    self.sleep(self.retry_wait)           # give a waking network a moment, then try again
        reason = explain_ssh_error(failure.detail)
        raise TunnelError(f'{failure.summary} after {self.attempts} tries: {reason} (see logs/tunnel.log)')

    def _open_once(self, port):
        """One try. Raises TunnelAttemptFailed (retry) or TunnelError (not worth retrying)."""
        log_path = self.log_dir / 'tunnel.log'
        try:
            offset = log_path.stat().st_size
        except OSError:
            offset = 0
        err = log_path.open('a', encoding='utf-8')
        flags = CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            self.process = self.popen(ssh_command(self.settings), stdout=subprocess.DEVNULL, stderr=err,
                                      stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as exc:
            raise TunnelError(f'could not start ssh: {exc}') from exc     # ssh is not installed: retrying cannot help
        finally:
            err.close()
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.process = None
                raise TunnelAttemptFailed(f'ssh exited with code {code}', self._new_log_text(log_path, offset))
            if self.is_open('127.0.0.1', port):
                return
            self.sleep(1)
        self.close()
        raise TunnelAttemptFailed(f'tunnel did not open within {self.wait_seconds} seconds',
                                  self._new_log_text(log_path, offset))

    @staticmethod
    def _new_log_text(log_path, offset):
        """What ssh wrote to tunnel.log during the try that just failed (not older tries)."""
        try:
            with Path(log_path).open('rb') as stream:
                stream.seek(offset)
                return stream.read().decode('utf-8', errors='replace')
        except OSError:
            return ''

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def __exit__(self, *exc):
        self.close()
        return False
