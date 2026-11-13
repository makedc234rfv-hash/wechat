"""Generate daily, weekly, monthly, or custom period reports from selected chats."""
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import html
import json
from pathlib import Path
from urllib.parse import quote
from export_archive import page

KINDS={'day':'日报','week':'周报','month':'月报','custom':'自定义'}


def period(kind, anchor, start=None, end=None):
    anchor=date.fromisoformat(anchor)
    if kind=='day': return anchor.isoformat(),anchor.isoformat()
    if kind=='week':
        first=anchor-timedelta(days=anchor.weekday())
        return first.isoformat(),(first+timedelta(days=6)).isoformat()
    if kind=='month':
        first=anchor.replace(day=1)
        next_month=(first.replace(day=28)+timedelta(days=4)).replace(day=1)
        return first.isoformat(),(next_month-timedelta(days=1)).isoformat()
    if kind=='custom' and start and end:
        first,last=date.fromisoformat(start),date.fromisoformat(end)
        if first>last: raise ValueError('开始日期不能晚于结束日期')
        return first.isoformat(),last.isoformat()
    raise ValueError('请选择日报、周报、月报，或填写自定义开始和结束日期')


def build(archive, kind, start, end):
    archive=Path(archive)
    if not (archive/'manifest.json').is_file(): raise FileNotFoundError('找不到归档清单')
    if kind not in KINDS: raise ValueError('不支持的报表类型')
    date.fromisoformat(start);date.fromisoformat(end)
    by_conversation=defaultdict(Counter)
    links=defaultdict(set)
    by_date=Counter();types=Counter();direction=Counter()
    for path in archive.rglob('messages.jsonl'):
        if not start<=path.parent.name<=end: continue
        for line in path.open(encoding='utf-8'):
            item=json.loads(line)
            day=item['timestamp'][:10]
            if not start<=day<=end: continue
            name=item['conversation_name'];kind_of_message=item['message_type'];way=item['direction']
            by_conversation[name]['total']+=1
            by_conversation[name][way]+=1
            by_date[day]+=1;types[kind_of_message]+=1;direction[way]+=1
            links[name].add(path.parent.relative_to(archive).as_posix()+'/messages.html')
    total=sum(by_date.values())
    title=KINDS[kind]+' '+start+((' 至 '+end) if start!=end else '')
    body='<a href="../index.html">← 本期聊天归档</a><h1>'+html.escape(title)+'</h1>'
    body+='<p class="note">以下是指定时间段内，这台电脑保存的微信聊天记录。可点开会话日期查看发送人、时间、正文和已找到的附件。本机缺少的历史或附件不会出现在统计中。</p>'
    body+='<div class="stats"><b>'+str(total)+' 条消息</b><b>'+str(len(by_conversation))+' 个会话</b><b>发出 '+str(direction['sent'])+'</b><b>收到 '+str(direction['received'])+'</b></div>'
    body+='<h2>每日记录</h2>'
    body+=''.join('<div class="day">'+day+' · '+str(count)+' 条</div>' for day,count in sorted(by_date.items())) or '<p>这个时间段没有找到本地聊天记录。</p>'
    body+='<h2>会话与原始记录</h2>'
    for name,counts in sorted(by_conversation.items(),key=lambda item:-item[1]['total']):
        body+='<div class="conversation"><b>'+html.escape(name)+'</b> · '+str(counts['total'])+' 条（发出 '+str(counts['sent'])+'，收到 '+str(counts['received'])+'）<br>'
        body+=' · '.join('<a href="../'+quote(link,safe='/')+'">'+Path(link).parent.name+'</a>' for link in sorted(links[name]))+'</div>'
    body+='<h2>消息类型</h2><p>'+', '.join(html.escape(k)+' '+str(v) for k,v in types.most_common())+'</p>'
    folder=archive/'报表';folder.mkdir(exist_ok=True)
    filename={'day':'日报','week':'周报','month':'月报','custom':'自定义'}[kind]+'_'+start+(('_'+end) if start!=end else '')
    output=folder/(filename+'.html')
    output.write_text(page(title,body),encoding='utf-8')
    data={'kind':kind,'start':start,'end':end,'message_count':total,'conversation_count':len(by_conversation),
          'directions':dict(direction),'daily_counts':dict(sorted(by_date.items())),
          'message_types':dict(types),'conversations':{k:dict(v) for k,v in by_conversation.items()}}
    (folder/(filename+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return output,data


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',required=True)
    parser.add_argument('--kind',choices=KINDS,required=True)
    parser.add_argument('--anchor',required=True)
    parser.add_argument('--start')
    parser.add_argument('--end')
    args=parser.parse_args()
    start,end=period(args.kind,args.anchor,args.start,args.end)
    result,stats=build(args.archive,args.kind,start,end)
    print(json.dumps({'report':str(result),'messages':stats['message_count']},ensure_ascii=False))
