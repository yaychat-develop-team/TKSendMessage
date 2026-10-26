"""Create a fresh allowlisted portable package, never copying a runtime tree."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

QUICK_START = '''TKChatWave 快速使用说明

支持 Windows 10/11 64 位，发布版已内置运行依赖及登录浏览器。
请先完整解压到可写文件夹，再双击 TKChatWave.exe。无需安装 Python。
同一 Windows 用户只能运行一份；再次打开会唤起已有窗口。

一、新增 / 更新账号：在官方登录页面自行完成登录，等待工具提示保存成功。
二、勾选发送账号，填写主播用户名或 @用户名，多个房间用英文分号 ; 分隔。
三、填写消息、发送次数和间隔，再主动点击开始发送。登录不会自动发送。
四、默认串行：每个账号独立按顺序切换直播间；并行最多同时执行 3 个直播间。
五、绿色展示标记仅表示旁观监听确认收到消息，接口接受不等于实际展示。
六、停止或退出需要等待当前操作和连接安全收尾，不能撤回已发送消息。
七、同一账号在同一房间连续 3 条消息未观察到展示，会停止其在该房间的后续发送。
中间成功展示或其他结果会清零，重试及重复日志不重复累计；不等同于禁言。
发送次数可填写正整数，已取消 100 上限。运行中显示蓝色；部分展示显示橙色。
旁观监听断开后最多重连 3 次；断线期间没有证据的消息显示无法确认，不自动重发。

账号及设置保存在 EXE 同级 runtime 文件夹中。更新前退出程序，只替换 EXE。
移动已有程序时，请连同原 runtime 一起移动；不要删除它。
删除账号仅移除本机登录信息，不会注销 TikTok 账号。
不要把包含账号、Cookie 和密钥的 runtime 文件夹或账号详情截图发给别人。
本分发包不含发布者账号、日志或登录信息，首次使用需要自行登录。

启动失败时按提示检查写入权限、磁盘空间和是否完整解压。
网络或业务失败请查看界面中的失败详情和 runtime/logs 下的日志。
仅对自己有权使用的账号及直播间进行操作，遵守平台规则和主播设置。
'''


def create_distribution(executable, destination, *, version, expected_sha256):
    executable = Path(executable).resolve()
    destination = Path(destination).absolute()
    archive_path = destination.with_name(destination.name + '.zip')
    if destination.exists() or archive_path.exists():
        raise FileExistsError('Distribution destination must be fresh')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2,3}', version):
        raise ValueError('Invalid release version')
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ValueError('Executable hash mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = dict(product='TKChatWave', version=version, sha256=digest,
                    executable='TKChatWave.exe', platform='Windows 10/11 x64',
                    contains_user_data=False)
    with tempfile.TemporaryDirectory(prefix='.distribution-', dir=destination.parent) as scratch:
        scratch = Path(scratch)
        payload = scratch / 'payload'
        payload.mkdir()
        shutil.copyfile(executable, payload / 'TKChatWave.exe')
        if hashlib.sha256((payload / 'TKChatWave.exe').read_bytes()).hexdigest() != digest:
            raise ValueError('Executable changed during packaging')
        (payload / '使用说明.txt').write_text(QUICK_START, encoding='utf-8-sig')
        (payload / '版本信息.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        archive = scratch / 'release.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
            for name in ('TKChatWave.exe', '使用说明.txt', '版本信息.json'):
                output.write(payload / name, name)
        os.rename(payload, destination)
        os.rename(archive, archive_path)
    return dict(directory=str(destination), zip=str(archive_path), **metadata)
