"""One-click monthly WeChat archive for the current Windows account.
All processing is local. Key material is used in memory and never displayed.
"""
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from probe import run as probe_run
from export_archive import export, list_conversations
from period_report import build, period

LABELS={'日报':'day','周报':'week','月报':'month','自定义':'custom'}


def discover_account():
    candidates = [Path.home()/'Documents/xwechat_files',
                  Path(os.environ.get('USERPROFILE',''))/'Documents/xwechat_files']
    for root in candidates:
        if root.is_dir():
            for account in sorted(root.glob('wxid_*')):
                if (account/'db_storage/message/message_0.db').is_file(): return account
    return None


def discover_weixin():
    command = ['powershell','-NoProfile','-Command',
               'Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object Id,Path,MainWindowHandle | ConvertTo-Json -Compress']
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    if result.returncode or not result.stdout.strip(): raise RuntimeError('没有找到正在运行的微信。请先登录微信。')
    processes=json.loads(result.stdout)
    if isinstance(processes,dict): processes=[processes]
    processes=[p for p in processes if p.get('Path') and Path(p['Path']).name.lower()=='weixin.exe']
    processes.sort(key=lambda p:(not bool(p.get('MainWindowHandle')),p['Id']))
    if not processes: raise RuntimeError('找不到微信主进程。')
    executable=Path(processes[0]['Path'])
    dlls=sorted(executable.parent.glob('*/Weixin.dll'),key=lambda p: tuple(int(x) for x in p.parent.name.split('.') if x.isdigit()),reverse=True)
    if not dlls: raise RuntimeError('找不到安装目录中的 Weixin.dll。')
    return processes,executable,dlls[0]


