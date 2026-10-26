"""Render synthetic UI at a process-local DPI; never read real runtime data."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--output', type=Path)
parser.add_argument('--native-font', action='store_true')
args = parser.parse_args()
sys.path.insert(0, str(args.source))

from PySide6.QtCore import QSize
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
from desktop.account_view import AccountSummary
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths

app = QApplication([])
font = Path('C:/Windows/Fonts/msyh.ttc')
if font.exists() and not args.native_font:
    QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Microsoft YaHei UI', 9))
with tempfile.TemporaryDirectory() as directory:
    window = MainWindow(RuntimePaths.from_application(Path(directory)))
    table = window.account_table
    keys = [str(n) * 64 for n in range(1, 5)]
    table.set_accounts([AccountSummary('demo_viewer_' + str(n), key[:12], key)
                        for n, key in enumerate(keys, 1)])
    table.begin_delivery(keys[:3], 3)
    table.update_delivery({'rooms': [{'target': 'demo', 'accounts': [
        {'id': keys[0][:12], 'delivered': 3, 'status': 'completed'},
        {'id': keys[1][:12], 'delivered': 0, 'status': 'completed'},
        {'id': keys[2][:12], 'delivered': 2, 'status': 'completed'}]}]})
    window.resize(1180, 820)
    window._size_account_table()
    window.show()
    for _ in range(4):
        app.processEvents()
    ratio = window.devicePixelRatioF()
    pixmap = table.item(0, 4).icon().pixmap(QSize(16, 16), ratio)
    text_fits = all(table.fontMetrics().horizontalAdvance(table.item(row, column).text())
                    + (26 if column == 4 else 8) <= table.columnWidth(column)
                    and table.fontMetrics().height() + 4 <= table.rowHeight(row)
                    for row in range(4) for column in (1, 2, 3, 4))
    if args.output:
        table.grab().save(str(args.output))
    print(json.dumps({'dpr': ratio, 'brand_pixels': window.brand_icon.pixmap().width(),
                     'mark_pixels': pixmap.width(), 'text_fits': text_fits,
                     'font': table.font().toString()}))
    window.close()
    app.processEvents()
