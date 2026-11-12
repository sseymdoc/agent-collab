"""Small verified-TLS protocol client; transport injection keeps tests off sockets."""
import http.client
import ipaddress
import json
import os
from pathlib import Path
import ssl
import threading
from urllib.parse import urlsplit

from . import protocol as p


class APIError(Exception):
    def __init__(self, status, body):
        self.status = status
        # 错误体形状不合规时退回通用错误，不能因 AttributeError/TypeError 让 agent 在一次 4xx 上崩溃。
        error = body.get('error') if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        self.code = error.get('code') if isinstance(error.get('code'), str) else 'invalid'
        self.details = error.get('details') if isinstance(error.get('details'), dict) else {}
        self.body = p.error_body(self.code, 'Controller request failed')
        self.body['error']['details'] = self.details  # 不用 **：details 可能含 code/message 键
        super().__init__(self.code)


class ConnectionFailure(Exception):
    pass


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in ('https', 'http') or not parts.hostname or
            parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('controller must be an HTTP(S) origin without credentials')
    if parts.path.rstrip('/') not in ('', p.API_PREFIX):
        raise ValueError('controller path must be empty or /api/v1')
    loopback = parts.hostname.lower() == 'localhost'
    try:
        loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme == 'http' and not loopback:
        raise ValueError('HTTP is permitted only for a loopback host')
    parts.port  # Validate malformed ports before sending credentials.
    return url.rstrip('/').removesuffix(p.API_PREFIX)


class Client:
    def __init__(self, controller, token=None, ca_file=None, transport=None, timeout=15):
        self.controller = validate_url(controller)
        self.token = token
        self.context = ssl.create_default_context(cafile=ca_file)
        self.timeout = timeout
        self.transport = transport or self._transport
        self.wait_hook = None
        self.error_filter = lambda value: value

    def _send(self, method, url, headers, body):
        if self.wait_hook is None:
            return self.transport(method, url, headers, body)
        # Network waits must not prevent lease-expiry pauses or local progress.
        # The hook runs only on the agent thread; transport never mutates agent state.
        result, errors = [], []
        def send():
            try:
                result.append(self.transport(method, url, headers, body))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(.05)
            if thread.is_alive():
                self.wait_hook()
        if errors:
            raise errors[0]
        return result[0]

    def _transport(self, method, url, headers, body):
        parts = urlsplit(url)
        if parts.scheme == 'https':
            connection = http.client.HTTPSConnection(parts.hostname, parts.port,
                                                     context=self.context, timeout=self.timeout)
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self.timeout)
        try:
            # 保留查询串（如 /events?after=<seq>），否则游标丢失、从缺省位置重放。
            connection.request(method, parts.path + ('?' + parts.query if parts.query else ''), body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def request(self, method, path, value=None, raw=None, allow=()):
        headers = {'content-type': 'application/octet-stream' if raw is not None else 'application/json'}
        if self.token:
            headers['authorization'] = 'Bearer ' + self.token
        body = raw if raw is not None else p.canonical_json(value) if value is not None else b''
        try:
            fault = os.environ.get('AGENT_COLLAB_FAULT_PARTITION')
            if fault and Path(fault).exists():
                raise OSError('injected partition')
            status, response_headers, data = self._send(
                method, self.controller + p.API_PREFIX + path, headers, body)
        except ssl.SSLCertVerificationError as exc:
            # 校验失败的原因（自签、缺少扩展、地址不匹配）只描述服务器出示的证书，不含凭据；
            # 原样给出，否则入口证书的问题和“连不上”无法区分。
            raise ConnectionFailure('TLS certificate verification failed: {}'.format(
                exc.verify_message or 'certificate rejected')) from None
        except (OSError, http.client.HTTPException, ConnectionFailure) as exc:
            # Transport exceptions may contain headers, URLs, or credentials.
            raise ConnectionFailure('controller unavailable or TLS verification failed') from None
        if status >= 500:
            raise ConnectionFailure('controller temporarily unavailable')
        if not 200 <= status < 300 and status not in allow:
            try:
                error = json.loads(data)
            except (ValueError, TypeError):
                error = p.error_body('invalid', 'Invalid controller response')
            # A controller/proxy can echo the request credential in error details.
            # Scrub at the HTTP boundary, before constructing an exposed exception.
            secrets = [self.token, value.get('lease_token') if isinstance(value, dict) else None]
            def clean(item):
                if isinstance(item, str):
                    for secret in secrets:
                        if secret:
                            item = item.replace(secret, '[REDACTED]')
                    return item
                if isinstance(item, dict):
                    return {clean(key): clean(entry) for key, entry in item.items()}
                if isinstance(item, list):
                    return [clean(entry) for entry in item]
                return item
            raise APIError(status, self.error_filter(clean(error)))
        return status, data

    def json(self, method, path, value=None):
        status, data = self.request(method, path, value)
        if status == 204:
            return None
        try:
            return json.loads(data)
        except ValueError:
            # 代理/网关返回的 HTML 等：按控制端不可用处理（断开、退避、重新对账），不能让 attempt 记成代码失败。
            raise ConnectionFailure('controller returned a non-JSON response') from None

    def fetch_blob(self, sha):
        p.check_sha256(sha)
        data = self.request('GET', '/blobs/' + sha)[1]
        import hmac
        if not hmac.compare_digest(p.sha256_bytes(data), sha):
            raise ValueError('downloaded blob hash mismatch')
        return data

    def put_blob(self, sha, data):
        p.check_sha256(sha)
        import hmac
        if not hmac.compare_digest(p.sha256_bytes(data), sha):
            raise ValueError('upload blob hash mismatch')
        if self.request('HEAD', '/blobs/' + sha, allow=(404,))[0] == 404:
            self.request('PUT', '/blobs/' + sha, raw=data)
