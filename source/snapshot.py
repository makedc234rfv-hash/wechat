"""Authenticated SQLCipher page decoding and committed SQLite WAL replay.
Reads only source files. Outputs new, validated SQLite snapshots.
WAL format: https://www.sqlite.org/fileformat2.html#walformat
"""
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct
import time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def checksum(data, state=(0, 0), endian='<'):
    if len(data) % 8:
        raise ValueError('Invalid WAL checksum block length')
    a, b = state
    for x, y in struct.iter_unpack(endian + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def wal_frames(data):
    if not data:
        return [], {'valid_frames': 0, 'committed_frames': 0, 'commit_pages': 0}
    if len(data) < 32:
        raise ValueError('Incomplete WAL header')
    magic, version, page_size = struct.unpack('>III', data[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != 4096:
        raise ValueError('Unsupported WAL format')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(data[:24], endian=endian)
    if state != struct.unpack('>II', data[24:32]):
        raise ValueError('WAL header checksum failed')
    frames = []
    last_commit = 0
    commit_pages = 0
    stop = 'end'
    for offset in range(32, len(data), 4120):
        frame = data[offset:offset + 4120]
        if len(frame) < 4120:
            stop = 'partial_tail'; break
        number, size = struct.unpack('>II', frame[:8])
        if frame[8:16] != data[16:24] or not number:
            stop = 'old_generation_tail'; break
        current = checksum(frame[:8] + frame[24:], state, endian)
        if current != struct.unpack('>II', frame[16:24]):
            stop = 'invalid_checksum_tail'; break
        state = current
        frames.append((number, frame[24:]))
        if size:
            last_commit = len(frames)
            commit_pages = size
    return frames[:last_commit], {'valid_frames': len(frames), 'committed_frames': last_commit,
                                  'commit_pages': commit_pages, 'tail': stop}


def stable_pair(source):
    wal = Path(str(source) + '-wal')
    def read():
        return source.read_bytes(), wal.read_bytes() if wal.exists() else b''
    for _ in range(4):
        first = read()
        second = read()
        if first == second:
            return first
        time.sleep(.15)
    raise RuntimeError('Database/WAL changed during snapshot; retry when idle')


def snapshot(source, destination, material):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError('Snapshot destination already exists')
    data, wal = stable_pair(source)
    if len(data) < 4096 or len(data) % 4096:
        raise ValueError('Database is not aligned to 4096-byte pages')
    salt = data[:16]
    enc = hashlib.pbkdf2_hmac('sha512', material, salt, 256000, 32)
    mac = hashlib.pbkdf2_hmac('sha512', enc, bytes(x ^ 0x3a for x in salt), 2, 32)

    def decode(page, number):
        start = 16 if number == 1 else 0
        expected = hmac.digest(mac, page[start:4032] + struct.pack('<I', number), 'sha512')
        if not hmac.compare_digest(expected, page[4032:]):
            raise ValueError(f'Page {number} authentication failed')
        decryptor = Cipher(algorithms.AES(enc), modes.CBC(page[4016:4032])).decryptor()
        return ((b'SQLite format 3\0' if number == 1 else b'') +
                decryptor.update(page[start:4016]) + decryptor.finalize() + bytes(80))

    frames, wal_info = wal_frames(wal)
    plain = bytearray()
    for offset in range(0, len(data), 4096):
        plain.extend(decode(data[offset:offset + 4096], offset // 4096 + 1))
    for number, page in frames:
        end = number * 4096
        if end > len(plain):
            plain.extend(bytes(end - len(plain)))
        plain[end - 4096:end] = decode(page, number)
    if frames:
        plain = plain[:wal_info['commit_pages'] * 4096]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(plain)
    with sqlite3.connect(destination.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        checks = [row[0] for row in db.execute('PRAGMA integrity_check')]
    if checks != ['ok']:
        raise ValueError('SQLite integrity check failed: ' + ';'.join(checks[:3]))
    return {'database': source.name, 'integrity': 'ok', 'pages': len(plain)//4096,
            'source_sha256': hashlib.sha256(data).hexdigest(),
            'wal_sha256': hashlib.sha256(wal).hexdigest(), 'wal': wal_info}


def account_snapshot(account, output, material):
    root, output = Path(account) / 'db_storage', Path(output)
    selected = [root / 'contact/contact.db', root / 'session/session.db',
                root / 'hardlink/hardlink.db', root / 'message/message_resource.db']
    selected += sorted((root / 'message').glob('message_[0-9]*.db'))
    selected += sorted((root / 'message').glob('media_[0-9]*.db'))
    manifest = {'captured_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'databases': [], 'errors': []}
    for source in selected:
        try:
            report = snapshot(source, output / source.relative_to(root), material)
            manifest['databases'].append(report)
            print(json.dumps({'stage':'database_validated','name':source.name,'wal_frames':report['wal']['committed_frames']}), flush=True)
        except Exception as exc:
            manifest['errors'].append({'database':source.name,'error':str(exc)})
            print(json.dumps({'stage':'database_failed','name':source.name,'error':str(exc)}), flush=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'snapshot_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if manifest['errors']:
        raise RuntimeError('Some databases did not validate; see snapshot manifest')
