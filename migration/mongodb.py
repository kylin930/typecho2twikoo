"""Credential-safe configuration, bounded PyMongo connections and error categories."""
from __future__ import annotations

import hashlib
import json
import os
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit, urlencode

from dotenv import dotenv_values
from pymongo import MongoClient, errors
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.uri_parser import split_hosts
from pymongo.write_concern import WriteConcern

from migrator.common import MigrationError


class DatabaseError(MigrationError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f'{category}: {message}')


HINTS = {
    'configuration': '连接配置无效；检查本地环境变量/.env，禁止关闭证书或主机名校验。',
    'credentials_missing': '未配置 MONGODB_URI；请在本地 .env 或安全环境变量中设置，勿在聊天提供密码。',
    'authentication': '认证失败；核对数据库用户、密码编码、authSource 和认证机制。',
    'authorization': '权限不足；核对该用户对目标库的最小所需权限。',
    'tls_certificate': '证书校验失败；核对官方主机名、系统时间和可信 CA，禁止跳过校验。',
    'tls_handshake': 'TLS 握手失败；核对 TLS、网络代理和服务端配置。',
    'dns': 'DNS 查询失败；检测 SRV/TXT/A/AAAA，优先使用 Atlas 官方提供的标准 URI。',
    'timeout': '连接/选择服务器超时；检查出站网络、DNS 和 Atlas IP Access List；此错误不能单独证明白名单错误。',
    'network': '网络不可达/连接中断；检查出站 TCP、VPN/防火墙和 Atlas 网络访问规则。',
    'duplicate': '存在唯一键冲突；不覆盖已有评论，请查看预检和迁移日志后再恢复。',
    'write_uncertain': '写入结果可能部分成功；停止重试，用相同批次 --resume 核对数据库。',
    'database': '数据库操作失败；错误原文已隐藏，避免泄露凭据或评论。',
}


def classify(exc: BaseException) -> DatabaseError:
    if isinstance(exc, DatabaseError):
        return exc
    # Inspect messages for classification only: NEVER return or log the raw message.
    text = str(exc).lower()
    if isinstance(exc, ssl.SSLCertVerificationError) or 'certificate_verify_failed' in text or 'certificate verify failed' in text:
        category = 'tls_certificate'
    elif isinstance(exc, ssl.SSLError) or 'ssl handshake' in text or 'tls handshake' in text:
        category = 'tls_handshake'
    elif isinstance(exc, errors.OperationFailure) and exc.code == 18:
        category = 'authentication'
    elif isinstance(exc, errors.OperationFailure) and exc.code == 13:
        category = 'authorization'
    elif isinstance(exc, errors.DuplicateKeyError):
        category = 'duplicate'
    elif isinstance(exc, (errors.BulkWriteError, errors.WriteConcernError)):
        category = 'write_uncertain'
    elif 'dns' in text or 'nxdomain' in text or 'getaddrinfo' in text:
        category = 'dns'
    elif isinstance(exc, errors.ConfigurationError):
        category = 'configuration'
    elif isinstance(exc, (errors.ServerSelectionTimeoutError, errors.NetworkTimeout, TimeoutError)):
        category = 'timeout'
    elif isinstance(exc, (errors.ConnectionFailure, OSError)):
        category = 'network'
    else:
        category = 'database'
    return DatabaseError(category, HINTS[category])


def db_name(value: str) -> str:
    if not value or any(c.isspace() for c in value) or len(value.encode('utf-8')) > 63 or any(c in value for c in '/\\. "$\0'):
        raise DatabaseError('configuration', '数据库名不合法，请在本地核对实际部署。')
    return value


def number(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(key, str(default)))
        if not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise DatabaseError('configuration', f'{key} 数值超出允许范围。') from None


