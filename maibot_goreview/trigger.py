"""Read only this message's own text; quoted/forwarded links do not trigger jobs."""
import re


def mentioned_fox_request(message):
    if not isinstance(message,dict) or message.get('is_at') is not True:
        return None
    info=message.get('message_info') or {}
    group=info.get('group_info') or {}
    user=info.get('user_info') or {}
    if not group.get('group_id') or not user.get('user_id'):
        return None
    text=' '.join(s['data'] for s in message.get('raw_message',[])
                  if isinstance(s,dict) and s.get('type')=='text' and isinstance(s.get('data'),str))
    if '/复盘' in text:return None
    urls=re.findall(r'https?://[^\s<>"\u3000]+',text)
    urls=[u.rstrip('，。！!；;）)') for u in urls if 'h5.foxwq.com' in u]
    if not urls:return None
    if len(urls)!=1:raise ValueError('一次发一份棋谱就好啦～')
    from urllib.parse import urlsplit,parse_qs
    p=urlsplit(urls[0]);ids=parse_qs(p.query,keep_blank_values=True)
    if (p.hostname!='h5.foxwq.com' or p.username or p.password or p.port or p.fragment
        or p.path not in ('/yehunewshare','/yehunewshare/')
        or 'chessid' not in ids or len(ids['chessid'])!=1
        or not re.fullmatch(r'[0-9]{1,30}',ids['chessid'][0])):
        raise ValueError('这条棋谱链接阿羽没认出来，请发送完整的野狐分享链接哦～')
    return {'url':'https://h5.foxwq.com/yehunewshare/?chessid='+ids['chessid'][0],
            'group_id':str(group['group_id']),'user_id':str(user['user_id']),
            'stream_id':message.get('session_id'),'platform':message.get('platform'),
            'message':message}
