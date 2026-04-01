"""直接用 imaplib 测试 IMAP 连接和邮件读取"""
import imaplib
import sys
import base64
sys.path.insert(0, ".")

from src.services.outlook.account import OutlookAccount
from src.services.outlook.token_manager import TokenManager
from src.services.outlook.base import ProviderType, TokenEndpoint

# 从备份文件读取凭据
lines = open("outlook_token_backup.txt", encoding="utf-8").readlines()
email = lines[0].split(": ", 1)[1].strip()
password = lines[1].split(": ", 1)[1].strip()
client_id = lines[2].split(": ", 1)[1].strip()
refresh_token = lines[3].split(": ", 1)[1].strip()

account = OutlookAccount(
    email=email,
    password=password,
    client_id=client_id,
    refresh_token=refresh_token,
)

print(f"账户: {email}")
print(f"has_oauth: {account.has_oauth()}")

# 获取 access token
print("\n[1] 获取 access token...")
tm = TokenManager(account, ProviderType.IMAP_OLD)
token = tm.get_access_token()
print(f"Token: {token[:30]}..." if token else "Token 获取失败!")
if not token:
    sys.exit(1)

# 尝试多个 IMAP 服务器
print(f"Token URL: {TokenEndpoint.LIVE.value}")
IMAP_SERVERS = [
    "outlook.office365.com",
    "imap-mail.outlook.com",
]

for server in IMAP_SERVERS:
    print(f"\n[2] 尝试 IMAP 服务器: {server}")
    try:
        conn = imaplib.IMAP4_SSL(server, 993, timeout=15)
        auth_string = f"user={email}\x01auth=Bearer {token}\x01\x01"
        conn.authenticate("XOAUTH2", lambda _: auth_string.encode())
        print(f"  认证成功! 状态: {conn.state}")

        # 尝试 SELECT INBOX
        try:
            status, data = conn.select("INBOX", readonly=True)
            print(f"  SELECT INBOX: {status} {data}")
            if status == "OK":
                status2, data2 = conn.search(None, "ALL")
                all_count = len(data2[0].split()) if data2 and data2[0] else 0
                status3, data3 = conn.search(None, "UNSEEN")
                unseen_count = len(data3[0].split()) if data3 and data3[0] else 0
                print(f"  INBOX: 总共={all_count}, 未读={unseen_count}")
                
                if all_count > 0:
                    ids = data2[0].split()
                    for mid in ids[-3:]:
                        st, md = conn.fetch(mid, "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])")
                        if st == "OK" and md and md[0]:
                            raw = md[0][1] if isinstance(md[0], tuple) else md[0]
                            print(f"    邮件 {mid.decode()}: {raw.decode(errors='ignore').strip()[:150]}")
                print(f"\n  >>> {server} 可用!")
                conn.logout()
                break
        except Exception as e:
            print(f"  SELECT 失败: {e}")
        
        try:
            conn.logout()
        except:
            pass
    except Exception as e:
        print(f"  连接/认证失败: {e}")

print("\n完成!")

