"""
security/crypto.py —— 敏感数据加密存储模块

使用 cryptography 库的 Fernet 对称加密（AES-128-CBC + HMAC-SHA256）。
密钥从环境变量 ENCRYPTION_KEY 读取（base64 编码的 32 字节密钥）。
若未设置，自动生成并持久化到 data/.encryption_key（仅开发环境）。

向后兼容策略：
- 加密后的数据以 "enc::" 前缀标识
- 读取时检测前缀：有前缀则解密，无前缀则视为明文直接返回
- 提供 migrate_encrypted_fields() 函数批量迁移旧数据
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("security.crypto")

# 加密数据前缀标识
ENC_PREFIX = "enc::v1::"

# 密钥文件路径（开发环境自动生成时使用）
_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".encryption_key"

_fernet: Optional[Fernet] = None
_key_source: str = "not_initialized"


def _generate_key() -> bytes:
    """生成新的 Fernet 密钥。"""
    return Fernet.generate_key()


def _load_or_create_key() -> bytes:
    """
    加载加密密钥，优先级：
    1. 环境变量 ENCRYPTION_KEY
    2. data/.encryption_key 文件
    3. 自动生成并保存到 data/.encryption_key（开发模式）
    """
    global _key_source

    # 1. 环境变量
    env_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            # 验证是有效的 base64 密钥
            Fernet(env_key.encode())
            _key_source = "environment"
            logger.info("加密密钥已从环境变量 ENCRYPTION_KEY 加载")
            return env_key.encode()
        except Exception:
            logger.warning("环境变量 ENCRYPTION_KEY 格式无效，将尝试其他来源")

    # 2. 密钥文件
    if _KEY_FILE.exists():
        try:
            key = _KEY_FILE.read_text(encoding="utf-8").strip().encode()
            Fernet(key)
            _key_source = "file"
            logger.info("加密密钥已从文件 %s 加载", _KEY_FILE)
            return key
        except Exception:
            logger.warning("密钥文件 %s 格式无效，将重新生成", _KEY_FILE)

    # 3. 自动生成（开发模式）
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = _generate_key()
    _KEY_FILE.write_text(key.decode(), encoding="utf-8")
    # 限制文件权限（Windows 下尽量设置）
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    _key_source = "auto_generated"
    logger.warning(
        "未设置 ENCRYPTION_KEY 环境变量，已自动生成开发密钥并保存到 %s。"
        "生产环境请务必设置 ENCRYPTION_KEY 环境变量！",
        _KEY_FILE,
    )
    return key


def init_crypto() -> None:
    """初始化加密模块（在应用启动时调用）。"""
    global _fernet
    if _fernet is None:
        key = _load_or_create_key()
        _fernet = Fernet(key)


def get_fernet() -> Fernet:
    """获取 Fernet 实例（懒加载）。"""
    if _fernet is None:
        init_crypto()
    return _fernet  # type: ignore[return-value]


def encrypt(plaintext: str) -> str:
    """
    加密字符串，返回带前缀的密文。

    Args:
        plaintext: 待加密的明文字符串

    Returns:
        带 "enc::v1::" 前缀的密文字符串
    """
    if plaintext is None:
        return plaintext
    f = get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return ENC_PREFIX + token.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """
    解密字符串。自动检测是否加密：
    - 以 "enc::v1::" 开头：解密
    - 其他：视为明文直接返回（向后兼容）

    Args:
        ciphertext: 密文或明文

    Returns:
        明文字符串
    """
    if ciphertext is None:
        return ciphertext
    if not isinstance(ciphertext, str):
        return ciphertext
    if not ciphertext.startswith(ENC_PREFIX):
        return ciphertext
    try:
        f = get_fernet()
        token = ciphertext[len(ENC_PREFIX):].encode("ascii")
        return f.decrypt(token).decode("utf-8")
    except InvalidToken:
        logger.error("解密失败：无效的 token，可能密钥不匹配。返回原始值。")
        return ciphertext
    except Exception as e:
        logger.error("解密异常：%s，返回原始值。", e)
        return ciphertext


def is_encrypted(value: str) -> bool:
    """检查值是否已加密。"""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def get_key_info() -> dict:
    """获取密钥信息（不含密钥本身，用于诊断）。"""
    return {
        "initialized": _fernet is not None,
        "key_source": _key_source,
        "prefix": ENC_PREFIX,
        "algorithm": "Fernet (AES-128-CBC + HMAC-SHA256)",
    }


# 模块加载时自动初始化
init_crypto()
