import math
import torch
import torch.nn.functional as F
from torch import nn

from .inference import make_paa_postprocessor
from .loss import make_paa_loss_evaluator, make_paa_iou_calculator

from paa_core.layers import Scale
from paa_core.layers import DFConv2d
from ..anchor_generator import make_anchor_generator_paa
from ..atss.atss import BoxCoder


class PAAHead(torch.nn.Module):
    def __init__(self, cfg, in_channels):
        super(PAAHead, self).__init__()
        self.cfg = cfg
        num_classes = cfg.MODEL.PAA.NUM_CLASSES - 1
        num_anchors = len(cfg.MODEL.PAA.ASPECT_RATIOS) * cfg.MODEL.PAA.SCALES_PER_OCTAVE

        self.use_iou_pred = cfg.MODEL.PAA.USE_IOU_PRED

        cls_tower = []
        bbox_tower = []
        for i in range(cfg.MODEL.PAA.NUM_CONVS):
            if self.cfg.MODEL.PAA.USE_DCN_IN_TOWER and \
                    i == cfg.MODEL.PAA.NUM_CONVS - 1:
                conv_func = DFConv2d
            else:
                conv_func = nn.Conv2d

            cls_tower.append(
                conv_func(
                    in_channels,
                    in_channels,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    bias=True
                )
            )
            cls_tower.append(nn.GroupNorm(32, in_channels))
            cls_tower.append(nn.ReLU())
            bbox_tower.append(
                conv_func(
                    in_channels,
                    in_channels,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    bias=True
                )
            )
            bbox_tower.append(nn.GroupNorm(32, in_channels))
            bbox_tower.append(nn.ReLU())

        self.add_module('cls_tower', nn.Sequential(*cls_tower))
        self.add_module('bbox_tower', nn.Sequential(*bbox_tower))
        self.cls_logits = nn.Conv2d(
            in_channels, num_anchors * num_classes, kernel_size=3, stride=1,
            padding=1
        )
        self.bbox_pred = nn.Conv2d(
            in_channels, num_anchors * 4, kernel_size=3, stride=1,
            padding=1
        )
        all_modules = [self.cls_tower, self.bbox_tower,
                       self.cls_logits, self.bbox_pred]
        if self.use_iou_pred:
            self.iou_pred = nn.Conv2d(
                in_channels, num_anchors * 1, kernel_size=3, stride=1,
                padding=1
            )
            all_modules.append(self.iou_pred)

        # initialization
        for modules in all_modules:
            for l in modules.modules():
                if isinstance(l, nn.Conv2d):
                    torch.nn.init.normal_(l.weight, std=0.01)
                    torch.nn.init.constant_(l.bias, 0)

        # initialize the bias for focal loss
        prior_prob = cfg.MODEL.PAA.PRIOR_PROB
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        torch.nn.init.constant_(self.cls_logits.bias, bias_value)
        self.scales = nn.ModuleList([Scale(init_value=1.0) for _ in range(5)])

    def forward(self, x):
        logits = []
        bbox_reg = []
        iou_pred = []
        for l, feature in enumerate(x):
            cls_tower = self.cls_tower(feature)
            box_tower = self.bbox_tower(feature)

            logits.append(self.cls_logits(cls_tower))

            bbox_pred = self.scales[l](self.bbox_pred(box_tower))
            bbox_reg.append(bbox_pred)

            if self.use_iou_pred:
                iou_pred.append(self.iou_pred(box_tower))
        res = [logits, bbox_reg]
        if self.use_iou_pred:
            res.append(iou_pred)
        return res 


class PAAModule(torch.nn.Module):

    def __init__(self, cfg, in_channels):
        super(PAAModule, self).__init__()
        self.cfg = cfg
        self.head = PAAHead(cfg, in_channels)
        box_coder = BoxCoder(cfg)
        self.loss_evaluator = make_paa_loss_evaluator(cfg, box_coder)
        self.box_selector_test = make_paa_postprocessor(cfg, box_coder)
        self.anchor_generator = make_anchor_generator_paa(cfg)
        self.use_iou_pred = cfg.MODEL.PAA.USE_IOU_PRED
        self.fpn_strides = cfg.MODEL.PAA.ANCHOR_STRIDES
        self.iou_calculator = make_paa_iou_calculator(cfg, box_coder)

    def forward(self, images, features, targets=None):
        preds = self.head(features)
        box_cls, box_regression = preds[:2]
        iou_pred = preds[2] if self.use_iou_pred else None
        anchors = self.anchor_generator(images, features)
        locations = self.compute_locations(features)
 
        if self.training:
            return self._forward_train(box_cls, box_regression, iou_pred,
                                       targets, anchors, locations)
        else:
            return self._forward_test(box_cls, box_regression, iou_pred, anchors, targets)

    def _forward_train(self, box_cls, box_regression, iou_pred, targets, anchors, locations):
        losses = self.loss_evaluator(
            box_cls, box_regression, iou_pred, targets, anchors, locations
        )
        loss_box_cls, loss_box_reg = losses[:2]
        losses_dict = {
            "loss_cls": loss_box_cls,
            "loss_reg": loss_box_reg
        }
        if self.use_iou_pred:
            losses_dict['loss_iou_pred'] = losses[2]
        return None, losses_dict

    def  _forward_test(self, box_cls, box_regression, iou_pred, anchors, targets=None):
        """
        if targets is not None:
            targets = self.iou_calculator(box_cls, box_regression, iou_pred, targets, anchors)
        """
        boxes = self.box_selector_test(box_cls, box_regression, iou_pred, anchors, targets)
        return boxes, {}

    def compute_locations(self, features):
        locations = []
        for level, feature in enumerate(features):
            h, w = feature.size()[-2:]
            locations_per_level = self.compute_locations_per_level(
                h, w, self.fpn_strides[level],
                feature.device
            )
            locations.append(locations_per_level)
        return locations

    def compute_locations_per_level(self, h, w, stride, device):
        shifts_x = torch.arange(
            0, w * stride, step=stride,
            dtype=torch.float32, device=device
        )
        shifts_y = torch.arange(
            0, h * stride, step=stride,
            dtype=torch.float32, device=device
        )
        shift_y, shift_x = torch.meshgrid(shifts_y, shifts_x)
        shift_x = shift_x.reshape(-1)
        shift_y = shift_y.reshape(-1)
        locations = torch.stack((shift_x, shift_y), dim=1) + stride // 2
        return locations


def build_paa(cfg, in_channels):
    return PAAModule(cfg, in_channels)
