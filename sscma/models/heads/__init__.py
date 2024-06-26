# Copyright (c) Seeed Technology Co.,Ltd. All rights reserved.
from .axes_head import AxesClsHead
from .cls_head import Audio_head, ClsHead
from .fastestdet_head import Fastest_Head
from .fomo_head import FomoHead
from .pfld_head import PFLDhead
from .taggregate_head import TAggregate
from .yolov5_head import YOLOV5Head
from .linear_head import LinearClsHead
from .stacked_head import StackedLinearClsHead

__all__ = [
    'Audio_head',
    'TAggregate',
    'PFLDhead',
    'Fastest_Head',
    'FomoHead',
    'AxesClsHead',
    'YOLOV5Head',
    'LinearClsHead',
    'StackedLinearClsHead',
    'ClsHead'
]
