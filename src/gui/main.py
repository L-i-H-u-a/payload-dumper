import os
import sys

from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QListWidgetItem,
    QMainWindow,
)
from ui_mainwindow import Ui_MainWindow

from payload_dumper.dumper import Dumper
from payload_dumper.http_file import HttpFile


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._ui = Ui_MainWindow()
        self._ui.setupUi(self)
        self._ui.pushButton.clicked.connect(self._dialog)
        self._ui.pushButton_3.clicked.connect(self._namelist)
        self._ui.retranslateUi(self)

    def _dialog(self):
        dialog = QFileDialog(self)
        dialog.setFileMode(QFileDialog.ExistingFile)
        if dialog.exec():
            self._ui.lineEdit.setText(dialog.selectedFiles()[0])

    def _namelist(self):
        self._ui.pushButton_3.clicked.disconnect(self._namelist)
        payload_file = self._ui.lineEdit.text()
        if payload_file.startswith("http://") or payload_file.startswith("https://"):
            payload_file = HttpFile(payload_file)
        else:
            payload_file = open(payload_file, "rb")

        self._dumper = Dumper(
            payload_file,
            out=self._ui.lineEdit_2.text() or "out",
        )
        self._ui.listWidget.setSelectionMode(QAbstractItemView.MultiSelection)
        for partition in self._dumper.dam.partitions:
            QListWidgetItem(partition.partition_name, self._ui.listWidget)
        self._ui.listWidget.setEnabled(True)
        self._ui.pushButton_3.setText("下载所选")
        self._ui.pushButton_3.clicked.connect(self._save)

    def _save(self):
        self._dumper.images = ",".join(
            i.text() for i in self._ui.listWidget.selectedItems()
        )
        print(self._dumper.images)
        self._dumper.run()


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
