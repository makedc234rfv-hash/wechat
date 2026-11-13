"""Export validated local snapshots to offline conversation/date archives."""
import argparse
import base64
from collections import Counter, defaultdict
from compression import zstd
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sqlite3
from urllib.parse import quote
import xml.etree.ElementTree as ET
from media import decode_dat, resolve_image_keys, kind

TZ = timezone(timedelta(hours=8))
STYLE = '''body{margin:0;background:#f4f6f8;color:#18212b;font:15px/1.65 "Microsoft YaHei",sans-serif}main{max-width:1000px;margin:32px auto;padding:0 20px}h1{font-size:28px;margin-bottom:8px}a{color:#176443}small,.meta{color:#687681}.note{padding:14px 18px;background:#fff7df;border-radius:8px}.msg,.day,.conversation{background:white;padding:16px 20px;margin:12px 0;border:1px solid #e2e8ec;border-radius:12px}.sent{border-left:5px solid #40956c;margin-left:48px}.received{margin-right:48px}.body{white-space:pre-wrap;overflow-wrap:anywhere}.meta{font-size:12px;margin-bottom:6px}.preview{max-width:100%;max-height:440px;display:block;margin:10px 0}.missing{color:#9b5710}.stats{display:flex;gap:24px;flex-wrap:wrap}input{font:inherit;padding:10px;width:90%;border:1px solid #cad5dc;border-radius:8px}summary{cursor:pointer;color:#647781}video,audio{max-width:100%}code{overflow-wrap:anywhere}'''


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    return db


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', value).strip(' .')[:64] or '未命名'
    if value.upper().split('.')[0] in {'CON','PRN','AUX','NUL', *[f'COM{i}' for i in range(1,10)], *[f'LPT{i}' for i in range(1,10)]}:
        value = '_' + value
    return value


def page(title, body, search=False):
    script = '''<script>document.getElementById('search').addEventListener('input',function(){let q=this.value.toLowerCase();document.querySelectorAll('.conversation').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(q))})</script>''' if search else ''
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; media-src \'self\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style></head><body><main>'+body+'</main>'+script+'</body></html>'


def text_decode(value):
    if value is None: return ''
    if isinstance(value, bytes):
        if value.startswith(bytes.fromhex('28b52ffd')): value = zstd.decompress(value)
        return value.decode('utf-8')
    return str(value)


def parse_xml(text):
    start = text.find('<')
    if start < 0 or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper(): return None
    try: return ET.fromstring(text[start:])
    except ET.ParseError: return None


def display_text(base, subtype, raw, xml):
    if base == 1: return raw
    if xml is None: return raw or '[无文字内容]'
    def value(path): return xml.findtext(path, default='') or ''
    if base == 3: return '[图片]'
    if base == 34:
        node=xml.find('.//voicemsg')
        try: seconds=int(node.get('voicelength','0'))/1000 if node is not None else 0
        except ValueError: seconds=0
        return f'[语音 {seconds:g} 秒]'
    if base == 43: return '[视频]'
    if base == 47:
        node=xml.find('.//emoji')
        return '[表情] '+(node.get('desc','') if node is not None else '')
    if base == 48:
        node=xml.find('.//location')
        return '[位置] '+(' '.join(node.get(key,'') for key in ('poiname','label')) if node is not None else '')
    if base == 49:
        labels={6:'文件',5:'链接',57:'引用',19:'合并聊天',33:'小程序',36:'小程序',2000:'转账',2001:'红包',51:'视频号'}
        title=value('.//appmsg/title'); des=value('.//appmsg/des')
        result='['+labels.get(subtype,'卡片')+'] '+title
        if des and des != title: result+='\n'+des
        if subtype == 57:
            quote_text=value('.//refermsg/content'); quote_sender=value('.//refermsg/displayname')
            result+='\n引用 '+quote_sender+'：'+quote_text
        return result
    if base == 50: return '[通话] '+(value('.//VoIPBubbleMsg/msg') or value('.//msg') or '通话记录')
    if base == 10000: return value('.//revokemsg/content') or value('.//content') or ' '.join(xml.itertext()).strip() or '[系统消息]'
    return '[其他消息] '+' '.join(xml.itertext()).strip()


