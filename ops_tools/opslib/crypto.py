#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.crypto —— 对称加密（ChaCha20 + HMAC-SHA256，Encrypt-then-MAC）。

为什么不用 AES：标准库不含 AES，引入 pycryptodome 就违背了「尽量少装环境」；
ChaCha20 用纯 Python 实现仅需几十行，安全性足够，且不需要查表（无缓存时序风险）。

密文格式（Base64）：
  magic(4B "OPS1") | salt(16B) | nonce(12B) | mac(32B) | ciphertext
其中 key = PBKDF2-HMAC-SHA256(password, salt, 100000, 64)，
前 32 字节做加密密钥，后 32 字节做 MAC 密钥。

用于凭据保险箱：服务器口令/私钥在落盘前加密，前端永不回显明文。
"""
from __future__ import print_function

import base64
import hashlib
import hmac as _hmac
import os
import struct

from . import compat
from .auth import _pbkdf2

MAGIC = b'OPS1'
NONCE_LEN = 12
SALT_LEN = 16
TAG_LEN = 32
ITERATIONS = 100000
CONST = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]


# ---------------------------------------------------------------- ChaCha20
def _rotl32(value, count):
    return ((value << count) & 0xffffffff) | (value >> (32 - count))


def _quarter_round(state, a, b, c, d):
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] = _rotl32(state[b] ^ state[c], 7)


def _chacha_block(key, counter, nonce):
    """生成 64 字节密钥流块"""
    state = CONST + list(struct.unpack('<8I', key)) + \
        [counter & 0xffffffff, (counter >> 32) & 0xffffffff] + list(struct.unpack('<3I', nonce))
    working = list(state)
    for _ in range(10):                                  # 20 轮 = 10 次双轮
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    out = [(working[i] + state[i]) & 0xffffffff for i in range(16)]
    return struct.pack('<16I', *out)


def _keystream(key, nonce, length, counter=1):
    out = []
    produced = 0
    while produced < length:
        block = _chacha_block(key, counter, nonce)
        out.append(block)
        produced += len(block)
        counter += 1
    return b''.join(out)[:length]


def _xor(data, stream):
    if compat.PY2:
        return ''.join(chr(ord(data[i]) ^ ord(stream[i])) for i in range(len(data)))
    return bytes(bytearray(a ^ b for a, b in zip(bytearray(data), bytearray(stream))))


# ---------------------------------------------------------------- 密钥派生
def derive_keys(password, salt, iterations=ITERATIONS):
    """返回 (加密密钥 32B, MAC 密钥 32B)"""
    material = _pbkdf2(password, salt, iterations, 64)
    if not isinstance(material, bytes):
        material = compat.to_bytes(material)
    return material[:32], material[32:64]


def random_bytes(n=16):
    try:
        return os.urandom(n)
    except Exception:
        return hashlib.sha256(compat.to_bytes('%s%s' % (os.getpid(), compat.time.time()))).digest()[:n]


# ---------------------------------------------------------------- 对外接口
def encrypt(plaintext, password, iterations=ITERATIONS, aad=''):
    """加密字符串 -> Base64 文本"""
    if plaintext is None:
        plaintext = ''
    raw = compat.to_bytes(plaintext)
    salt = random_bytes(SALT_LEN)
    nonce = random_bytes(NONCE_LEN)
    enc_key, mac_key = derive_keys(password, salt, iterations)
    stream = _keystream(enc_key, nonce, len(raw))
    cipher = _xor(raw, stream)
    body = MAGIC + salt + nonce + cipher
    mac = _hmac.new(mac_key, compat.to_bytes(aad) + body, hashlib.sha256).digest()
    blob = MAGIC + salt + nonce + mac + cipher
    encoded = base64.b64encode(blob)
    return compat.to_text(encoded)


def decrypt(token, password, aad=''):
    """解密 Base64 文本 -> 明文字符串；失败抛 ValueError"""
    raw = compat.to_text(token or '').strip()
    if not raw:
        return ''
    if raw.startswith('plain:'):
        return raw[len('plain:'):]
    try:
        blob = base64.b64decode(compat.to_bytes(raw))
    except Exception:
        raise ValueError('密文格式不合法')
    if len(blob) < 4 + SALT_LEN + NONCE_LEN + TAG_LEN:
        raise ValueError('密文长度不足')
    if blob[:4] != MAGIC:
        raise ValueError('密文标记不匹配（可能不是本工具生成的）')
    salt = blob[4:4 + SALT_LEN]
    nonce = blob[4 + SALT_LEN:4 + SALT_LEN + NONCE_LEN]
    mac = blob[4 + SALT_LEN + NONCE_LEN:4 + SALT_LEN + NONCE_LEN + TAG_LEN]
    cipher = blob[4 + SALT_LEN + NONCE_LEN + TAG_LEN:]
    enc_key, mac_key = derive_keys(password, salt, ITERATIONS)
    head = MAGIC + salt + nonce + cipher
    expect = _hmac.new(mac_key, compat.to_bytes(aad) + head, hashlib.sha256).digest()
    if not compat.constant_time_eq(expect, mac):
        raise ValueError('校验失败：口令错误或数据被篡改')
    stream = _keystream(enc_key, nonce, len(cipher))
    return compat.to_text(_xor(cipher, stream))


def is_encrypted(token):
    text = compat.to_text(token or '')
    return bool(text) and not text.startswith('plain:') and len(text) > 40


def mask(secret, head=2, tail=2):
    """用于前端展示的脱敏字符串"""
    text = compat.safe_str(secret)
    if len(text) <= head + tail:
        return '*' * len(text)
    return text[:head] + '*' * max(3, len(text) - head - tail) + text[-tail:]


class Vault(object):
    """凭据保险箱：把敏感字段加密后落盘，读取时才解密。

    主口令来源优先级：构造参数 > 环境变量 OPS_VAULT_KEY > 自动生成并存放在
    <data_dir>/.vault_key（0600）。生产环境建议显式传入或使用环境变量。
    """

    def __init__(self, data_dir, master_password=None):
        self.data_dir = os.path.abspath(data_dir or '.')
        self.key_file = os.path.join(self.data_dir, '.vault_key')
        self.master = master_password or os.environ.get('OPS_VAULT_KEY') or self._load_or_create_key()

    def _load_or_create_key(self):
        try:
            if os.path.exists(self.key_file):
                with compat.open_text(self.key_file, 'r') as f:
                    key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass
        key = compat.rand_hex(32)
        try:
            compat.makedirs(self.data_dir)
            with compat.open_text(self.key_file, 'w') as f:
                f.write(key)
            os.chmod(self.key_file, 0o600)
        except Exception:
            pass
        return key

    def encrypt(self, plaintext, aad=''):
        return encrypt(plaintext, self.master, aad=aad)

    def decrypt(self, token, aad=''):
        try:
            return decrypt(token, self.master, aad=aad)
        except ValueError:
            return ''

    def seal(self, data, fields, aad=''):
        """把 dict 中指定字段加密，返回新 dict（不修改入参）"""
        out = dict(data or {})
        for field in fields:
            if out.get(field):
                out[field] = 'enc:' + self.encrypt(out[field], aad=field + aad)
        return out

    def open(self, data, fields, aad=''):
        """解密 seal() 处理过的字段"""
        out = dict(data or {})
        for field in fields:
            value = out.get(field)
            if isinstance(value, compat.string_types) and value.startswith('enc:'):
                out[field] = self.decrypt(value[4:], aad=field + aad)
        return out
