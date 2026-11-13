"""Local, read-only compatibility probe for the explicitly selected Weixin process.
No injection, process writes, network calls, or memory dumps. Keys stay in RAM.
Algorithm references: LifeArchiveProject/WeChatDataAnalysis key_v4.py and dll_key_scan.py.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import hmac
import json
import mmap
from pathlib import Path
import re
import struct
import time
import sqlite3


def decrypt_snapshot(source, destination, material):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    source = Path(source)
    before = source.stat()
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Source changed during read; retry required')
    if len(data) % 4096:
        raise ValueError('Database size is not page aligned')
    salt = data[:16]
    enc = hashlib.pbkdf2_hmac('sha512', material, salt, 256000, 32)
    mac = hashlib.pbkdf2_hmac('sha512', enc, bytes(x ^ 0x3a for x in salt), 2, 32)
    plain = bytearray()
    for index in range(len(data) // 4096):
        page = data[index * 4096:(index + 1) * 4096]
        offset = 16 if index == 0 else 0
        expected = hmac.digest(mac, page[offset:4032] + struct.pack('<I', index + 1), 'sha512')
        if not hmac.compare_digest(expected, page[4032:4096]):
            raise ValueError(f'Page authentication failed: {index + 1}')
        decryptor = Cipher(algorithms.AES(enc), modes.CBC(page[4016:4032])).decryptor()
        payload = decryptor.update(page[offset:4016]) + decryptor.finalize()
        if index == 0:
            plain.extend(b'SQLite format 3\x00')
        plain.extend(payload)
        plain.extend(bytes(80))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(plain)
    uri = destination.resolve().as_uri() + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as db:
        check = [row[0] for row in db.execute('PRAGMA integrity_check')]
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        schemas = {table: list(db.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')) for table in tables[:3]}
    print(json.dumps({'stage':'snapshot_decrypted','pages':len(data)//4096,'integrity':check,
                      'table_count':len(tables),'sample_schemas':schemas,
                      'wal_present':Path(str(source)+'-wal').exists(),
                      'wal_included':False,'output':str(destination)}, ensure_ascii=False), flush=True)
    if check != ['ok']:
        raise ValueError('Snapshot did not pass SQLite integrity check')


class MemoryInfo(c.Structure):
    _fields_ = [('base', c.c_void_p), ('allocation', c.c_void_p),
                ('protect0', w.DWORD), ('size', c.c_size_t),
                ('state', w.DWORD), ('protect', w.DWORD), ('kind', w.DWORD)]


def run(args):
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(MemoryInfo), c.c_size_t]
    kernel.VirtualQueryEx.restype = c.c_size_t
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    handle = kernel.OpenProcess(0x410, False, args.pid)
    if not handle:
        raise OSError(c.get_last_error(), 'Read-only process access denied')
    try:
        name = c.create_unicode_buffer(32768)
        size = w.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, c.byref(size)):
            raise OSError('Cannot verify process identity')
        if Path(name.value).resolve() != Path(args.exe).resolve():
            raise ValueError('Process executable does not match the explicitly selected Weixin.exe')

        def read(address, count):
            buffer = c.create_string_buffer(count)
            actual = c.c_size_t()
            kernel.ReadProcessMemory(handle, c.c_void_p(address), buffer, count, c.byref(actual))
            return buffer.raw[:actual.value]

        with Path(args.db).open('rb') as stream:
            page = stream.read(4096)
        if len(page) != 4096 or page.startswith(b'SQLite format 3'):
            raise ValueError('Expected an encrypted 4096-byte first page')

        signature = re.compile(b'\x48\xba(.{8}).{3,8}?\x48\xba(.{8}).{3,8}?\x48\xba(.{8}).{3,8}?\x48\xba(.{8}).{3,8}?\x48\x85\xc0', re.S)
        with Path(args.dll).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            masks = list(dict.fromkeys(b''.join(match.groups()) for match in signature.finditer(data)))
        print(json.dumps({'stage': 'dll_checked', 'auxiliary_candidates': len(masks)}), flush=True)
        if len(masks) > 16:
            raise ValueError('Ambiguous DLL signature; automatic probe stopped')
        masks.append(bytes(32))

        pattern = re.compile(b'(.{6}\x00\x00)' + b'\x00' * 8 + b'\x20' + b'\x00' * 7 + b'\x2f' + b'\x00' * 7, re.S)
        candidates = set()
        address = 0
        total = 0
        regions = 0
        started = time.monotonic()
        last_report = started
        while address < 0x7fffffffffff:
            if time.monotonic() - started > 120:
                raise TimeoutError('Memory enumeration exceeded 120 seconds')
            info = MemoryInfo()
            if not kernel.VirtualQueryEx(handle, c.c_void_p(address), c.byref(info), c.sizeof(info)):
                break
            base = info.base or 0
            next_address = base + info.size
            if next_address <= address:
                break
            if info.state == 0x1000 and info.kind == 0x20000 and not info.protect & 0x101:
                regions += 1
                for offset in range(0, info.size, 4 * 1024 * 1024):
                    data = read(base + offset, min(4 * 1024 * 1024 + 31, info.size - offset))
                    total += len(data)
                    for match in pattern.finditer(data):
                        pointer = struct.unpack('<Q', match.group(1))[0]
                        key = read(pointer, 32)
                        if len(key) == 32 and len(set(key)) >= 15 and sum(32 <= x <= 126 for x in key) <= 24:
                            candidates.add(key)
                    if len(candidates) > 3000:
                        raise ValueError('Too many candidates; signature may not match this version')
            address = next_address
            if time.monotonic() - last_report > 15:
                print(json.dumps({'stage':'scanning','regions':regions,'read_mb':total//1048576,'candidate_count':len(candidates)}), flush=True)
                last_report = time.monotonic()
        print(json.dumps({'stage':'scan_complete','regions':regions,'read_mb':total//1048576,'candidate_count':len(candidates)}), flush=True)

        salt = page[:16]
        mac_salt = bytes(x ^ 0x3a for x in salt)
        checked = 0
        for key in candidates:
            for mask in masks:
                if time.monotonic() - started > 300:
                    raise TimeoutError('Candidate verification exceeded total 300-second limit')
                material = bytes(a ^ b for a, b in zip(key, mask))
                encryption_key = hashlib.pbkdf2_hmac('sha512', material, salt, 256000, 32)
                mac_key = hashlib.pbkdf2_hmac('sha512', encryption_key, mac_salt, 2, 32)
                expected = hmac.digest(mac_key, page[16:4032] + struct.pack('<I', 1), 'sha512')
                checked += 1
                if hmac.compare_digest(expected, page[4032:4096]):
                    print(json.dumps({'stage':'verified_key','checked':checked,'key_value':'not printed or saved'}), flush=True)
                    if args.output:
                        decrypt_snapshot(args.db, args.output, material)
                    if args.account and args.snapshot_dir:
                        from snapshot import account_snapshot
                        account_snapshot(args.account, args.snapshot_dir, material)
                    return 0
                if checked % 50 == 0:
                    print(json.dumps({'stage':'verifying','checked':checked,'total':len(candidates)*len(masks)}), flush=True)
        print(json.dumps({'stage':'no_verified_key','checked':checked}), flush=True)
        return 2
    finally:
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--exe', required=True)
    parser.add_argument('--dll', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--output')
    parser.add_argument('--account')
    parser.add_argument('--snapshot-dir')
    try:
        raise SystemExit(run(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({'stage':'stopped','reason':str(exc)}), flush=True)
        raise SystemExit(1)