def list_conversations(snapshot_root):
    snapshot_root=Path(snapshot_root)
    contacts={}
    contact=connect(snapshot_root/'contact/contact.db')
    for table in ('stranger','contact'):
        for row in contact.execute('SELECT username,remark,nick_name FROM '+table):
            contacts[row['username']]=row['remark'] or row['nick_name'] or row['username']
    contact.close()
    found={}
    for path in sorted((snapshot_root/'message').glob('message_[0-9]*.db')):
        db=connect(path)
        names={hashlib.md5(row['user_name'].encode()).hexdigest():row['user_name']
               for row in db.execute('SELECT user_name FROM Name2Id')}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
            conversation_id=names.get(table[4:])
            if conversation_id:
                found[conversation_id]=(contacts.get(conversation_id,conversation_id),
                                        '群' if conversation_id.endswith('@chatroom') else '联系人')
        db.close()
    return sorted(((name,kind,identifier) for identifier,(name,kind) in found.items()),
                  key=lambda item:(item[1],item[0].casefold(),item[2]))


def export(snapshot_root, account, output, start_date=None, end_date=None, conversation_ids=None):
    snapshot_root, account, output = Path(snapshot_root), Path(account), Path(output)
    selected_ids=None if conversation_ids is None else set(conversation_ids)
    if output.exists(): raise FileExistsError('请选择新的输出目录，避免覆盖已有归档')
    manifest=json.loads((snapshot_root/'snapshot_manifest.json').read_text(encoding='utf-8'))
    if manifest['errors']: raise ValueError('存在未通过校验的数据库')
    output.mkdir(parents=True)
    self_id=account.name.rsplit('_',1)[0]
    contact=connect(snapshot_root/'contact/contact.db')
    contacts={}
    for table in ('stranger','contact'):
        for row in contact.execute('SELECT username,remark,nick_name FROM '+table):
            contacts[row['username']]=row['remark'] or row['nick_name'] or row['username']
    contact.close()
    sessions=connect(snapshot_root/'session/session.db')
    for row in sessions.execute('SELECT username,session_title FROM SessionNoContactInfoTable'):
        if row['username'] not in contacts: contacts[row['username']]=row['session_title'] or row['username']
    sessions.close()
    files=defaultdict(list)
    for path in (account/'msg').rglob('*'):
        if path.is_file(): files[path.name.lower()].append(path)
    hard=connect(snapshot_root/'hardlink/hardlink.db')
    hardlinks=defaultdict(set)
    for table in ('image_hardlink_info_v4','file_hardlink_info_v4','video_hardlink_info_v4'):
        for row in hard.execute('SELECT md5,file_name FROM '+table):
            if row['md5'] and row['file_name']: hardlinks[row['md5'].lower()].add(row['file_name'].lower())
    hard.close()
    image_keys=resolve_image_keys(account)
    voices={}
    for path in sorted((snapshot_root/'message').glob('media_*.db')):
        db=connect(path)
        names={r['rowid']:r['user_name'] for r in db.execute('SELECT rowid,user_name FROM Name2Id')}
        for row in db.execute('SELECT * FROM VoiceInfo'):
            voices[(names.get(row['chat_name_id']),row['svr_id'],row['local_id'])]=row['voice_data']
        db.close()

    stats=Counter(); by_conversation=defaultdict(list); issues=[]
    def save_attachment(data, name, day_dir, subdir, source, quality='original'):
        digest=hashlib.sha256(data).hexdigest()
        filename=safe_name(Path(name).stem)[:44]+'_'+digest[:12]+Path(name).suffix[:12]
        dest=day_dir/subdir/filename
        dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists(): dest.write_bytes(data)
        return {'path':dest.relative_to(day_dir).as_posix(),'name':name,'sha256':digest,
                'bytes':len(data),'source':str(source),'quality':quality}

    for path in sorted((snapshot_root/'message').glob('message_[0-9]*.db')):
        db=connect(path)
        id_names={r['rowid']:r['user_name'] for r in db.execute('SELECT rowid,user_name FROM Name2Id')}
        hash_names={hashlib.md5(name.encode()).hexdigest():name for name in id_names.values()}
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for table in tables:
            conv_id=hash_names.get(table[4:], table)
            if selected_ids is not None and conv_id not in selected_ids: continue
            conv_name=contacts.get(conv_id, conv_id)
            folder=safe_name(conv_name)+'__'+table[4:12]
            for row in db.execute('SELECT * FROM "'+table+'" ORDER BY create_time,sort_seq,local_id'):
                base=row['local_type']&0xffffffff; subtype=row['local_type']>>32
                stamp=datetime.fromtimestamp(row['create_time'],TZ)
                if start_date and stamp.date().isoformat()<start_date: continue
                if end_date and stamp.date().isoformat()>end_date: continue
                stats['messages']+=1
                day=stamp.strftime('%Y-%m-%d'); day_dir=output/folder/day
                day_dir.mkdir(parents=True,exist_ok=True)
                identifier=hashlib.sha256(f'{conv_id}|{row["server_id"]}|{row["local_id"]}|{path.name}'.encode()).hexdigest()
                record={'message_id':identifier,'conversation_id':conv_id,'conversation_name':conv_name,
                        'sender_id':id_names.get(row['real_sender_id']), 'timestamp':stamp.isoformat(),
                        'message_type':{1:'text',3:'image',34:'audio',43:'video',47:'sticker',48:'location',49:'card',50:'call',10000:'system'}.get(base,'unknown'),
                        'local_type':row['local_type'],'server_id':row['server_id'],'local_id':row['local_id'],
                        'attachments':[],'warnings':[],'source_database':path.name,'source_table':table}
                if base==49 and subtype==6: record['message_type']='file'
                record['sender_name']=contacts.get(record['sender_id'],record['sender_id'] or '未知发送者')
                record['direction']='sent' if record['sender_id']==self_id else 'received' if record['sender_id'] else 'unknown'
                if base==10000: record['sender_name']='系统';record['direction']='system'
                try: raw=text_decode(row['message_content'])
                except Exception as exc:
                    raw='[正文解码失败]'; record['warnings'].append('正文解码失败：'+type(exc).__name__)
                    stats['decode_failed']+=1
                if conv_id.endswith('@chatroom') and record['sender_id'] and raw.startswith(record['sender_id']+':\n'):
                    raw=raw[len(record['sender_id'])+2:]
                xml=parse_xml(raw)
                record['text']=display_text(base,subtype,raw,xml)
                if base==1: record['raw_text']=raw
                if record['sender_id'] is None and base!=10000: stats['unknown_senders']+=1
                if xml is not None:
                    url=xml.findtext('.//appmsg/url','')
                    if url.startswith(('https://','http://')): record['url']=url
                references=set(re.findall(r'(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])',raw))
                if row['packed_info_data']:
                    references.update(v.decode() for v in re.findall(rb'[0-9a-fA-F]{32}',row['packed_info_data']))
                candidates=set()
                for reference in references:
                    reference=reference.lower()
                    for name in hardlinks.get(reference,set()): candidates.update(files.get(name,[]))
                    for suffix in ('.dat','_h.dat','_t.dat','.mp4','.jpg','.png','.gif'):
                        candidates.update(files.get(reference+suffix,[]))
                if base==3:
                    candidates={p for p in candidates if p.suffix.lower()=='.dat'}
                    # If a full image is WXGF, add same-stem cached thumbnail for browser preview.
                    for p in list(candidates):
                        stem=re.sub(r'_[ht]$','',p.stem)
                        candidates.update(q for q in (p.with_name(stem+'_t.dat'),p.with_name(stem+'_h.dat')) if q.exists())
                    ordered=sorted(candidates,key=lambda p:('_t' in p.stem,-p.stat().st_size))
                    selected=[]
                    for p in ordered:
                        try:
                            image,ext=decode_dat(p.read_bytes(),image_keys)
                            quality='thumbnail' if p.stem.endswith('_t') else 'cached'
                            selected.append((p,image,ext,quality))
                        except Exception: continue
                    browser=[v for v in selected if v[2] in ('.jpg','.png','.gif','.webp')]
                    originals=[v for v in selected if v[2]=='.wxgf']
                    for p,data,ext,quality in (browser[:1]+originals[:1]):
                        record['attachments'].append(save_attachment(data,p.stem+ext,day_dir,'images',p.relative_to(account),quality))
                    if browser:
                        stats['images_previewable']+=1
                        if browser[0][3]=='thumbnail': stats['image_thumbnail_only']+=1;record['warnings'].append('浏览器展示缓存缩略图，不能视为原图')
                    if originals: record['warnings'].append('已保留 WXGF 图片数据；浏览器不支持直接显示该格式')
                    if not record['attachments']: record['warnings'].append('本地未找到对应图片');stats['images_missing']+=1
                elif base==49 and subtype==6:
                    title=xml.findtext('.//appmsg/title','') if xml is not None else ''
                    expected_size=xml.findtext('.//appattach/totallen','') if xml is not None else ''
                    candidates={p for p in candidates if p.suffix.lower()!='.dat'}
                    confidence='database_index'
                    if not candidates and title:
                        possible=files.get(title.lower(),[])
                        possible=[p for p in possible if expected_size.isdigit() and p.stat().st_size==int(expected_size)]
                        if len(possible)==1: candidates=set(possible);confidence='unique_filename_and_size'
                    if len(candidates)>1:
                        matching=[p for p in candidates if p.name==title]
                        if len(matching)==1: candidates=set(matching)
                    if len(candidates)==1:
                        p=next(iter(candidates));a=save_attachment(p.read_bytes(),p.name,day_dir,'files',p.relative_to(account))
                        a['match_method']=confidence;record['attachments'].append(a);stats['files_saved']+=1
                    else: record['warnings'].append('本地文件未下载、已清理或无法唯一匹配');stats['files_missing']+=1
                elif base==43:
                    videos=[p for p in candidates if p.suffix.lower()=='.mp4']
                    if len(videos)==1:
                        p=videos[0];record['attachments'].append(save_attachment(p.read_bytes(),p.name,day_dir,'video',p.relative_to(account)));stats['videos_saved']+=1
                    else: record['warnings'].append('本地未找到可唯一匹配的视频');stats['videos_missing']+=1
                elif base==34:
                    data=voices.get((conv_id,row['server_id'],row['local_id']))
                    if data:
                        record['attachments'].append(save_attachment(data,str(row['server_id'])+'.silk',day_dir,'audio','media_0.db/VoiceInfo'))
                        record['warnings'].append('已保存原始 SILK 语音，浏览器不能直接播放');stats['voices_saved']+=1
                    else: record['warnings'].append('本地语音数据缺失');stats['voices_missing']+=1
                elif base==47:
                    record['warnings'].append('表情图片未导出');stats['stickers_unresolved']+=1
                stats['type_'+record['message_type']]+=1
                if record['warnings']: issues.append({'message_id':identifier,'conversation':conv_name,'timestamp':record['timestamp'],'warnings':record['warnings']})
                record['_day']=day; record['_folder']=folder
                by_conversation[conv_id].append(record)
        db.close()

    index=[]
    for conv_id,records in by_conversation.items():
        records.sort(key=lambda r:(r['timestamp'],r['local_id']))
        days=defaultdict(list)
        for record in records: days[record['_day']].append(record)
        folder=records[0]['_folder']; name=records[0]['conversation_name']
        day_links=[]
        for day,messages in sorted(days.items(),reverse=True):
            dest=output/folder/day
            exported=[{k:v for k,v in m.items() if not k.startswith('_')} for m in messages]
            (dest/'messages.jsonl').write_text(''.join(json.dumps(m,ensure_ascii=False)+'\n' for m in exported),encoding='utf-8')
            body='<a href="../index.html">← 会话日期列表</a><h1>'+html.escape(name)+'</h1><p>'+day+' · '+str(len(messages))+' 条记录</p>'
            for message in exported:
                body+='<article class="msg '+message['direction']+'"><div class="meta">'+html.escape(message['sender_name'])+' · '+html.escape(message['timestamp'])+'</div><div class="body">'+html.escape(message['text'])+'</div>'
                if message.get('url'): body+='<p><a rel="noreferrer" href="'+html.escape(message['url'],quote=True)+'">打开消息中的链接（需联网）</a></p>'
                for attachment in message['attachments']:
                    link=quote(attachment['path'],safe='/')
                    ext=Path(attachment['path']).suffix.lower()
                    if ext in ('.jpg','.png','.gif','.webp'):
                        body+='<a href="'+link+'"><img loading="lazy" class="preview" src="'+link+'" alt="归档图片"></a>'
                    elif ext=='.mp4': body+='<video controls preload="none" src="'+link+'"></video>'
                    body+='<p><a download href="'+link+'">'+html.escape(attachment['name'])+'</a> <small>'+str(attachment['bytes'])+' 字节</small></p>'
                for warning in message['warnings']: body+='<p class="missing">'+html.escape(warning)+'</p>'
                body+='</article>'
            (dest/'messages.html').write_text(page(name+' '+day,body),encoding='utf-8')
            day_links.append('<div class="day"><a href="'+day+'/messages.html">'+day+'</a> · '+str(len(messages))+' 条</div>')
        (output/folder/'index.html').write_text(page(name,'<a href="../index.html">← 全部会话</a><h1>'+html.escape(name)+'</h1><p>'+str(len(records))+' 条记录</p>'+''.join(day_links)),encoding='utf-8')
        index.append('<div class="conversation"><a href="'+quote(folder,safe='')+'/index.html">'+html.escape(name)+'</a><br><small>'+str(len(records))+' 条 · '+records[0]['_day']+' 至 '+records[-1]['_day']+'</small></div>')
    stats['conversations']=len(by_conversation)
    period=(start_date or '最早')+' 至 '+(end_date or '最新')
    body='<h1>微信聊天归档</h1><p>'+html.escape(period)+' · 本地数据库快照 '+html.escape(manifest['captured_at'])+'</p><div class="stats"><b>'+str(stats['conversations'])+' 个会话</b><b>'+str(stats['messages'])+' 条消息</b><b>'+str(stats['images_previewable'])+' 条图片可预览</b></div><p class="note">仅包含此电脑本地已有记录。缺失媒体、缩略图和未解析内容会在消息下标注。联系人名称来自当前通讯录，可能不同于消息发送时的昵称。</p><p><a href="归档说明.txt">归档说明</a> · <a href="manifest.json">校验与统计</a></p><input id="search" placeholder="筛选会话名称">'+''.join(index)
    (output/'index.html').write_text(page('微信聊天归档',body,True),encoding='utf-8')
    report={'snapshot':manifest,'period':{'start':start_date,'end':end_date},'statistics':dict(stats),'issues':issues}
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'归档说明.txt').write_text('双击 index.html 打开会话目录。每个会话下按日期保存 messages.html 和 messages.jsonl。\n本次导出来自当前电脑本地数据库；不代表手机或账号全部历史。\n数据库及有效提交日志经过校验，六个数据库逐个读取，并非跨库同一时刻的事务快照。\n姓名来自当前联系人备注或昵称；无法找到姓名时保留账号标识，不猜测。\n图片优先使用本地可解码缓存，缩略图有提示；WXGF 原数据另存。\n附件仅关联本地找到的文件；不会联网补下载。语音保留 SILK 原始数据。表情图及某些特殊卡片未完整解析。\n原始数据库未修改；数据库和图片密钥未保存。\n',encoding='utf-8')
    print(json.dumps(dict(stats),ensure_ascii=False),flush=True)
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshots',required=True)
    parser.add_argument('--account',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--start-date')
    parser.add_argument('--end-date')
    args=parser.parse_args()
    export(args.snapshots,args.account,args.output,args.start_date,args.end_date)
