# pylint: disable=no-member
"""The ShellDriver provides the CommandProtocol, ConsoleProtocol and
 InfoProtocol on top of a SerialPort."""
import logging
import re
import shlex
from time import sleep

import attr
from pexpect import TIMEOUT

from ..factory import target_factory
from ..protocol import CommandProtocol, ConsoleProtocol, InfoProtocol
from ..step import step
from ..util import gen_marker, Timeout
from .common import Driver
from .commandmixin import CommandMixin
from .exception import ExecutionError


@target_factory.reg_driver
@attr.s
class ShellDriver(CommandMixin, Driver, CommandProtocol):
    """ShellDriver - Driver to execute commands on the shell"""
    bindings = {"console": ConsoleProtocol, }
    prompt = attr.ib(validator=attr.validators.instance_of(str))
    login_prompt = attr.ib(validator=attr.validators.instance_of(str))
    username = attr.ib(validator=attr.validators.instance_of(str))
    password = attr.ib(default="", validator=attr.validators.instance_of(str))
    keyfile = attr.ib(default="", validator=attr.validators.instance_of(str))

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.re_vt100 = re.compile(
            r'(\x1b\[|\x9b)[^@-_a-z]*[@-_a-z]|\x1b[@-_a-z]'
        )  #pylint: disable=attribute-defined-outside-init,anomalous-backslash-in-string
        self.logger = logging.getLogger("{}:{}".format(self, self.target))
        self._status = 0  #pylint: disable=attribute-defined-outside-init

    def on_activate(self):
        if self._status == 0:
            self.await_login()
            self._inject_run()
        if self.keyfile:
            self.put_ssh_key(self.keyfile)
        self.run("dmesg -n 1")  # Turn off Kernel Messages to the console

    def on_deactivate(self):
        self._status = 0

    @step(args=['cmd'], result=True)
    def run(self, cmd, *, step):
        """
        Runs the specified cmd on the shell and returns the output.

        Arguments:
        cmd - cmd to run on the shell
        """
        # FIXME: Handle pexpect Timeout
        self._check_prompt()
        marker = gen_marker()
        # hide marker from expect
        cmp_command = '''MARKER='{}''{}' run {}'''.format(
            marker[:4], marker[4:], shlex.quote(cmd)
        )
        self.console.sendline(cmp_command)
        _, _, match, _ = self.console.expect(r'{}(.*){}\s+(\d+)\s+{}'.format(
            marker, marker, self.prompt
        ))
        # Remove VT100 Codes, split by newline and remove surrounding newline
        data = self.re_vt100.sub('', match.group(1).decode('utf-8')).split('\r\n')[1:-1]
        self.logger.debug("Received Data: %s", data)
        # Get exit code
        exitcode = int(match.group(2))
        return (data, [], exitcode)

    @step()
    def await_login(self):
        """Awaits the login prompt and logs the user in"""
        self.console.sendline("")
        index, _, _, _ = self.console.expect([self.prompt, self.login_prompt])
        if index == 0:
            self.status = 1
            return  # already logged in
        self.console.sendline(self.username)
        if self.password:
            self.console.expect("Password: ")
            self.console.sendline(self.password)
        self.console.expect(self.prompt, timeout=5)
        self._check_prompt()

    @step(args=['cmd'], result=True)
    def run_check(self, cmd):
        """
        Runs the specified cmd on the shell and returns the output if successful,
        raises ExecutionError otherwise.

        Arguments:
        cmd - cmd to run on the shell
        """
        out, _, res = self.run(cmd)
        if res != 0:
            raise ExecutionError(cmd)
        return out

    @step()
    def get_status(self):
        """Returns the status of the shell-driver.
        0 means not connected/found, 1 means shell
        """
        return self._status

    def _check_prompt(self):
        """
        Internal function to check if we have a valid prompt
        """
        marker = gen_marker()
        # hide marker from expect
        self.console.sendline("echo '{}''{}'".format(marker[:4], marker[4:]))
        try:
            self.console.expect("{}".format(marker), timeout=2)
            self.console.expect(self.prompt, timeout=1)
            self._status = 1
        except TIMEOUT:
            self._status = 0
            raise

    def _inject_run(self):
        self.console.sendline(
            '''run() { echo "$MARKER"; sh -c "$@"; echo "$MARKER $?"; }'''
        )
        self.console.expect(self.prompt)

    @step(args=['key'])
    def put_ssh_key(self, key):
        """Upload an SSH Key to a target"""
        regex = re.compile(
            r"""ssh-rsa # Only match RSA Keys
            \s+(?P<key>[a-zA-Z0-9/+=]+) # Match Keystring
            \s+(?P<comment>.*) # Match comment""", re.X
        )
        if self._status == 1:
            with open(key) as keyfile:
                keyline = keyfile.readline()
                self.logger.debug("Read Keyline: %s", keyline)
                match = regex.match(keyline)
                if match:
                    new_key = match.groupdict()
                else:
                    raise IOError(
                        "Could not parse SSH-Key from file: {}".
                        format(keyfile)
                    )
            self.logger.debug("Read Key: %s", new_key)
            auth_keys, _, exitcode = self.run("cat ~/.ssh/authorized_keys")
            self.logger.debug("Exitcode: %s", exitcode)
            if exitcode != 0:
                self.run("mkdir ~/.ssh")
                self.run("touch ~/.ssh/authorized_keys")
            result = []
            for line in auth_keys:
                match = regex.match(line)
                if match:
                    match = match.groupdict()
                    self.logger.debug("Match dict: %s", match)
                    result.append(match)
            self.logger.debug("Complete result: %s", result)
            for key in result:
                self.logger.debug(
                    "Key, newkey: %s,%s", key['key'], new_key['key']
                )
                if key['key'] == new_key['key']:
                    self.logger.info("Key already on target")
                    return
            self.logger.info("Key not on target, mounting...")
            self.run_check('echo "{}" > /tmp/keys'.format(keyline))
            self.run_check('chmod 600 /tmp/keys')
            self.run_check('mount --bind /tmp/keys ~/.ssh/authorized_keys')
            self.run_check('chmod 700 ~/.ssh')
            self.run_check('chmod 644 ~/.ssh/authorized_keys')