@dataclass(frozen=True)
class Settings:
    uri: str = field(repr=False)
    database: str
    collection: str = 'comment'
    database_source: str = 'explicit'
    selection_ms: int = 5000
    connect_ms: int = 5000
    socket_ms: int = 10000
    pool_size: int = 5
    ca_file: str | None = field(default=None, repr=False)

    @property
    def seeds(self) -> list[tuple[str, int]]:
        try:
            uri = urlsplit(self.uri)
            return split_hosts(uri.netloc.rsplit('@', 1)[-1], default_port=27017)
        except (ValueError, errors.ConfigurationError):
            raise DatabaseError('configuration', HINTS['configuration']) from None

    @property
    def srv(self) -> bool:
        return self.uri.startswith('mongodb+srv://')

    @property
    def target_id(self) -> str:
        # No password, username, or URI hash in any persisted record.
        uri = urlsplit(self.uri)
        options = {k.lower(): v for k, v in parse_qsl(uri.query)
                   if k.lower() in {'replicaset', 'loadbalanced'}}
        identity = [uri.scheme, sorted(self.seeds), options, self.database, self.collection]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def public(self) -> dict[str, Any]:
        return {'database': self.database, 'collection': self.collection,
                'database_source': self.database_source, 'scheme': 'SRV' if self.srv else 'standard',
                'target_id': self.target_id, 'tls_verification': True}

    @classmethod
    def from_env(cls, *, database: str | None = None, env_file: Path | None = Path('.env'),
                 environ: Mapping[str, str] | None = None) -> Settings:
        env: dict[str, str] = {}
        if env_file and env_file.is_file():
            # No shell evaluation or interpolation, and process bindings take precedence.
            env.update({k: v for k, v in dotenv_values(env_file, interpolate=False).items() if v is not None})
        env.update(os.environ if environ is None else environ)
        uri = env.get('MONGODB_URI', '').strip()
        if not uri:
            raise DatabaseError('credentials_missing', HINTS['credentials_missing'])
        try:
            parts = urlsplit(uri)
            if parts.scheme not in {'mongodb', 'mongodb+srv'} or not parts.netloc or parts.fragment:
                raise ValueError
            options = list(parse_qsl(parts.query, keep_blank_values=True))
            for key, value in options:
                k, v = key.lower(), value.lower()
                if (k in {'tlsinsecure', 'tlsallowinvalidcertificates', 'tlsallowinvalidhostnames', 'ssl_cert_reqs'} and v not in {'false', '0'}):
                    raise ValueError
                if k in {'tls', 'ssl'} and v != 'true':
                    raise ValueError
            if any(c.isspace() for c in uri):
                raise ValueError
        except ValueError:
            raise DatabaseError('configuration', HINTS['configuration']) from None
        # Remove harmless legacy aliases that PyMongo rejects when combined with explicit secure flags.
        filtered = [(k,v) for k,v in options if k.lower() not in {'tlsinsecure', 'ssl_cert_reqs'}]
        uri = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), ''))
        path_db = unquote(parts.path.lstrip('/'))
        chosen = database or env.get('MONGODB_DATABASE') or path_db or 'test'
        source = 'cli' if database else 'environment' if env.get('MONGODB_DATABASE') else 'uri' if path_db else 'driver_default_test_unconfirmed'
        collection = env.get('MONGODB_COLLECTION', 'comment')
        if not collection or '$' in collection or '\0' in collection or collection.startswith('system.'):
            raise DatabaseError('configuration', '集合名称无效。')
        result = cls(uri, db_name(chosen), collection, source,
                     number(env, 'MONGODB_SELECTION_TIMEOUT_MS', 5000, 100, 60000),
                     number(env, 'MONGODB_CONNECT_TIMEOUT_MS', 5000, 100, 60000),
                     number(env, 'MONGODB_SOCKET_TIMEOUT_MS', 10000, 100, 120000),
                     number(env, 'MONGODB_MAX_POOL_SIZE', 5, 1, 100), env.get('MONGODB_CA_FILE') or None)
        seeds = result.seeds
        if result.srv and (len(seeds) != 1 or ':' in parts.netloc.rsplit('@', 1)[-1]):
            raise DatabaseError('configuration', 'SRV URI 只允许一个不带端口的官方 seed 域名。')
        return result


@contextmanager
def connect(settings: Settings) -> Iterator[Any]:
    client = None
    try:
        kwargs: dict[str, Any] = dict(connect=False, tls=True,
            tlsAllowInvalidCertificates=False, tlsAllowInvalidHostnames=False,
            serverSelectionTimeoutMS=settings.selection_ms, connectTimeoutMS=settings.connect_ms,
            socketTimeoutMS=settings.socket_ms, waitQueueTimeoutMS=settings.selection_ms,
            maxPoolSize=settings.pool_size, minPoolSize=0, maxIdleTimeMS=60000,
            retryReads=True, retryWrites=False, appname='typecho2twikoo', uuidRepresentation='standard')
        if settings.ca_file:
            kwargs['tlsCAFile'] = settings.ca_file
        client = MongoClient(settings.uri, **kwargs)
        # Majority primary reads avoid deciding that an acknowledged import is missing on a stale secondary.
        db = client.get_database(settings.database, read_preference=ReadPreference.PRIMARY,
                                 read_concern=ReadConcern('majority'),
                                 write_concern=WriteConcern(w='majority', wtimeout=settings.socket_ms))
        db.command('ping')
        yield db
    except MigrationError:
        raise
    except (errors.PyMongoError, OSError, ValueError) as exc:
        raise classify(exc) from None
    finally:
        if client is not None:
            client.close()
