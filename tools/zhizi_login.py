"""Log in to Zhizi and save the account token to the local private env file."""
from getpass import getpass
from pathlib import Path
import os
import re
import tempfile
import httpx

URL = 'https://www.zhizigo.com/api/cluster/account/login'
ENV_FILE = Path(__file__).resolve().parents[1] / 'deploy' / '.env'


def save_token(path, token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]+', token):
        raise ValueError('invalid_token')
    text = path.read_text(encoding='utf-8-sig')
    pattern = r'^ZHIZI_USER_TOKEN=.*$'
    if len(re.findall(pattern, text, flags=re.M)) != 1:
        raise ValueError('missing_or_duplicate_entry')
    updated = re.sub(pattern, lambda _: 'ZHIZI_USER_TOKEN=' + token, text, flags=re.M)
    fd, tmp = tempfile.mkstemp(prefix='.zhizi-', dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(updated)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def login(client, account, password):
    kind = 'email' if '@' in account else 'phone'
    response = client.post(URL, json={kind: account, 'password': password}, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data['token']


def main():
    if not ENV_FILE.is_file():
        print('请先把 deploy/.env.example 复制为 deploy/.env。')
        return 1
    account = input('智子账号（手机号或邮箱）：').strip()
    password = getpass('智子密码：')
    if not account or not password:
        print('账号和密码不能为空。')
        return 1
    try:
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            token = login(client, account, password)
        save_token(ENV_FILE, token)
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        print('登录或保存失败，请检查账号、网络和配置文件。')
        return 1
    print('智子 Token 已保存到 deploy/.env，请妥善保管该文件。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

