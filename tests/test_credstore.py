"""CLI 凭据后端：系统存储优先、文件后端必须显式选择、两者之间没有自动回退，文件后端核对实际访问权限。"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_collab import artifacts, cli, credstore

CONTROLLER = '11111111-1111-4111-8111-111111111111'
TOKEN = 'client-token-0123456789abcdef'
PAIRED = {'client_id': 'c1', 'name': 'automation', 'kind': 'cli', 'scopes': ['read'], 'controller_id': CONTROLLER, 'client_token': TOKEN}


class Memory:
    """内存里的系统凭据存储替身；available=False 模拟没有图形会话。"""
    def __init__(self, available=True, writable=True):
        self.items, self.available, self.writable = {}, available, writable

    def probe(self):
        if not self.available:
            raise credstore.StoreError('no graphical session')

    def store(self, account, secret):
        if not self.writable:
            raise credstore.StoreError('write refused')
        self.items[account] = secret

    def load(self, account):
        if not self.available:
            raise credstore.StoreError('no graphical session')
        return self.items.get(account)

    def delete(self, account):
        self.items.pop(account, None)

    def patched(self):
        return patch.multiple(credstore, probe=self.probe, store=self.store, load=self.load, delete=self.delete)


class CredentialBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.temp), True)
        self.home = self.temp / 'home'
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_COLLAB_')}
        env['AGENT_COLLAB_HOME'] = str(self.home)
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = self.home / 'clients' / 'default.json'

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def pair(self, *extra, answers=((200, PAIRED),)):
        with patch.object(cli, 'request', side_effect=list(answers)) as sent:
            code, stdout, _ = self.run_cli('client', 'pair', '--url', 'https://paired.example', '--code', 'one-time', *extra)
        return code, stdout, sent

    def conn(self):
        return cli.Conn(argparse.Namespace(profile='default', url=None, token_file=None, ca_file=None))

    def test_system_store_is_the_default_and_the_profile_file_holds_no_credential(self):
        store = Memory()
        with store.patched():
            code, stdout, _ = self.pair()
            self.assertEqual(code, 0)
            printed = json.loads(stdout)
            self.assertEqual(printed['credential_store'], 'system')
            self.assertNotIn(TOKEN, stdout)  # 输出位置，不输出凭据
            self.assertEqual(store.items, {CONTROLLER + '/c1': TOKEN})
            self.assertNotIn(TOKEN, self.profile.read_text(encoding='utf-8'))
            self.assertEqual(self.conn().token, TOKEN)
            # 忘记：系统存储里的那一项一并删除。
            self.assertEqual(self.run_cli('client', 'forget')[0], 0)
            self.assertEqual((store.items, self.profile.exists()), ({}, False))

    def test_unavailable_system_store_never_falls_back_to_a_file(self):
        with Memory(available=False).patched():
            code, _, sent = self.pair()
        self.assertTrue(isinstance(code, str) and '--credential-store file' in code, code)  # 说明显式选择文件后端的办法
        sent.assert_not_called()  # 配对码没有用掉
        self.assertEqual(list(self.home.rglob('*.json')), [])

    def test_file_backend_is_used_only_when_chosen_and_says_where_the_credential_is(self):
        with Memory(available=False).patched():
            code, stdout, _ = self.pair('--credential-store', 'file')
            self.assertEqual(code, 0)
            printed = json.loads(stdout)
            self.assertEqual((printed['credential_store'], printed['credential_location']), ('file', str(self.profile)))
            self.assertNotIn(TOKEN, stdout)
            self.assertEqual(self.conn().token, TOKEN)
        # 无图形部署也可以用环境变量声明后端；它同样是显式选择。
        self.profile.unlink()
        with Memory(available=False).patched(), patch.dict(os.environ, {'AGENT_COLLAB_CREDENTIAL_STORE': 'file'}):
            self.assertEqual(self.pair()[0], 0)
        self.assertEqual(json.loads(self.profile.read_text(encoding='utf-8'))['credential_store'], 'file')

    def test_credential_that_cannot_be_stored_after_pairing_is_revoked(self):
        with Memory(writable=False).patched():
            code, _, sent = self.pair(answers=((200, PAIRED), (200, {})))
        self.assertTrue(isinstance(code, str) and '已在 controller 上撤销' in code, code)
        self.assertEqual(sent.call_args_list[1][0][1:4], (TOKEN, 'DELETE', '/client'))
        self.assertFalse(self.profile.exists())

    def test_system_profile_does_not_read_a_file_when_the_store_is_unreadable(self):
        store = Memory()
        with store.patched():
            self.assertEqual(self.pair()[0], 0)
        with Memory(available=False).patched(), self.assertRaises(SystemExit) as refused:
            self.conn()
        self.assertIn('系统凭据存储', str(refused.exception.code))

    def test_file_backend_refuses_a_credential_file_others_can_access(self):
        with Memory().patched():
            self.assertEqual(self.pair('--credential-store', 'file')[0], 0)
        self.assertIsNone(credstore.file_problem(self.profile))
        if os.name == 'nt':
            # Users 组被授予读取：权限位看不出来，要读回 DACL 才知道。
            subprocess.run([artifacts._system_tool('icacls.exe'), str(self.profile), '/grant', '*S-1-5-32-545:R'], capture_output=True, check=True)
        else:
            os.chmod(str(self.profile), 0o644)
        self.assertIsNotNone(credstore.file_problem(self.profile))
        with self.assertRaises(SystemExit) as refused:
            self.conn()
        self.assertIn('访问权限', str(refused.exception.code))

    def test_profiles_written_before_backends_existed_are_file_profiles(self):
        with artifacts.SecretFile(self.profile) as secret:
            secret.save(json.dumps({'url': 'https://paired.example', 'token': TOKEN, 'controller_id': CONTROLLER, 'ca_file': None}).encode())
        with Memory(available=False).patched():
            self.assertEqual(self.conn().token, TOKEN)


def system_store_usable():
    if sys.platform == 'win32':
        return True
    if sys.platform == 'darwin':
        try:  # SSH 等非图形会话里钥匙串不可用
            return subprocess.run([credstore.SECURITY, 'show-keychain-info'], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return bool(shutil.which('secret-tool')) and bool(os.environ.get('DBUS_SESSION_BUS_ADDRESS'))


@unittest.skipUnless(system_store_usable(), '当前会话没有可用的系统凭据存储')
class RealSystemStoreTests(unittest.TestCase):
    """对这台机器真实的系统凭据存储做一次写入、读回、删除；用一次性的名称，结束时不留条目。"""
    def test_round_trip_and_probe(self):
        account, secret = 'test/' + uuid.uuid4().hex, 'value_' + uuid.uuid4().hex
        self.addCleanup(credstore.delete, account)
        self.assertIsNone(credstore.load(account))
        credstore.store(account, secret)
        self.assertEqual(credstore.load(account), secret)
        credstore.store(account, secret + 'x')  # 覆盖已有条目
        self.assertEqual(credstore.load(account), secret + 'x')
        credstore.delete(account)
        self.assertIsNone(credstore.load(account))
        credstore.delete(account)  # 删除不存在的条目不报错
        credstore.probe()


if __name__ == '__main__':
    unittest.main()
