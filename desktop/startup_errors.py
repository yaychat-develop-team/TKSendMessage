"""Credential-free startup failure messages, usable before runtime exists."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox


def show_startup_error(classification):
    messages = {
        'startup_data_permission': '无法创建或写入程序数据目录。\n请退出程序，将 EXE 移动到有写入权限的文件夹后重试。\n已有账号请连同原 runtime 文件夹一起移动，不要删除它。',
        'startup_storage_unavailable': '无法访问程序需要的数据目录或文件。\n请检查磁盘空间、文件夹权限和文件是否被占用。\n不要删除 runtime 文件夹；其中保存了账号和设置。',
        'startup_instance_unavailable': '无法建立防重复启动保护，程序未继续运行。\n请确认当前用户的数据目录可写；若已有窗口正在退出，请等待其完成后重试。',
        'startup_initialization_failed': '程序初始化失败，未开始登录或发送任务。\n请重新获取完整发布包并解压到可写文件夹后重试。\n保留原 runtime 文件夹，反馈时不要发送账号文件或 Cookie。',
    }
    dialog = QMessageBox()
    dialog.setWindowTitle('TKChatWave — 启动失败')
    dialog.setIcon(QMessageBox.Icon.Critical)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(messages.get(classification, messages['startup_initialization_failed']))
    dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
    dialog.exec()
