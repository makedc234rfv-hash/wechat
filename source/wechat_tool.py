"""Portable entry point for the user's local Weixin archive. No network client."""
import argparse
import contextlib
from datetime import date
import json
import os
from pathlib import Path
import shutil
import sys

from archive_app import App, StreamToQueue, discover_weixin
from probe import run as probe_run
from export_archive import export, list_conversations
from period_report import build, period


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class Progress:
    def put(self, value):
        # Only StreamToQueue's allowlisted progress text reaches this sink.
        print(str(value), file=sys.stderr, flush=True)


def accounts(data_root=None):
    roots = [Path(data_root)] if data_root else [Path.home() / 'Documents' / 'xwechat_files']
    if not data_root and sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                documents = os.path.expandvars(winreg.QueryValueEx(key, 'Personal')[0])
                roots.append(Path(documents) / 'xwechat_files')
        except OSError:
            pass
    found = set()
    for root in roots:
        if root.is_dir():
            for account in root.iterdir():
                if (account / 'db_storage/message/message_0.db').is_file():
                    found.add(account.resolve())
    return sorted(found)


def choose_account(explicit=None, data_root=None):
    if explicit:
        result = Path(explicit).resolve()
        if not (result / 'db_storage/message/message_0.db').is_file():
            raise ValueError('指定账号目录中没有 db_storage/message/message_0.db')
        return result
    found = accounts(data_root)
    if len(found) != 1:
        raise ValueError('未找到唯一账号。请先用 --list-accounts 查看，再用 --account 明确指定；自定义根目录可用 --data-root。')
    return found[0]


def export_local(account, output, kind, start, end, conversation_ids=None, list_only=False):
    account, output = Path(account).resolve(), Path(output).resolve()
    if account == output or account in output.parents or output in account.parents:
        raise ValueError('输出目录与微信账号目录不能重叠')
    if output.exists():
        raise FileExistsError('输出目录已存在，请指定一个新目录，避免覆盖旧归档')
    processes, exe, dll = discover_weixin()
    output.mkdir(parents=True, exist_ok=False)
    snapshots = output / '_temporary_snapshot'
    try:
        errors = []
        for process in processes:
            # Each process attempt owns a new scratch folder. Never reuse a partial snapshot.
            scratch = snapshots / str(int(process['Id']))
            options = argparse.Namespace(pid=int(process['Id']), exe=str(exe), dll=str(dll),
                                         db=str(account / 'db_storage/message/message_0.db'),
                                         output=None, account=str(account), snapshot_dir=str(scratch))
            try:
                with contextlib.redirect_stdout(StreamToQueue(Progress())):
                    status = probe_run(options)
                if status == 0:
                    break
                errors.append('未找到通过校验的数据库密钥')
            except Exception as exc:
                errors.append(type(exc).__name__)
        else:
            raise RuntimeError('当前微信进程未能完成数据库校验：' + '; '.join(errors))
        if list_only:
            conversations = [{'name': name, 'kind': way, 'id': identifier}
                             for name, way, identifier in list_conversations(scratch)]
            listing = output / 'conversations.json'
            listing.write_text(json.dumps(conversations, ensure_ascii=False, indent=2), encoding='utf-8')
            return {'status': 'ok', 'conversation_count': len(conversations), 'file': str(listing)}
        if conversation_ids:
            available = {item[2] for item in list_conversations(scratch)}
            unknown = set(conversation_ids) - available
            if unknown:
                raise ValueError('指定会话ID不存在，请先用 --list-conversations 核对；不会回退到全部会话')
        with contextlib.redirect_stdout(StreamToQueue(Progress())):
            stats = export(scratch, account, output / '聊天归档', start, end, conversation_ids)
        report, summary = build(output / '聊天归档', kind, start, end)
        return {'status': 'ok', 'start': start, 'end': end, 'timezone': 'Asia/Shanghai (UTC+08:00)',
                'messages': stats['messages'], 'conversations': stats['conversations'],
                'archive': str(output / '聊天归档'), 'report': str(report)}
    finally:
        # This is our newly-created output subtree, never a source-data directory.
        if snapshots.is_dir():
            shutil.rmtree(snapshots)


def parser():
    result = argparse.ArgumentParser(description='本机微信4.x聊天归档；仅处理本机已登录的本人账号。')
    modes = result.add_mutually_exclusive_group()
    modes.add_argument('--gui', action='store_true', help='打开原图形界面')
    modes.add_argument('--self-test', action='store_true', help='仅用合成数据离线自检，不读取微信')
    modes.add_argument('--list-accounts', action='store_true', help='仅列出本地账号目录，不读取进程')
    modes.add_argument('--list-conversations', action='store_true', help='读取当前微信并列出可导出的会话；需 --output 新目录')
    result.add_argument('--account', help='具体账号目录；不是 xwechat_files 父目录')
    result.add_argument('--data-root', help='非默认位置的 xwechat_files 根目录')
    result.add_argument('--date', '--day', dest='day', help='导出一天，YYYY-MM-DD，北京时间')
    result.add_argument('--kind', choices=('day', 'week', 'month', 'custom'), default='day')
    result.add_argument('--anchor', help='周/月所属日期；YYYY-MM-DD')
    result.add_argument('--start', help='自定义起始日期，包含当天')
    result.add_argument('--end', help='自定义结束日期，包含当天')
    result.add_argument('--conversation', action='append', help='只导出指定会话ID；可重复，先列表再选择')
    result.add_argument('--output', help='本次输出目录，必须尚不存在')
    return result


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    if args.gui:
        App().mainloop()
        return 0
    if args.self_test:
        from portable_tests import run
        outcome = run()
        emit({'status': 'ok' if outcome.wasSuccessful() else 'failed', 'tests': outcome.testsRun,
              'uses_weixin': False})
        return 0 if outcome.wasSuccessful() else 1
    if args.list_accounts:
        emit({'accounts': [str(item) for item in accounts(args.data_root)]})
        return 0
    if not args.output:
        command.error('导出或列会话需要 --output 新目录；打开图形界面请用 --gui')
    if args.list_conversations:
        start = end = None
    elif args.start or args.end:
        if not args.start or not args.end or args.day or args.anchor:
            command.error('自定义范围请同时给出 --start 和 --end，不要混用 --date/--anchor')
        args.kind = 'custom'
        start, end = period('custom', args.start, args.start, args.end)
    else:
        anchor = args.day or args.anchor
        if not anchor or (args.day and args.anchor) or args.kind == 'custom':
            command.error('请用 --date 指定一天，或 --kind week/month --anchor 指定周期')
        start, end = period(args.kind, anchor)
    selected = choose_account(args.account, args.data_root)
    outcome = export_local(selected, args.output, args.kind, start, end, args.conversation,
                           args.list_conversations)
    emit(outcome)
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit({'status': 'error', 'error_type': type(exc).__name__, 'message': str(exc)})
        raise SystemExit(1)
