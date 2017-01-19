# ~*~ coding: utf-8 ~*~

import re
import logging
import time
import socket
import select

import paramiko

from . import wr, warning
from .globals import request, g
from .utils import TtyIOParser


logger = logging.getLogger(__file__)


class ProxyServer(object):
    """
    We are using this class proxy client channel (user) with backend channel

    When receive client input command, send to backend ssh channel
    and when receive output of command from backend, send to client

    We also record the command and result to database for audit

    """
    ENTER_CHAR = ['\r', '\n', '\r\n']
    OUTPUT_END_PATTERN = re.compile(r'\x1b]0;.+@.+:.+\x07.*')
    VIM_PATTERN = re.compile(r'\x1b\[\?1049', re.X)
    IGNORE_OUTPUT_COMMAND = [re.compile(r'^cat\s+'),
                             re.compile(r'^tailf?\s+')]

    def __init__(self, app, asset, system_user):
        self.app = app
        self.asset = asset
        self.system_user = system_user
        self.service = app.service
        self.backend_channel = None
        self.ssh = None
        # If is first input, will clear the output data: ssh banner and PS1
        self.is_first_input = True
        self.in_input_state = False
        # This ssh session command serial no
        self.in_vim_state = False
        self.command_no = 1
        self.input = ''
        self.output = ''
        self.output_data = []
        self.input_data = []
        self.history = {}

    def is_finish_input(self, s):
        for char in s:
            if char in self.ENTER_CHAR:
                return True
        return False

    def get_output(self):
        parser = TtyIOParser(width=request.win_width,
                             height=request.win_height)
        self.output = parser.parse_output(b''.join(self.output_data))
        print('>' * 10 + 'Output' + '<' * 10)
        print(self.output)
        print('>' * 10 + 'End output' + '<' * 10)
        if self.input:
            data = {
                'proxy_log': g.proxy_log_id,
                'command_no': self.command_no,
                'command': self.input,
                'output': self.output,
                'datetime': time.time(),
            }
            self.service.send_command_log(data)
            self.command_no += 1

    def get_input(self):
        parser = TtyIOParser(width=request.win_width,
                             height=request.win_height)
        self.input = parser.parse_input(b''.join(self.input_data))
        print('#' * 10 + 'Command' + '#' * 10)
        print(self.input)
        print('#' * 10 + 'End command' + '#' * 10)

    # Todo: App check user permission
    def validate_user_asset_permission(self, user_id, asset_id, system_user_id):
        return self.service.validate_user_asset_permission(
            user_id, asset_id, system_user_id)

    def get_asset_auth(self, system_user):
        return self.service.get_system_user_auth_info(system_user)

    def connect(self, term=b'xterm', width=80, height=24, timeout=10):
        asset = self.asset
        system_user = self.system_user
        if not self.validate_user_asset_permission(
                request.user.id, asset.id, system_user.id):
            logger.warning('User %s have no permission connect %s with %s' %
                           (request.user.username,
                            asset.ip, system_user.username))
            return None
        self.ssh = ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        password, private_key = self.get_asset_auth(self.system_user)

        data = {"username": request.user.username, "name": request.user.name,
                "hostname": self.asset.hostname, "ip": self.asset.ip,
                "system_user": self.system_user.username,  "login_type": "ST",
                "date_start": time.time(), "was_failed": 0}
        g.proxy_log_id = proxy_log_id = self.service.send_proxy_log(data)
        try:
            g.client_channel.send(
                wr('Connecting %s@%s:%s ... ' %
                   (system_user.username, asset.ip, asset.port)))
            ssh.connect(hostname=asset.ip, port=asset.port,
                        username=system_user.username,
                        password=password, pkey=private_key,
                        look_for_keys=False, allow_agent=True,
                        compress=True, timeout=timeout)

        except (paramiko.AuthenticationException,
                paramiko.ssh_exception.SSHException):
            msg = 'Connect backend server %s failed: %s' \
                  % (asset.ip, 'Auth failed')
            logger.warning(msg)
            failed = True

        except socket.error:
            msg = 'Connect asset %s failed: %s' % (asset.ip, 'Timeout')
            logger.warning(msg)
            failed = True
        else:
            msg = 'Connect asset %(username)s@%(host)s:%(port)s successfully' % {
                       'username': system_user.username,
                       'host': asset.ip,
                       'port': asset.port}
            failed = False
            logger.info(msg)

        if failed:
            g.client_channel.send(wr(warning(msg+'\r\n')))
            data = {
                "proxy_log_id": proxy_log_id,
                "date_finished": time.time(),
                "was_failed": 1
            }
            self.service.finish_proxy_log(data)
            return None

        self.backend_channel = channel = ssh.invoke_shell(
            term=term, width=width, height=height)
        channel.settimeout(100)
        return channel

    def is_match_ignore_command(self, data):
        for pattern in self.IGNORE_OUTPUT_COMMAND:
            if pattern.match(data):
                return True
        return False

    def proxy(self):
        self.backend_channel = backend_channel = self.connect()

        if backend_channel is None:
            return

        while True:
            r, w, x = select.select([g.client_channel, backend_channel], [], [])

            if request.change_win_size_event.is_set():
                request.change_win_size_event.clear()
                backend_channel.resize_pty(width=request.win_width,
                                           height=request.win_height)

            if g.client_channel in r:
                # Get output of the command
                self.is_first_input = False
                if self.in_input_state is False:
                    self.get_output()
                    del self.output_data[:]

                self.in_input_state = True
                client_data = g.client_channel.recv(1024)

                if self.is_finish_input(client_data):
                    self.in_input_state = False
                    self.get_input()
                    del self.input_data[:]

                if len(client_data) == 0:
                    logger.info('Logout from ssh server %(host)s: %(username)s' % {
                        'host': request.environ['REMOTE_ADDR'],
                        'username': request.user.username,
                    })
                    break
                backend_channel.send(client_data)

            if backend_channel in r:
                backend_data = backend_channel.recv(1024)
                # print(repr(backend_data))
                if self.in_input_state:
                    self.input_data.append(backend_data)
                elif self.is_match_ignore_command(self.input):
                    pass
                else:
                    self.output_data.append(backend_data)

                if len(backend_data) == 0:
                    g.client_channel.send(
                        wr('Disconnect from %s' % request.asset.ip))
                    logger.info('Logout from asset %(host)s: %(username)s' % {
                        'host': request.asset.ip,
                        'username': request.user.username,
                    })
                    break

                g.client_channel.send(backend_data)

        data = {
            "proxy_log_id": g.proxy_log_id,
            "date_finished": time.time(),
        }
        self.service.finish_proxy_log(data)
