import hashlib
from pathlib import Path
import re
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def kind(data):
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')): return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return '.webp'
    if data.startswith((b'wxgf', b'WXGF')): return '.wxgf'
    if data[4:8] == b'ftyp': return '.mp4'
    return None


def ecb(data, key):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def resolve_image_keys(account):
    # Enumerate account-local cached code filenames; never print the resulting keys.
    import os
    root = Path(os.environ['APPDATA']) / 'Tencent/xwechat'
    codes = set()
    for folder in root.rglob('kvcomm'):
        for path in folder.glob('key_*.statistic'):
            match = re.match(r'key_(\d+)_', path.name)
            if match: codes.add(int(match.group(1)))
    account_id = account.name.rsplit('_', 1)[0]
    samples = sorted((account / 'msg/attach').rglob('*_t.dat'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    templates = [p.read_bytes() for p in samples]
    for code in codes:
        key = hashlib.md5(f'{code}{account_id}'.encode()).hexdigest()[:16].encode('ascii')
        xor = code & 255
        validated = 0
        for data in templates:
            if data[:6] != b'\x07\x08V2\x08\x07' or len(data) < 31: continue
            if kind(ecb(data[15:31], key)) and (data[-2] ^ xor, data[-1] ^ xor) == (255, 217):
                validated += 1
        if validated >= 2:
            return key, xor
    return None


def decode_dat(data, keys):
    if kind(data): return data, kind(data)
    if data[:6] == b'\x07\x08V2\x08\x07':
        if keys is None: raise ValueError('图片密钥未找到')
        key, xor = keys
        aes_size, xor_size = struct.unpack('<II', data[6:14])
        encrypted_size = (aes_size // 16 + 1) * 16
        if len(data) < 15 + encrypted_size + xor_size:
            raise ValueError('图片数据长度不完整')
        decrypted = ecb(data[15:15+encrypted_size], key)
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(decrypted) + unpadder.finalize()
        end = len(data) - xor_size
        plain += data[15+encrypted_size:end]
        plain += bytes(value ^ xor for value in data[end:])
        extension = kind(plain)
        if extension is None: raise ValueError('无法识别解密后的图片格式')
        return plain, extension
    for header in (b'\xff\xd8\xff', b'\x89PNG', b'GIF8'):
        xor = data[0] ^ header[0]
        if bytes(value ^ xor for value in data[:len(header)]) == header:
            plain = bytes(value ^ xor for value in data)
            return plain, kind(plain)
    raise ValueError('不支持的图片格式')


if __name__ == '__main__':
    import sys, json
    account = Path(sys.argv[1])
    keys = resolve_image_keys(account)
    stats = {'verified_image_key': keys is not None, 'formats': {}, 'errors': 0}
    for path in (account / 'msg/attach').rglob('*.dat'):
        try:
            _, extension = decode_dat(path.read_bytes(), keys)
            stats['formats'][extension] = stats['formats'].get(extension, 0) + 1
        except Exception:
            stats['errors'] += 1
    print(json.dumps(stats))
