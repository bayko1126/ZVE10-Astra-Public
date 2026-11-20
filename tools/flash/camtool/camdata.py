#!/usr/bin/env python3
"""camdata.py - 统一解析「代码目录」与「数据目录」

代码放在 camtool/（工具箱本体），出厂备份放在 firmware/backup/，运行时证据与状态放在 camtool/data/。
两者分开，是为了让工具箱可以整体拷走而不带走出厂备份镜像。

覆盖方式：
  export CAM_DATA_ROOT=<repo>/tools/flash/camtool/data
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

PROJECT_ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
DATA_ROOT = os.environ.get('CAM_DATA_ROOT') or os.path.join(HERE, 'data')
BACKUP_DIR = os.path.join(PROJECT_ROOT, 'firmware', 'backup')
EVIDENCE_DIR = os.path.join(DATA_ROOT, 'evidence')
STATE_FILE = os.path.join(DATA_ROOT, '.cam_state.json')
PAYLOAD_DIR = os.path.join(HERE, 'payload')