class StreamToQueue:
    def __init__(self,messages): self.messages=messages;self.pending=''
    def write(self,data):
        self.pending+=data
        while '\n' in self.pending:
            line,self.pending=self.pending.split('\n',1)
            try:
                event=json.loads(line)
                stage=event.get('stage','')
                permitted={'dll_checked':'已检查微信安装文件',
                           'scan_complete':'已扫描当前微信进程',
                           'verified_key':'数据库密钥校验成功',
                           'database_validated':'数据库校验成功：'+str(event.get('name','')),
                           'database_failed':'数据库校验失败：'+str(event.get('name','')),
                           'no_verified_key':'当前微信进程中没有找到可用的数据库密钥'}
                if stage in permitted: self.messages.put(permitted[stage])
            except (ValueError,TypeError): pass
    def flush(self): pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('微信聊天归档与报表')
        self.geometry('760x550')
        self.minsize(680,480)
        self.messages=queue.Queue()
        account=discover_account()
        self.account=tk.StringVar(value=str(account) if account else '')
        self.output=tk.StringVar(value=str(Path.home()/'Documents/微信聊天归档'))
        self.kind=tk.StringVar(value='月报')
        self.conversation_scope=tk.StringVar(value='全部会话')
        self.anchor=tk.StringVar(value=datetime.now().strftime('%Y-%m-%d'))
        self.start_date=tk.StringVar()
        self.end_date=tk.StringVar()
        self.progress=tk.StringVar(value='就绪')
        box=ttk.Frame(self,padding=20);box.pack(fill='both',expand=True)
        ttk.Label(box,text='微信聊天归档与报表',font=('Microsoft YaHei',18,'bold')).pack(anchor='w')
        ttk.Label(box,text='选择日期后，重新读取微信数据，只导出该时间段的聊天记录与报表。',wraplength=690).pack(anchor='w',pady=(8,18))
        self.row(box,'微信数据目录',self.account)
        self.row(box,'保存到',self.output)
        row=ttk.Frame(box);row.pack(fill='x',pady=8)
        ttk.Label(row,text='报表类型',width=13).pack(side='left')
        chooser=ttk.Combobox(row,textvariable=self.kind,values=list(LABELS),state='readonly',width=12)
        chooser.pack(side='left')
        ttk.Label(row,text='可随时切换日报、周报、月报或自定义日期').pack(side='left',padx=12)
        row=ttk.Frame(box);row.pack(fill='x',pady=6)
        ttk.Label(row,text='联系人/群',width=13).pack(side='left')
        ttk.Combobox(row,textvariable=self.conversation_scope,
                     values=('全部会话','选择联系人或群（可多选）'),state='readonly',width=24).pack(side='left')
        ttk.Label(row,text='选择后会从当前微信数据中列出会话').pack(side='left',padx=12)
        row=ttk.Frame(box);row.pack(fill='x',pady=6)
        ttk.Label(row,text='选定日期',width=13).pack(side='left')
        ttk.Entry(row,textvariable=self.anchor,width=13).pack(side='left')
        ttk.Label(row,text='格式：2026-09-24；周报按周一至周日，月报按自然月').pack(side='left',padx=12)
        row=ttk.Frame(box);row.pack(fill='x',pady=6)
        ttk.Label(row,text='开始 / 结束',width=13).pack(side='left')
        ttk.Entry(row,textvariable=self.start_date,width=13).pack(side='left')
        ttk.Label(row,text=' 至 ').pack(side='left')
        ttk.Entry(row,textvariable=self.end_date,width=13).pack(side='left')
        ttk.Label(row,text='自定义时可输入；其他类型自动计算').pack(side='left',padx=12)
        self.kind.trace_add('write',lambda *_:self.update_period())
        self.anchor.trace_add('write',lambda *_:self.update_period())
        self.update_period()
        ttk.Label(box,text='会读取当前微信进程以验证本机数据库密钥；全部数据只保存在所选目录。生成过程通常需要数分钟。',foreground='#875800',wraplength=680).pack(anchor='w',pady=(12,6))
        buttons=ttk.Frame(box);buttons.pack(fill='x',pady=12)
        self.start=ttk.Button(buttons,text='找出记录并生成报表',command=self.start_run)
        self.start.pack(side='left')
        ttk.Button(buttons,text='打开保存目录',command=self.open_output).pack(side='left',padx=12)
        ttk.Label(box,textvariable=self.progress).pack(anchor='w')
        self.log=tk.Text(box,height=10,state='disabled',wrap='word')
        self.log.pack(fill='both',expand=True,pady=(8,0))
        self.after(200,self.drain)

    def row(self,parent,label,variable):
        row=ttk.Frame(parent);row.pack(fill='x',pady=6)
        ttk.Label(row,text=label,width=13).pack(side='left')
        ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='选择…',command=lambda:self.choose(variable)).pack(side='left',padx=(8,0))

    def choose(self,variable):
        folder=filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if folder: variable.set(folder)

    def update_period(self):
        if LABELS[self.kind.get()]=='custom': return
        try:
            start,end=period(LABELS[self.kind.get()],self.anchor.get())
            self.start_date.set(start);self.end_date.set(end)
        except ValueError: pass

    def open_output(self):
        path=Path(self.output.get())
        path.mkdir(parents=True,exist_ok=True)
        os.startfile(path)

    def start_run(self):
        try:
            account=Path(self.account.get()).resolve()
            if not (account/'db_storage/message/message_0.db').is_file(): raise ValueError('微信数据目录中没有找到 message_0.db。')
            selected_kind=LABELS[self.kind.get()]
            start,end=period(selected_kind,self.anchor.get(),self.start_date.get(),self.end_date.get())
            output=Path(self.output.get()).resolve()
            if output == account or account in output.parents: raise ValueError('输出目录不能位于微信原始数据目录内。')
            processes,exe,dll=discover_weixin()
            run_dir=output/('归档_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            run_dir.mkdir(parents=True,exist_ok=False)
        except Exception as exc:
            messagebox.showerror('无法开始',str(exc));return
        self.start.state(['disabled'])
        self.messages.put('开始读取当前微信数据…')
        threading.Thread(target=self.worker,args=(account,run_dir,processes,exe,dll,selected_kind,start,end,
                                                 self.conversation_scope.get()=='选择联系人或群（可多选）'),daemon=True).start()

    def choose_conversation(self,choices,result,ready):
        window=tk.Toplevel(self)
        window.title('选择联系人或群（可多选）')
        window.geometry('480x510')
        window.transient(self)
        window.grab_set()
        ttk.Label(window,text='点击可勾选多个联系人或群；搜索不会清除已选项。').pack(anchor='w',padx=16,pady=10)
        query=tk.StringVar()
        entry=ttk.Entry(window,textvariable=query)
        entry.pack(fill='x',padx=16)
        listing=tk.Listbox(window,exportselection=False,activestyle='none')
        listing.pack(fill='both',expand=True,padx=16,pady=10)
        shown=[]
        selected=set()
        count=tk.StringVar(value='已选择 0 个会话')
        def refresh(*_):
            listing.delete(0,'end');shown.clear()
            needle=query.get().casefold().strip()
            for name,kind,identifier in choices:
                if needle in (name+' '+kind+' '+identifier).casefold():
                    shown.append(identifier)
                    listing.insert('end',f'{"☑" if identifier in selected else "□"} [{kind}] {name}  ({identifier})')
            count.set(f'已选择 {len(selected)} 个会话')
        def toggle(event):
            index=listing.nearest(event.y)
            bounds=listing.bbox(index)
            if bounds and bounds[1]<=event.y<bounds[1]+bounds[3]:
                identifier=shown[index]
                if identifier in selected: selected.remove(identifier)
                else: selected.add(identifier)
                refresh()
            return 'break'
        def finish():
            if not selected:
                messagebox.showwarning('尚未选择','请至少勾选一个联系人或群。',parent=window)
                return
            result.extend(sorted(selected))
            ready.set();window.destroy()
        def cancel():
            ready.set();window.destroy()
        query.trace_add('write',refresh);refresh()
        ttk.Label(window,textvariable=count).pack(anchor='w',padx=16)
        ttk.Button(window,text='确定',command=finish).pack(pady=(0,12))
        listing.bind('<Button-1>',toggle)
        window.protocol('WM_DELETE_WINDOW',cancel)
        entry.focus_set()

    def worker(self,account,run_dir,processes,exe,dll,kind,start,end,select_conversation):
        snapshots=run_dir/'数据库快照'
        try:
            db=account/'db_storage/message/message_0.db'
            destination=run_dir/'聊天归档'
            outcome=None
            for process in processes:
                args=type('Options',(),dict(pid=int(process['Id']),exe=str(exe),dll=str(dll),db=str(db),
                                             output=None,account=str(account),snapshot_dir=str(snapshots)))()
                with contextlib.redirect_stdout(StreamToQueue(self.messages)):
                    outcome=probe_run(args)
                if outcome==0: break
            if outcome!=0: raise RuntimeError('未能验证微信数据库密钥。请保持微信已登录并重试。')
            conversation_ids=None
            if select_conversation:
                choices=list_conversations(snapshots)
                if not choices: raise RuntimeError('当前数据库中没有找到可选会话。')
                result=[];ready=threading.Event()
                self.after(0,lambda:self.choose_conversation(choices,result,ready))
                ready.wait()
                if not result: raise RuntimeError('已取消选择联系人或群。')
                conversation_ids=result
            self.messages.put('正在按会话和日期生成归档…')
            with contextlib.redirect_stdout(StreamToQueue(self.messages)):
                stats=export(snapshots,account,destination,start,end,conversation_ids)
            report,summary=build(destination,kind,start,end)
            self.messages.put(('complete',str(report),f'完成：{start} 至 {end}，找到 {stats["messages"]} 条消息、{stats["conversations"]} 个会话。'))
        except Exception as exc:
            self.messages.put(('error',str(exc)))
        finally:
            # Decrypted full-history snapshots are temporary; only selected-period output remains.
            if snapshots.parent.resolve()==run_dir.resolve() and snapshots.is_dir():
                shutil.rmtree(snapshots)

    def drain(self):
        try:
            while True:
                item=self.messages.get_nowait()
                if isinstance(item,tuple) and item[0]=='complete':
                    self.log_line(item[2]);self.progress.set('完成')
                    self.start.state(['!disabled'])
                    messagebox.showinfo('归档完成',item[2]+'\n\n报表：'+item[1])
                    webbrowser.open(Path(item[1]).resolve().as_uri())
                elif isinstance(item,tuple) and item[0]=='error':
                    self.log_line('错误：'+item[1]);self.progress.set('失败')
                    self.start.state(['!disabled'])
                    messagebox.showerror('归档失败',item[1])
                else:
                    self.log_line(item);self.progress.set(item)
        except queue.Empty: pass
        self.after(200,self.drain)

    def log_line(self,line):
        self.log.config(state='normal');self.log.insert('end',line+'\n');self.log.see('end');self.log.config(state='disabled')


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--self-test':
        import compression.zstd
        from cryptography.hazmat.primitives.ciphers import Cipher
        assert period('week','2026-09-24')==('2026-09-21','2026-09-27')
        Path(sys.argv[2]).write_text(json.dumps({'status':'ok','app':'WeChatArchiveReports',
                                                  'account_detected':discover_account() is not None},
                                                 ensure_ascii=False),encoding='utf-8')
    else:
        App().mainloop()
